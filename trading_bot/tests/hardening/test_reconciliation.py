"""Hardening: reconciliation converges after a simulated disconnect.

The invariant under test — **reconcile, don't assume**: after the engine has been
disconnected from the venue (orders placed and fills landed while it was offline,
plus a local order the venue never knew about), one
:func:`~trading_bot.application.reconcile.reconcile` pass converges the engine's
two local views to the venue's truth, with **no order duplicated and none lost**,
and a second pass is a no-op.

Fault story
-----------
The :class:`~trading_bot.tests.hardening._faulty_broker.FaultyBroker` is used to
*disconnect*: orders are placed directly on the wrapped venue (via
``broker.disconnect(...)``) so the venue holds open orders + a fill history the
local router/tracker never saw. We additionally track a local order the venue has
no record of — an *orphan*. ``reconcile`` must:

* **ingest** the venue's open orders the router missed (adopt, never re-submit);
* **rebuild** positions to exactly ``Position.from_fills`` over the venue's fills;
* **close-and-forget** the orphan (the venue is the truth; a phantom must not stay
  "live");
* leave the router tracking **exactly** the venue's open set — no duplicate, no
  loss;
* be **idempotent** — a second pass reports all zeros / ``changed is False``.

All offline: :class:`~trading_bot.brokers.paper.PaperBroker` under a fault wrapper,
no venue, no key, no network. Async tests run un-decorated
(``asyncio_mode = "auto"``).
"""

from __future__ import annotations

from trading_bot.application import (
    EventBus,
    OrderRouter,
    PositionTracker,
    reconcile,
)
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Symbol,
    money,
)
from trading_bot.tests.hardening._faulty_broker import FaultyBroker

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _limit(
    cid: str,
    side: OrderSide = OrderSide.BUY,
    qty: str = "1",
    price: str = "30000",
    instrument: Instrument = BTC_USD,
) -> Order:
    """A realistic limit order for seeding the broker or the router."""
    return Order(
        client_order_id=cid,
        instrument=instrument,
        side=side,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(price),
    )


def _engine(
    broker: FaultyBroker,
) -> tuple[EventBus, OrderRouter, PositionTracker]:
    """Wire a fresh bus + router + tracker over ``broker`` (both empty)."""
    bus = EventBus()
    router = OrderRouter(broker, bus)
    tracker = PositionTracker()
    return bus, router, tracker


def _positions_from_broker_fills(
    broker_fills: list,
) -> dict[Instrument, Position]:
    """``Position.from_fills`` per instrument over ``broker_fills`` — the truth."""
    by_instrument: dict[Instrument, list] = {}
    for f in broker_fills:
        by_instrument.setdefault(f.instrument, []).append(f)
    return {
        inst: Position.from_fills(fills)
        for inst, fills in by_instrument.items()
    }


