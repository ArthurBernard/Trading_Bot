"""Tests for :mod:`trading_bot.brokers.kraken_ws` (Kraken v2 private WS).

Offline-only: a fake ``connect`` seam (mirroring
:mod:`trading_bot.tests.transport.test_ws`) feeds a canned sequence of realistic
Kraken v2 private frames (a ``status`` frame and a ``heartbeat`` to ignore, an
``executions`` snapshot, then an ``executions`` update carrying a trade), and an
injected fake ``token_provider`` returns a dummy token — so the whole
auth-token + parse path is exercised with **no API key and no real connection**.
The live private connection is deferred until a key is provided.

The canned frame shapes follow Kraken's v2 private ``executions`` docs.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from trading_bot.brokers import KrakenPrivateWS
from trading_bot.brokers.kraken_ws import OrderUpdate
from trading_bot.domain.errors import BrokerError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Symbol
from trading_bot.domain.order import OrderSide

# --- fakes (mirror transport/test_ws.py) ----------------------------------- #


class RecordingSleep:
    """Async ``asyncio.sleep`` stand-in that records every requested delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


class FakeWS:
    """A fake live connection: async-iterates *frames* and records sends.

    An optional *on_exhaust* callback fires once the scripted frames are drained
    — used to :meth:`~KrakenPrivateWS.stop` the stream from tests that expect no
    events (so the reconnect loop terminates instead of spinning).
    """

    def __init__(
        self, frames: list[str], on_exhaust: Any = None
    ) -> None:
        self._frames = frames
        self._on_exhaust = on_exhaust
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    def __aiter__(self) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for frame in self._frames:
                yield frame
            if self._on_exhaust is not None:
                self._on_exhaust()

        return _gen()


class _Conn:
    """Async context manager standing in for ``websockets.connect(url)``."""

    def __init__(self, result: BaseException | FakeWS) -> None:
        self._result = result

    async def __aenter__(self) -> FakeWS:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeConnector:
    """Injectable ``connect`` seam yielding a scripted sequence of attempts."""

    def __init__(self, results: list[BaseException | FakeWS]) -> None:
        self._results = list(results)
        self.urls: list[str] = []

    def __call__(self, url: str) -> _Conn:
        self.urls.append(url)
        if not self._results:
            raise AssertionError("FakeConnector called more times than scripted")
        return _Conn(self._results.pop(0))


class FakeTokenProvider:
    """Async token-provider seam: returns a dummy token, counts awaits."""

    def __init__(self, token: str = "dummy-ws-token") -> None:
        self.token = token
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return self.token


# --- canned Kraken v2 private frames (shape from the v2 private docs) ------- #

# A status frame (connection metadata) — ignored.
_STATUS_FRAME = json.dumps(
    {
        "channel": "status",
        "type": "update",
        "data": [{"system": "online", "api_version": "v2", "version": "2.0.0"}],
    }
)

# A heartbeat frame — ignored.
_HEARTBEAT_FRAME = json.dumps({"channel": "heartbeat"})

# The subscription ack (success) — ignored.
_SUB_ACK_FRAME = json.dumps(
    {
        "method": "subscribe",
        "success": True,
        "result": {"channel": "executions", "snapshot": True},
        "time_in": "2024-01-02T03:04:05.000000Z",
        "time_out": "2024-01-02T03:04:05.000100Z",
    }
)

# An executions snapshot: one open order (exec_type "new", no trade) — surfaces
# as an OrderUpdate, not a Fill.
_SNAPSHOT_FRAME = json.dumps(
    {
        "channel": "executions",
        "type": "snapshot",
        "data": [
            {
                "exec_type": "new",
                "order_id": "OABCDE-12345-FGHIJK",
                "order_userref": 1001,
                "symbol": "BTC/USD",
                "side": "buy",
                "order_status": "new",
                "order_qty": 1.5,
                "timestamp": "2024-01-02T03:04:00.000000Z",
            }
        ],
    }
)

