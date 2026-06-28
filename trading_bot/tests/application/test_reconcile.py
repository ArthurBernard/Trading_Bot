"""Tests for :func:`~trading_bot.application.reconcile.reconcile`.

These prove the *reconcile, don't assume* contract against the real
:class:`~trading_bot.brokers.paper.PaperBroker`: after a simulated disconnect the
engine's local state (the :class:`~trading_bot.application.order_router.
OrderRouter`'s tracked orders and the :class:`~trading_bot.application.
position_tracker.PositionTracker`'s positions) **converges** to the venue's truth,
with no order duplicated or lost.

The cases cover the two divergence directions:

* **(a) the venue knows more than the engine** — orders/fills placed *directly* on
  the broker (bypassing the router, as if submitted before a restart) are
  *ingested* into the router and *rebuild* the tracker's position;
* **(b) the engine knows more than the venue** — a non-terminal order the router
  tracks that the broker does not list is *closed and forgotten* per the orphan
  policy.

Plus: positions equal ``Position.from_fills`` over the broker's fills; a second
``reconcile`` is a no-op (``ReconResult`` all zeros / ``changed is False``); and
the core safety property — no duplicated or lost order — holds across the pass.
Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

from trading_bot.application import (
    EventBus,
    LogEvent,
    OrderRouter,
    PositionTracker,
    ReconResult,
    reconcile,
)
from trading_bot.brokers import PaperBroker
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


def _engine(broker: PaperBroker) -> tuple[EventBus, OrderRouter, PositionTracker]:
    """Wire a fresh bus + router + tracker over ``broker`` (router/tracker empty)."""
    bus = EventBus()
    router = OrderRouter(broker, bus)
    tracker = PositionTracker()
    return bus, router, tracker


def _assert_positions_match_broker_fills(
    tracker: PositionTracker, broker_fills: list
) -> None:
    """Assert every tracked position equals ``Position.from_fills`` per instrument."""
    by_instrument: dict[Instrument, list] = {}
    for f in broker_fills:
        by_instrument.setdefault(f.instrument, []).append(f)
    expected = {
        inst: Position.from_fills(fills) for inst, fills in by_instrument.items()
    }
    assert tracker.all_positions() == expected


# --- (a) venue knows an order/fill the engine never saw -------------------- #


async def test_reconcile_ingests_venue_open_order_and_rebuilds_position() -> None:
    """A venue order + fill the engine missed is ingested and folds the position.

    Simulates a restart: an order is placed *directly* on the broker (the router
    is empty, as in-memory state would be after a crash). The broker fully fills
    a buy and leaves a partially-filled order open. After ``reconcile`` the router
    tracks the open order and the tracker's position equals a fold over the
    broker's fills.
    """
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("1000000")},
    )
    bus, router, tracker = _engine(broker)

    # Placed directly on the broker (not via the router) -> the engine never saw
    # them. A full buy (closes, no open order) and a half-filled buy (stays open).
    await broker.place_order(_limit("missed-full", qty="2", price="30000"))
    broker.arm_partial(money("0.5"))
    await broker.place_order(_limit("missed-open", qty="4", price="30000"))

    assert router.get("missed-full") is None
    assert router.get("missed-open") is None
    assert tracker.all_positions() == {}

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # The still-open order is ingested; the fully-filled one left no open record.
    venue_open = await broker.open_orders()
    venue_open_cids = {o.client_order_id for o in venue_open}
    assert venue_open_cids == {"missed-open"}
    assert router.get("missed-open") is not None
    assert router.get("missed-open").status in (
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
    )
    assert result.ingested_orders == 1
    assert result.adopted_orders == 0
    assert result.closed_orphans == 0

    # Position rebuilt from the broker's fills (the truth).
    broker_fills = await broker.fills()
    assert result.fills_applied == len(broker_fills)
    _assert_positions_match_broker_fills(tracker, broker_fills)


# --- (b) engine tracks an order the venue does not know -------------------- #


async def test_reconcile_closes_orphan_order_not_on_venue() -> None:
    """A non-terminal tracked order the venue doesn't list is closed and forgotten.

    The router is made to track an ``OPEN`` order (ingested) that the broker has
    no record of — a phantom left after a disconnect. ``reconcile`` must close it
    (``CANCELLED``) and drop it from the map per the orphan policy.
    """
    broker = PaperBroker(starting_balances={"USD": money("1000000")})
    bus, router, tracker = _engine(broker)

    # Build a live OPEN order and ingest it into the router *without* the broker
    # ever knowing it (the broker's open_orders is empty) — an orphan.
    phantom = _limit("orphan-1", qty="1", price="30000")
    phantom.submit()
    phantom.open("VENUE-GONE")
    router.ingest(phantom)
    assert router.get("orphan-1") is phantom
    assert not phantom.is_terminal

    assert await broker.open_orders() == []

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # Closed (CANCELLED) and evicted from the tracked map.
    assert phantom.status is OrderStatus.CANCELLED
    assert router.get("orphan-1") is None
    assert result.closed_orphans == 1
    assert result.ingested_orders == 0
    assert result.adopted_orders == 0


async def test_reconcile_closes_new_orphan_nudged_through_submitted() -> None:
    """A ``NEW`` orphan (never opened) is nudged SUBMITTED then closed CANCELLED.

    Exercises the orphan-close path for an order that never reached the venue:
    ``cancel`` is illegal from ``NEW``, so the policy nudges it through
    ``SUBMITTED`` (reaching no venue) before cancelling.
    """
    broker = PaperBroker(starting_balances={"USD": money("1000000")})
    bus, router, tracker = _engine(broker)

    fresh = _limit("new-orphan", qty="1", price="30000")  # still NEW
    assert fresh.status is OrderStatus.NEW
    assert not fresh.is_terminal
    router.ingest(fresh)

    result = await reconcile(broker, router, tracker, event_bus=bus)

    assert fresh.status is OrderStatus.CANCELLED
    assert router.get("new-orphan") is None
    assert result.closed_orphans == 1


async def test_reconcile_leaves_terminal_local_order_alone() -> None:
    """A terminal tracked order is history, not an orphan — left untouched."""
    broker = PaperBroker(starting_balances={"USD": money("1000000")})
    bus, router, tracker = _engine(broker)

    # A FILLED order the venue (rightly) no longer lists as open.
    done = _limit("done-1", qty="1", price="30000")
    done.submit()
    done.open("VENUE-1")
    done.apply_fill(money("1"), money("30000"))
    assert done.status is OrderStatus.FILLED
    router.ingest(done)

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # Untouched: still tracked, still FILLED, not counted as an orphan.
    assert router.get("done-1") is done
    assert done.status is OrderStatus.FILLED
    assert result.closed_orphans == 0


# --- positions equal Position.from_fills over the broker's fills ----------- #


async def test_reconcile_positions_equal_from_fills_multi_instrument() -> None:
    """Positions are rebuilt from broker fills, exactly, across instruments."""
    broker = PaperBroker(
        prices={BTC_USD: money("30000"), ETH_USD: money("2000")},
        starting_balances={"USD": money("10000000")},
    )
    bus, router, tracker = _engine(broker)

    # A realistic mixed sequence placed straight on the broker.
    await broker.place_order(_limit("b1", OrderSide.BUY, "2", "30000", BTC_USD))
    await broker.place_order(_limit("b2", OrderSide.BUY, "1", "31500", BTC_USD))
    await broker.place_order(_limit("b3", OrderSide.SELL, "1", "32000", BTC_USD))
    await broker.place_order(_limit("e1", OrderSide.BUY, "10", "2000", ETH_USD))

    await reconcile(broker, router, tracker, event_bus=bus)

    broker_fills = await broker.fills()
    _assert_positions_match_broker_fills(tracker, broker_fills)
    # Sanity on the BTC leg: long 2 @ 30000 + 1 @ 31500 -> 3 @ 30500; sell 1 -> 2.
    btc = tracker.position(BTC_USD)
    assert btc is not None
    assert btc.net_qty == money("2")
    assert btc.avg_entry_price == money("30500")


# --- idempotency: a second reconcile is a no-op ---------------------------- #


async def test_second_reconcile_is_a_noop() -> None:
    """Running ``reconcile`` twice with no venue change: the second is all zeros."""
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("1000000")},
    )
    bus, router, tracker = _engine(broker)

    await broker.place_order(_limit("full", qty="2", price="30000"))
    broker.arm_partial(money("0.5"))
    await broker.place_order(_limit("open", qty="4", price="30000"))

    first = await reconcile(broker, router, tracker, event_bus=bus)
    assert first.changed  # the first pass ingested the open order

    snapshot_orders = router.tracked_orders()
    snapshot_positions = tracker.all_positions()

    second = await reconcile(broker, router, tracker, event_bus=bus)

    # Idempotent: nothing new ingested, no new orphans -> not "changed".
    assert second.ingested_orders == 0
    assert second.adopted_orders == 1  # the still-open order is re-confirmed
    assert second.closed_orphans == 0
    assert second.changed is False
    # Local state identical to after the first pass.
    assert router.tracked_orders() == snapshot_orders
    assert tracker.all_positions() == snapshot_positions


def test_recon_result_changed_flag() -> None:
    """``ReconResult.changed`` reflects only the mutating counts."""
    assert ReconResult(0, 0, 0, 0, 0).changed is False
    assert ReconResult(0, 3, 0, 5, 2).changed is False  # adopt/fills/pos only
    assert ReconResult(1, 0, 0, 0, 0).changed is True  # an ingest
    assert ReconResult(0, 0, 1, 0, 0).changed is True  # an orphan close


# --- a LogEvent summarises the pass ---------------------------------------- #


async def test_reconcile_emits_log_event_summary() -> None:
    """A single ``LogEvent`` summarising the pass is emitted on the bus."""
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("1000000")},
    )
    bus, router, tracker = _engine(broker)
    seen: list[object] = []
    bus.subscribe(seen.append)

    await broker.place_order(_limit("x", qty="2", price="30000"))
    await reconcile(broker, router, tracker, event_bus=bus)

    logs = [e for e in seen if isinstance(e, LogEvent)]
    assert len(logs) == 1
    assert "reconcile:" in logs[0].message


async def test_reconcile_without_bus_emits_nothing_and_still_converges() -> None:
    """``event_bus=None`` is valid: no event, state still converges."""
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("1000000")},
    )
    _bus, router, tracker = _engine(broker)
    broker.arm_partial(money("0.5"))
    await broker.place_order(_limit("open", qty="4", price="30000"))

    result = await reconcile(broker, router, tracker)  # no event_bus

    assert result.ingested_orders == 1
    assert router.get("open") is not None


# --- core safety property: no order duplicated or lost --------------------- #


async def test_no_order_duplicated_or_lost_across_reconcile() -> None:
    """The router's tracked-order set converges to the venue open set, no dup/loss.

    A realistic divergence: one order the engine submitted *and* the venue still
    holds open (must remain, exactly once), one order only the venue holds (must
    be ingested), and one phantom only the engine holds (must be dropped). After
    reconcile the tracked **open** orders equal the venue's open set — every venue
    order present exactly once (no loss, no duplicate) and no phantom left behind.
    """
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("1000000")},
    )
    bus, router, tracker = _engine(broker)

    # 1. Engine-and-venue: routed through the engine AND still open on the venue.
    broker.arm_partial(money("0.5"))
    shared = await router.submit(_limit("shared", qty="4", price="30000"))
    assert shared.status is OrderStatus.OPEN
    assert router.get("shared") is not None

    # 2. Venue-only: placed straight on the broker, engine never saw it.
    broker.arm_partial(money("0.5"))
    await broker.place_order(_limit("venue-only", qty="4", price="30000"))

    # 3. Engine-only phantom: tracked OPEN order the venue has no record of.
    phantom = _limit("phantom", qty="1", price="30000")
    phantom.submit()
    phantom.open("GONE")
    router.ingest(phantom)

    result = await reconcile(broker, router, tracker, event_bus=bus)

    # The venue's open set is the truth we converge to.
    venue_open = {o.client_order_id for o in await broker.open_orders()}
    assert venue_open == {"shared", "venue-only"}

    # Every venue-open order is tracked exactly once (no loss, no duplicate); the
    # tracked *non-terminal* set equals the venue open set; the phantom is gone.
    tracked = router.tracked_orders()
    non_terminal = {cid for cid, o in tracked.items() if not o.is_terminal}
    assert non_terminal == venue_open
    assert router.get("phantom") is None
    assert phantom.status is OrderStatus.CANCELLED

    # Counts: 'shared' was already tracked (adopted), 'venue-only' ingested,
    # 'phantom' closed.
    assert result.adopted_orders == 1
    assert result.ingested_orders == 1
    assert result.closed_orphans == 1

    # And no order object is duplicated in the map (ids are unique by construction
    # of a dict, but assert the engine's own 'shared' object was kept, not replaced
    # by the venue's reconstructed snapshot).
    assert tracked["shared"] is shared
