"""The :class:`KrakenPrivateWS` — Kraken v2 private WebSocket (executions/fills).

This is the **live, authenticated** counterpart to the REST
:class:`~trading_bot.brokers.kraken.KrakenBroker`: it streams Kraken's v2
``executions`` channel (own trades + order-status changes) on top of the
venue-neutral :class:`~trading_bot.transport.ws.WebSocketBase`, parsing each
frame into **domain types** — a :class:`~trading_bot.domain.fill.Fill` for every
executed trade and an :class:`OrderUpdate` for every order-status change. It is
the feed the order router / position tracker (E4) consume.

Auth-token flow
---------------
Kraken's private WebSocket is not signed per-frame; instead the client first
fetches a short-lived **WebSocket token** from the *private REST* endpoint
``GetWebSocketsToken`` (which is HMAC-signed with the API key/secret, exactly
like every other private REST call — see
:func:`trading_bot.brokers.kraken._sign`) and then presents that token in the
``subscribe`` message. Because :meth:`~trading_bot.transport.ws.WebSocketBase.on_connect`
re-runs on every reconnect, the token is re-fetched and the subscription
re-established automatically after a drop (self-healing).

The token fetch is injected as the ``token_provider`` seam — an ``async``
callable returning the token string. The default
(:func:`_broker_token_provider`) calls a :class:`KrakenBroker`'s private REST;
tests inject a fake that returns a dummy token, so the whole parse path is
exercised **offline with no API key**.

Posture
-------
**No API key is used here.** The live private connection is *deferred* until a
key is provided: the auth-token fetch and the frame parsing are proven against
**realistic canned private frames** (shape from Kraken's v2 private docs) that
parse to the expected domain :class:`Fill`s. Tokens are credentials and are
**never logged**. The public WS path is already proven against a real Kraken
frame (E2).

Money
-----
Every amount Kraken reports on the wire is a number/decimal string; it is parsed
with ``money(str(...))`` so qty/price/fee stay exact :class:`~decimal.Decimal`
and never round-trip through ``float``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, parse_kraken_pair
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide
from trading_bot.transport.ws import WebSocketBase

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trading_bot.brokers.kraken import KrakenBroker
    from trading_bot.domain.money import Money

__all__ = [
    "KrakenPrivateWS",
    "OrderUpdate",
    "TokenProvider",
]

logger = logging.getLogger(__name__)

_WS_AUTH_URL = "wss://ws-auth.kraken.com/v2"

#: An ``async`` callable returning a fresh Kraken WebSocket auth token.
TokenProvider = Callable[[], Awaitable[str]]

# Kraken v2 ``exec_type`` values that correspond to an executed trade (the only
# exec types that carry ``last_qty`` / ``last_price`` and so map to a Fill).
# Everything else (``new``, ``filled``, ``canceled``, ``expired``, ...) is an
# order-status change, surfaced as an :class:`OrderUpdate`.
_TRADE_EXEC_TYPES: frozenset[str] = frozenset({"trade"})


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    """An order-status change parsed from the Kraken v2 ``executions`` channel.

    A lightweight, immutable view of a non-trade execution event (``new``,
    ``filled``, ``canceled``, ``expired``, ...): just enough to tie the venue's
    order back to the originating order and reflect its new status. Unlike a
    :class:`~trading_bot.domain.fill.Fill` (which is an executed slice), an
    update carries no executed qty/price of its own.

    Parameters
    ----------
    client_order_id : str
        The caller-assigned order id (Kraken ``order_userref`` / ``cl_ord_id``),
        falling back to the venue order id when absent.
    venue_order_id : str
        Kraken's order id (``order_id``).
    exec_type : str
        The Kraken ``exec_type`` (``"new"``, ``"filled"``, ``"canceled"``, ...).
    status : str or None
        Kraken's ``order_status`` for the order, if the frame carried one.
    instrument : Instrument or None
        The instrument, when the frame named a ``symbol``.
    ts : int
        Event timestamp as **milliseconds since the Unix epoch (UTC)**.
    """

    client_order_id: str
    venue_order_id: str
    exec_type: str
    status: str | None
    instrument: Instrument | None
    ts: int


def _broker_token_provider(broker: KrakenBroker) -> Callable[[], Awaitable[str]]:
    """Build the default :data:`TokenProvider` from a :class:`KrakenBroker`.

    Returns an ``async`` callable that fetches a fresh WebSocket token via the
    broker's signed private REST (``GetWebSocketsToken``). This needs API
    credentials, so it is **only** wired up when the caller supplies a
    credentialed broker; the offline tests inject a fake provider instead.
    """

    async def _provider() -> str:
        # GetWebSocketsToken is a private (signed) endpoint; the broker raises a
        # clear BrokerError if it lacks credentials, before any I/O.
        result = await broker._private_post("GetWebSocketsToken", {})
        token = result.get("token")
        if not token:
            from trading_bot.domain.errors import BrokerError

            raise BrokerError(
                "Kraken GetWebSocketsToken: no token in response"
            )
        return str(token)

    return _provider


def _parse_iso_ms(ts_str: str) -> int:
    """Parse a Kraken ISO-8601 timestamp to milliseconds since the epoch (UTC).

    Kraken v2 stamps events as e.g. ``"2024-01-02T03:04:05.123456Z"``. Returns
    ``0`` for a missing/unparseable value (callers carry ``ts`` for record-keeping
    only; it never re-sorts fills).
    """
    if not ts_str:
        return 0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    return int(dt.timestamp() * 1000)


def _fee_of(entry: dict[str, Any]) -> Money:
    """Extract the quote-currency fee from a Kraken v2 execution entry.

    Kraken v2 reports fees either as a scalar ``fee`` / ``fee_usd_equiv`` or as a
    ``fees`` list of ``{"asset": ..., "qty": ...}`` objects (one per fee
    currency). We sum the ``qty`` of the ``fees`` list when present (the fee in
    the order's quote currency), else fall back to the scalar ``fee`` field.
    Returns an exact :class:`~decimal.Decimal`, defaulting to ``0``.
    """
    fees = entry.get("fees")
    if isinstance(fees, list) and fees:
        total = money("0")
        for f in fees:
            qty = f.get("qty") if isinstance(f, dict) else None
            if qty is not None:
                total += money(str(qty))
        return total
    scalar = entry.get("fee")
    if scalar is not None:
        return money(str(scalar))
    return money("0")


class KrakenPrivateWS(WebSocketBase):
    """Kraken v2 private WebSocket adapter — streams executions as domain events.

    Subscribes to Kraken's v2 ``executions`` channel (own trades + order-status
    changes) using a WebSocket auth token, and parses inbound frames into domain
    :class:`~trading_bot.domain.fill.Fill`s (executed trades) and
    :class:`OrderUpdate`s (order-status changes). Heartbeats, ``status`` frames
    and subscription acks are ignored.

    The auth token is obtained on every (re)connect via the injected
    ``token_provider`` and presented in the ``subscribe`` message, so the
    subscription self-heals after a reconnect. The ``connect`` / ``sleep`` seams
    are inherited from :class:`~trading_bot.transport.ws.WebSocketBase`, so the
    whole path is offline-testable.

    Parameters
    ----------
    token_provider : callable
        An ``async`` callable returning a fresh Kraken WebSocket token string.
        Use :meth:`from_broker` to build one from a credentialed
        :class:`~trading_bot.brokers.kraken.KrakenBroker`; tests inject a fake.
    snap_trades : bool, default True
        Request a snapshot of recent trades on subscribe (Kraken ``snap_trades``).
    snap_orders : bool, default True
        Request a snapshot of open orders on subscribe (Kraken ``snap_orders``).
    url : str, optional
        The WS endpoint. Defaults to Kraken's v2 auth endpoint.
    **ws_kwargs
        Forwarded to :class:`~trading_bot.transport.ws.WebSocketBase`
        (``max_backoff``, ``backoff_base``, ``connect``, ``sleep``).

    Notes
    -----
    The auth token is a credential and is **never logged**. The live private
    connection is deferred until a key is provided (parse path proven via mocks).
    """

    def __init__(
        self,
        token_provider: Callable[[], Awaitable[str]],
        *,
        snap_trades: bool = True,
        snap_orders: bool = True,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        url: str = _WS_AUTH_URL,
        **ws_kwargs: Any,
    ) -> None:
        super().__init__(url, **ws_kwargs)
        self._token_provider = token_provider
        self._snap_trades = snap_trades
        self._snap_orders = snap_orders
        self._on_connected = on_connected

    @classmethod
    def from_broker(
        cls,
        broker: KrakenBroker,
        *,
        snap_trades: bool = True,
        snap_orders: bool = True,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        url: str = _WS_AUTH_URL,
        **ws_kwargs: Any,
    ) -> KrakenPrivateWS:
        """Build a :class:`KrakenPrivateWS` whose token comes from ``broker``.

        Wires the default :data:`TokenProvider` to ``broker``'s signed private
        REST (``GetWebSocketsToken``). Requires the broker to carry credentials;
        the token fetch raises :class:`~trading_bot.domain.errors.BrokerError`
        without them, before any I/O. ``on_connected`` (if given) is awaited after
        each (re)connect's subscribe — the seam a live caller uses to reconcile.
        """
        return cls(
            _broker_token_provider(broker),
            snap_trades=snap_trades,
            snap_orders=snap_orders,
            on_connected=on_connected,
            url=url,
            **ws_kwargs,
        )

    async def on_connect(self, ws: Any) -> None:
        """Fetch the auth token and subscribe to ``executions`` (re-runs on reconnect).

        Obtains a fresh token via the injected ``token_provider`` and sends the
        v2 ``subscribe`` for the ``executions`` channel with the token in
        ``params``. Because the base re-runs this on every reconnect, the token
        is refreshed and the subscription re-established automatically — and, if an
        ``on_connected`` hook was supplied, it is awaited afterwards (the seam a
        live caller uses to **reconcile** the engine to the venue after every
        reconnect; a failure there is logged, never breaking the stream).
        """
        token = await self._token_provider()
        sub: dict[str, Any] = {
            "method": "subscribe",
            "params": {
                "channel": "executions",
                "token": token,
                "snap_trades": self._snap_trades,
                "snap_orders": self._snap_orders,
            },
        }
        await ws.send(json.dumps(sub))
        if self._on_connected is not None:
            try:
                await self._on_connected()
            except Exception:
                logger.exception("on_connected hook failed after WS (re)connect")

    async def events(self) -> AsyncIterator[Fill | OrderUpdate]:
        """Yield domain :class:`Fill`s and :class:`OrderUpdate`s from the feed.

        Consumes :meth:`~trading_bot.transport.ws.WebSocketBase.stream_raw`,
        parsing each ``executions`` frame (snapshot or update). A ``trade``
        execution becomes a :class:`~trading_bot.domain.fill.Fill`; any other
        ``exec_type`` becomes an :class:`OrderUpdate`. Heartbeats, ``status``
        frames and subscription acks are ignored. A rejected subscription raises
        :class:`~trading_bot.domain.errors.BrokerError`.

        Yields
        ------
        Fill or OrderUpdate
            One event per execution entry, in arrival order.
        """
        async for raw in self.stream_raw():
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            self._check_sub_ack(data)
            if data.get("channel") != "executions":
                # Ignore heartbeats, status frames, subscription acks, etc.
                continue
            for entry in data.get("data", []):
                event = self._parse_entry(entry)
                if event is not None:
                    yield event

    async def fills(self) -> AsyncIterator[Fill]:
        """Yield only the executed-trade :class:`Fill`s (drops order updates).

        A convenience filter over :meth:`events` for callers (e.g. the position
        tracker) that care only about executed trades, not order-status changes.

        Yields
        ------
        Fill
            One fill per ``trade`` execution, in arrival order.
        """
        async for event in self.events():
            if isinstance(event, Fill):
                yield event

    @staticmethod
    def _check_sub_ack(data: dict[str, Any]) -> None:
        """Raise on a rejected subscription instead of silently filtering it.

        Kraken v2 answers a failed subscribe with ``{"method": "subscribe",
        "success": false, "error": ...}``; dropping that frame would leave a
        "live" stream that never produces anything. The error string is a venue
        diagnostic (never the token), so it is safe to surface.
        """
        if data.get("method") == "subscribe" and data.get("success") is False:
            from trading_bot.domain.errors import BrokerError

            raise BrokerError(
                f"Kraken executions subscription rejected: "
                f"{data.get('error', 'unknown error')}"
            )

    def _parse_entry(self, entry: Any) -> Fill | OrderUpdate | None:
        """Parse one Kraken v2 ``executions`` data entry into a domain event.

        A ``trade`` execution (carrying ``last_qty`` / ``last_price``) becomes a
        :class:`~trading_bot.domain.fill.Fill`; any other ``exec_type`` becomes an
        :class:`OrderUpdate`. Returns ``None`` for an entry we cannot make sense
        of (e.g. a malformed/empty object), so the stream is resilient to
        unexpected shapes.
        """
        if not isinstance(entry, dict):
            return None
        exec_type = str(entry.get("exec_type", ""))
        if exec_type in _TRADE_EXEC_TYPES:
            return self._build_fill(entry)
        return self._build_order_update(entry, exec_type)

    @staticmethod
    def _instrument_of(entry: dict[str, Any]) -> Instrument | None:
        """Build an :class:`Instrument` from the entry's ``symbol``, if present."""
        symbol_str = entry.get("symbol")
        if not symbol_str:
            return None
        return Instrument(parse_kraken_pair(str(symbol_str)))

    def _build_fill(self, entry: dict[str, Any]) -> Fill | None:
        """Build a domain :class:`Fill` from a ``trade`` execution entry.

        Maps Kraken v2 ``executions`` trade fields to the domain fill:
        ``exec_id``/``trade_id`` → ``fill_id``, ``order_userref`` (else
        ``order_id``) → ``client_order_id``, ``last_qty`` → ``qty``,
        ``last_price`` → ``price``, the fee (see :func:`_fee_of`) → ``fee``, and
        the ISO ``timestamp`` → ``ts`` (ms). Returns ``None`` if the entry lacks
        the symbol/qty/price a fill requires.
        """
        instrument = self._instrument_of(entry)
        if instrument is None:
            return None
        last_qty = entry.get("last_qty")
        last_price = entry.get("last_price")
        if last_qty is None or last_price is None:
            return None

        venue_order_id = str(entry.get("order_id", ""))
        # Kraken v2 carries the caller's id as ``order_userref`` (numeric) or
        # ``cl_ord_id`` (string); fall back to the venue order id so the fill
        # always ties back to *some* order identity.
        userref = entry.get("cl_ord_id") or entry.get("order_userref")
        client_order_id = str(userref) if userref else venue_order_id

        fill_id = str(
            entry.get("exec_id")
            or entry.get("trade_id")
            or entry.get("order_id", "")
        )
        side = OrderSide(str(entry.get("side", "buy")))
        return Fill(
            fill_id=fill_id,
            client_order_id=client_order_id,
            instrument=instrument,
            side=side,
            qty=money(str(last_qty)),
            price=money(str(last_price)),
            fee=_fee_of(entry),
            ts=_parse_iso_ms(str(entry.get("timestamp", ""))),
        )

    def _build_order_update(
        self, entry: dict[str, Any], exec_type: str
    ) -> OrderUpdate:
        """Build an :class:`OrderUpdate` from a non-trade execution entry."""
        venue_order_id = str(entry.get("order_id", ""))
        userref = entry.get("cl_ord_id") or entry.get("order_userref")
        client_order_id = str(userref) if userref else venue_order_id
        status = entry.get("order_status")
        return OrderUpdate(
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            exec_type=exec_type,
            status=str(status) if status is not None else None,
            instrument=self._instrument_of(entry),
            ts=_parse_iso_ms(str(entry.get("timestamp", ""))),
        )
