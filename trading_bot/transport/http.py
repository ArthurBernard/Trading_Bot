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
import urllib.parse
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

__all__ = [
    "AsyncHTTPClient",
    "AmbiguousRequestError",
    "HTTPError",
    "ResponseTooLargeError",
    "redact_url",
]

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 0.5
# Cap a single backoff sleep so an unlucky ``backoff_base`` / attempt pair can
# never park a request for an unreasonable time.
_MAX_BACKOFF = 60.0

# --- Connection-pool bounds ------------------------------------------------- #
# Explicit pool bounds so the shared, reference-counted client (one per exchange,
# used by concurrent operations) can never open an unbounded number of sockets to
# a venue, and idle keep-alive connections are reaped. Left as constants — the
# exchange APIs this fronts never need a wide pool.
_MAX_CONNECTIONS = 20
_MAX_KEEPALIVE_CONNECTIONS = 10
_KEEPALIVE_EXPIRY = 30.0

# --- Response-size cap ------------------------------------------------------ #
# Hard ceiling on a response body before it is buffered and JSON-parsed. Exchange
# REST payloads (balances, open orders, a page of fills) are kilobytes; a body an
# order of magnitude past any legitimate response is treated as hostile/broken
# and rejected *before* it is fully read into memory or handed to ``json()``,
# rather than letting a runaway body exhaust memory. 8 MiB is generous headroom.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# Whether the underlying httpx client reads OS proxy / TLS-bundle environment
# variables (``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` / ``SSL_CERT_FILE``).
# Made **explicit** (httpx's own default is ``True``) so the proxy posture is a
# documented, opt-in choice rather than an implicit ambient one: a signed API
# client should not silently route through an unexpected proxy. Overridable per
# instance via the ``trust_env`` constructor argument.
_DEFAULT_TRUST_ENV = False

# Query-string parameters whose *value* is a secret (or a nonce that reveals
# call ordering) and must never reach a log record or an exception message. The
# comparison is case-insensitive so both Binance's ``signature`` / ``apiKey``
# and snake-cased variants (``api_key``) are covered. This is the transport's
# side of the "secrets never logged" invariant: Binance signs on the query
# string, so the full signed URL — carrying ``signature`` and ``apiKey`` — flows
# verbatim into every failure path here unless it is scrubbed first.
_SENSITIVE_QUERY_KEYS = frozenset({"signature", "api_key", "apikey", "token", "nonce"})
_REDACTED = "<redacted>"

# Binance reports the weight consumed in the trailing rolling minute in this
# response header. Feeding it back to a weight-aware limiter (via its ``observe``
# hook) keeps the proactive budget in step with the venue's own accounting —
# including weight burnt by other clients sharing the IP. Case-insensitive lookup
# is via ``httpx.Headers`` (which lower-cases keys), so the constant is lower.
_USED_WEIGHT_HEADER = "x-mbx-used-weight-1m"