async def test_reconcile_converges_after_disconnect() -> None:
    """A disconnect leaves the venue ahead of the engine; reconcile converges it.

    Scenario: while the engine was offline the venue executed a full BUY (closed,
    no open order but a fill), left a half-filled BUY *open*, and executed a
    standalone SELL on a second instrument. The router and tracker are empty. One
    ``reconcile`` pass must ingest the open order, rebuild positions to exactly
    the fold over the venue's fills, duplicate or lose nothing, and a second pass
    must be a no-op.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000"), ETH_USD: money("2000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    bus, router, tracker = _engine(broker)

    # --- disconnect window: the venue works while the engine is offline ----- #
    # A full BUY closes (fill, no open order).
    await broker.disconnect(_limit("disc-full", qty="2", price="30000"))
    # A half-filled BUY stays open on the venue.
    inner.arm_partial(money("0.5"))
    await broker.disconnect(_limit("disc-open", qty="4", price="30000"))
    # A standalone SELL on a second instrument (fully fills, closes).
    await broker.disconnect(
        _limit("disc-eth", side=OrderSide.SELL, qty="3", price="2000",
               instrument=ETH_USD)
    )

    # The engine saw none of it.
    assert router.tracked_orders() == {}
    assert tracker.all_positions() == {}

    venue_open_before = await broker.open_orders()
    venue_open_cids = {o.client_order_id for o in venue_open_before}
    assert venue_open_cids == {"disc-open"}  # only the half-filled order is open

    # --- one reconcile pass ------------------------------------------------- #
    result = await reconcile(broker, router, tracker, event_bus=bus)
    assert result.changed is True
    assert result.ingested_orders == 1  # disc-open adopted
    assert result.adopted_orders == 0
    assert result.closed_orphans == 0

    # No order duplicated or lost: the router tracks EXACTLY the venue's open set.
    tracked_nonterminal = {
        cid
        for cid, o in router.tracked_orders().items()
        if not o.is_terminal
    }
    assert tracked_nonterminal == venue_open_cids
    # The ingested order carries the venue's view (OPEN, venue id, partial fill).
    ingested = router.get("disc-open")
    assert ingested is not None
    assert ingested.status is OrderStatus.PARTIALLY_FILLED
    assert ingested.venue_order_id is not None

    # Positions are EXACTLY the fold over the venue's confirmed fills.
    broker_fills = await broker.fills()
    assert tracker.all_positions() == _positions_from_broker_fills(broker_fills)
    # Both instruments traded, so both have a folded position.
    assert set(tracker.all_positions()) == {BTC_USD, ETH_USD}

    # --- idempotency: a second pass changes nothing ------------------------- #
    again = await reconcile(broker, router, tracker, event_bus=bus)
    assert again.changed is False
    assert again.ingested_orders == 0
    assert again.closed_orphans == 0
    # Positions unchanged by the second fold of the same fills.
    assert tracker.all_positions() == _positions_from_broker_fills(broker_fills)


async def test_reconcile_closes_local_orphan_the_venue_never_knew() -> None:
    """A local non-terminal order the venue has no record of is closed-and-forgotten.

    The other divergence direction: the engine believes an order is live but the
    venue lists it neither as open nor in any fill (e.g. it was rejected after the
    engine recorded it, or cancelled out-of-band). The orphan policy is to drive
    it terminal (``CANCELLED``) and drop it from the tracked map, so the engine
    stops acting on a phantom — never leaving a lost order live.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    bus, router, tracker = _engine(broker)

    # Track a live order locally that the venue does NOT know about. Built and
    # driven OPEN by hand (ingest records it as-is) to model "engine thinks it is
    # live, venue has no record".
    orphan = _limit("orphan-1", qty="1", price="30000")
    orphan.submit()
    orphan.open("PHANTOM-VID")
    router.ingest(orphan)
    assert router.get("orphan-1") is not None
    assert not orphan.is_terminal

    # The venue has nothing: no open orders, no fills.
    assert await broker.open_orders() == []
    assert await broker.fills() == []

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # The orphan is closed and forgotten — not left live, not duplicated.
    assert result.closed_orphans == 1
    assert result.changed is True
    assert router.get("orphan-1") is None
    assert orphan.status is OrderStatus.CANCELLED

    # A second pass is clean (the orphan is gone, nothing else diverges).
    again = await reconcile(broker, router, tracker, event_bus=bus)
    assert again.changed is False
    assert again.closed_orphans == 0


async def test_reconcile_adopts_already_tracked_order_without_duplicating() -> None:
    """An order the engine already tracks AND the venue lists open is adopted, not duped.

    Guards the "no duplicate" half of the invariant from the adopt side: the
    engine submitted an order normally (so the router tracks it and the venue
    holds it open), then reconcile runs. The order must be *adopted* — counted,
    left as the engine's own object — and never ingested a second time, so the
    tracked map still has exactly one entry for that id.
    """
    inner = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("10000000")},
    )
    broker = FaultyBroker(inner)
    bus, router, tracker = _engine(broker)

    # Submit through the engine, leaving the order half-filled and open.
    inner.arm_partial(money("0.5"))
    submitted = await router.submit(_limit("kept-open", qty="4", price="30000"))
    assert submitted.status is OrderStatus.OPEN
    own_object = router.get("kept-open")

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # Adopted (already tracked), not ingested or duplicated.
    assert result.adopted_orders == 1
    assert result.ingested_orders == 0
    assert result.changed is False  # adopt alone is not a mutation
    # The router still holds the engine's own object, exactly once.
    assert router.get("kept-open") is own_object
    assert list(router.tracked_orders()) == ["kept-open"]
