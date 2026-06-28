"""Hardening: the kill-switch cancels open orders and halts new ones mid-run.

The invariant under test — **the kill-switch gates every order**: once tripped it
cancels all open orders and refuses every subsequent submission, so a runaway
strategy (or a human pulling the cord) stops *now* — no order placed after the
halt, and the engine's local state stays consistent with the venue.

Fault story
-----------
This is the deliberate operator fault, not a broker malfunction: partway through a
run we call :meth:`~trading_bot.application.risk.RiskManager.kill` (the panic
entry point). The test asserts:

* every order the engine had open is **cancelled** on the venue (no live order
  left behind);
* every later :meth:`~trading_bot.application.order_router.OrderRouter.submit`
  raises :class:`~trading_bot.domain.errors.RiskLimitBreached` and **places
  nothing** — the broker's venue placement count does not move;
* the engine's local order state stays consistent (the cancelled orders are
  ``CANCELLED`` locally, the refused ones leave no tracked record).

A second variant proves the broker-direct halt (``kill(broker=...)``) and that a
``trip`` alone (no cancel) still refuses new orders.

All offline: :class:`~trading_bot.brokers.paper.PaperBroker` under a fault
wrapper. Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import pytest

from trading_bot.application import (
    EventBus,
    OrderRouter,
    PositionTracker,
    RiskManager,
)
from trading_bot.application.config import RiskConfig
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskLimitBreached,
    Symbol,
    money,
)
from trading_bot.tests.hardening._faulty_broker import FaultyBroker

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _limit(cid: str, qty: str = "4", price: str = "30000") -> Order:
    """A realistic limit BUY order keyed by ``cid``."""
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(price),
    )


def _engine_with_risk(
    broker: FaultyBroker,
    rm: RiskManager,
) -> tuple[EventBus, OrderRouter, PositionTracker]:
    """Wire a bus + risk-gated router + tracker over ``broker``."""
    bus = EventBus()
    router = OrderRouter(broker, bus, risk_manager=rm)
    tracker = PositionTracker()
    return bus, router, tracker


async def test_kill_via_router_cancels_open_orders_and_halts() -> None:
    """``kill(router=...)`` cancels every open order and refuses all new submits.

    Two orders are open mid-run; ``kill`` cancels both on the venue (driving the
    local orders to ``CANCELLED``) and trips the switch. Every later submit then
    raises ``RiskLimitBreached`` and places nothing — the venue placement count is
    frozen at the pre-kill total.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    rm = RiskManager(RiskConfig())  # no limits — isolate the kill-switch
    _, router, _ = _engine_with_risk(broker, rm)

    # Two live (half-filled, so still open) orders mid-run.
    inner.arm_partial(money("0.5"))
    o1 = await router.submit(_limit("ks-1", qty="4"))
    inner.arm_partial(money("0.5"))
    o2 = await router.submit(_limit("ks-2", qty="4"))
    assert o1.status is OrderStatus.OPEN
    assert o2.status is OrderStatus.OPEN
    assert len(await broker.open_orders()) == 2
    places_before_kill = broker.venue_place_count

    # --- pull the cord ------------------------------------------------------ #
    await rm.kill(router=router)

    assert rm.tripped is True
    # Every open order is cancelled on the venue and locally.
    assert await broker.open_orders() == []
    assert o1.status is OrderStatus.CANCELLED
    assert o2.status is OrderStatus.CANCELLED

    # Every subsequent submit is refused and places NOTHING.
    with pytest.raises(RiskLimitBreached):
        await router.submit(_limit("ks-3", qty="1"))
    with pytest.raises(RiskLimitBreached):
        await router.submit(_limit("ks-4", qty="1"))

    assert broker.venue_place_count == places_before_kill  # no new venue order
    assert await broker.open_orders() == []  # still nothing live
    # A refused order leaves no tracked record (it was never a submission).
    assert router.get("ks-3") is None
    assert router.get("ks-4") is None


async def test_kill_via_broker_cancels_open_orders_directly() -> None:
    """``kill(broker=...)`` cancels the venue's open orders without a router.

    The broker-direct halt path: with no router to source tracked orders from,
    ``kill`` reads ``broker.open_orders()`` and cancels each on the venue. After
    the kill no order is live on the venue and the switch is tripped.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    rm = RiskManager(RiskConfig())

    # Orders placed straight on the venue (e.g. recovered, no router yet).
    inner.arm_partial(money("0.5"))
    await broker.seed_order(_limit("kb-1", qty="4"))
    inner.arm_partial(money("0.5"))
    await broker.seed_order(_limit("kb-2", qty="4"))
    assert len(await broker.open_orders()) == 2

    await rm.kill(broker=broker)

    assert rm.tripped is True
    assert await broker.open_orders() == []  # all cancelled directly on the venue

    # The tripped switch refuses any order routed through a risk-gated router.
    _, router, _ = _engine_with_risk(broker, rm)
    with pytest.raises(RiskLimitBreached):
        await router.submit(_limit("kb-3", qty="1"))
    assert broker.venue_place_count == 0  # seeded orders bypass the wrapper count


async def test_trip_without_cancel_still_refuses_new_orders() -> None:
    """``trip`` alone halts new submissions even without cancelling live orders.

    ``trip`` is the soft halt (refuse new orders) distinct from ``kill`` (cancel +
    halt). After ``trip`` an existing open order is left live on the venue, but
    every new submit is refused and places nothing — proving the gate is the
    kill-switch flag, independent of the cancellation step.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    rm = RiskManager(RiskConfig())
    _, router, _ = _engine_with_risk(broker, rm)

    inner.arm_partial(money("0.5"))
    live = await router.submit(_limit("tr-1", qty="4"))
    assert live.status is OrderStatus.OPEN
    places_before_trip = broker.venue_place_count

    rm.trip("manual halt")

    # The live order is untouched by a bare trip...
    assert live.status is OrderStatus.OPEN
    assert len(await broker.open_orders()) == 1
    # ...but new orders are refused and never reach the venue.
    with pytest.raises(RiskLimitBreached):
        await router.submit(_limit("tr-2", qty="1"))
    assert broker.venue_place_count == places_before_trip
    assert router.get("tr-2") is None