# An executions update carrying a trade execution (exec_type "trade") — the Fill.
_TRADE_UPDATE_FRAME = json.dumps(
    {
        "channel": "executions",
        "type": "update",
        "data": [
            {
                "exec_type": "trade",
                "exec_id": "TXXXXX-EXEC1-AAAAAA",
                "trade_id": 42,
                "order_id": "OABCDE-12345-FGHIJK",
                "order_userref": 1001,
                "symbol": "BTC/USD",
                "side": "buy",
                "last_qty": 0.5,
                "last_price": 42000.10,
                "cost": 21000.05,
                "fees": [{"asset": "USD", "qty": 33.60}],
                "liquidity_ind": "t",
                "order_status": "partially_filled",
                "timestamp": "2024-01-02T03:04:05.123000Z",
            }
        ],
    }
)

# An order-status update (exec_type "filled", no trade payload) — OrderUpdate.
_ORDER_STATUS_FRAME = json.dumps(
    {
        "channel": "executions",
        "type": "update",
        "data": [
            {
                "exec_type": "filled",
                "order_id": "OABCDE-12345-FGHIJK",
                "order_userref": 1001,
                "symbol": "BTC/USD",
                "side": "buy",
                "order_status": "filled",
                "timestamp": "2024-01-02T03:04:06.000000Z",
            }
        ],
    }
)


def _make_ws(
    frames: list[str],
    *,
    token: FakeTokenProvider | None = None,
    extra_results: list[BaseException | FakeWS] | None = None,
) -> tuple[KrakenPrivateWS, FakeConnector, FakeTokenProvider]:
    """Build a KrakenPrivateWS wired to a fake connect yielding *frames*."""
    provider = token or FakeTokenProvider()
    ws = FakeWS(frames)
    results: list[BaseException | FakeWS] = [ws, *(extra_results or [])]
    connector = FakeConnector(results)
    private = KrakenPrivateWS(
        provider,
        connect=connector,
        sleep=RecordingSleep(),
    )
    return private, connector, provider


async def _drain(
    private: KrakenPrivateWS, *, limit: int
) -> list[Fill | OrderUpdate]:
    """Collect up to *limit* events, then stop the stream."""
    out: list[Fill | OrderUpdate] = []
    async for event in private.events():
        out.append(event)
        if len(out) >= limit:
            private.stop()
            break
    return out


# --- tests ----------------------------------------------------------------- #


async def test_canned_sequence_parses_snapshot_and_trade() -> None:
    # status + heartbeat (ignored) → snapshot (OrderUpdate) → trade (Fill).
    private, _, _ = _make_ws(
        [
            _STATUS_FRAME,
            _HEARTBEAT_FRAME,
            _SUB_ACK_FRAME,
            _SNAPSHOT_FRAME,
            _TRADE_UPDATE_FRAME,
        ]
    )
    events = await _drain(private, limit=2)

    # Two domain events emitted: the snapshot order-update, then the trade fill.
    assert len(events) == 2
    update, fill = events

    assert isinstance(update, OrderUpdate)
    assert update.exec_type == "new"
    assert update.venue_order_id == "OABCDE-12345-FGHIJK"
    assert update.client_order_id == "1001"
    assert update.status == "new"
    assert update.instrument is not None
    assert update.instrument.symbol == Symbol("BTC", "USD")

    assert isinstance(fill, Fill)
    # Exact Decimal fields — no float round-trip.
    assert fill.qty == Decimal("0.5")
    assert fill.price == Decimal("42000.10")
    assert fill.fee == Decimal("33.60")
    assert fill.side is OrderSide.BUY
    assert fill.instrument.symbol == Symbol("BTC", "USD")
    # Identity / linkage: exec id → fill_id, userref → client_order_id,
    # order_id → carried via client_order_id fallback (here userref wins).
    assert fill.fill_id == "TXXXXX-EXEC1-AAAAAA"
    assert fill.client_order_id == "1001"
    # ISO timestamp → ms since epoch (2024-01-02T03:04:05.123Z).
    assert fill.ts == 1_704_164_645_123


