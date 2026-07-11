"""Tests for :class:`OrderFillSync` — fills reach the tracked order (and store).

These prove the fill loop actually closes, against the **real** synchronous
event ordering (a real :class:`EventBus`, a real :class:`OrderRouter` over a
real :class:`PaperBroker` — no fakes on the hot path):

* a paper order that fills at placement ends ``FILLED`` on the *tracked* order
  (the ``FillEvent`` fires before the router tracks it, so this exercises the
  stash-and-drain ordering, not just the apply);
* an attached :class:`SqliteStore` row reaches the terminal state — the
  frozen-rows regression (``status='open'``, ``filled_qty=0`` forever);
* partial fills accumulate to the exact quantity-weighted average
  (``Decimal``-exact);
* consumption is idempotent by ``fill_id`` (a re-emitted event is a no-op);
* the re-emitted ``OrderEvent`` never recurses (bounded emit count);
* :meth:`OrderFillSync.replay` heals a restored, filled-but-``open`` store row
  to ``FILLED`` **before** reconcile runs, so the orphan rule no longer
  mis-cancels it — and skips untracked/terminal fills as well as the fill
  prefix a restored row's persisted ``filled_qty`` already covers (an accurate
  ``PARTIALLY_FILLED`` row is a no-op; a crash-lagged row gets exactly its
  missing tail — never an over-count).

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

# Built-in
import pathlib

# Local
from trading_bot.application.events import EventBus, FillEvent, OrderEvent
from trading_bot.application.order_fill_sync import OrderFillSync
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.reconcile import reconcile
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    money,
)
from trading_bot.storage.sqlite_store import SqliteStore

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _order(cid: str = "cid-1", qty: str = "1", limit: str = "30000") -> Order:
    """A realistic limit BUY order (fills at its limit price on the simulator)."""
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(limit),
    )


def _fill(
    fill_id: str,
    cid: str,
    qty: str,
    price: str,
    *,
    ts: int = 1_700_000_000_000,
) -> Fill:
    """A hand-built venue-confirmed fill for ``cid``."""
    return Fill(
        fill_id=fill_id,
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        price=money(price),
        fee=money("0"),
        ts=ts,
    )


def _wire(
    **broker_kwargs: object,
) -> tuple[EventBus, PaperBroker, OrderRouter, OrderFillSync]:
    """A real bus + bus-wired PaperBroker + router, with the sync subscribed.

    Mirrors ``build_engine``'s wiring order (broker on the bus, then the
    router, then the :class:`OrderFillSync`) so the tests exercise the exact
    synchronous event ordering the engine sees.
    """
    bus = EventBus()
    broker = PaperBroker(event_bus=bus, **broker_kwargs)  # type: ignore[arg-type]
    router = OrderRouter(broker, bus)
    sync = OrderFillSync(router, bus)
    return bus, broker, router, sync


def _count_order_events(bus: EventBus) -> list[OrderEvent]:
    """Subscribe a counting sink for ``OrderEvent``s; returns its accumulator."""
    seen: list[OrderEvent] = []
    bus.subscribe(lambda e: seen.append(e) if isinstance(e, OrderEvent) else None)
    return seen


# --- live path: paper fills reach the tracked order ------------------------- #


async def test_paper_immediate_fill_updates_order() -> None:
    """An immediately-filled paper order ends FILLED on the tracked instance.

    The PaperBroker emits the ``FillEvent`` during ``place_order`` — before the
    router tracks the order — so this passes only if the stash-and-drain
    ordering works end to end.
    """
    _bus, _broker, router, _sync = _wire()
    order = _order()

    result = await router.submit(order)

    assert result is order
    assert order.status is OrderStatus.FILLED
    assert order.filled_qty == money("1")
    assert order.avg_fill_price == money("30000")


async def test_store_row_reaches_terminal(tmp_path: pathlib.Path) -> None:
    """The store's order row follows the fill to FILLED — the frozen-rows bug."""
    bus, _broker, router, _sync = _wire()
    store = SqliteStore(tmp_path / "book.sqlite")
    store.attach(bus)

    await router.submit(_order())

    store.flush()
    row = store.get_order("cid-1")
    store.close()
    assert row is not None
    assert row.status is OrderStatus.FILLED
    assert row.filled_qty == money("1")
    assert row.avg_fill_price == money("30000")


