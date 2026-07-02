"""The :class:`BinanceBroker` — Binance spot REST adapter behind the :class:`Broker` port.

This is the concrete Binance implementation of the venue-neutral
:class:`~trading_bot.brokers.base.Broker` port — the sibling of
:class:`~trading_bot.brokers.kraken.KrakenBroker`. It speaks **domain types only**
(:class:`~trading_bot.domain.order.Order`,
:class:`~trading_bot.domain.fill.Fill`,
:class:`~trading_bot.domain.instrument.Instrument`,
:class:`~trading_bot.domain.money.Money`) on its surface and translates them to
and from Binance's spot REST payloads underneath, using the
:mod:`trading_bot.transport` plumbing for I/O, retry/backoff and rate limiting.

Signing
-------
Private endpoints are authenticated with Binance's HMAC-SHA256 scheme, factored
into the module-level :func:`_sign` helper:

* ``query`` is the urlencoded params **including** ``timestamp`` (ms) and
  ``recvWindow``;
* ``signature = hmac_sha256(secret, query).hexdigest()``;
* the signed request appends ``&signature=<sig>`` and sends the
  ``X-MBX-APIKEY: <key>`` header.

:func:`_sign` is a pure function (no I/O, no clock) so it can be checked against
Binance's published vector deterministically — that vector is the *only* proof of
signing correctness exercised in the unit suite; the real round-trip is proven on
Binance's **testnet** (opt-in, key-gated).

Credentials & posture
---------------------
Credentials come from the environment (``BINANCE_API_KEY`` /
``BINANCE_API_SECRET``) and **never** from code. The broker is constructible
*without* them — public market-data calls (:meth:`BinanceBroker.ticker`, the
:class:`Instrument` builder) work key-free — and any private call attempted
without credentials raises a clear :class:`~trading_bot.domain.errors.BrokerError`
*before* a request goes out. Key material is never logged. Add a
:attr:`has_credentials` property (mirrors Kraken).

Base URL / testnet
------------------
The constructor's ``base_url`` defaults to ``$BINANCE_API_BASE`` or
``https://api.binance.com``. Point it at ``https://testnet.binance.vision`` to
exercise the signed round-trip against Binance's spot testnet — both speak the
same ``/api/v3/*`` paths.

Composite venue-order-id
------------------------
Binance ``cancel`` / order-status require a **symbol** alongside the order id,
but the port's :meth:`cancel_order` carries only an id. The adapter therefore
makes its venue id the **composite** ``"{SYMBOL}:{orderId}"`` (e.g.
``"BTCUSDT:123456"``): :meth:`place_order` and :meth:`open_orders` both produce
this form and :meth:`cancel_order` splits it back. The id stays opaque text to
the router / reconcile / store, so this is self-contained.

Rate limit
----------
Construction wires an :class:`~trading_bot.transport.http.AsyncHTTPClient` for the
``"binance"`` exchange with a :class:`~trading_bot.transport.ratelimit.RateLimiter`.
Binance is **weight-metered**, not flat-rate: the limiter builds a
:class:`~trading_bot.transport.ratelimit.WeightBucket` (1200 request-weight per
rolling minute) for ``"binance"``, and each call is charged its per-endpoint
*weight* (:data:`_ENDPOINT_WEIGHTS`). The transport also feeds Binance's
``X-MBX-USED-WEIGHT-1M`` response header back into the bucket to stay in step with
the venue, and honours **418** (IP ban) / **429** ``Retry-After`` by parking the
next call. See :meth:`BinanceBroker._weight_for`.

Money
-----
Every amount Binance reports is a decimal string; it is parsed with
``money(str(...))`` so prices, volumes and fees stay exact :class:`~decimal.
Decimal` and never round-trip through ``float``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from trading_bot.brokers.base import Broker, Capability
from trading_bot.domain.errors import (
    BrokerError,
    InsufficientBalance,
    InvalidInstrument,
    RateLimited,
    ServiceUnavailable,
)
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    normalise,
    parse_binance_symbol,
)
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.transport.http import AsyncHTTPClient
from trading_bot.transport.ratelimit import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["BinanceBroker"]

_DEFAULT_API_BASE = "https://api.binance.com"
#: Binance spot testnet base URL (same ``/api/v3/*`` paths as mainnet).
TESTNET_API_BASE = "https://testnet.binance.vision"
_API_PREFIX = "/api/v3"
#: Default ``recvWindow`` (ms): how long a signed request stays valid server-side.
_RECV_WINDOW = 5000
#: Separator between the symbol and the numeric order id in the composite venue id.
_VENUE_ID_SEP = ":"

# Domain OrderType -> Binance ``type`` string. BEST_LIMIT renders as a plain
# LIMIT (its price is discovered by the caller before submission, so by the time
# it reaches the broker it carries a concrete ``limit_price``).
_ORDERTYPE_TO_BINANCE: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_LOSS: "STOP_LOSS_LIMIT",
    OrderType.BEST_LIMIT: "LIMIT",
}

# Binance ``type`` string -> domain OrderType (for rebuilding open orders).
_BINANCE_TO_ORDERTYPE: dict[str, OrderType] = {
    "MARKET": OrderType.MARKET,
    "LIMIT": OrderType.LIMIT,
    "LIMIT_MAKER": OrderType.LIMIT,
    "STOP_LOSS_LIMIT": OrderType.STOP_LOSS,
    "STOP_LOSS": OrderType.STOP_LOSS,
}

# Binance's ``newClientOrderId`` constraint: at most 36 chars from the charset
# [.A-Za-z0-9:/_-]. The runner's ``f"{name}-{step}"`` ids satisfy this verbatim;
# an arbitrary domain client_order_id may not, in which case it is deterministically
# *transformed* into a fitting id (never dropped — see :func:`_binance_client_order_id`
# and :meth:`BinanceBroker._order_params`).
_CLIENT_ORDER_ID_RE = re.compile(r"^[.A-Za-z0-9:/_-]{1,36}$")

# Binance API error codes -> retriable class. ``-1003`` (too many requests) and
# ``-1015`` (too many orders) are rate limits; ``-1001`` (disconnected) and
# ``-1016`` (service shutting down) are service-unavailable. All are retriable on
# the idempotent path; a non-idempotent order POST still never blind-retries.
_BINANCE_RATE_LIMIT_CODES: frozenset[int] = frozenset({-1003, -1015})
_BINANCE_SERVICE_CODES: frozenset[int] = frozenset({-1001, -1016})
# ``-2010`` (order would not execute / rejected) can carry "insufficient balance".
_BINANCE_INSUFFICIENT_MARKERS: tuple[str, ...] = (
    "INSUFFICIENT BALANCE",
    "INSUFFICIENT FUNDS",
)
# ``-1121`` invalid symbol; ``-1100``/`-1130` illegal params sometimes name a bad
# symbol, but ``-1121`` is the unambiguous invalid-instrument code.
_BINANCE_INVALID_SYMBOL_CODES: frozenset[int] = frozenset({-1121})

# Per-endpoint Binance spot *request weight* (charged against the 1200/min budget
# via the WeightBucket). Values from Binance's published spot API weights; a
# symbol-scoped query costs less than an account-wide one. An endpoint not listed
# here charges :data:`_DEFAULT_ENDPOINT_WEIGHT` (conservative). These are the
# venue-published costs, so the proactive limiter depletes the budget at the same
# rate the venue does — the "never exceed the venue budget" invariant.
_ENDPOINT_WEIGHTS: dict[str, int] = {
    "ticker/price": 2,  # single-symbol price
    "exchangeInfo": 20,  # symbol metadata
    "account": 20,  # balances
    "order": 1,  # place (POST) / cancel (DELETE), symbol-scoped
    "openOrders": 6,  # symbol-scoped; account-wide is 80 (we scope per call)
    "myTrades": 20,  # per-symbol trade history
}
#: Weight charged for an endpoint absent from :data:`_ENDPOINT_WEIGHTS`.
_DEFAULT_ENDPOINT_WEIGHT = 1


def _binance_client_order_id(client_order_id: str) -> str:
    """Return a Binance-valid ``newClientOrderId`` for ``client_order_id``.

    When the domain id already fits Binance's constraint (≤36 chars, charset
    ``[.A-Za-z0-9:/_-]``) it is returned **verbatim** — the common case for the
    runner's ``f"{name}-{step}"`` ids. Otherwise it is deterministically
    **transformed** (not dropped): a ``"tb-"`` prefix plus the hex BLAKE2b digest
    of the original id, which is always a valid 35-char id. Determinism matters
    because :func:`~trading_bot.application.reconcile.reconcile` correlates on the
    value **actually sent**: dropping the id would make a placed order look like an
    untracked orphan (and re-ingest it), so the id is always carried in some
    stable, venue-valid form.
    """
    if _is_valid_new_client_order_id(client_order_id):
        return client_order_id
    digest = hashlib.blake2b(client_order_id.encode(), digest_size=16).hexdigest()
    return f"tb-{digest}"


def _map_binance_error(code: int, msg: str, *, context: str) -> BrokerError:
    """Map a Binance ``{"code", "msg"}`` error to the most specific domain error.

    Returns :class:`~trading_bot.domain.errors.InvalidInstrument` (``-1121``),
    :class:`~trading_bot.domain.errors.RateLimited` (``-1003`` / ``-1015``),
    :class:`~trading_bot.domain.errors.ServiceUnavailable` (``-1001`` / ``-1016``),
    or :class:`~trading_bot.domain.errors.InsufficientBalance` (``-2010`` naming
    an insufficient balance), falling back to a generic :class:`BrokerError`. The
    ``msg`` is a plain venue diagnostic (never key material), safe to surface.
    """
    detail = f"Binance {context}: {msg} (code {code})"
    upper = msg.upper()
    if code in _BINANCE_INVALID_SYMBOL_CODES or "INVALID SYMBOL" in upper:
        return InvalidInstrument(context, f"{msg} (code {code})")
    if code in _BINANCE_RATE_LIMIT_CODES:
        return RateLimited(detail)
    if code in _BINANCE_SERVICE_CODES:
        return ServiceUnavailable(detail)
    if any(marker in upper for marker in _BINANCE_INSUFFICIENT_MARKERS):
        return InsufficientBalance(detail)
    return BrokerError(detail)


def _sign(query: str, secret: str) -> str:
    """Compute Binance's HMAC-SHA256 ``signature`` for a signed request.

    Pure function (no I/O, no clock): given the urlencoded ``query`` string
    (which must already carry ``timestamp`` and ``recvWindow``) and the API
    ``secret``, returns the hex ``signature`` to append as ``&signature=<sig>``.
    Matched against Binance's published test vector.

    The algorithm is simply
    ``hmac.new(secret, query, sha256).hexdigest()``.

    Parameters
    ----------
    query : str
        The urlencoded request parameters, e.g.
        ``"symbol=LTCBTC&side=BUY&...&recvWindow=5000&timestamp=1499827319559"``.
    secret : str
        The Binance API secret.

    Returns
    -------
    str
        The hex-encoded HMAC-SHA256 signature.

    """
    return hmac.new(
        secret.encode(), query.encode(), hashlib.sha256
    ).hexdigest()


def _is_valid_new_client_order_id(client_order_id: str) -> bool:
    """Whether ``client_order_id`` fits Binance's ``newClientOrderId`` constraint."""
    return bool(_CLIENT_ORDER_ID_RE.match(client_order_id))


