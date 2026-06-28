"""Async HTTP client with retry/backoff.

Thin async wrapper over :class:`httpx.AsyncClient` for the execution layer. It
adds bounded retry with exponential backoff, ``Retry-After`` handling on 429,
and an optional proactive rate-limit hook. It is **venue-neutral plumbing**:
it does no auth/signing (that belongs to the broker layer) and does not import
domain business logic — :class:`HTTPError` is a transport-local error type.

Mirrors ``dccd.transport.http.AsyncHTTPClient``; adapted for execution by adding
:meth:`AsyncHTTPClient.post` (order placement is a POST) and an injectable
``sleep`` seam so retry timing is testable without real waits.

Idempotency of POST retries
---------------------------
A blind retry is safe only for an **idempotent** request — one a duplicate of
which has no extra effect. GETs and idempotent POSTs (balance/open-orders/
trade-history queries) retry freely. But a non-idempotent POST — placing an
order — must **not** be blindly retried: after an *ambiguous* failure (the order
landed at the venue but the response was lost to a 5xx or a dropped connection),
a retry would submit a **second** order. :meth:`AsyncHTTPClient.post` therefore
takes a ``retry`` flag; with ``retry=False`` the request is sent **at most
once** and an ambiguous failure raises :class:`AmbiguousRequestError` telling the
caller to *reconcile before retrying* — never to blind-retry. See
:meth:`trading_bot.brokers.kraken.KrakenBroker.place_order`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

__all__ = ["AsyncHTTPClient", "AmbiguousRequestError", "HTTPError"]

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 0.5
# Cap a single backoff sleep so an unlucky ``backoff_base`` / attempt pair can
# never park a request for an unreasonable time.
_MAX_BACKOFF = 60.0


class HTTPError(Exception):
    """Raised when an HTTP request fails (non-retryable, or retries exhausted).

    Parameters
    ----------
    status : int
        HTTP status code that triggered the failure.
    url : str
        Target URL of the failed request.
    body : str, optional
        Response body (truncated in the message), kept for diagnostics.
    """

    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")


class AmbiguousRequestError(Exception):
    """A non-retryable request failed *ambiguously* — outcome unknown.

    Raised by :meth:`AsyncHTTPClient.post` with ``retry=False`` when a single
    attempt fails on a 5xx / 429 / transport error: the request may or may not
    have taken effect at the server (e.g. an order that landed but whose response
    was lost). Because the request is non-idempotent, the client refuses to
    retry — a retry could duplicate the effect — and surfaces this error instead.
    The caller must **reconcile** the server's actual state before deciding
    whether to retry; it must **never** blind-retry.

    Parameters
    ----------
    url : str
        Target URL of the ambiguous request.
    reason : str
        Human-readable description of the failure that made the outcome
        ambiguous (status code or transport error).
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(
            f"ambiguous non-idempotent request to {url}: {reason}; "
            "the request may have taken effect — reconcile before retrying "
            "(never blind-retry a non-idempotent request)"
        )


class _Limiter(Protocol):
    """Minimal structural type for a proactive rate limiter.

    Typed as a :class:`~typing.Protocol` so this module has no hard import on
    the (not-yet-built) ratelimit module: any object exposing an async
    ``acquire(exchange)`` satisfies it.
    """

    async def acquire(self, exchange: str | None) -> None:
        """Block until a token is available for *exchange*."""
        ...