async def test_partial_fills_accumulate() -> None:
    """Partial fills move OPEN -> PARTIALLY_FILLED -> FILLED, avg Decimal-exact."""
    bus, _broker, router, _sync = _wire(
        fill_model="partial",
        partial_chunks=2,
        partial_fill_ratio=money("0.5"),
    )
    order = _order(qty="2")

    # Placement fills half the qty (two stashed slices, drained on tracking).
    await router.submit(order)
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == money("1")
    assert order.avg_fill_price == money("30000")

    # The venue fills the remainder later, at a different price — the live
    # path: the order is already tracked and fillable, so the fill applies at
    # arrival (no stash).
    bus.emit(FillEvent(_fill("T-REST", "cid-1", "1", "30100")))
    assert order.status is OrderStatus.FILLED
    assert order.filled_qty == money("2")
    # Quantity-weighted average, exact: (1*30000 + 1*30100) / 2.
    assert order.avg_fill_price == money("30050")


async def test_idempotent_by_fill_id() -> None:
    """Re-emitting the same FillEvent changes nothing (dedup by fill_id)."""
    bus, broker, router, _sync = _wire()
    order = _order()
    await router.submit(order)
    assert order.status is OrderStatus.FILLED

    (fill,) = await broker.fills()
    events = _count_order_events(bus)
    bus.emit(FillEvent(fill))
    bus.emit(FillEvent(fill))

    assert order.filled_qty == money("1")
    assert order.avg_fill_price == money("30000")
    assert events == []  # already consumed: no re-apply, no re-emit


async def test_no_event_loop() -> None:
    """The re-emitted OrderEvent does not recurse — the emit count is bounded."""
    bus, _broker, router, _sync = _wire()
    events = _count_order_events(bus)

    await router.submit(_order())

    # Exactly two: the router's tracking event + the sync's single re-emit
    # after the drain. An unguarded re-emit would recurse unboundedly here.
    assert len(events) == 2
    assert all(e.order.client_order_id == "cid-1" for e in events)


# --- restart healing: replay ------------------------------------------------ #


async def test_replay_heals_restored_orders(tmp_path: pathlib.Path) -> None:
    """Restore + replay heals a filled-but-'open' row; reconcile keeps it.

    The store is seeded exactly like the frozen dashboard DBs: an order row
    stuck at ``open``/``0``/``NULL`` with its confirmed fill in the fills
    table. After ``restore`` + ``replay`` the tracked order is FILLED, the
    store row is updated, and the startup reconcile no longer orphan-cancels
    it (it is terminal before the orphan rule runs).
    """
    db = tmp_path / "unit.sqlite"

    # Seed: the pre-fix world — the fill was recorded, the order row froze.
    seed = SqliteStore(db)
    stuck = _order("ls1-btc-1")
    stuck.submit()
    stuck.open("VID-1")
    seed.upsert_order(stuck)  # row: status=open, filled_qty=0, avg=NULL
    seed.record_fill(_fill("F-1", "ls1-btc-1", "1", "30000"))
    seed.close()

    # The restart, through the real seams: store -> router -> sync -> reconcile.
    bus = EventBus()
    broker = PaperBroker(event_bus=bus)
    router = OrderRouter(broker, bus)
    sync = OrderFillSync(router, bus)
    tracker = PositionTracker(event_bus=bus)
    store = SqliteStore(db)
    store.attach(bus)

    router.restore(store.orders())
    healed = sync.replay(
        sorted(
            (r.fill for r in store.stored_fills() if r.mode == "paper"),
            key=lambda fill: fill.ts,
        )
    )
    assert healed == 1
    tracked = router.get("ls1-btc-1")
    assert tracked is not None
    assert tracked.status is OrderStatus.FILLED
    assert tracked.filled_qty == money("1")
    assert tracked.avg_fill_price == money("30000")

    # Healing touches orders, never positions.
    positions_before = dict(tracker.all_positions())

    result = await reconcile(broker, router, tracker, event_bus=bus)
    assert result.closed_orphans == 0  # NOT mis-cancelled: terminal before the rule
    assert router.get("ls1-btc-1") is tracked
    assert tracked.status is OrderStatus.FILLED
    assert dict(tracker.all_positions()) == positions_before

    # The healed state was persisted via the re-emitted OrderEvent.
    store.flush()
    row = store.get_order("ls1-btc-1")
    store.close()
    assert row is not None
    assert row.status is OrderStatus.FILLED
    assert row.filled_qty == money("1")
    assert row.avg_fill_price == money("30000")