async def test_fills_filter_drops_order_updates() -> None:
    private, _, _ = _make_ws([_SNAPSHOT_FRAME, _TRADE_UPDATE_FRAME])
    fills: list[Fill] = []
    async for f in private.fills():
        fills.append(f)
        private.stop()
        break
    # The snapshot's OrderUpdate is dropped; the first yielded item is the Fill.
    assert len(fills) == 1
    assert isinstance(fills[0], Fill)
    assert fills[0].fill_id == "TXXXXX-EXEC1-AAAAAA"


async def test_order_status_update_frame_yields_order_update() -> None:
    private, _, _ = _make_ws([_ORDER_STATUS_FRAME])
    events = await _drain(private, limit=1)

    assert len(events) == 1
    (update,) = events
    assert isinstance(update, OrderUpdate)
    assert update.exec_type == "filled"
    assert update.status == "filled"
    assert update.venue_order_id == "OABCDE-12345-FGHIJK"
    assert update.client_order_id == "1001"
    assert update.instrument is not None
    assert update.instrument.symbol == Symbol("BTC", "USD")


async def test_on_connect_fetches_token_and_subscribe_includes_it() -> None:
    provider = FakeTokenProvider(token="secret-token-xyz")
    ws = FakeWS([_TRADE_UPDATE_FRAME])
    connector = FakeConnector([ws])
    private = KrakenPrivateWS(
        provider, connect=connector, sleep=RecordingSleep()
    )

    await _drain(private, limit=1)

    # The token provider was awaited exactly once for the single connect.
    assert provider.calls == 1
    # Exactly one subscribe frame was sent, carrying the token + executions chan.
    assert len(ws.sent) == 1
    sub = json.loads(ws.sent[0])
    assert sub["method"] == "subscribe"
    assert sub["params"]["channel"] == "executions"
    assert sub["params"]["token"] == "secret-token-xyz"
    assert sub["params"]["snap_trades"] is True
    assert sub["params"]["snap_orders"] is True
    # The auth endpoint URL was used.
    assert connector.urls == ["wss://ws-auth.kraken.com/v2"]


async def test_reconnect_refetches_token_and_resubscribes() -> None:
    # First connect drops after one frame; reconnect yields the trade. on_connect
    # must re-run: token re-fetched, subscribe re-sent (self-heal).
    provider = FakeTokenProvider(token="tok")
    ws1 = FakeWS([_SNAPSHOT_FRAME])
    ws2 = FakeWS([_TRADE_UPDATE_FRAME])
    connector = FakeConnector([ws1, ConnectionError("drop"), ws2])
    private = KrakenPrivateWS(
        provider, connect=connector, sleep=RecordingSleep()
    )

    events = await _drain(private, limit=2)

    # One OrderUpdate (first connect) + one Fill (after reconnect).
    assert isinstance(events[0], OrderUpdate)
    assert isinstance(events[1], Fill)
    # Token fetched once per successful connect (two connects → two fetches).
    assert provider.calls == 2
    # Both sockets received a subscribe carrying the token (re-subscribe).
    assert len(ws1.sent) == 1
    assert len(ws2.sent) == 1
    assert json.loads(ws1.sent[0])["params"]["token"] == "tok"
    assert json.loads(ws2.sent[0])["params"]["token"] == "tok"


async def test_heartbeat_and_status_frames_emit_no_fill() -> None:
    # No executions frame at all → no events. The fake stops the stream once its
    # frames drain so the reconnect loop terminates.
    provider = FakeTokenProvider()
    private = KrakenPrivateWS(
        provider, connect=FakeConnector([]), sleep=RecordingSleep()
    )
    ws = FakeWS(
        [_STATUS_FRAME, _HEARTBEAT_FRAME, _SUB_ACK_FRAME],
        on_exhaust=private.stop,
    )
    private._connect = FakeConnector([ws])  # type: ignore[attr-defined]

    events: list[Fill | OrderUpdate] = []
    async for event in private.events():
        events.append(event)
    assert events == []