def _precision_from_step(step: str | None) -> int | None:
    """Derive a decimal-place count from a Binance ``tickSize`` / ``stepSize``.

    Binance expresses precision as a step string (``"0.01000000"``,
    ``"0.00001000"``); the number of decimal places is the position of the last
    non-zero digit. ``"1.00000000"`` (whole-unit step) → ``0``. Returns ``None``
    when no step is given.
    """
    if step is None:
        return None
    text = step.strip()
    if "." not in text:
        return 0
    fractional = text.split(".", 1)[1].rstrip("0")
    return len(fractional)


class BinanceBroker(Broker):
    """Binance spot REST adapter implementing the venue-neutral :class:`Broker` port.

    Public market data (ticker, instrument metadata) needs no credentials;
    private order/balance/fill calls read ``BINANCE_API_KEY`` /
    ``BINANCE_API_SECRET`` from the environment and sign each request with
    :func:`_sign`. A private call attempted without credentials raises
    :class:`~trading_bot.domain.errors.BrokerError` before any I/O.

    Retry policy — idempotency-aware
    --------------------------------
    The transport retries transient failures (5xx / 429 / dropped connections)
    with backoff — safe for the **idempotent** endpoints (the read/query calls
    :meth:`balances`, :meth:`open_orders`, :meth:`fills`, and :meth:`cancel_order`,
    cancelling an already-cancelled order being a no-op). It is **not** safe for
    :meth:`place_order` (``POST /api/v3/order``): a blind retry after an *ambiguous*
    failure (the order landed but the response was lost) could place a **second**
    order. :meth:`place_order` therefore opts out of retries (``retry=False``); on
    an ambiguous failure the transport raises
    :class:`~trading_bot.transport.http.AmbiguousRequestError` and the caller must
    reconcile (:func:`~trading_bot.application.reconcile.reconcile`) — never
    blind-retry. (Binance's own ``newClientOrderId`` dedup is a second guard: a
    duplicate id is rejected with ``-2010``.)

    Parameters
    ----------
    api_key : str, optional
        Binance API key. Defaults to ``$BINANCE_API_KEY``. ``None``/empty leaves
        the broker public-only.
    api_secret : str, optional
        Binance API secret. Defaults to ``$BINANCE_API_SECRET``.
    base_url : str, optional
        REST base URL. Defaults to ``$BINANCE_API_BASE`` or
        ``https://api.binance.com``. Point it at
        :data:`TESTNET_API_BASE` (``https://testnet.binance.vision``) for the
        testnet round-trip.
    symbols : iterable of Symbol, optional
        The instruments :meth:`fills` should query (Binance has no account-wide
        trade history; ``myTrades`` is per-symbol). Without it, :meth:`fills`
        raises a clear :class:`~trading_bot.domain.errors.BrokerError`.
    http : AsyncHTTPClient, optional
        Transport client. Defaults to one wired for the ``"binance"`` exchange
        with a shared :class:`~trading_bot.transport.ratelimit.RateLimiter`.
    recv_window : int, default 5000
        Binance ``recvWindow`` (ms) carried on every signed request.

    Attributes
    ----------
    name : str
        The venue key, ``"binance"`` (the factory selects this adapter on it).

    """

    name = "binance"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str | None = None,
        symbols: Iterable[Symbol] | None = None,
        http: AsyncHTTPClient | None = None,
        recv_window: int = _RECV_WINDOW,
    ) -> None:
        # Credentials are env-sourced by default and never logged. An empty
        # string is treated as "absent" so a blank env var stays public-only.
        self._api_key = (
            api_key if api_key is not None else os.environ.get("BINANCE_API_KEY", "")
        )
        self._api_secret = (
            api_secret
            if api_secret is not None
            else os.environ.get("BINANCE_API_SECRET", "")
        )
        self._base_url = (
            base_url
            if base_url is not None
            else os.environ.get("BINANCE_API_BASE", _DEFAULT_API_BASE)
        ).rstrip("/")
        self._symbols = tuple(symbols) if symbols is not None else ()
        self._http = http or AsyncHTTPClient(
            exchange=self.name, limiter=RateLimiter()
        )
        self._recv_window = recv_window

    # --- capability declaration -------------------------------------------- #

    def capabilities(self) -> set[Capability]:
        """The :class:`Capability` set this adapter serves.

        All six REST operations are implemented (place/cancel/open-orders,
        balances, fills, ticker). The private/authenticated WebSocket feed
        (:data:`~trading_bot.brokers.base.Capability.PRIVATE_WS`) is **not** part
        of this REST adapter (WS is deferred), so it is omitted.
        """
        return {
            Capability.PLACE_ORDER,
            Capability.CANCEL,
            Capability.OPEN_ORDERS,
            Capability.BALANCES,
            Capability.FILLS,
            Capability.TICKER,
        }

    # --- credentials ------------------------------------------------------- #

    @property
    def has_credentials(self) -> bool:
        """Whether both API key and secret are present (private calls possible)."""
        return bool(self._api_key) and bool(self._api_secret)

    @property
    def base_url(self) -> str:
        """The REST base URL this adapter targets (mainnet or testnet).

        Read-only introspection — useful to confirm a testnet-pinned adapter
        (:data:`TESTNET_API_BASE`) can never reach mainnet.
        """
        return self._base_url

    @property
    def is_testnet(self) -> bool:
        """Whether this adapter is pinned to Binance's spot **testnet**."""
        return self._base_url == TESTNET_API_BASE

    def _require_credentials(self) -> None:
        """Raise :class:`BrokerError` if a private call lacks credentials."""
        if not self.has_credentials:
            raise BrokerError(
                "Binance private endpoint requires credentials; set "
                "BINANCE_API_KEY and BINANCE_API_SECRET in the environment"
            )

    @staticmethod
    def _timestamp_ms() -> int:
        """The current Unix time in milliseconds (Binance ``timestamp``)."""
        return int(time.time() * 1000)

    # --- request plumbing -------------------------------------------------- #

    @staticmethod
    def _raise_on_error(payload: Any, *, context: str) -> Any:
        """Return ``payload`` or raise a mapped domain error on a Binance error body.

        Binance signals a rejection with a JSON object
        ``{"code": -xxxx, "msg": "..."}``. On a **batch/list** endpoint the error
        instead arrives **inside a JSON array** — either a single-element list
        wrapping the error object, or a mixed list where one element failed
        (Binance's batch-order responses interleave successes and per-item
        ``{"code", "msg"}`` errors). Both shapes are detected here: a bare error
        object *or* the first error object found in a top-level list raises,
        rather than being silently returned as a "successful" payload. The
        code/msg are mapped to the most specific domain error via
        :func:`_map_binance_error` (invalid instrument / rate limit / service
        unavailable / insufficient funds / generic). The ``msg`` is a plain venue
        diagnostic (never key material), so it is safe to surface. A successful
        payload is a non-error object, or a list with no error element.
        """
        error = BinanceBroker._find_error(payload)
        if error is not None:
            code, msg = error
            raise _map_binance_error(code, msg, context=context)
        return payload

    @staticmethod
    def _find_error(payload: Any) -> tuple[int, str] | None:
        """Return the ``(code, msg)`` of a Binance error in ``payload``, or ``None``.

        Handles both the bare error object ``{"code", "msg"}`` and the
        **array-wrapped** shape (an error object nested in a top-level list, as
        batch/list endpoints return). Scans only the top level of a list — a
        single failed item anywhere in a batch response is surfaced. A ``code``
        that is not coercible to ``int`` degrades to ``0`` (still an error).
        """
        if isinstance(payload, dict) and "code" in payload and "msg" in payload:
            code = payload["code"]
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                code_int = 0
            return code_int, str(payload["msg"])
        if isinstance(payload, list):
            for item in payload:
                nested = BinanceBroker._find_error(item)
                if nested is not None:
                    return nested
        return None

    @staticmethod
    def _weight_for(endpoint: str) -> int:
        """Return the Binance request *weight* charged for ``endpoint``.

        Looks ``endpoint`` up in :data:`_ENDPOINT_WEIGHTS`, falling back to
        :data:`_DEFAULT_ENDPOINT_WEIGHT`. The weight is passed to the transport,
        which charges it against the ``"binance"``
        :class:`~trading_bot.transport.ratelimit.WeightBucket` so the proactive
        limiter depletes the rolling-minute budget at the venue's own rate.
        """
        return _ENDPOINT_WEIGHTS.get(endpoint, _DEFAULT_ENDPOINT_WEIGHT)

    async def _public_get(
        self, endpoint: str, params: Mapping[str, Any]
    ) -> Any:
        """GET a public endpoint and return its parsed JSON (or raise)."""
        url = f"{self._base_url}{_API_PREFIX}/{endpoint}"
        async with self._http as client:
            payload = await client.get(
                url, params=dict(params), weight=self._weight_for(endpoint)
            )
        return self._raise_on_error(payload, context=endpoint)

    async def _signed_request(
        self,
        method: str,
        endpoint: str,
        params: Mapping[str, Any],
        *,
        retry: bool = True,
    ) -> Any:
        """Sign and send a private request, returning its parsed JSON (or raise).

        Builds the canonical query (caller params + ``recvWindow`` + ``timestamp``),
        signs it with :func:`_sign`, appends ``&signature=<sig>`` and sends the
        ``X-MBX-APIKEY`` header. Binance accepts signed params on the query string
        for every verb (GET / POST / DELETE), so the body stays empty. Requires
        credentials.

        Parameters
        ----------
        method : str
            ``"GET"``, ``"POST"`` or ``"DELETE"``.
        endpoint : str
            The endpoint path under ``/api/v3`` (e.g. ``"order"``, ``"account"``).
        params : mapping
            The request parameters (``recvWindow`` / ``timestamp`` are appended).
        retry : bool, default True
            Forwarded to the transport. ``True`` for **idempotent** endpoints
            (queries / cancel); ``False`` for the **non-idempotent** order POST so
            a blind retry can never double-submit (see :meth:`place_order`).
        """
        self._require_credentials()
        # Binance signs the full query (caller params + recvWindow + timestamp)
        # and carries it on the query string for every verb (GET / POST / DELETE),
        # with an empty body and the API key in the X-MBX-APIKEY header.
        signed = {
            **params,
            "recvWindow": self._recv_window,
            "timestamp": self._timestamp_ms(),
        }
        query = urllib.parse.urlencode(signed)
        signature = _sign(query, self._api_secret)
        url = (
            f"{self._base_url}{_API_PREFIX}/{endpoint}"
            f"?{query}&signature={signature}"
        )
        headers = {"X-MBX-APIKEY": self._api_key}

        async with self._http as client:
            payload = await client.request(
                method,
                url,
                headers=headers,
                retry=retry,
                weight=self._weight_for(endpoint),
            )
        return self._raise_on_error(payload, context=endpoint)

    # --- public endpoints -------------------------------------------------- #

    async def instrument(self, symbol: Symbol) -> Instrument:
        """Build an :class:`Instrument` for ``symbol`` from ``exchangeInfo``.

        Reads the symbol's ``filters`` — ``PRICE_FILTER.tickSize`` /
        ``LOT_SIZE.stepSize`` → price/quantity precision (decimal places). Falls
        back to ``baseAssetPrecision`` / ``quoteAssetPrecision`` when a filter is
        absent.

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
            If Binance returns an error or no entry for the symbol.

        """
        venue_symbol = symbol.to_venue_symbol(self.name)
        payload = await self._public_get(
            "exchangeInfo", {"symbol": venue_symbol}
        )
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not symbols:
            raise BrokerError(
                f"Binance exchangeInfo: no entry for {venue_symbol!r}"
            )
        entry = symbols[0]
        filters = {
            f.get("filterType"): f for f in entry.get("filters", [])
        }
        price_step = filters.get("PRICE_FILTER", {}).get("tickSize")
        qty_step = filters.get("LOT_SIZE", {}).get("stepSize")
        price_precision = _precision_from_step(price_step)
        qty_precision = _precision_from_step(qty_step)
        if price_precision is None:
            quote_prec = entry.get("quoteAssetPrecision")
            price_precision = int(quote_prec) if quote_prec is not None else None
        if qty_precision is None:
            base_prec = entry.get("baseAssetPrecision")
            qty_precision = int(base_prec) if base_prec is not None else None
        # LOT_SIZE.minQty is the minimum order quantity; NOTIONAL/MIN_NOTIONAL
        # carries the minimum order value (Binance renamed MIN_NOTIONAL ->
        # NOTIONAL). Both are decimal strings when present.
        min_qty_str = filters.get("LOT_SIZE", {}).get("minQty")
        notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        min_notional_str = notional_filter.get("minNotional")
        return Instrument(
            symbol=symbol,
            price_precision=price_precision,
            qty_precision=qty_precision,
            min_qty=money(str(min_qty_str)) if min_qty_str is not None else None,
            min_notional=(
                money(str(min_notional_str))
                if min_notional_str is not None
                else None
            ),
        )

    async def ticker(self, instrument: Instrument) -> Money:
        """Return the public last price for ``instrument`` as a ``Decimal``.

        Parameters
        ----------
        instrument : Instrument
            The instrument to price.

        Returns
        -------
        Decimal
            The last trade price (``GET /api/v3/ticker/price`` field ``price``),
            exact.

        Raises
        ------
        BrokerError
            If Binance errors or the ticker payload lacks a price.

        """
        venue_symbol = instrument.symbol.to_venue_symbol(self.name)
        payload = await self._public_get(
            "ticker/price", {"symbol": venue_symbol}
        )
        price = payload.get("price") if isinstance(payload, dict) else None
        if price is None:
            raise BrokerError(
                f"Binance ticker/price: no price for {venue_symbol!r}"
            )
        return money(str(price))

    # --- private endpoints ------------------------------------------------- #

    async def balances(self) -> dict[str, Money]:
        """Return free balances keyed by canonical asset code (``GET /account``).

        Parses ``balances: [{asset, free, locked}]``, normalises each asset code
        and keeps the **free** amount, skipping zero-free assets.

        Returns
        -------
        dict of str to Decimal
            Canonical asset code (``"USDT"``, ``"BTC"``, ...) to its free balance
            as an exact :class:`~decimal.Decimal`.

        Raises
        ------
        BrokerError
            Without credentials, or on a Binance error.

        """
        payload = await self._signed_request("GET", "account", {})
        result: dict[str, Money] = {}
        for entry in payload.get("balances", []):
            free = money(str(entry.get("free", "0")))
            if free > 0:
                result[normalise(str(entry.get("asset", "")))] = free
        return result

    async def place_order(self, order: Order) -> str:
        """Submit ``order`` via ``POST /api/v3/order``; return the composite venue id.

        Maps the domain order to Binance's ``/order`` parameters:
        ``symbol`` from the instrument, ``side``=BUY/SELL from :class:`OrderSide`,
        ``type``=MARKET/LIMIT/STOP_LOSS_LIMIT from :class:`OrderType`,
        ``quantity``=qty, ``price`` (+ ``timeInForce=GTC``) for LIMIT, and
        ``stopPrice`` for stop orders (see :meth:`_order_params`).

        Venue-idempotency
        -----------------
        The domain ``client_order_id`` is forwarded as Binance's
        ``newClientOrderId`` **iff** it fits the venue constraint (≤36 chars,
        charset ``[.A-Za-z0-9:/_-]``); the runner's ``f"{name}-{step}"`` ids do.
        Binance then dedups venue-side — a duplicate id is rejected with ``-2010``.

        ``/order`` is **non-idempotent** — a second submission places a second
        order — so this call is sent **at most once** (``retry=False``): the
        transport will not blindly retry it on an ambiguous transient failure (a
        5xx / dropped connection after the order may already have landed). Instead
        it raises :class:`~trading_bot.transport.http.AmbiguousRequestError`,
        signalling the caller to reconcile
        (:func:`~trading_bot.application.reconcile.reconcile`) and decide from the
        venue's actual state — **never** to blind-retry.

        Parameters
        ----------
        order : Order
            The domain order to submit.

        Returns
        -------
        str
            The **composite** venue order id ``"{SYMBOL}:{orderId}"`` — opaque to
            the caller, round-tripped by :meth:`cancel_order` / :meth:`open_orders`.

        Raises
        ------
        BrokerError
            Without credentials, on a Binance error, or if no order id is returned.
        AmbiguousRequestError
            On an ambiguous transient failure — the order may have landed;
            reconcile before any retry.

        """
        params = self._order_params(order)
        payload = await self._signed_request(
            "POST", "order", params, retry=False
        )
        order_id = payload.get("orderId")
        if order_id is None:
            raise BrokerError(
                f"Binance order: no orderId returned for {order.client_order_id}"
            )
        symbol = str(payload.get("symbol") or params["symbol"])
        return self._compose_venue_id(symbol, order_id)

    def _order_params(self, order: Order) -> dict[str, str]:
        """Render a domain :class:`Order` to Binance ``/order`` parameters.

        Pure (no I/O), so the order-to-payload mapping is unit-testable on its
        own. ``price`` + ``timeInForce=GTC`` are set for limit orders;
        ``stopPrice`` (and ``price``) for stop-loss-limit orders.

        Quantization (B-2)
        ------------------
        ``quantity`` and ``price`` / ``stopPrice`` are quantized to the
        instrument's lot / tick
        (:meth:`~trading_bot.domain.instrument.Instrument.prepare_order_values`),
        rounding **down** so a submission never overshoots the intended size or
        price (a sell never sells more than held). An order that quantizes to a
        sub-lot / sub-min-notional size is rejected there with
        :class:`~trading_bot.domain.errors.OrderTooSmall` before the request goes
        out, rather than sent to be rejected by Binance.

        Idempotency key (B-5 / B-15)
        ----------------------------
        The domain ``client_order_id`` is **always** forwarded as
        ``newClientOrderId`` — verbatim when it fits Binance's charset/length, or
        deterministically transformed via :func:`_binance_client_order_id` when it
        does not (never dropped: a dropped id makes
        :func:`~trading_bot.application.reconcile.reconcile` treat a placed order as
        an orphan). Binance dedups venue-side on this id (a duplicate is rejected
        with ``-2010``).
        """
        qty, limit_price, stop_price = order.instrument.prepare_order_values(
            order.qty, limit_price=order.limit_price, stop_price=order.stop_price
        )
        params: dict[str, str] = {
            "symbol": order.instrument.symbol.to_venue_symbol(self.name),
            "side": order.side.value.upper(),  # "BUY" / "SELL"
            "type": _ORDERTYPE_TO_BINANCE[order.type],
            "quantity": str(qty),
        }
        if order.type is OrderType.STOP_LOSS:
            # STOP_LOSS maps to a STOP_LOSS_LIMIT: the trigger is ``stopPrice``;
            # Binance also wants a working ``price`` + ``timeInForce`` for the
            # resting limit. With no explicit limit, rest at the stop price.
            params["stopPrice"] = str(stop_price)
            params["price"] = str(limit_price if limit_price is not None else stop_price)
            params["timeInForce"] = "GTC"
        elif order.type in (OrderType.LIMIT, OrderType.BEST_LIMIT):
            if limit_price is not None:
                params["price"] = str(limit_price)
                params["timeInForce"] = "GTC"
        # Forward the idempotency key venue-side, verbatim when valid or
        # deterministically transformed otherwise — never dropped (reconcile
        # correlates on the value actually sent).
        params["newClientOrderId"] = _binance_client_order_id(order.client_order_id)
        return params

    async def cancel_order(self, venue_order_id: str) -> None:
        """Cancel the live order identified by ``venue_order_id`` (``DELETE /order``).

        ``venue_order_id`` is the **composite** ``"{SYMBOL}:{orderId}"`` returned
        by :meth:`place_order` / :meth:`open_orders`; it is split back into the
        ``symbol`` + ``orderId`` Binance's cancel requires.

        Parameters
        ----------
        venue_order_id : str
            The composite venue id (``"BTCUSDT:123456"``).

        Raises
        ------
        BrokerError
            Without credentials, on a malformed composite id, or on a Binance error.

        """
        symbol, order_id = self._split_venue_id(venue_order_id)
        await self._signed_request(
            "DELETE", "order", {"symbol": symbol, "orderId": order_id}
        )

    async def open_orders(self) -> list[Order]:
        """Return account-wide open orders as domain :class:`Order`s (``GET /openOrders``).

        Each Binance open order is reconstructed into a domain order driven
        through ``submit`` → ``open`` (and ``apply_fill`` if partially executed)
        so the status matches Binance's view, with ``venue_order_id`` set to the
        **composite** ``"{SYMBOL}:{orderId}"`` — reproduced identically so a later
        :meth:`cancel_order` works.

        Returns
        -------
        list of Order
            The open orders as domain objects.

        Raises
        ------
        BrokerError
            Without credentials, or on a Binance error.

        """
        payload = await self._signed_request("GET", "openOrders", {})
        return [self._rebuild_order(info) for info in payload]

    def _rebuild_order(self, info: Mapping[str, Any]) -> Order:
        """Rebuild a domain :class:`Order` from a Binance open-order entry."""
        venue_symbol = str(info.get("symbol", ""))
        symbol = parse_binance_symbol(venue_symbol)
        side = OrderSide(str(info.get("side", "BUY")).lower())
        # B-12: never silently coerce an unrecognised venue order-type to LIMIT —
        # a wrong type is a wrong order. Reject it so ``reconcile`` surfaces the
        # unmapped type instead of adopting a mislabelled order (e.g. a Binance
        # ``TAKE_PROFIT_LIMIT`` rebuilt as a plain LIMIT).
        raw_type = str(info.get("type", ""))
        try:
            otype = _BINANCE_TO_ORDERTYPE[raw_type]
        except KeyError:
            raise BrokerError(
                f"Binance open order {info.get('orderId')}: unknown type "
                f"{raw_type!r} (not one of {sorted(_BINANCE_TO_ORDERTYPE)}); "
                "refusing to guess"
            ) from None
        qty = money(str(info.get("origQty", "0")))
        price = info.get("price")
        limit_price = (
            money(str(price))
            if otype in (OrderType.LIMIT, OrderType.BEST_LIMIT)
            and price
            and money(str(price)) > 0
            else None
        )
        stop_price = (
            money(str(info.get("stopPrice")))
            if otype is OrderType.STOP_LOSS and info.get("stopPrice")
            else None
        )
        # A STOP_LOSS_LIMIT on Binance also carries a resting ``price``; the
        # domain STOP_LOSS forbids a limit_price, so drop it on rebuild.
        client_id = str(info.get("clientOrderId") or info.get("orderId"))
        composite = self._compose_venue_id(venue_symbol, info.get("orderId"))
        order = Order(
            client_order_id=client_id,
            instrument=Instrument(symbol),
            side=side,
            qty=qty,
            type=otype,
            limit_price=limit_price,
            stop_price=stop_price,
        )
        order.submit()
        order.open(composite)
        # Reflect any already-executed volume so the status matches Binance's.
        # B-8: derive the *true* average fill price from Binance's executed
        # fields — ``cummulativeQuoteQty / executedQty`` — never the resting
        # LIMIT/STOP price. Fills are the PnL source of truth, so a partial
        # fill's basis must be what the venue actually executed at, not what the
        # order asked for (the two diverge whenever the book fills through the
        # limit). With no executed qty there is no average yet, so no fill is
        # applied.
        executed = money(str(info.get("executedQty", "0")))
        if executed > 0:
            avg_price = self._avg_fill_price(info, executed)
            if avg_price is not None and avg_price > 0:
                order.apply_fill(executed, avg_price)
        return order

    @staticmethod
    def _avg_fill_price(info: Mapping[str, Any], executed: Money) -> Money | None:
        """Average fill price of a partial fill: quote spent / base executed.

        Binance reports the cumulative quote spent on the executed portion as
        ``cummulativeQuoteQty`` and the executed base quantity as
        ``executedQty``; their ratio is the volume-weighted average execution
        price — the correct PnL basis (:class:`~trading_bot.domain.fill.Fill` is
        the source of truth for PnL, not the resting limit price).

        Returns ``None`` when the average cannot be derived (``executed`` is
        non-positive, or the venue reported no ``cummulativeQuoteQty`` yet) so
        the caller applies no bogus fill. All arithmetic stays exact ``Decimal``.
        """
        if executed <= 0:
            return None
        cumulative_quote = money(str(info.get("cummulativeQuoteQty", "0")))
        if cumulative_quote <= 0:
            return None
        return cumulative_quote / executed

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        """Return executions as domain :class:`Fill`s (``GET /myTrades`` per symbol).

        Binance has **no account-wide** trade history; ``myTrades`` is per-symbol.
        This queries ``myTrades`` for each configured symbol (see the ``symbols``
        constructor arg), concatenating the results.

        Parameters
        ----------
        since_ms : int, optional
            Lower time bound as **milliseconds since the Unix epoch (UTC)**,
            forwarded as Binance's ``startTime``. ``None`` returns each symbol's
            default recent window.

        Returns
        -------
        list of Fill
            The executions as immutable domain fills (exact Decimal qty/price/fee).

        Raises
        ------
        BrokerError
            Without credentials, on a Binance error, or if no symbols are
            configured (Binance has no account-wide trade history, so ``fills()``
            needs an explicit symbol set).

        """
        if not self._symbols:
            raise BrokerError(
                "Binance fills() needs a symbol set: Binance has no "
                "account-wide trade history (myTrades is per-symbol). Pass "
                "symbols=[...] to BinanceBroker."
            )
        fills: list[Fill] = []
        for symbol in self._symbols:
            venue_symbol = symbol.to_venue_symbol(self.name)
            params: dict[str, Any] = {"symbol": venue_symbol}
            if since_ms is not None:
                params["startTime"] = since_ms
            payload = await self._signed_request("GET", "myTrades", params)
            for trade in payload:
                fills.append(self._build_fill(symbol, trade))
        return fills

    @staticmethod
    def _build_fill(symbol: Symbol, info: Mapping[str, Any]) -> Fill:
        """Build a domain :class:`Fill` from a Binance ``myTrades`` entry."""
        side = OrderSide.BUY if info.get("isBuyer") else OrderSide.SELL
        return Fill(
            fill_id=str(info.get("id")),
            client_order_id=str(info.get("orderId")),
            instrument=Instrument(symbol),
            side=side,
            qty=money(str(info.get("qty", "0"))),
            price=money(str(info.get("price", "0"))),
            fee=money(str(info.get("commission", "0"))),
            ts=int(info.get("time", 0)),
        )

    # --- composite venue id ------------------------------------------------- #

    @staticmethod
    def _compose_venue_id(symbol: str, order_id: Any) -> str:
        """Compose the opaque venue id ``"{SYMBOL}:{orderId}"``."""
        return f"{symbol}{_VENUE_ID_SEP}{order_id}"

    @staticmethod
    def _split_venue_id(venue_order_id: str) -> tuple[str, str]:
        """Split the composite venue id back into ``(symbol, orderId)``.

        Splits on the **last** ``":"`` so a symbol can never be mistaken for the
        id boundary (Binance symbols carry no colon, but split-on-last is robust).
        """
        symbol, sep, order_id = venue_order_id.rpartition(_VENUE_ID_SEP)
        if not sep or not symbol or not order_id:
            raise BrokerError(
                f"Binance cancel: malformed composite venue id "
                f"{venue_order_id!r}; expected '<SYMBOL>:<orderId>'"
            )
        return symbol, order_id