async def test_replay_skips_persisted_fill_prefix(tmp_path: pathlib.Path) -> None:
    """Replay skips the fill prefix the persisted ``filled_qty`` already covers.

    A crash-lagged row: the order's first fill was applied and persisted
    (PARTIALLY_FILLED, ``filled_qty`` == that fill's qty) but the second fill
    only made it to the fills table. Replay must apply ONLY the second fill —
    the exact missing tail, never an over-count.
    """
    db = tmp_path / "unit.sqlite"

    # Seed: the row reflects fill 1; both fills are in the fills table.
    seed = SqliteStore(db)
    lagged = _order("ls1-btc-1", qty="2")
    lagged.submit()
    lagged.open("VID-1")
    lagged.apply_fill(money("1"), money("30000"))  # fill 1 reached the row
    seed.upsert_order(lagged)  # row: partially_filled, filled_qty=1, avg=30000
    seed.record_fill(_fill("F-1", "ls1-btc-1", "1", "30000", ts=1_700_000_000_000))
    seed.record_fill(_fill("F-2", "ls1-btc-1", "1", "30100", ts=1_700_000_000_001))
    seed.close()

    bus = EventBus()
    broker = PaperBroker(event_bus=bus)
    router = OrderRouter(broker, bus)
    sync = OrderFillSync(router, bus)
    store = SqliteStore(db)
    store.attach(bus)

    router.restore(store.orders())
    events = _count_order_events(bus)
    healed = sync.replay(
        sorted(
            (r.fill for r in store.stored_fills() if r.mode == "paper"),
            key=lambda fill: fill.ts,
        )
    )

    assert healed == 1
    assert len(events) == 1  # one re-emit for the healed order, not per fill
    tracked = router.get("ls1-btc-1")
    assert tracked is not None
    assert tracked.status is OrderStatus.FILLED
    assert tracked.filled_qty == money("2")  # 1 (persisted) + 1 (tail) — exact
    # The restored avg seeds the notional, so the weighted average stays exact:
    # (1*30000 + 1*30100) / 2 — fill 1 counted once, never twice.
    assert tracked.avg_fill_price == money("30050")
    # Both fills consumed: the covered prefix and the applied tail.
    assert {"F-1", "F-2"} <= sync._seen_fill_ids

    store.flush()
    row = store.get_order("ls1-btc-1")
    store.close()
    assert row is not None
    assert row.status is OrderStatus.FILLED
    assert row.filled_qty == money("2")


async def test_replay_noop_on_accurate_partial_row(tmp_path: pathlib.Path) -> None:
    """Replay re-applies nothing to a row whose ``filled_qty`` covers its fills."""
    db = tmp_path / "unit.sqlite"

    # Seed: an accurate post-fix partial row — its one fill is already applied.
    seed = SqliteStore(db)
    accurate = _order("ls1-btc-1", qty="2")
    accurate.submit()
    accurate.open("VID-1")
    accurate.apply_fill(money("1"), money("30000"))
    seed.upsert_order(accurate)  # row: partially_filled, filled_qty=1
    seed.record_fill(_fill("F-1", "ls1-btc-1", "1", "30000"))
    seed.close()

    bus = EventBus()
    broker = PaperBroker(event_bus=bus)
    router = OrderRouter(broker, bus)
    sync = OrderFillSync(router, bus)
    store = SqliteStore(db)
    store.attach(bus)

    router.restore(store.orders())
    events = _count_order_events(bus)
    healed = sync.replay(
        sorted(
            (r.fill for r in store.stored_fills() if r.mode == "paper"),
            key=lambda fill: fill.ts,
        )
    )

    assert healed == 0
    assert events == []  # nothing healed -> nothing re-emitted
    tracked = router.get("ls1-btc-1")
    assert tracked is not None
    assert tracked.status is OrderStatus.PARTIALLY_FILLED  # unchanged
    assert tracked.filled_qty == money("1")  # no over-count
    assert tracked.avg_fill_price == money("30000")
    assert "F-1" in sync._seen_fill_ids  # covered prefix is still consumed
    store.close()


async def test_replay_skips_untracked_and_terminal() -> None:
    """Replay records untracked/terminal fills as seen, heals nothing, returns 0."""
    bus, _broker, router, sync = _wire()

    # A tracked but already-terminal (cancelled) order: its fill must not apply.
    cancelled = _order("cid-cancelled")
    cancelled.submit()
    cancelled.open("VID-9")
    cancelled.cancel()
    router.restore([cancelled])

    events = _count_order_events(bus)
    healed = sync.replay(
        [
            _fill("F-UNKNOWN", "cid-nobody", "1", "30000"),
            _fill("F-TERMINAL", "cid-cancelled", "1", "30000"),
        ]
    )

    assert healed == 0
    assert events == []  # nothing healed -> nothing re-emitted
    assert cancelled.status is OrderStatus.CANCELLED
    assert cancelled.filled_qty == money("0")
    # Both fills are recorded as seen, so a later re-emitted event is a no-op.
    assert {"F-UNKNOWN", "F-TERMINAL"} <= sync._seen_fill_ids
