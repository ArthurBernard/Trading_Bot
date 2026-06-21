"""Tests for the :class:`OrderRouter` — the engine's idempotent write path.

These prove the router's safety contract:

* **idempotent submit** — submitting the same ``client_order_id`` twice calls the
  broker's ``place_order`` exactly *once* and returns the same tracked order
  (asserted against a counting spy broker and against the real ``PaperBroker``);
* **concurrent** idempotent submit — ``asyncio.gather`` of two submits of one id
  still produces exactly one broker order;
* submit drives ``NEW -> SUBMITTED -> OPEN`` (venue id set) and emits exactly one
  ``OrderEvent``;
* a broker that raises :class:`BrokerError` on ``place_order`` drives the order to
  ``REJECTED``, emits a reject event, surfaces the error, and *records* the
  attempt so a re-submit of the same id does **not** re-call the broker;
* ``cancel`` cancels on the broker and transitions the order, emitting an event.

The final "real data" test routes a realistic sequence through the actual
``PaperBroker`` and asserts the lifecycle end to end. Async tests run un-decorated
(``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio

import pytest

from trading_bot.application import EventBus, OrderEvent, OrderRouter
from trading_bot.brokers.base import Broker, Capability
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    BrokerError,
    Instrument,
    MissingOrder,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _order(cid: str = "cid-1", qty: str = "1") -> Order:
    """A realistic limit BUY order for assertions."""
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )


# --- test doubles ---------------------------------------------------------- #


class _SpyBroker(Broker):
    """A counting broker: records every ``place_order``/``cancel_order`` call.

    Serves only ``PLACE_ORDER`` and ``CANCEL`` (the two capabilities the router
    requires); other port methods are not exercised by the router and raise.
    """

    name = "spy"

    def __init__(self, *, fail: bool = False, slow: bool = False) -> None:
        self.place_calls = 0
        self.cancel_calls = 0
        self._fail = fail
        self._slow = slow
        self._ids = 0

    def capabilities(self) -> set[Capability]:
        return {Capability.PLACE_ORDER, Capability.CANCEL}

    async def place_order(self, order: Order) -> str:
        # Yield first so two concurrent submits genuinely interleave: if the
        # router's guard were broken, both would have incremented the counter.
        if self._slow:
            await asyncio.sleep(0)
        self.place_calls += 1
        if self._fail:
            raise BrokerError("venue rejected the order")
        self._ids += 1
        return f"SPY-{self._ids}"

    async def cancel_order(self, venue_order_id: str) -> None:
        self.cancel_calls += 1

    # --- unused port surface (router never calls these) ---
    async def open_orders(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def balances(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def fills(self, since_ms=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def ticker(self, instrument):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def _capture(bus: EventBus) -> list[object]:
    """Subscribe a sink to ``bus`` and return the list it accumulates into."""
    seen: list[object] = []
    bus.subscribe(seen.append)
    return seen


# --- capability gate ------------------------------------------------------- #


class _NoPlaceBroker(_SpyBroker):
    name = "noplace"

    def capabilities(self) -> set[Capability]:
        return {Capability.CANCEL}


def test_construction_requires_place_and_cancel() -> None:
    """A broker missing PLACE_ORDER is rejected up front (NoCapability)."""
    from trading_bot.domain import NoCapability

    with pytest.raises(NoCapability):
        OrderRouter(_NoPlaceBroker(), EventBus())


# --- submit lifecycle ------------------------------------------------------ #


async def test_submit_drives_new_submitted_open_and_emits_one_event() -> None:
    """Submit drives NEW->SUBMITTED->OPEN, sets the venue id, emits one event."""
    broker = _SpyBroker()
    bus = EventBus()
    seen = _capture(bus)
    router = OrderRouter(broker, bus)

    order = _order()
    assert order.status is OrderStatus.NEW

    returned = await router.submit(order)

    assert returned is order
    assert order.status is OrderStatus.OPEN
    assert order.venue_order_id == "SPY-1"
    assert broker.place_calls == 1
    # Exactly one OrderEvent, carrying the live order.
    assert len(seen) == 1
    assert isinstance(seen[0], OrderEvent)
    assert seen[0].order is order


# --- idempotency: sequential ----------------------------------------------- #


async def test_duplicate_submit_calls_broker_once_and_returns_same_order() -> None:
    """A second submit of the same id returns the tracked order, no 2nd call."""
    broker = _SpyBroker()
    bus = EventBus()
    seen = _capture(bus)
    router = OrderRouter(broker, bus)

    first = await router.submit(_order(cid="dup"))
    # A *different* Order object, same client_order_id.
    second = await router.submit(_order(cid="dup"))

    assert broker.place_calls == 1, "broker must be called exactly once"
    assert second is first, "duplicate submit returns the original tracked order"
    # Only the first submission emitted an event.
    assert len(seen) == 1


# --- idempotency: concurrent ----------------------------------------------- #


async def test_concurrent_submit_of_same_id_produces_one_broker_order() -> None:
    """Two gathered submits of one id still yield exactly one broker order."""
    broker = _SpyBroker(slow=True)  # awaits inside place_order to force interleave
    bus = EventBus()
    seen = _capture(bus)
    router = OrderRouter(broker, bus)

    a, b = await asyncio.gather(
        router.submit(_order(cid="race")),
        router.submit(_order(cid="race")),
    )

    assert broker.place_calls == 1, "concurrency guard must collapse to one call"
    assert a is b, "both submits resolve to the same tracked order"
    assert a.status is OrderStatus.OPEN
    assert len(seen) == 1


# --- rejection ------------------------------------------------------------- #


async def test_broker_error_rejects_order_emits_event_and_surfaces() -> None:
    """A BrokerError on place_order -> REJECTED + reject event + raised error."""
    broker = _SpyBroker(fail=True)
    bus = EventBus()
    seen = _capture(bus)
    router = OrderRouter(broker, bus)

    order = _order(cid="bad")
    with pytest.raises(BrokerError):
        await router.submit(order)

    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "venue rejected the order"
    # A reject OrderEvent was emitted carrying the rejected order.
    assert len(seen) == 1
    assert isinstance(seen[0], OrderEvent)
    assert seen[0].order.status is OrderStatus.REJECTED


async def test_resubmit_after_rejection_does_not_recall_broker() -> None:
    """A retry of a rejected id is deduped: no second broker call."""
    broker = _SpyBroker(fail=True)
    bus = EventBus()
    router = OrderRouter(broker, bus)

    with pytest.raises(BrokerError):
        await router.submit(_order(cid="bad"))
    assert broker.place_calls == 1

    # Re-submit the same id: returns the tracked (rejected) order, no 2nd call.
    again = await router.submit(_order(cid="bad"))
    assert broker.place_calls == 1, "rejected id must not double-submit"
    assert again.status is OrderStatus.REJECTED


# --- cancel ---------------------------------------------------------------- #


async def test_cancel_cancels_on_broker_and_transitions_order() -> None:
    """Cancel calls the broker, drives CANCELLED, and emits an event."""
    # A partially-filling paper broker leaves the order live so it is cancellable.
    # The broker is port-pure (never touches the caller's Order), so after submit
    # the router has driven the order only to OPEN — cancel runs from OPEN.
    broker = PaperBroker(fill_model="partial", partial_fill_ratio=money("0.5"))
    bus = EventBus()
    seen = _capture(bus)
    router = OrderRouter(broker, bus)

    order = await router.submit(_order(cid="to-cancel"))
    assert order.status is OrderStatus.OPEN
    # The venue's own view shows the partial fill (reconstructed by the broker).
    assert len(await broker.open_orders()) == 1

    cancelled = await router.cancel("to-cancel")

    assert cancelled is order
    assert order.status is OrderStatus.CANCELLED
    assert len(await broker.open_orders()) == 0
    # submit event + cancel event.
    assert len(seen) == 2
    assert seen[-1].order.status is OrderStatus.CANCELLED


async def test_cancel_unknown_id_raises_missing_order() -> None:
    """Cancelling an id the router never tracked raises MissingOrder."""
    router = OrderRouter(PaperBroker(), EventBus())
    with pytest.raises(MissingOrder):
        await router.cancel("never-seen")


# --- verification on real data (PaperBroker) ------------------------------- #


async def test_real_paperbroker_duplicate_id_yields_one_paper_order() -> None:
    """End-to-end: a duplicate client-order-id produces exactly one paper order.

    Routes a realistic sequence through the *actual* ``PaperBroker``: submit an
    order (one paper order + one fill), then submit the SAME client-order-id again
    and assert the broker produced exactly one order/fill and the events match the
    lifecycle.
    """
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("100000")},
    )
    bus = EventBus()
    seen = _capture(bus)
    router = OrderRouter(broker, bus)

    first = await router.submit(_order(cid="real-1"))
    # Port-pure broker: the router drives the order to OPEN and sets the venue id;
    # the broker's *own* fill of it lives in broker.fills(), not on this order.
    assert first.status is OrderStatus.OPEN
    assert first.venue_order_id == "PAPER-1"

    # Same client-order-id again -> dedup, no second paper order/fill.
    second = await router.submit(_order(cid="real-1"))
    assert second is first

    fills = await broker.fills()
    assert len(fills) == 1, "duplicate id must not create a second paper fill"
    assert fills[0].client_order_id == "real-1"

    # Exactly one OrderEvent for the single accepted submission, OPEN.
    order_events = [e for e in seen if isinstance(e, OrderEvent)]
    assert len(order_events) == 1
    assert order_events[0].order.status is OrderStatus.OPEN


async def test_real_paperbroker_concurrent_duplicate_one_order() -> None:
    """End-to-end concurrency: gathered duplicate submits -> one paper order."""
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("100000")},
    )
    router = OrderRouter(broker, EventBus())

    a, b = await asyncio.gather(
        router.submit(_order(cid="real-race")),
        router.submit(_order(cid="real-race")),
    )

    assert a is b
    assert len(await broker.fills()) == 1, "one paper fill despite two submits"