def _redact_url(url: str) -> str:
    """Return *url* with the values of sensitive query params masked.

    Masks the value of every query parameter whose name matches (case-
    insensitively) one of :data:`_SENSITIVE_QUERY_KEYS` — ``signature``,
    ``api_key`` / ``apiKey``, ``token``, ``nonce`` — replacing it with
    :data:`_REDACTED` while leaving the URL otherwise intact. A URL with no
    query string, or no sensitive params, is returned unchanged. Non-sensitive
    parameters (symbol, side, quantity, …) are preserved so diagnostics stay
    useful.

    This is a pure string transform (no I/O); it is applied to any URL before it
    enters a log record or an exception message so a 429 / 5xx / timeout on a
    signed request can never write the HMAC signature or the API key to a log.

    Parameters
    ----------
    url : str
        The (possibly signed) request URL. May be a full URL or a bare path.

    Returns
    -------
    str
        The URL with sensitive query-parameter values replaced by
        ``<redacted>``.
    """
    split = urllib.parse.urlsplit(url)
    if not split.query:
        return url
    # keep_blank_values so a valueless ``&signature=`` still round-trips.
    pairs = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
    redacted = [
        (key, _REDACTED if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in pairs
    ]
    # quote_via=quote (not the default quote_plus) re-encodes values path-style;
    # the marker's angle brackets still percent-encode, so it reaches a log line
    # as ``%3Credacted%3E`` — grep for ``redacted`` to find every masked value.
    new_query = urllib.parse.urlencode(redacted, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit(split._replace(query=new_query))


#: Public alias of :func:`_redact_url` for the other layers that must scrub a URL
#: before it reaches a log. Sharing the one implementation — rather than
#: re-deriving a key set per layer — is what makes a URL redacted *identically*
#: wherever it could be logged: same :data:`_SENSITIVE_QUERY_KEYS`, same
#: ``<redacted>`` marker, so one grep finds every masked value. Used by
#: :mod:`trading_bot.application.log_setup` to scrub uvicorn's access log (the
#: dashboard's ``?token=`` script auth would otherwise be written verbatim).
redact_url = _redact_url


def _redact_exc(exc: BaseException) -> str:
    """Return ``str(exc)`` with any embedded signed URL scrubbed of secrets.

    An :class:`httpx.TransportError` renders with the failing request's URL in
    its message (e.g. ``ConnectError`` for ``GET https://…?…&signature=…``), so
    the raw ``str(exc)`` would re-leak the signature into a log line. When the
    exception carries a ``request`` with a URL, its redacted form is substituted
    for the raw one; otherwise the plain message is returned (transport errors
    with no request carry no URL to leak).

    Parameters
    ----------
    exc : BaseException
        The transport (or other) exception whose text is about to be logged.

    Returns
    -------
    str
        The exception message, secret-free.
    """
    text = str(exc)
    request = getattr(exc, "request", None)
    raw_url = getattr(request, "url", None)
    if raw_url is None:
        return text
    raw = str(raw_url)
    return text.replace(raw, _redact_url(raw))


class ResponseTooLargeError(Exception):
    """A response body exceeded the transport's size cap and was rejected.

    Raised by :meth:`AsyncHTTPClient._read_capped_json` when a response body
    grows past :data:`_MAX_RESPONSE_BYTES` before it is fully read — so a
    runaway or hostile body is refused **before** it is buffered in full or
    parsed as JSON, rather than being allowed to exhaust memory. A legitimate
    exchange payload (balances, open orders, a page of fills) is kilobytes, far
    under the cap.

    Parameters
    ----------
    url : str
        Target URL of the over-large response. Sensitive query parameters are
        redacted before the URL is stored or rendered.
    limit : int
        The byte ceiling that was exceeded.
    """

    def __init__(self, url: str, limit: int) -> None:
        # Store the redacted URL so ``self.url``, ``args`` and ``str(self)`` are
        # all secret-free even if the exception is re-logged far from here.
        self.url = _redact_url(url)
        self.limit = limit
        super().__init__(
            f"response from {self.url} exceeded the {limit}-byte cap; "
            "body refused before buffering to avoid memory exhaustion"
        )


class HTTPError(Exception):
    """Raised when an HTTP request fails (non-retryable, or retries exhausted).

    Parameters
    ----------
    status : int
        HTTP status code that triggered the failure.
    url : str
        Target URL of the failed request. Sensitive query parameters
        (``signature``, ``apiKey``, ``token``, ``nonce``) are redacted before
        the URL is stored or rendered, so re-logging the exception is safe.
    body : str, optional
        Response body (truncated in the message), kept for diagnostics.
    """

    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        # Store the redacted URL so ``self.url``, ``args`` and ``str(self)`` are
        # all secret-free — even if the exception is re-logged far from here.
        self.url = _redact_url(url)
        self.body = body
        super().__init__(f"HTTP {status} from {self.url}: {body[:200]}")


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
        Target URL of the ambiguous request. Sensitive query parameters
        (``signature``, ``apiKey``, ``token``, ``nonce``) are redacted before
        the URL is stored or rendered, so re-logging the exception is safe.
    reason : str
        Human-readable description of the failure that made the outcome
        ambiguous (status code or transport error).
    """

    def __init__(self, url: str, reason: str) -> None:
        # Store the redacted URL so ``self.url``, ``args`` and ``str(self)`` are
        # all secret-free — even if the exception is re-logged far from here.
        self.url = _redact_url(url)
        self.reason = reason
        super().__init__(
            f"ambiguous non-idempotent request to {self.url}: {reason}; "
            "the request may have taken effect — reconcile before retrying "
            "(never blind-retry a non-idempotent request)"
        )


@runtime_checkable
class _Limiter(Protocol):
    """Minimal structural type for a proactive rate limiter.

    Typed as a :class:`~typing.Protocol` so this module has no hard import on
    the ratelimit module: any object exposing an async ``acquire(exchange,
    weight=...)`` satisfies it. The reactive-feedback hooks — ``observe`` (feed
    back a venue-reported used-weight header) and ``penalise`` (feed back a
    418/429 back-off) — are **optional**: they are consulted only when present
    (checked via :func:`hasattr`), so a bare token-bucket limiter still works.
    """

    async def acquire(self, exchange: str | None, weight: float = ...) -> None:
        """Block until *exchange*'s budget admits a call charging ``weight``."""
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
        Default per-phase timeout, in seconds, used for any phase left
        unspecified below. Distinguishing the phases matters for order
        submission: a **connect**-timeout means the request likely never left
        the client, whereas a **read**-timeout means it was sent and its
        outcome is unknown — the caller must then reconcile, never blind-retry
        (see :class:`AmbiguousRequestError`).
    connect_timeout : float, optional
        Connection-establishment timeout, in seconds. Defaults to ``timeout``.
    read_timeout : float, optional
        Socket-read timeout, in seconds. Defaults to ``timeout``.
    write_timeout : float, optional
        Socket-write timeout, in seconds. Defaults to ``timeout``.
    trust_env : bool, default False
        Whether the underlying httpx client honours OS proxy / TLS-bundle
        environment variables (``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` /
        ``SSL_CERT_FILE``). Made explicit (httpx's own default is ``True``) so a
        signed API client does not silently route through an ambient proxy;
        opt in deliberately when a proxy is intended.
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
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        trust_env: bool = _DEFAULT_TRUST_ENV,
        headers: dict[str, str] | None = None,
        exchange: str | None = None,
        limiter: _Limiter | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._base_url = base_url
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._timeout = timeout
        # Explicit per-phase timeouts: connect vs read is the material split for
        # order submission (a read-timeout is ambiguous — the order may have
        # landed — while a connect-timeout most likely never left). ``pool`` (the
        # wait for a free pooled connection) reuses the scalar default.
        self._httpx_timeout = httpx.Timeout(
            timeout,
            connect=connect_timeout if connect_timeout is not None else timeout,
            read=read_timeout if read_timeout is not None else timeout,
            write=write_timeout if write_timeout is not None else timeout,
        )
        self._trust_env = trust_env
        # Explicit connection-pool bounds for the shared, concurrently-used
        # client. Stored so the configured posture is introspectable (and so the
        # single source of truth is the constant set above).
        self._limits = httpx.Limits(
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        )
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
                # Per-phase timeouts (connect / read / write / pool) rather than a
                # single scalar, so ``_request`` can tell a likely-not-sent
                # ConnectTimeout from a sent-but-unknown ReadTimeout.
                timeout=self._httpx_timeout,
                # Bound the socket pool for the shared, concurrently-used client
                # and reap idle keep-alives.
                limits=self._limits,
                headers=self._headers,
                # A signed API client must not follow a redirect: a 3xx could
                # replay the signed query to a different host, and Binance/Kraken
                # never redirect a valid request.
                follow_redirects=False,
                # Explicit proxy/TLS-env posture (see ``trust_env``).
                trust_env=self._trust_env,
            )
        self._depth += 1
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._depth -= 1
        if self._depth <= 0 and self._client is not None:
            self._depth = 0
            await self._client.aclose()
            self._client = None

    @property
    def max_retries(self) -> int:
        """The bounded number of attempts on transient errors (read-only).

        Exposed so a caller running its own retry loop over a body-level venue
        error (e.g. Kraken's HTTP-200 ``EService:Unavailable``, which the
        transport's status-code retry never sees) can bound its attempts to the
        same budget.
        """
        return self._max_retries

    def _backoff(self, attempt: int) -> float:
        """Backoff delay (seconds) for a zero-based *attempt*, capped.

        Monotonically increasing exponential backoff: ``backoff_base * 2**attempt``
        (so a sustained outage waits longer each retry, never shorter).
        """
        return min(self._backoff_base * 2.0**attempt, _MAX_BACKOFF)

    async def sleep_backoff(self, attempt: int) -> None:
        """Sleep this client's exponential backoff for a zero-based *attempt*.

        Uses the injected ``sleep`` seam (so tests can record delays without real
        waits) and the same :meth:`_backoff` schedule as the internal retry loop.
        Exposed for a caller running its own retry loop over a body-level venue
        error that the transport's status-code retry cannot observe.
        """
        await self._sleep(self._backoff(attempt))

    async def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        weight: float = 1.0,
    ) -> Any:
        """Perform a GET request with retry/backoff. Returns parsed JSON.

        Parameters
        ----------
        url : str
            Request URL (or path, if ``base_url`` is set).
        params : mapping, optional
            Query-string parameters.
        weight : float, optional
            Request weight charged to a weight-metered limiter (Binance's
            per-endpoint weight). Ignored by flat-rate limiters. Defaults to 1.

        Returns
        -------
        Any
            The parsed JSON body of the 2xx response.

        Raises
        ------
        HTTPError
            On a non-retryable 4xx, or once retries are exhausted.
        """
        return await self._request("GET", url, params=params, weight=weight)

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
        weight: float = 1.0,
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
        weight : float, optional
            Request weight charged to a weight-metered limiter (ignored by
            flat-rate limiters). Defaults to 1.
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
            "POST",
            url,
            data=data,
            json=json,
            headers=headers,
            retry=retry,
            weight=weight,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool = True,
        weight: float = 1.0,
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
        weight : float, optional
            Request weight charged to a weight-metered limiter (ignored by
            flat-rate limiters). Defaults to 1.

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
            method,
            url,
            params=params,
            headers=headers,
            retry=retry,
            weight=weight,
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
        weight: float = 1.0,
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

        # Redact once up front: ``url`` may be a signed Binance query string
        # (``…&signature=<hmac>&apiKey=…``). Only the scrubbed form is ever
        # logged; the raw ``url`` is used solely for the outbound httpx call and
        # is passed to the exception constructors, which redact it themselves.
        safe_url = _redact_url(url)

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                # Proactive throttle: charge the call its ``weight`` before each
                # outbound request so concurrent operations on the same exchange
                # stay under its published budget. Reactive 429/418 handling
                # below remains a backstop.
                if self._limiter is not None and self._exchange is not None:
                    await self._limiter.acquire(self._exchange, weight)

                resp = await client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=dict(headers) if headers is not None else None,
                )

                # Resync the proactive weight budget to the venue's own report
                # (``X-MBX-USED-WEIGHT-1M``) on *every* response, success or not,
                # so it tracks reality (including other clients on this IP).
                self._observe_used_weight(resp)

                # 418 (IP auto-ban) and 429 (rate-limited) both carry a
                # ``Retry-After`` the venue wants honoured. Feed it to the
                # limiter so the NEXT call on this exchange waits it out — even
                # when this call does not retry (an order submit that must never
                # blind-retry still slows what follows).
                if resp.status_code in (418, 429):
                    wait = self._retry_after(resp, attempt)
                    self._penalise(wait)
                    banned = resp.status_code == 418
                    kind = "IP-banned" if banned else "rate-limited"
                    if not retry:
                        raise AmbiguousRequestError(
                            url,
                            f"HTTP {resp.status_code} ({kind}): {resp.text[:200]}",
                        )
                    logger.warning(
                        "HTTP %d (%s) from %s, sleeping %.1fs",
                        resp.status_code,
                        kind,
                        safe_url,
                        wait,
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
                        safe_url,
                        wait,
                    )
                    last_exc = HTTPError(resp.status_code, url, resp.text)
                    await self._sleep(wait)
                    continue

                if resp.status_code >= 400:
                    raise HTTPError(resp.status_code, url, resp.text)

                return self._capped_json(resp, url)

            except httpx.TransportError as exc:
                if not retry:
                    # The request may have reached the server before the
                    # connection dropped / read timed out — outcome unknown.
                    # Refuse to retry a non-idempotent request. A ``ReadTimeout``
                    # is the canonical UNKNOWN case (the request was sent; the
                    # response was lost), so it must stay ambiguous and reconcile
                    # — NEVER auto-retry (a retry could place a second order). A
                    # ``ConnectTimeout`` most likely never left, but we still
                    # surface it as ambiguous (safe, over-conservative): the
                    # caller reconciles rather than risking a double-submit on a
                    # mis-classified phase. ``_redact_exc`` scrubs any signed URL
                    # httpx embeds in the message before it enters ``reason``.
                    raise AmbiguousRequestError(
                        url,
                        f"{self._transport_phase(exc)}: {_redact_exc(exc)}",
                    ) from exc
                wait = self._backoff(attempt)
                logger.warning(
                    "Transport error %s (attempt %d/%d), retry in %.1fs",
                    _redact_exc(exc),
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                last_exc = exc
                await self._sleep(wait)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"{method} {safe_url} failed after {self._max_retries} retries"
        )

    @staticmethod
    def _transport_phase(exc: httpx.TransportError) -> str:
        """Classify a transport error's phase for the ambiguity ``reason``.

        Distinguishes a **read**-timeout (request sent, response lost — the
        canonical UNKNOWN/ambiguous case, must reconcile) from a **connect**
        failure (likely never sent). This only labels the ``reason`` — a
        ``retry=False`` request is ambiguous either way, never auto-retried —
        so the operator/log can tell *why* the outcome is unknown.
        """
        if isinstance(exc, httpx.ReadTimeout):
            return "read-timeout (request sent, response unknown)"
        if isinstance(exc, httpx.ConnectTimeout):
            return "connect-timeout (request likely not sent)"
        if isinstance(exc, httpx.ConnectError):
            return "connect error (request likely not sent)"
        return "transport error"

    def _capped_json(self, resp: httpx.Response, url: str) -> Any:
        """Parse a 2xx response body as JSON, enforcing the size cap.

        Rejects an over-large body with :class:`ResponseTooLargeError` rather
        than parsing it: first on the declared ``Content-Length`` (so a hostile
        server advertising a huge body is refused without trusting the header
        alone), then on the actually-buffered byte length (the authoritative
        check, catching a missing or lying ``Content-Length``). A legitimate
        exchange payload is far under :data:`_MAX_RESPONSE_BYTES`.
        """
        declared = resp.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > _MAX_RESPONSE_BYTES:
                    raise ResponseTooLargeError(url, _MAX_RESPONSE_BYTES)
            except ValueError:
                logger.debug("Unparseable Content-Length %r", declared)
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            raise ResponseTooLargeError(url, _MAX_RESPONSE_BYTES)
        return resp.json()

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float:
        """Delay to honour a 429/418: ``Retry-After`` header, else backoff."""
        header = resp.headers.get("Retry-After")
        if header is not None:
            try:
                return min(float(header), _MAX_BACKOFF)
            except ValueError:
                # Retry-After can be an HTTP-date; fall back to backoff rather
                # than parse the date format here.
                logger.debug("Unparseable Retry-After %r, using backoff", header)
        return self._backoff(attempt)

    def _observe_used_weight(self, resp: httpx.Response) -> None:
        """Feed a venue ``X-MBX-USED-WEIGHT-1M`` header back to the limiter.

        When the limiter exposes an ``observe`` hook and the response carries the
        used-weight header, resync the proactive budget to the venue's figure so
        the next call throttles against the venue's own accounting. A no-op when
        the limiter has no ``observe`` hook, no exchange is configured, or the
        header is absent / unparseable.
        """
        limiter = self._limiter
        if limiter is None or self._exchange is None:
            return
        observe = getattr(limiter, "observe", None)
        if observe is None:
            return
        header = resp.headers.get(_USED_WEIGHT_HEADER)
        if header is None:
            return
        try:
            used = float(header)
        except ValueError:
            logger.debug("Unparseable %s %r", _USED_WEIGHT_HEADER, header)
            return
        observe(self._exchange, used)

    def _penalise(self, seconds: float) -> None:
        """Feed a 418/429 back-off (``seconds``) back to the limiter.

        When the limiter exposes a ``penalise`` hook and an exchange is
        configured, park the exchange for ``seconds`` so the next call waits out
        the venue's advised window. A no-op otherwise (the reactive per-call
        sleep above is still applied on the retrying path).
        """
        limiter = self._limiter
        if limiter is None or self._exchange is None or seconds <= 0.0:
            return
        penalise = getattr(limiter, "penalise", None)
        if penalise is None:
            return
        penalise(self._exchange, seconds)
