"""The :class:`KrakenBroker` — Kraken REST adapter behind the :class:`Broker` port.

This is the concrete Kraken implementation of the venue-neutral
:class:`~trading_bot.brokers.base.Broker` port. It speaks **domain types only**
(:class:`~trading_bot.domain.order.Order`,
:class:`~trading_bot.domain.fill.Fill`,
:class:`~trading_bot.domain.instrument.Instrument`,
:class:`~trading_bot.domain.money.Money`) on its surface and translates them to
and from Kraken's REST payloads underneath, using the
:mod:`trading_bot.transport` plumbing for I/O, retry/backoff and rate limiting.

Signing
-------
Private endpoints are authenticated with Kraken's HMAC-SHA512 scheme, factored
into the module-level :func:`_sign` helper (ported from the legacy
``API_kraken.set_sign``):

* ``postdata = urllib.parse.urlencode(data)`` (``nonce`` first, so it matches
  Kraken's published test vector);
* ``message = path.encode() + sha256((nonce + postdata).encode()).digest()``;
* ``API-Sign = base64(hmac_sha512(b64decode(secret), message))``.

:func:`_sign` is a pure function (no I/O, no clock) so it can be checked against
Kraken's published vector deterministically — that vector is the *only* proof of
signing correctness exercised here; real private calls are deferred (no key).

Credentials & posture
---------------------
Credentials come from the environment (``KRAKEN_API_KEY`` /
``KRAKEN_API_SECRET``) and **never** from code. The broker is constructible
*without* them — public market-data calls (:meth:`KrakenBroker.ticker`, the
:class:`Instrument` builder) work key-free — and any private call attempted
without credentials raises a clear :class:`~trading_bot.domain.errors.BrokerError`
*before* a request goes out. Key material is never logged.

Money
-----
Every amount Kraken reports is a decimal string; it is parsed with
``money(str(...))`` so prices, volumes and fees stay exact :class:`~decimal.
Decimal` and never round-trip through ``float``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from trading_bot.brokers.base import Broker, Capability
from trading_bot.domain.errors import (
    BrokerError,
    InsufficientBalance,
    InvalidInstrument,
    InvalidNonce,
    RateLimited,
    ServiceUnavailable,
)
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    normalise,
    parse_kraken_pair,
)
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.transport.http import AsyncHTTPClient
from trading_bot.transport.ratelimit import KrakenCallCounter, RateLimiter

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["KrakenBroker"]

_API_BASE = "https://api.kraken.com"
_PUBLIC = "/0/public"
_PRIVATE = "/0/private"

# Domain OrderType -> Kraken ``ordertype`` string. BEST_LIMIT renders as a plain
# limit (its price is discovered by the caller before submission, so by the time
# it reaches the broker it carries a concrete ``limit_price``).
_ORDERTYPE_TO_KRAKEN: dict[OrderType, str] = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP_LOSS: "stop-loss",
    OrderType.BEST_LIMIT: "limit",
}

# Kraken ``ordertype`` string -> domain OrderType (for rebuilding open orders).
_KRAKEN_TO_ORDERTYPE: dict[str, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop-loss": OrderType.STOP_LOSS,
}

# 32-bit unsigned ceiling for a Kraken ``userref`` (a signed 32-bit int; Kraken
# accepts 0 .. 2**31-1). The domain ``client_order_id`` is arbitrary text, so it
# is hashed deterministically into this range (see :func:`_userref_for`) — a
# *transformation*, never a silent drop, so ``reconcile`` can match on it.
_USERREF_MODULUS = 2**31 - 1

# Substrings (upper-cased) that classify a Kraken error string returned inside an
# HTTP-200 body into a retriable transient condition. Kraken returns
# ``EService:Unavailable`` / ``EService:Busy`` and ``EAPI:Rate limit exceeded`` /
# ``EGeneral:Too many requests`` with HTTP 200, so the transport's 5xx/429 retry
# never sees them — they must be retried at this layer.
_RETRIABLE_ERROR_MARKERS: tuple[str, ...] = (
    "ESERVICE:UNAVAILABLE",
    "ESERVICE:BUSY",
    "ERATE",
    "EAPI:RATE LIMIT",
    "EGENERAL:TOO MANY",
)


def _userref_for(client_order_id: str) -> int:
    """Deterministically map an arbitrary ``client_order_id`` to a Kraken userref.

    Kraken's ``userref`` is a 32-bit signed integer, so an arbitrary domain
    ``client_order_id`` (free text) cannot be forwarded verbatim. Rather than
    drop it — which would make :func:`~trading_bot.application.reconcile.
    reconcile` unable to correlate the order — it is hashed (BLAKE2b, digest
    folded) into ``[0, 2**31-1]``. The mapping is **stable** (same id → same
    userref across processes/restarts), so a reconcile pass matches the userref
    the order was submitted with. It is one-way and may collide in principle,
    but the primary correlation key stays the ``txid``; the userref is a
    best-effort venue-side idempotency/lookup tag.
    """
    digest = hashlib.blake2b(client_order_id.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % _USERREF_MODULUS


def _map_kraken_error(errors: list[str], *, context: str) -> BrokerError:
    """Map Kraken error strings to the most specific domain :class:`BrokerError`.

    Kraken wraps failures as an ``error`` array of ``"ECategory:Detail"`` codes
    (e.g. ``"EOrder:Insufficient funds"``). This inspects the (joined) codes and
    returns the most specific domain error — :class:`~trading_bot.domain.errors.
    InsufficientBalance`, :class:`~trading_bot.domain.errors.InvalidInstrument`,
    :class:`~trading_bot.domain.errors.InvalidNonce`,
    :class:`~trading_bot.domain.errors.RateLimited`,
    :class:`~trading_bot.domain.errors.ServiceUnavailable` — falling back to a
    generic :class:`BrokerError`. The error strings are plain venue diagnostics
    (never key material), so they are safe to surface.
    """
    joined = "; ".join(errors)
    upper = joined.upper()
    detail = f"Kraken {context}: {joined}"
    if "INSUFFICIENT FUNDS" in upper or "INSUFFICIENTFUNDS" in upper:
        return InsufficientBalance(detail)
    if "UNKNOWN ASSET PAIR" in upper or "UNKNOWN PAIR" in upper:
        return InvalidInstrument(context, joined)
    if "INVALID NONCE" in upper:
        return InvalidNonce(detail)
    if any(marker in upper for marker in ("ERATE", "EAPI:RATE LIMIT", "EGENERAL:TOO MANY")):
        return RateLimited(detail)
    if "ESERVICE:UNAVAILABLE" in upper or "ESERVICE:BUSY" in upper:
        return ServiceUnavailable(detail)
    return BrokerError(detail)


def _is_retriable_kraken_error(errors: list[str]) -> bool:
    """Whether a Kraken HTTP-200 ``error`` array is a retriable transient one.

    ``True`` for the service-unavailable / rate-limit codes Kraken returns inside
    a 200 body (see :data:`_RETRIABLE_ERROR_MARKERS`); ``False`` for a terminal
    rejection (insufficient funds, invalid pair, invalid nonce, ...).
    """
    upper = "; ".join(errors).upper()
    return any(marker in upper for marker in _RETRIABLE_ERROR_MARKERS)


def _sign(path: str, data: Mapping[str, Any], secret: str) -> str:
    """Compute Kraken's ``API-Sign`` for a private request.

    Pure function (no I/O, no clock): given the request ``path``, the body
    ``data`` (which must already carry a ``nonce``) and the base64 API
    ``secret``, returns the ``API-Sign`` header value. Ported from the legacy
    ``API_kraken.set_sign`` and matched against Kraken's published test vector.

    The algorithm:

    1. ``postdata = urllib.parse.urlencode(data)`` — the form body, with
       ``nonce`` first so the encoded string matches Kraken's vector;
    2. ``encoded = (str(nonce) + postdata).encode()``;
    3. ``message = path.encode() + sha256(encoded).digest()``;
    4. ``API-Sign = base64(hmac_sha512(b64decode(secret), message))``.

    Parameters
    ----------
    path : str
        The request path, e.g. ``"/0/private/AddOrder"``.
    data : mapping
        The request body. Must contain a ``"nonce"`` key; iteration order is
        preserved by ``urlencode``, so build it ``nonce``-first.
    secret : str
        The base64-encoded Kraken API secret.

    Returns
    -------
    str
        The ``API-Sign`` header value (base64).

    """
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    signature = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(signature.digest()).decode()


class KrakenBroker(Broker):
    """Kraken REST adapter implementing the venue-neutral :class:`Broker` port.

    Public market data (ticker, instrument metadata) needs no credentials;
    private order/balance/fill calls read ``KRAKEN_API_KEY`` /
    ``KRAKEN_API_SECRET`` from the environment and sign each request with
    :func:`_sign`. A private call attempted without credentials raises
    :class:`~trading_bot.domain.errors.BrokerError` before any I/O.

    Retry policy — idempotency-aware
    --------------------------------
    The transport retries transient failures (5xx / 429 / dropped connections)
    with backoff. That is safe for the **idempotent** private endpoints — the
    read/query calls :meth:`balances`, :meth:`open_orders`, :meth:`fills`,
    :meth:`cancel_order` (cancelling an already-cancelled order is a no-op) — so
    they keep retrying. It is **not** safe for :meth:`place_order` (``AddOrder``):
    a blind retry after an *ambiguous* failure (the order landed but the response
    was lost) would place a **second** order. :meth:`place_order` therefore opts
    out of retries (``retry=False``); on an ambiguous failure the transport
    raises :class:`~trading_bot.transport.http.AmbiguousRequestError` and the
    caller must reconcile
    (:func:`~trading_bot.application.reconcile.reconcile`) — never blind-retry.

    Parameters
    ----------
    api_key : str, optional
        Kraken API key. Defaults to ``$KRAKEN_API_KEY``. ``None``/empty leaves
        the broker public-only.
    api_secret : str, optional
        Base64 Kraken API secret. Defaults to ``$KRAKEN_API_SECRET``.
    http : AsyncHTTPClient, optional
        Transport client. Defaults to one wired for the ``"kraken"`` exchange
        with a shared :class:`~trading_bot.transport.ratelimit.RateLimiter`.
    call_counter : KrakenCallCounter, optional
        Kraken's decaying private-endpoint counter. Defaults to the conservative
        ``"starter"`` tier; consulted (with the per-endpoint cost) before each
        private request.

    Attributes
    ----------
    name : str
        The venue key, ``"kraken"`` (the factory selects this adapter on it).

    """

    name = "kraken"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        http: AsyncHTTPClient | None = None,
        call_counter: KrakenCallCounter | None = None,
    ) -> None:
        # Credentials are env-sourced by default and never logged. An empty
        # string is treated as "absent" so a blank env var stays public-only.
        self._api_key = api_key if api_key is not None else os.environ.get(
            "KRAKEN_API_KEY", ""
        )
        self._api_secret = (
            api_secret
            if api_secret is not None
            else os.environ.get("KRAKEN_API_SECRET", "")
        )
        self._http = http or AsyncHTTPClient(
            exchange=self.name, limiter=RateLimiter()
        )
        self._counter = call_counter or KrakenCallCounter.for_tier("starter")
        # Monotonic nonce state (B-3): a counter seeded from the current time in
        # microseconds, advanced under a lock so it is strictly increasing across
        # concurrent private calls and a backward clock step. See :meth:`_nonce`.
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    # --- capability declaration -------------------------------------------- #

    def capabilities(self) -> set[Capability]:
        """The :class:`Capability` set this adapter serves.

        All six REST operations are implemented (place/cancel/open-orders,
        balances, fills, ticker). The private/authenticated WebSocket feed
        (:data:`~trading_bot.brokers.base.Capability.PRIVATE_WS`) is **not** part
        of this REST adapter (it lands in the WS leaf), so it is omitted.
        """
        return {
            Capability.PLACE_ORDER,
            Capability.CANCEL,
            Capability.OPEN_ORDERS,
            Capability.BALANCES,
            Capability.FILLS,
            Capability.TICKER,
        }

    # --- credentials / nonce ----------------------------------------------- #

    @property
    def has_credentials(self) -> bool:
        """Whether both API key and secret are present (private calls possible)."""
        return bool(self._api_key) and bool(self._api_secret)

    def _require_credentials(self) -> None:
        """Raise :class:`BrokerError` if a private call lacks credentials."""
        if not self.has_credentials:
            raise BrokerError(
                "Kraken private endpoint requires credentials; set "
                "KRAKEN_API_KEY and KRAKEN_API_SECRET in the environment"
            )

    def _nonce(self) -> str:
        """A fresh, strictly-increasing nonce, safe under concurrency and clock skew.

        Kraken rejects a signed request whose nonce is not greater than the
        previous one for the same key (``EAPI:Invalid nonce``), which is a hard,
        non-retriable failure on ``AddOrder``. A bare ``int(time.time()*1e6)``
        can *repeat* (two calls within the same microsecond) or *go backwards*
        (an NTP step-back), both of which trigger it.

        This holds a monotonic counter guarded by a lock: the next nonce is the
        larger of the current microsecond clock and ``last_nonce + 1``. So it is
        strictly increasing across concurrent callers **and** across a backward
        clock step, while still tracking wall time when the clock advances
        normally. The lock section takes no ``await``, so it is safe under
        asyncio and across threads.
        """
        with self._nonce_lock:
            candidate = int(time.time() * 1_000_000)
            nonce = candidate if candidate > self._last_nonce else self._last_nonce + 1
            self._last_nonce = nonce
        return str(nonce)

    # --- request plumbing -------------------------------------------------- #

    @staticmethod
    def _raise_on_error(payload: Any, *, context: str) -> dict[str, Any]:
        """Return ``payload["result"]`` or raise a mapped domain error on a venue error.

        Kraken always wraps responses as ``{"error": [...], "result": {...}}``; a
        non-empty ``error`` array is a venue rejection. The array is mapped to the
        most specific domain error via :func:`_map_kraken_error` (insufficient
        funds / invalid pair / invalid nonce / rate limit / service unavailable /
        generic). The error strings are plain venue diagnostics (never key
        material), so they are safe to surface.
        """
        if not isinstance(payload, dict):
            raise BrokerError(f"Kraken {context}: malformed response {payload!r}")
        errors = payload.get("error") or []
        if errors:
            raise _map_kraken_error(errors, context=context)
        result = payload.get("result")
        if result is None:
            raise BrokerError(f"Kraken {context}: missing result")
        if not isinstance(result, dict):
            raise BrokerError(
                f"Kraken {context}: unexpected result {result!r}"
            )
        return result

    async def _public_get(
        self, endpoint: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """GET a public endpoint and return its ``result`` (or raise)."""
        url = f"{_API_BASE}{_PUBLIC}/{endpoint}"
        async with self._http as client:
            payload = await client.get(url, params=dict(params))
        return self._raise_on_error(payload, context=endpoint)

    async def _private_post(
        self, endpoint: str, data: Mapping[str, Any], *, retry: bool = True
    ) -> dict[str, Any]:
        """Sign and POST a private endpoint, returning its ``result`` (or raise).

        Builds a fresh ``nonce``-first body (a strictly-increasing nonce — see
        :meth:`_nonce`), throttles via the Kraken call counter at the endpoint's
        cost, signs with :func:`_sign`, and sends the ``API-Key`` / ``API-Sign``
        headers. Requires credentials.

        Retriable HTTP-200 venue errors
        --------------------------------
        Kraken returns ``EService:Unavailable`` / ``EAPI:Rate limit`` inside an
        **HTTP 200** body, so the transport's 5xx/429 retry never sees them. When
        ``retry`` is ``True`` (the idempotent read/query endpoints) such an error
        is retried here with a bounded exponential backoff before it is finally
        surfaced as the mapped domain error. When ``retry`` is ``False`` (the
        **non-idempotent** ``AddOrder``) it is **not** retried — a retriable
        200-error still maps to :class:`~trading_bot.domain.errors.
        ServiceUnavailable` / :class:`~trading_bot.domain.errors.RateLimited` and
        is raised, preserving the reconcile-before-retry guarantee for order
        submission (an ambiguous 5xx/timeout is separately raised as
        :class:`~trading_bot.transport.http.AmbiguousRequestError` by the
        transport).

        Parameters
        ----------
        endpoint : str
            The private endpoint name (e.g. ``"Balance"``, ``"AddOrder"``).
        data : mapping
            The endpoint body (the ``nonce`` is prepended here).
        retry : bool, default True
            Forwarded to :meth:`~trading_bot.transport.http.AsyncHTTPClient.post`
            for the transport-level (5xx/429/transport) retry, **and** gates the
            HTTP-200 venue-error retry above. ``True`` for **idempotent**
            endpoints (queries/reads — a duplicate is harmless); ``False`` for the
            **non-idempotent** ``AddOrder`` so a blind retry can never
            double-submit (see :meth:`place_order`).
        """
        self._require_credentials()
        max_attempts = self._http.max_retries if retry else 1
        last_errors: list[str] = []
        for attempt in range(max_attempts):
            payload = await self._send_signed(endpoint, data, retry=retry)
            errors = payload.get("error") or [] if isinstance(payload, dict) else []
            # A retriable HTTP-200 venue error (service unavailable / rate limit)
            # is retried only on the idempotent path; AddOrder (retry=False) never
            # loops here, preserving reconcile-before-retry.
            if retry and errors and _is_retriable_kraken_error(errors):
                last_errors = errors
                if attempt + 1 < max_attempts:
                    await self._http.sleep_backoff(attempt)
                    continue
            return self._raise_on_error(payload, context=endpoint)
        # Retries exhausted on a retriable 200-error: surface the mapped error.
        raise _map_kraken_error(last_errors, context=endpoint)

    async def _send_signed(
        self, endpoint: str, data: Mapping[str, Any], *, retry: bool
    ) -> Any:
        """Sign and POST one attempt of a private request; return the raw payload.

        A single signed attempt (fresh nonce, call-counter throttle, ``API-Key`` /
        ``API-Sign`` headers). ``retry`` is forwarded to the transport: ``False``
        (``AddOrder``) sends it **at most once** so an ambiguous transient failure
        raises :class:`~trading_bot.transport.http.AmbiguousRequestError` rather
        than double-submitting; the HTTP-200 venue-error retry loop lives in
        :meth:`_private_post`.
        """
        path = f"{_PRIVATE}/{endpoint}"
        # Build the body nonce-first so the signed postdata is deterministic.
        body: dict[str, Any] = {"nonce": self._nonce()}
        body.update(data)
        signature = _sign(path, body, self._api_secret)
        headers = {"API-Key": self._api_key, "API-Sign": signature}

        await self._counter.acquire_method(endpoint)
        url = f"{_API_BASE}{path}"
        async with self._http as client:
            return await client.post(
                url, data=body, headers=headers, retry=retry
            )

    # --- public endpoints -------------------------------------------------- #

    async def instrument(self, symbol: Symbol) -> Instrument:
        """Build an :class:`Instrument` for ``symbol`` from Kraken ``AssetPairs``.

        Reads Kraken's ``pair_decimals`` / ``lot_decimals`` to populate the
        instrument's price/quantity precision.

        Parameters
        ----------
        symbol : Symbol
            The canonical pair to describe.

        Returns
        -------
        Instrument
            The instrument with venue price/qty precision filled in.

        Raises
        ------
        BrokerError
            If Kraken returns an error or an unrecognisable pair entry.

        """
        pair = symbol.to_venue_symbol(self.name)
        result = await self._public_get("AssetPairs", {"pair": pair})
        if not result:
            raise BrokerError(f"Kraken AssetPairs: no entry for {pair!r}")
        # Kraken keys the result by its canonical pair name (often the legacy
        # X/Z form), not by the altname we queried with; take the sole entry.
        entry = next(iter(result.values()))
        price_precision = entry.get("pair_decimals")
        qty_precision = entry.get("lot_decimals")
        ordermin = entry.get("ordermin")
        costmin = entry.get("costmin")
        return Instrument(
            symbol=symbol,
            price_precision=(
                int(price_precision) if price_precision is not None else None
            ),
            qty_precision=(
                int(qty_precision) if qty_precision is not None else None
            ),
            # Kraken ``ordermin`` is the minimum order volume; ``costmin`` the
            # minimum order cost (notional). Both are decimal strings.
            min_qty=money(str(ordermin)) if ordermin is not None else None,
            min_notional=money(str(costmin)) if costmin is not None else None,
        )

    async def ticker(self, instrument: Instrument) -> Money:
        """Return the public last-trade price for ``instrument`` as a ``Decimal``.

        Parameters
        ----------
        instrument : Instrument
            The instrument to price.

        Returns
        -------
        Decimal
            The last trade price (Kraken ``Ticker`` field ``c[0]``), exact.

        Raises
        ------
        BrokerError
            If Kraken errors or the ticker payload lacks a last price.

        """
        pair = instrument.symbol.to_venue_symbol(self.name)
        result = await self._public_get("Ticker", {"pair": pair})
        if not result:
            raise BrokerError(f"Kraken Ticker: no entry for {pair!r}")
        entry = next(iter(result.values()))
        # ``c`` is [last_trade_price, lot_volume]; the last price is c[0].
        close = entry.get("c")
        if not close:
            raise BrokerError(f"Kraken Ticker: no last price for {pair!r}")
        return money(str(close[0]))

    # --- private endpoints ------------------------------------------------- #

    async def balances(self) -> dict[str, Money]:
        """Return free balances keyed by canonical asset code (``Balance``).

        Returns
        -------
        dict of str to Decimal
            Canonical asset code (``"USD"``, ``"BTC"``, ...) to its balance as an
            exact :class:`~decimal.Decimal`.

        Raises
        ------
        BrokerError
            Without credentials, or on a Kraken error.

        """
        result = await self._private_post("Balance", {})
        # Kraken keys balances by venue asset code (ZUSD, XXBT, ...). Normalise
        # to canonical codes; amounts are exact decimal strings.
        return {
            normalise(asset): money(str(amount))
            for asset, amount in result.items()
        }

    async def place_order(self, order: Order) -> str:
        """Submit ``order`` via ``AddOrder`` and return Kraken's ``txid``.

        Maps the domain order to Kraken's ``AddOrder`` parameters:
        ``type``=buy/sell from :class:`OrderSide`, ``ordertype``=market/limit/
        stop-loss from :class:`OrderType`, ``volume``=qty, ``pair`` from the
        instrument, ``price``=``limit_price`` (limit) or ``stop_price`` (stop).

        Venue-idempotency
        -----------------
        ``AddOrder`` is **non-idempotent** — a second submission places a second
        order. So this call is sent **at most once** (``retry=False``): the
        transport will not blindly retry it on an ambiguous transient failure (a
        5xx / dropped connection after the order may already have landed).
        Instead it raises
        :class:`~trading_bot.transport.http.AmbiguousRequestError`, signalling
        the caller to reconcile
        (:func:`~trading_bot.application.reconcile.reconcile` — refetch open
        orders + fills) and decide from the venue's actual state — **never** to
        blind-retry and risk a duplicate. (Engine-side, the
        :class:`~trading_bot.application.order_router.OrderRouter`'s
        client-order-id dedup also guards against re-submitting the *same* logical
        order; this guard closes the remaining transport-level retry window.)

        Parameters
        ----------
        order : Order
            The domain order to submit.

        Returns
        -------
        str
            Kraken's order id (the first ``txid``).

        Raises
        ------
        BrokerError
            Without credentials, on a Kraken error, or if no ``txid`` is
            returned.
        AmbiguousRequestError
            On an ambiguous transient failure — the order may have landed;
            reconcile (:func:`~trading_bot.application.reconcile.reconcile`)
            before any retry.

        """
        result = await self._private_post(
            "AddOrder", self._add_order_params(order), retry=False
        )
        txids = result.get("txid") or []
        if not txids:
            raise BrokerError(
                f"Kraken AddOrder: no txid returned for {order.client_order_id}"
            )
        return str(txids[0])

    def _add_order_params(self, order: Order) -> dict[str, str]:
        """Render a domain :class:`Order` to Kraken ``AddOrder`` parameters.

        Pure (no I/O), so the order-to-payload mapping is unit-testable on its
        own. ``price`` is the limit price for limit orders and the stop price
        for stop-loss orders; market orders carry no price.

        Quantization (B-2)
        ------------------
        ``volume`` and ``price`` are quantized to the instrument's lot / tick
        (:meth:`~trading_bot.domain.instrument.Instrument.prepare_order_values`),
        rounding **down** so a submission never overshoots the intended size or
        price (a sell never sells more than held). An order that quantizes to a
        sub-lot / sub-minimum size is rejected there with
        :class:`~trading_bot.domain.errors.OrderTooSmall` — before a doomed
        request goes out — rather than sent to be rejected by Kraken.

        Idempotency key (B-5 / B-15)
        ----------------------------
        The domain ``client_order_id`` is forwarded as Kraken's ``userref`` — a
        32-bit int — via a deterministic hash (:func:`_userref_for`), so an
        arbitrary id is **transformed**, never dropped; a dropped id would make
        :func:`~trading_bot.application.reconcile.reconcile` unable to correlate
        the order. The primary correlation key remains the ``txid``; the userref
        is a best-effort venue-side lookup/idempotency tag. Idempotency is also
        enforced engine-side by the ``OrderRouter`` (client-order-id dedup) and,
        at the transport level, by :meth:`place_order` not blindly retrying a
        non-idempotent ``AddOrder`` on an ambiguous failure.
        """
        qty, limit_price, stop_price = order.instrument.prepare_order_values(
            order.qty, limit_price=order.limit_price, stop_price=order.stop_price
        )
        params: dict[str, str] = {
            "pair": order.instrument.symbol.to_venue_symbol(self.name),
            "type": order.side.value,  # "buy" / "sell"
            "ordertype": _ORDERTYPE_TO_KRAKEN[order.type],
            "volume": str(qty),
            "userref": str(_userref_for(order.client_order_id)),
        }
        if order.type is OrderType.STOP_LOSS:
            # STOP_LOSS carries its trigger in ``stop_price``.
            params["price"] = str(stop_price)
        elif limit_price is not None:
            # LIMIT (and a priced BEST_LIMIT) carry a ``limit_price``.
            params["price"] = str(limit_price)
        return params

    async def cancel_order(self, venue_order_id: str) -> None:
        """Cancel the live order identified by ``venue_order_id`` (``CancelOrder``).

        Parameters
        ----------
        venue_order_id : str
            Kraken's order ``txid`` (as returned by :meth:`place_order`).

        Raises
        ------
        BrokerError
            Without credentials, or on a Kraken error.

        """
        await self._private_post("CancelOrder", {"txid": venue_order_id})

    async def open_orders(self) -> list[Order]:
        """Return Kraken's open orders rebuilt as domain :class:`Order`s (``OpenOrders``).

        Each Kraken open order is reconstructed into a domain order driven
        through ``submit`` -> ``open`` (and ``apply_fill`` if partially executed)
        so the status matches Kraken's view, with ``venue_order_id`` set to the
        ``txid``.

        Returns
        -------
        list of Order
            The open orders as domain objects.

        Raises
        ------
        BrokerError
            Without credentials, or on a Kraken error.

        """
        result = await self._private_post("OpenOrders", {})
        orders: list[Order] = []
        for txid, info in (result.get("open") or {}).items():
            orders.append(self._rebuild_order(txid, info))
        return orders

    def _rebuild_order(self, txid: str, info: Mapping[str, Any]) -> Order:
        """Rebuild a domain :class:`Order` from a Kraken open-order entry.

        The rebuilt order's ``client_order_id`` is the ``userref`` Kraken echoes
        back (the value :meth:`_add_order_params` sent, derived from the domain
        id via :func:`_userref_for`) so :func:`~trading_bot.application.reconcile.
        reconcile` correlates on the value **actually sent**. When no ``userref``
        is present (an order placed outside this engine), it falls back to the
        ``txid`` (still a stable, unique venue id) so a foreign order is never
        left without an identity.
        """
        descr = info.get("descr", {})
        symbol = parse_kraken_pair(str(descr.get("pair", "")))
        side = OrderSide(descr.get("type", "buy"))
        otype = _KRAKEN_TO_ORDERTYPE.get(
            str(descr.get("ordertype", "")), OrderType.LIMIT
        )
        qty = money(str(info.get("vol", "0")))
        price_str = descr.get("price")
        limit_price = (
            money(str(price_str))
            if otype in (OrderType.LIMIT, OrderType.BEST_LIMIT)
            and price_str
            and money(str(price_str)) > 0
            else None
        )
        stop_price = (
            money(str(price_str))
            if otype is OrderType.STOP_LOSS and price_str
            else None
        )
        userref = info.get("userref")
        client_order_id = str(userref) if userref not in (None, "", 0) else txid
        order = Order(
            client_order_id=client_order_id,
            instrument=Instrument(symbol),
            side=side,
            qty=qty,
            type=otype,
            limit_price=limit_price,
            stop_price=stop_price,
        )
        order.submit()
        order.open(txid)
        # Reflect any already-executed volume so the status matches Kraken's.
        vol_exec = money(str(info.get("vol_exec", "0")))
        if vol_exec > 0:
            avg_price = info.get("price") or descr.get("price") or "0"
            fill_price = money(str(avg_price))
            if fill_price > 0:
                order.apply_fill(vol_exec, fill_price)
        return order

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        """Return executions as domain :class:`Fill`s (``TradesHistory``).

        Parameters
        ----------
        since_ms : int, optional
            Lower time bound as **milliseconds since the Unix epoch (UTC)**.
            ``None`` returns Kraken's default recent window. Converted to the
            seconds ``start`` cursor Kraken expects.

        Returns
        -------
        list of Fill
            The executions as immutable domain fills (exact Decimal qty/price/fee).

        Raises
        ------
        BrokerError
            Without credentials, or on a Kraken error.

        """
        params: dict[str, Any] = {}
        if since_ms is not None:
            # Kraken's ``start`` is in seconds (it accepts fractional seconds).
            params["start"] = since_ms / 1000.0
        result = await self._private_post("TradesHistory", params)
        fills: list[Fill] = []
        for trade_id, info in (result.get("trades") or {}).items():
            fills.append(self._build_fill(trade_id, info))
        return fills

    @staticmethod
    def _build_fill(trade_id: str, info: Mapping[str, Any]) -> Fill:
        """Build a domain :class:`Fill` from a Kraken trade-history entry."""
        symbol = parse_kraken_pair(str(info.get("pair", "")))
        side = OrderSide(info.get("type", "buy"))
        # Kraken ``time`` is fractional seconds since epoch -> ms int.
        ts_ms = int(float(info.get("time", 0)) * 1000)
        return Fill(
            fill_id=str(trade_id),
            client_order_id=str(info.get("ordertxid", trade_id)),
            instrument=Instrument(symbol),
            side=side,
            qty=money(str(info.get("vol", "0"))),
            price=money(str(info.get("price", "0"))),
            fee=money(str(info.get("fee", "0"))),
            ts=ts_ms,
        )