async def test_rejected_subscription_raises_broker_error() -> None:
    reject = json.dumps(
        {
            "method": "subscribe",
            "success": False,
            "error": "EAuth:Invalid token",
        }
    )
    private, _, _ = _make_ws([reject])
    with pytest.raises(BrokerError, match="subscription rejected"):
        async for _ in private.events():
            pass


async def test_fee_falls_back_to_scalar_fee_field() -> None:
    # A trade frame whose fee is a scalar ``fee`` (not a ``fees`` list).
    frame = json.dumps(
        {
            "channel": "executions",
            "type": "update",
            "data": [
                {
                    "exec_type": "trade",
                    "exec_id": "EXEC-SCALAR",
                    "order_id": "O-1",
                    "symbol": "ETH/USD",
                    "side": "sell",
                    "last_qty": 2,
                    "last_price": 2500,
                    "fee": 5.0,
                    "timestamp": "2024-01-02T03:04:05.000000Z",
                }
            ],
        }
    )
    private, _, _ = _make_ws([frame])
    events = await _drain(private, limit=1)
    (fill,) = events
    assert isinstance(fill, Fill)
    assert fill.fee == Decimal("5.0")
    assert fill.side is OrderSide.SELL
    assert fill.instrument.symbol == Symbol("ETH", "USD")
    # No userref/cl_ord_id → client_order_id falls back to the venue order id.
    assert fill.client_order_id == "O-1"


async def test_from_broker_builds_token_provider_from_signed_rest() -> None:
    # The default provider routes through the broker's signed private REST. We
    # stub _private_post so no real I/O / key is needed, and assert it is the
    # GetWebSocketsToken endpoint that is called and its token forwarded.
    calls: list[str] = []

    class FakeBroker:
        async def _private_post(
            self, endpoint: str, data: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append(endpoint)
            return {"token": "from-rest-token", "expires": 900}

    ws = FakeWS([_TRADE_UPDATE_FRAME])
    connector = FakeConnector([ws])
    private = KrakenPrivateWS.from_broker(
        FakeBroker(),  # type: ignore[arg-type]
        connect=connector,
        sleep=RecordingSleep(),
    )

    await _drain(private, limit=1)

    assert calls == ["GetWebSocketsToken"]
    assert json.loads(ws.sent[0])["params"]["token"] == "from-rest-token"


# --- on_connected reconcile hook ------------------------------------------- #


async def test_on_connected_hook_fires_after_subscribe_on_each_connect() -> None:
    """The ``on_connected`` hook is awaited on connect AND on every reconnect.

    The first socket yields one trade then ends (forcing a reconnect); the second
    yields another. The hook (a live caller's reconcile trigger) must fire once per
    (re)connect — twice here.
    """
    calls: list[int] = []

    async def hook() -> None:
        calls.append(1)

    private = KrakenPrivateWS(
        FakeTokenProvider(),
        connect=FakeConnector([FakeWS([_TRADE_UPDATE_FRAME]), FakeWS([_TRADE_UPDATE_FRAME])]),
        sleep=RecordingSleep(),
        on_connected=hook,
    )

    events = await _drain(private, limit=2)

    assert len(events) == 2  # one trade per connect
    assert len(calls) == 2  # hook fired on connect AND reconnect


async def test_on_connected_failure_does_not_break_the_stream() -> None:
    """A raising ``on_connected`` hook is logged, not propagated — the stream lives."""

    async def bad_hook() -> None:
        raise RuntimeError("reconcile boom")

    private = KrakenPrivateWS(
        FakeTokenProvider(),
        connect=FakeConnector([FakeWS([_TRADE_UPDATE_FRAME])]),
        sleep=RecordingSleep(),
        on_connected=bad_hook,
    )

    events = await _drain(private, limit=1)

    assert len(events) == 1  # the trade still streamed despite the hook raising