class AsyncHTTPClient:
    """Thin wrapper around :class:`httpx.AsyncClient` with retry/backoff.

    Parameters
    ----------
    base_url : str, optional
        Base URL prepended to relative request paths by httpx.
    max_retries : int, default 3
        Number of attempts on transient errors (5xx, network errors, 429).
    backoff_base : float, default 0.5
        Exponential backoff base, in seconds: zero-based attempt *n* waits
        ``backoff_base * 2**n`` (0.5, 1.0, 2.0, … — increasing, capped at 60s).
    timeout : float, default 10.0
        Per-request timeout, in seconds.
    headers : dict of str to str, optional
        Default headers applied to every request.
    exchange : str, optional
        Exchange name used to key the proactive *limiter*. When both are set,
        every request awaits a token before going out, smoothing bursts to the
        exchange's published rate.
    limiter : _Limiter, optional
        Shared per-exchange limiter consulted only when *exchange* is also set.
    sleep : callable, optional
        ``asyncio.sleep``-compatible coroutine function used for backoff waits.
        Injected as a seam so retry timing is testable without real waits.

    Notes
    -----
    Must be used as an async context manager; the underlying client is created
    on first entry. Nested entries are reference-counted (``_depth``) so a
    shared instance survives concurrent users: the client is closed only when
    the last user exits.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        max_retries: int = _DEFAULT_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
        exchange: str | None = None,
        limiter: _Limiter | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._base_url = base_url
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._timeout = timeout
        self._headers = headers or {}
        self._exchange = exchange
        self._limiter = limiter
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        # Adapters share one AsyncHTTPClient and wrap each call in
        # ``async with self``. With two concurrent operations the first to
        # finish would otherwise close the shared httpx client mid-flight for
        # the other. Reference-count the context so the client is created on
        # first entry and closed only when the last concurrent user exits. Safe
        # under asyncio: the counter is mutated without intervening awaits.
        self._depth = 0

    async def __aenter__(self) -> AsyncHTTPClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url or "",
                timeout=self._timeout,
                headers=self._headers,
                follow_redirects=True,
            )
        self._depth += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._depth -= 1
        if self._depth <= 0 and self._client is not None:
            self._depth = 0
            await self._client.aclose()
            self._client = None

    def _backoff(self, attempt: int) -> float:
        """Backoff delay (seconds) for a zero-based *attempt*, capped.

        Monotonically increasing exponential backoff: ``backoff_base * 2**attempt``
        (so a sustained outage waits longer each retry, never shorter).
        """
        return min(self._backoff_base * 2.0**attempt, _MAX_BACKOFF)

    async def get(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        """Perform a GET request with retry/backoff. Returns parsed JSON.

        Parameters
        ----------
        url : str
            Request URL (or path, if ``base_url`` is set).
        params : mapping, optional
            Query-string parameters.

        Returns
        -------
        Any
            The parsed JSON body of the 2xx response.

        Raises
        ------
        HTTPError
            On a non-retryable 4xx, or once retries are exhausted.
        """
        return await self._request("GET", url, params=params)

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """Perform a POST request. Returns parsed JSON.

        Parameters
        ----------
        url : str
            Request URL (or path, if ``base_url`` is set).
        data : mapping, optional
            Form-encoded body.
        json : Any, optional
            JSON body (mutually exclusive with *data*, per httpx).
        headers : mapping, optional
            Per-request headers, merged over the client defaults.
        retry : bool, default True
            Whether transient failures (5xx / 429 / transport errors) may be
            retried with backoff. ``True`` (the default) is for **idempotent**
            POSTs (balance / open-orders / trade-history queries). Pass
            ``False`` for a **non-idempotent** POST (placing an order): the
            request is then sent **at most once** and an ambiguous transient
            failure raises :class:`AmbiguousRequestError` rather than risking a
            duplicate by retrying. A definite 4xx rejection still raises
            :class:`HTTPError` either way (it did not take effect, nothing to
            reconcile).

        Returns
        -------
        Any
            The parsed JSON body of the 2xx response.

        Raises
        ------
        HTTPError
            On a non-retryable 4xx, or (when ``retry=True``) once retries are
            exhausted.
        AmbiguousRequestError
            When ``retry=False`` and the single attempt fails on a 5xx / 429 /
            transport error — the outcome is unknown; reconcile before retrying.
        """
        return await self._request(
            "POST", url, data=data, json=json, headers=headers, retry=retry
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """Perform an arbitrary-verb request (GET / POST / DELETE / ...).

        A thin public seam over the shared request loop for venues whose signed
        endpoints use verbs beyond GET/POST (e.g. Binance signs ``DELETE
        /api/v3/order`` for cancels, carrying all params on the query string and
        an empty body). The same retry / ambiguity semantics as :meth:`post`
        apply: ``retry=True`` for idempotent calls, ``retry=False`` for a
        non-idempotent one (an ambiguous transient failure then raises
        :class:`AmbiguousRequestError` instead of risking a duplicate).

        Parameters
        ----------
        method : str
            The HTTP verb (``"GET"``, ``"POST"``, ``"DELETE"``, ...).
        url : str
            Request URL (or path, if ``base_url`` is set).
        params : mapping, optional
            Query-string parameters.
        headers : mapping, optional
            Per-request headers, merged over the client defaults.
        retry : bool, default True
            Whether transient failures may be retried (see :meth:`post`).

        Returns
        -------
        Any
            The parsed JSON body of the 2xx response.

        Raises
        ------
        HTTPError
            On a non-retryable 4xx, or (when ``retry=True``) once retries are
            exhausted.
        AmbiguousRequestError
            When ``retry=False`` and the single attempt fails ambiguously.
        """
        return await self._request(
            method, url, params=params, headers=headers, retry=retry
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        """Shared request loop for GET and POST.

        With ``retry=True`` (the default) transient failures (5xx / 429 /
        transport) are retried with backoff up to ``max_retries``. With
        ``retry=False`` the request is attempted **once**; a transient failure
        then raises :class:`AmbiguousRequestError` instead of retrying, so a
        non-idempotent request is never duplicated.
        """
        client = self._client
        if client is None:
            raise RuntimeError(
                "AsyncHTTPClient must be used as an async context manager"
            )

        # A non-retrying request is attempted exactly once; the retrying path
        # keeps its bounded-retry budget unchanged.
        max_attempts = self._max_retries if retry else 1

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                # Proactive throttle: wait for a token before each outbound
                # request so concurrent operations on the same exchange stay
                # under its published rate. Reactive 429 handling below remains
                # a backstop.
                if self._limiter is not None and self._exchange is not None:
                    await self._limiter.acquire(self._exchange)

                resp = await client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=dict(headers) if headers is not None else None,
                )

                if resp.status_code == 429:
                    if not retry:
                        raise AmbiguousRequestError(
                            url, f"HTTP 429 (rate-limited): {resp.text[:200]}"
                        )
                    wait = self._retry_after(resp, attempt)
                    logger.warning(
                        "Rate-limited by %s, sleeping %.1fs", url, wait
                    )
                    last_exc = HTTPError(resp.status_code, url, resp.text)
                    await self._sleep(wait)
                    continue

                if resp.status_code >= 500:
                    if not retry:
                        raise AmbiguousRequestError(
                            url, f"HTTP {resp.status_code}: {resp.text[:200]}"
                        )
                    wait = self._backoff(attempt)
                    logger.warning(
                        "HTTP %d from %s, retry in %.1fs",
                        resp.status_code,
                        url,
                        wait,
                    )
                    last_exc = HTTPError(resp.status_code, url, resp.text)
                    await self._sleep(wait)
                    continue

                if resp.status_code >= 400:
                    raise HTTPError(resp.status_code, url, resp.text)

                return resp.json()

            except httpx.TransportError as exc:
                if not retry:
                    # The request may have reached the server before the
                    # connection dropped — outcome unknown. Refuse to retry.
                    raise AmbiguousRequestError(
                        url, f"transport error: {exc}"
                    ) from exc
                wait = self._backoff(attempt)
                logger.warning(
                    "Transport error %s (attempt %d/%d), retry in %.1fs",
                    exc,
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                last_exc = exc
                await self._sleep(wait)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"{method} {url} failed after {self._max_retries} retries"
        )

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float:
        """Delay to honour a 429: ``Retry-After`` header, else backoff."""
        header = resp.headers.get("Retry-After")
        if header is not None:
            try:
                return min(float(header), _MAX_BACKOFF)
            except ValueError:
                # Retry-After can be an HTTP-date; fall back to backoff rather
                # than parse the date format here.
                logger.debug("Unparseable Retry-After %r, using backoff", header)
        return self._backoff(attempt)
