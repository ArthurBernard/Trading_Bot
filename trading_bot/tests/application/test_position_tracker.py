"""Tests for the :class:`~trading_bot.application.position_tracker.PositionTracker`.

These prove the tracker's contract: it folds broker-confirmed **fills** into a
live net :class:`~trading_bot.domain.position.Position` per instrument and is, by
construction, exactly :meth:`Position.from_fills` over the fills it has seen (in
arrival order). The cases cover a full life (buy, add, partial close, **flip**)
matched against ``from_fills`` to the exact ``Decimal``; ``EventBus`` subscription
(emitted ``FillEvent``\\ s drive the tracker, other events are ignored); multiple
instruments tracked independently; and an end-to-end run where an
:class:`~trading_bot.application.order_router.OrderRouter` submits to a
:class:`~trading_bot.brokers.paper.PaperBroker` whose emitted fills update the
tracker. Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

from decimal import Decimal

from trading_bot.application import (
    EventBus,
    FillEvent,
    LogEvent,
    OrderRouter,
    PositionTracker,
)
from trading_bot.brokers import PaperBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    Position,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _fill(
    *,
    fill_id: str,
    side: OrderSide,
    qty: str,
    price: str,
    fee: str = "0",
    ts: int = 1,
    instrument: Instrument = BTC_USD,
    cid: str = "cid-1",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        client_order_id=cid,
        instrument=instrument,
        side=side,
        qty=money(qty),
        price=money(price),
        fee=money(fee),
        ts=ts,
    )


def _assert_same_position(got: Position | None, fills: list[Fill]) -> None:
    """Assert ``got`` equals :meth:`Position.from_fills` over ``fills``, exactly."""
    expected = Position.from_fills(fills)
    assert got is not None
    assert got.instrument == expected.instrument
    assert got.net_qty == expected.net_qty
    assert got.avg_entry_price == expected.avg_entry_price
    assert got.realised_pnl == expected.realised_pnl
    assert got.fees_paid == expected.fees_paid


# --- empty state ----------------------------------------------------------- #


def test_unknown_instrument_is_none() -> None:
    """A never-seen instrument has no position."""
    tracker = PositionTracker()
    assert tracker.position(BTC_USD) is None
    assert tracker.all_positions() == {}


# --- apply matches Position.from_fills (buy, add, partial close, flip) ------ #


def test_apply_sequence_matches_from_fills_exactly() -> None:
    """buy -> add -> partial close -> **flip** folds to ``from_fills`` exactly."""
    fills = [
        # Open long 2 @ 30000, fee 30.
        _fill(fill_id="F1", side=OrderSide.BUY, qty="2", price="30000", fee="30"),
        # Add 1 @ 31500, fee 15.75 -> long 3, avg (30000*2 + 31500)/3 = 30500.
        _fill(fill_id="F2", side=OrderSide.BUY, qty="1", price="31500", fee="15.75"),
        # Partial close 1 @ 32000, fee 16 -> long 2 (realise on 1).
        _fill(fill_id="F3", side=OrderSide.SELL, qty="1", price="32000", fee="16"),
        # Flip: sell 5 @ 33000, fee 82.5 -> close 2 long, open 3 short @ 33000.
        _fill(fill_id="F4", side=OrderSide.SELL, qty="5", price="33000", fee="82.5"),
    ]
    tracker = PositionTracker()

    last: Position | None = None
    for f in fills:
        last = tracker.apply(f)

    # apply() returns the running position, and it matches the full fold.
    _assert_same_position(last, fills)
    _assert_same_position(tracker.position(BTC_USD), fills)
    # Sanity: the flip left us net short 3 @ 33000.
    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == Decimal("-3")
    assert pos.avg_entry_price == Decimal("33000")


def test_apply_incrementally_matches_from_fills_at_every_step() -> None:
    """At every prefix, the tracked position equals ``from_fills`` of that prefix."""
    fills = [
        _fill(fill_id="F1", side=OrderSide.BUY, qty="2", price="30000", fee="30"),
        _fill(fill_id="F2", side=OrderSide.BUY, qty="1", price="31500", fee="15.75"),
        _fill(fill_id="F3", side=OrderSide.SELL, qty="1", price="32000", fee="16"),
        _fill(fill_id="F4", side=OrderSide.SELL, qty="5", price="33000", fee="82.5"),
    ]
    tracker = PositionTracker()
    for i, f in enumerate(fills, start=1):
        tracker.apply(f)
        _assert_same_position(tracker.position(BTC_USD), fills[:i])


# --- EventBus subscription -------------------------------------------------- #


def test_subscribed_tracker_updates_on_fill_events() -> None:
    """Emitting ``FillEvent``\\ s on a subscribed bus updates the tracked position."""
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    fills = [
        _fill(fill_id="F1", side=OrderSide.BUY, qty="2", price="30000", fee="6"),
        _fill(fill_id="F2", side=OrderSide.SELL, qty="1", price="31000", fee="3.1"),
    ]

    for f in fills:
        bus.emit(FillEvent(f))

    _assert_same_position(tracker.position(BTC_USD), fills)


def test_subscribed_tracker_ignores_non_fill_events() -> None:
    """A subscribed tracker ignores non-:class:`FillEvent` events (no crash)."""
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)

    bus.emit(LogEvent(message="hello"))  # not a FillEvent

    assert tracker.all_positions() == {}


# --- multiple instruments --------------------------------------------------- #


def test_multiple_instruments_tracked_independently() -> None:
    """Each instrument folds its *own* fills; the buckets do not bleed."""
    btc = [
        _fill(fill_id="B1", side=OrderSide.BUY, qty="2", price="30000", fee="6",
              instrument=BTC_USD, cid="btc"),
        _fill(fill_id="B2", side=OrderSide.SELL, qty="1", price="31000", fee="3.1",
              instrument=BTC_USD, cid="btc"),
    ]
    eth = [
        _fill(fill_id="E1", side=OrderSide.BUY, qty="10", price="2000", fee="2",
              instrument=ETH_USD, cid="eth"),
    ]
    tracker = PositionTracker()
    # Interleave to prove arrival order per instrument is what is folded.
    tracker.apply(btc[0])
    tracker.apply(eth[0])
    tracker.apply(btc[1])

    _assert_same_position(tracker.position(BTC_USD), btc)
    _assert_same_position(tracker.position(ETH_USD), eth)
    assert set(tracker.all_positions()) == {BTC_USD, ETH_USD}


# --- verification on real data: OrderRouter -> PaperBroker -> tracker ------- #


async def test_end_to_end_router_paperbroker_fills_drive_tracker() -> None:
    """End-to-end: router submits to PaperBroker; emitted fills update the tracker.

    A realistic buy -> add -> sell sequence is routed through the *real*
    ``PaperBroker`` (which emits a ``FillEvent`` per simulated fill onto the bus
    the tracker is subscribed to). The tracked position must equal
    ``Position.from_fills`` over the broker's reported fills — net_qty, avg entry,
    realised PnL and fees, exact ``Decimal``.
    """
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        fill_model="immediate",
        starting_balances={"USD": money("1000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)

    def _limit(cid: str, side: OrderSide, qty: str, price: str) -> Order:
        return Order(
            client_order_id=cid,
            instrument=BTC_USD,
            side=side,
            qty=money(qty),
            type=OrderType.LIMIT,
            limit_price=money(price),
        )

    await router.submit(_limit("buy-1", OrderSide.BUY, "2", "30000"))
    await router.submit(_limit("buy-2", OrderSide.BUY, "1", "31500"))
    await router.submit(_limit("sell-1", OrderSide.SELL, "1", "32000"))

    # The broker is the source of truth for the fills; the tracker should equal a
    # fold over exactly those (in execution order).
    broker_fills = await broker.fills()
    assert len(broker_fills) == 3
    _assert_same_position(tracker.position(BTC_USD), broker_fills)

    # Hand-checked: long 2 @ 30000 + 1 @ 31500 -> long 3 @ 30500; sell 1 @ 32000
    # -> long 2 @ 30500.
    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == Decimal("2")
    assert pos.avg_entry_price == Decimal("30500")
