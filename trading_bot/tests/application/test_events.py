"""Tests for the event taxonomy and the :class:`EventBus` fan-out.

These prove: each event type carries its payload with ``Decimal`` money intact;
:meth:`EventBus.emit` reaches every subscribed handler; two :meth:`add_queue`
consumers each receive every event (fan-out, not steal); :meth:`unsubscribe` and
:meth:`remove_queue` stop delivery; a handler that raises does not break the
others; and a full queue drops rather than blocking. Queue tests are async
(``asyncio_mode=auto``).
"""

from __future__ import annotations

from decimal import Decimal

from trading_bot.application import (
    EventBus,
    FillEvent,
    LogEvent,
    OrderEvent,
)
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _make_fill() -> Fill:
    """A realistic, fully-priced :class:`Fill` for assertions."""
    return Fill(
        fill_id="T1",
        client_order_id="cid-1",
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("0.5"),
        price=money("30000.10"),
        fee=money("12.34"),
        ts=1_700_000_000_000,
    )


def _make_order() -> Order:
    """A realistic limit :class:`Order` for assertions."""
    return Order(
        client_order_id="cid-1",
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("0.5"),
        type=OrderType.LIMIT,
        limit_price=money("30000.10"),
    )


# --- event payloads -------------------------------------------------------- #


def test_fill_event_carries_decimal_money() -> None:
    """A FillEvent keeps the fill's ``Decimal`` amounts intact."""
    fill = _make_fill()
    ev = FillEvent(fill=fill)
    assert ev.fill is fill
    assert ev.fill.price == Decimal("30000.10")
    assert ev.fill.fee == Decimal("12.34")
    assert isinstance(ev.fill.qty, Decimal)


def test_order_event_carries_the_aggregate() -> None:
    """An OrderEvent carries the live Order by reference (Decimal qty intact)."""
    order = _make_order()
    ev = OrderEvent(order=order)
    assert ev.order is order
    assert ev.order.qty == Decimal("0.5")
    assert isinstance(ev.order.limit_price, Decimal)


def test_log_event_fields() -> None:
    """A LogEvent carries message + level (default info)."""
    assert LogEvent(message="hello").level == "info"
    assert LogEvent(message="boom", level="error").level == "error"


# --- handler dispatch ------------------------------------------------------ #


def test_emit_reaches_every_subscriber() -> None:
    """emit calls every registered handler with the event."""
    bus = EventBus()
    seen_a: list = []
    seen_b: list = []
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)

    ev = LogEvent(message="started")
    bus.emit(ev)

    assert seen_a == [ev]
    assert seen_b == [ev]


def test_unsubscribe_stops_delivery() -> None:
    """An unsubscribed handler receives no further events."""
    bus = EventBus()
    seen: list = []
    bus.subscribe(seen.append)
    bus.emit(LogEvent(message="first"))
    bus.unsubscribe(seen.append)
    bus.emit(LogEvent(message="second"))

    assert [e.message for e in seen] == ["first"]


def test_handler_exception_does_not_break_others() -> None:
    """A raising handler is swallowed; later handlers still run."""
    bus = EventBus()
    seen: list = []

    def boom(_event) -> None:
        raise RuntimeError("bad subscriber")

    bus.subscribe(boom)
    bus.subscribe(seen.append)

    ev = LogEvent(message="resilient")
    bus.emit(ev)  # must not raise
    assert seen == [ev]


# --- async fan-out --------------------------------------------------------- #


async def test_two_queues_each_receive_the_event() -> None:
    """Two add_queue consumers each get the event (fan-out, not steal)."""
    bus = EventBus()
    q1 = bus.add_queue()
    q2 = bus.add_queue()

    ev = FillEvent(fill=_make_fill())
    bus.emit(ev)

    got1 = await q1.get()
    got2 = await q2.get()
    assert got1 is ev
    assert got2 is ev
    assert got1.fill.price == Decimal("30000.10")


async def test_remove_queue_stops_delivery() -> None:
    """A removed queue receives no further events."""
    bus = EventBus()
    q = bus.add_queue()
    bus.emit(LogEvent(message="first"))
    bus.remove_queue(q)
    bus.emit(LogEvent(message="second"))

    assert (await q.get()).message == "first"
    assert q.empty()


async def test_full_queue_drops_without_blocking() -> None:
    """A full queue drops new events instead of blocking emit."""
    bus = EventBus()
    q = bus.add_queue(maxsize=1)
    bus.emit(LogEvent(message="kept"))
    bus.emit(LogEvent(message="dropped"))  # must not block / raise

    assert (await q.get()).message == "kept"
    assert q.empty()


def test_subscribers_and_queues_all_receive() -> None:
    """A FillEvent reaches two subscribers and two queues — four receivers."""
    bus = EventBus()
    seen_a: list = []
    seen_b: list = []
    bus.subscribe(seen_a.append)
    bus.subscribe(seen_b.append)
    q1 = bus.add_queue()
    q2 = bus.add_queue()

    ev = FillEvent(fill=_make_fill())
    bus.emit(ev)

    assert seen_a == [ev]
    assert seen_b == [ev]
    assert q1.get_nowait() is ev
    assert q2.get_nowait() is ev
