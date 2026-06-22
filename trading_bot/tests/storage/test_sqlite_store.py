"""Tests for the :class:`~trading_bot.storage.sqlite_store.SqliteStore`.

These prove the store's persistence contract end to end:

* orders round-trip through ``upsert_order``/``get_order`` to an **equal** domain
  :class:`~trading_bot.domain.order.Order` (status, ``filled_qty``, exact
  ``Decimal`` qty/prices), and re-upserting the same ``client_order_id`` updates
  the single row to its latest state (no duplicate);
* fills round-trip through ``record_fill``/``fills`` to equal immutable
  :class:`~trading_bot.domain.fill.Fill`\\ s, re-recording a ``fill_id`` is a
  no-op (append-only), and ``fills(since_ms=...)`` filters by ``ts``;
* ``set_state``/``get_state`` round-trips and a missing key is ``None``;
* **no float**: a price like ``money("0.1")`` round-trips exactly and the raw
  stored column is TEXT (``str``);
* ``attach(bus)`` wires the store to an :class:`~trading_bot.application.events.
  EventBus` so emitted ``OrderEvent``/``FillEvent``\\ s populate it;
* a realistic engine sequence driven through
  :class:`~trading_bot.application.order_router.OrderRouter` ->
  :class:`~trading_bot.brokers.paper.PaperBroker` (store attached to the bus),
  then **reopened from the file**, persists the orders/fills, and
  :meth:`Position.from_fills` over the stored fills matches the live tracker's
  position.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import sqlite3

from trading_bot.application import (
    EventBus,
    FillEvent,
    LogEvent,
    OrderEvent,
    OrderRouter,
    PositionTracker,
)
from trading_bot.brokers import PaperBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Symbol,
    money,
)
from trading_bot.storage import SqliteStore

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _store(tmp_path) -> SqliteStore:
    return SqliteStore(tmp_path / "engine.db")


def _order(
    *,
    cid: str = "cid-1",
    side: OrderSide = OrderSide.BUY,
    qty: str = "2",
    otype: OrderType = OrderType.LIMIT,
    limit_price: str | None = "30000",
    stop_price: str | None = None,
    instrument: Instrument = BTC_USD,
) -> Order:
    return Order(
        client_order_id=cid,
        instrument=instrument,
        side=side,
        qty=money(qty),
        type=otype,
        limit_price=None if limit_price is None else money(limit_price),
        stop_price=None if stop_price is None else money(stop_price),
    )


def _fill(
    *,
    fill_id: str,
    cid: str = "cid-1",
    side: OrderSide = OrderSide.BUY,
    qty: str = "1",
    price: str = "30000",
    fee: str = "3",
    ts: int = 1_700_000_000_000,
    instrument: Instrument = BTC_USD,
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


def _assert_orders_equal(got: Order | None, expected: Order) -> None:
    assert got is not None
    assert got.client_order_id == expected.client_order_id
    assert got.instrument == expected.instrument
    assert got.side == expected.side
    assert got.type == expected.type
    assert got.qty == expected.qty
    assert got.limit_price == expected.limit_price
    assert got.stop_price == expected.stop_price
    assert got.status == expected.status
    assert got.filled_qty == expected.filled_qty
    assert got.avg_fill_price == expected.avg_fill_price
    assert got.venue_order_id == expected.venue_order_id


def _assert_fills_equal(got: Fill, expected: Fill) -> None:
    assert got == expected  # frozen dataclass: structural equality
    # And money fields are exact Decimal (== on Decimal is value-exact).
    assert got.qty == expected.qty
    assert got.price == expected.price
    assert got.fee == expected.fee


# --- orders: round-trip + upsert ------------------------------------------- #


def test_upsert_order_round_trip(tmp_path) -> None:
    """A NEW order round-trips to an equal domain Order."""
    store = _store(tmp_path)
    order = _order()
    store.upsert_order(order)
    _assert_orders_equal(store.get_order("cid-1"), order)


def test_get_order_missing_is_none(tmp_path) -> None:
    """An unknown client_order_id yields None."""
    store = _store(tmp_path)
    assert store.get_order("nope") is None


def test_upsert_order_persists_lifecycle_state(tmp_path) -> None:
    """status / filled_qty / avg_fill_price / venue_order_id round-trip exactly."""
    store = _store(tmp_path)
    order = _order()
    order.submit()
    order.open("VID-9")
    order.apply_fill(money("1"), money("30100"))
    store.upsert_order(order)

    got = store.get_order("cid-1")
    assert got is not None
    assert got.status is OrderStatus.PARTIALLY_FILLED
    assert got.filled_qty == money("1")
    assert got.avg_fill_price == money("30100")
    assert got.venue_order_id == "VID-9"


def test_re_upsert_updates_single_row(tmp_path) -> None:
    """Re-upserting the same client_order_id updates one row to the latest state."""
    store = _store(tmp_path)
    order = _order()
    store.upsert_order(order)  # NEW

    order.submit()
    order.open("VID-1")
    order.apply_fill(money("2"), money("30000"))  # fully filled
    store.upsert_order(order)  # FILLED

    assert len(store.orders()) == 1
    got = store.get_order("cid-1")
    assert got is not None
    assert got.status is OrderStatus.FILLED
    assert got.filled_qty == money("2")
    assert got.venue_order_id == "VID-1"


def test_orders_lists_all(tmp_path) -> None:
    """orders() returns every distinct order."""
    store = _store(tmp_path)
    store.upsert_order(_order(cid="a"))
    store.upsert_order(_order(cid="b", instrument=ETH_USD))
    cids = {o.client_order_id for o in store.orders()}
    assert cids == {"a", "b"}


def test_order_stop_loss_round_trip(tmp_path) -> None:
    """A STOP_LOSS order (stop_price set, limit None) round-trips exactly."""
    store = _store(tmp_path)
    order = _order(
        cid="sl-1",
        side=OrderSide.SELL,
        otype=OrderType.STOP_LOSS,
        limit_price=None,
        stop_price="25000",
    )
    store.upsert_order(order)
    got = store.get_order("sl-1")
    assert got is not None
    assert got.type is OrderType.STOP_LOSS
    assert got.stop_price == money("25000")
    assert got.limit_price is None


# --- fills: append-only + filter ------------------------------------------- #


def test_record_fill_round_trip(tmp_path) -> None:
    """A fill round-trips to an equal immutable Fill (Decimal exact)."""
    store = _store(tmp_path)
    fill = _fill(fill_id="T1")
    store.record_fill(fill)
    got = store.fills()
    assert len(got) == 1
    _assert_fills_equal(got[0], fill)


def test_record_fill_is_append_only(tmp_path) -> None:
    """Re-recording the same fill_id is a no-op (no duplicate)."""
    store = _store(tmp_path)
    fill = _fill(fill_id="T1")
    store.record_fill(fill)
    store.record_fill(fill)  # duplicate fill_id
    # A different-content fill under the same id is also ignored (append-only).
    store.record_fill(_fill(fill_id="T1", qty="99", price="1"))
    got = store.fills()
    assert len(got) == 1
    _assert_fills_equal(got[0], fill)  # original wins, untouched


def test_fills_filter_since_ms(tmp_path) -> None:
    """fills(since_ms=...) returns only fills at/after the bound (inclusive)."""
    store = _store(tmp_path)
    store.record_fill(_fill(fill_id="T1", ts=100))
    store.record_fill(_fill(fill_id="T2", ts=200))
    store.record_fill(_fill(fill_id="T3", ts=300))

    assert [f.fill_id for f in store.fills()] == ["T1", "T2", "T3"]
    assert [f.fill_id for f in store.fills(since_ms=200)] == ["T2", "T3"]
    assert [f.fill_id for f in store.fills(since_ms=301)] == []


# --- state ----------------------------------------------------------------- #


def test_state_round_trip(tmp_path) -> None:
    """set_state/get_state round-trips; a missing key is None; updates overwrite."""
    store = _store(tmp_path)
    assert store.get_state("last_reconcile") is None
    store.set_state("last_reconcile", "1700000000000")
    assert store.get_state("last_reconcile") == "1700000000000"
    store.set_state("last_reconcile", "1800000000000")
    assert store.get_state("last_reconcile") == "1800000000000"


# --- money is TEXT, never float -------------------------------------------- #


def test_money_is_text_and_decimal_exact(tmp_path) -> None:
    """A price like 0.1 round-trips exactly and the raw stored column is TEXT."""
    db = tmp_path / "engine.db"
    store = SqliteStore(db)
    order = _order(cid="dust", qty="0.1", limit_price="0.1")
    store.upsert_order(order)
    store.record_fill(_fill(fill_id="Tx", cid="dust", qty="0.1", price="0.1", fee="0"))

    got_order = store.get_order("dust")
    assert got_order is not None
    assert got_order.qty == money("0.1")
    assert got_order.limit_price == money("0.1")
    got_fill = store.fills()[0]
    assert got_fill.qty == money("0.1")
    assert got_fill.price == money("0.1")

    # Assert the raw stored columns are TEXT (str), not REAL/float.
    raw = sqlite3.connect(str(db))
    try:
        o_qty, o_lim = raw.execute(
            "SELECT qty, limit_price FROM orders WHERE client_order_id='dust'"
        ).fetchone()
        f_qty, f_price = raw.execute(
            "SELECT qty, price FROM fills WHERE fill_id='Tx'"
        ).fetchone()
    finally:
        raw.close()
    assert isinstance(o_qty, str) and o_qty == "0.1"
    assert isinstance(o_lim, str) and o_lim == "0.1"
    assert isinstance(f_qty, str) and f_qty == "0.1"
    assert isinstance(f_price, str) and f_price == "0.1"


# --- bus integration ------------------------------------------------------- #


def test_attach_populates_from_events(tmp_path) -> None:
    """Emitting OrderEvent/FillEvent on an attached bus fills the store."""
    store = _store(tmp_path)
    bus = EventBus()
    store.attach(bus)

    order = _order()
    bus.emit(OrderEvent(order))
    bus.emit(FillEvent(_fill(fill_id="T1")))
    bus.emit(LogEvent(message="ignored"))  # non-order/fill: ignored

    assert store.get_order("cid-1") is not None
    assert len(store.fills()) == 1
    assert len(store.orders()) == 1


def test_attach_tracks_latest_order_state(tmp_path) -> None:
    """A second OrderEvent for the same id updates the persisted row."""
    store = _store(tmp_path)
    bus = EventBus()
    store.attach(bus)

    order = _order()
    bus.emit(OrderEvent(order))
    order.submit()
    order.open("VID-1")
    bus.emit(OrderEvent(order))

    assert len(store.orders()) == 1
    got = store.get_order("cid-1")
    assert got is not None
    assert got.status is OrderStatus.OPEN
    assert got.venue_order_id == "VID-1"


# --- verification on real data: reopen the file ---------------------------- #


async def test_engine_sequence_survives_reopen(tmp_path) -> None:
    """Drive OrderRouter->PaperBroker (store attached), reopen the file, verify.

    Submits a realistic mixed sequence (a full buy, then a partial buy that
    leaves a live remainder, then a sell) through the router to the paper
    broker, with the store attached to the bus so it records orders + fills as
    they flow. Then a **fresh** ``SqliteStore`` on the same file must report the
    same orders/fills, and ``Position.from_fills`` over the persisted fills must
    equal the live :class:`PositionTracker`'s position — proving the store is a
    faithful, reopenable reconciliation source.
    """
    db = tmp_path / "engine.db"
    bus = EventBus()
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        fee_bps=money("10"),
        event_bus=bus,  # broker emits FillEvent onto the bus
    )
    router = OrderRouter(broker, bus)
    tracker = PositionTracker(event_bus=bus)
    store = SqliteStore(db)
    store.attach(bus)

    # 1) full buy of 2 BTC @ 30000 (immediate fill model).
    await router.submit(
        _order(cid="o1", side=OrderSide.BUY, qty="2", limit_price="30000")
    )
    # 2) partial buy: arm a 0.5 ratio so only half the 2 BTC fills, remainder open.
    broker.arm_partial(money("0.5"))
    await router.submit(
        _order(cid="o2", side=OrderSide.BUY, qty="2", limit_price="31000")
    )
    # 3) sell 1 BTC @ 32000.
    await router.submit(
        _order(cid="o3", side=OrderSide.SELL, qty="1", limit_price="32000")
    )

    # Persist the post-submit order snapshots (the router emitted them as NEW->
    # OPEN; re-upsert the final tracked state for good measure / a real engine
    # would on each transition).
    for o in router.tracked_orders().values():
        store.upsert_order(o)

    live_pos = tracker.position(BTC_USD)
    live_fill_ids = [f.fill_id for f in await broker.fills()]
    store.close()

    # --- reopen the file in a fresh store and compare --- #
    reopened = SqliteStore(db)

    # Orders survived: same ids, with the venue id the router obtained. The
    # router owns only the write path (NEW->SUBMITTED->OPEN); it never ingests
    # fills onto the Order (that is the tracker's job), so a router-tracked
    # order is OPEN with venue_order_id set — exactly what we persisted.
    persisted_orders = {o.client_order_id: o for o in reopened.orders()}
    assert set(persisted_orders) == {"o1", "o2", "o3"}
    for cid in ("o1", "o2", "o3"):
        live = router.get(cid)
        assert live is not None
        _assert_orders_equal(persisted_orders[cid], live)
    assert persisted_orders["o2"].venue_order_id is not None

    # Fills survived in execution order, byte-for-byte.
    persisted_fills = reopened.fills()
    assert [f.fill_id for f in persisted_fills] == live_fill_ids
    assert all(isinstance(f.price, type(money("1"))) for f in persisted_fills)

    # Position rebuilt from the *persisted* fills equals the live tracker's.
    rebuilt = Position.from_fills(persisted_fills)
    assert live_pos is not None
    assert rebuilt.net_qty == live_pos.net_qty
    assert rebuilt.avg_entry_price == live_pos.avg_entry_price
    assert rebuilt.realised_pnl == live_pos.realised_pnl
    assert rebuilt.fees_paid == live_pos.fees_paid
    reopened.close()
