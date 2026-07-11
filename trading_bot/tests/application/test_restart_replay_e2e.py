"""E2E regression: two engine lifetimes over one store — restart/replay.

The scenario every existing test missed: every other test in this suite (see
``test_order_fill_sync.py``, ``test_reconcile.py``) exercises a *single* engine
lifetime, or hand-seeds a store row to *simulate* a restart. This module drives
the real thing — :func:`~trading_bot.application.service_factory.build_engine`
called **twice** over the **same** SQLite path, with a genuine restore -> replay
-> reconcile sequence in between (mirroring
:meth:`~trading_bot.application.supervisor.StrategySupervisor._start_locked`
exactly) — so the three restart fixes are proven **together**, not in isolation:

* leaf 01 — lifetime-unique :class:`~trading_bot.brokers.paper.PaperBroker` ids
  (``PAPER-{token}-{n}``): a second engine's broker must never mint a venue or
  fill id the first engine already used, or the fill-id dedup in the tracker /
  performance service / store would silently swallow a genuine execution;
* leaf 02 — :class:`~trading_bot.application.order_fill_sync.OrderFillSync`:
  fills update the tracked order + store row, and ``replay()`` heals restored
  orders between ``restore()`` and ``reconcile()``;
* leaf 03 — :func:`~trading_bot.application.reconcile.reconcile` persists an
  orphan-close via a re-emitted
  :class:`~trading_bot.application.events.OrderEvent`, so a resting order that
  the fresh (paper) lifetime's broker has no memory of lands in the store as
  ``CANCELLED`` — never left hanging at ``open``/``partially_filled``.

Lifetime 1 places two orders (one fills immediately, one is left resting via
:meth:`~trading_bot.brokers.paper.PaperBroker.arm_partial`), then lifetime 2
restarts over the same store and closes the position with a fresh order. The
final assertions are the epic's standing invariants: every venue/fill id is
pairwise distinct across both lifetimes, no fill is ever swallowed (tracker
positions equal the signed fold of the store's fills), every row lands
terminal (``FILLED`` or ``CANCELLED`` — never ``open``/``submitted``), and
realised PnL is computed from fills alone (the store's fold matches the live
:class:`~trading_bot.application.performance_service.PerformanceService`).

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

# Built-in
import pathlib

# Local
from trading_bot.application import AppConfig, build_engine, reconcile
from trading_bot.brokers.paper import PaperBroker
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

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _limit_order(
    cid: str,
    *,
    instrument: Instrument,
    side: OrderSide,
    qty: str,
    limit: str,
) -> Order:
    """A LIMIT order that fills at its own limit price on the simulator.

    LIMIT (rather than MARKET) sidesteps needing an injected mark price on the
    broker — the exact same shortcut ``test_order_fill_sync.py`` uses.
    """
    return Order(
        client_order_id=cid,
        instrument=instrument,
        side=side,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(limit),
    )


def _replay_paper_book(engine) -> None:  # noqa: ANN001 - test-local, Engine type
    """Fold a paper engine's stored fills into its tracker + perf.

    A test-local mirror of ``StrategySupervisor._replay_paper_book``: the
    startup step run **after** ``reconcile`` (paper only), so a paper unit's
    book survives a restart. Both ``apply``\\ s are idempotent by ``fill_id``,
    so this never double-counts. Kept inline (rather than reaching into the
    supervisor's private method) per the leaf contract's "no production code
    changes" — this is test code reproducing the documented sequence, not a
    new production seam.
    """
    if engine.store is None:
        return
    for record in engine.store.stored_fills():
        if record.mode != "paper":
            continue
        engine.tracker.apply(record.fill)
        engine.perf.apply(record.fill)


def _signed_fill_sums(fills: list[Fill]) -> dict[Instrument, object]:
    """``{instrument: signed net qty}`` over ``fills`` (BUY +qty, SELL -qty)."""
    totals: dict[Instrument, object] = {}
    for fill in fills:
        signed = fill.qty if fill.side is OrderSide.BUY else -fill.qty
        totals[fill.instrument] = totals.get(fill.instrument, money("0")) + signed
    return totals


async def test_two_engine_lifetimes_over_one_store(tmp_path: pathlib.Path) -> None:
    """Two ``build_engine`` lifetimes over one store: the epic's invariants hold.

    Lifetime 1 places a BTC buy that fills immediately and an ETH buy that is
    left resting (armed to a partial fill). "Restart": a second engine is built
    on the same store path and runs restore -> replay -> reconcile exactly like
    :meth:`~trading_bot.application.supervisor.StrategySupervisor._start_locked`.
    Lifetime 2 then places a BTC sell that closes the position. See the module
    docstring for why this proves leaves 01-03 together.
    """
    db_path = tmp_path / "restart.sqlite"
    config = AppConfig()

    # --- Lifetime 1 ---------------------------------------------------------- #
    engine1 = build_engine(config, db_path=db_path)
    broker1 = engine1.broker
    assert isinstance(broker1, PaperBroker)

    filled_order = _limit_order(
        "lt1-btc-buy", instrument=BTC_USD, side=OrderSide.BUY, qty="1", limit="30000"
    )
    await engine1.router.submit(filled_order)
    assert filled_order.status is OrderStatus.FILLED
    assert filled_order.filled_qty == money("1")

    # Arm the *next* placement to fill only half its qty, leaving it resting
    # (PARTIALLY_FILLED, live on the broker's open-order book) — the order a
    # fresh lifetime 2's broker will have no memory of.
    broker1.arm_partial(money("0.5"))
    resting_order = _limit_order(
        "lt1-eth-resting", instrument=ETH_USD, side=OrderSide.BUY, qty="2", limit="2000"
    )
    await engine1.router.submit(resting_order)
    assert resting_order.status is OrderStatus.PARTIALLY_FILLED
    assert resting_order.filled_qty == money("1")

    # Step 1's assertions: 2 order rows, >=1 fill, the filled row terminal.
    assert engine1.store is not None
    engine1.store.flush()
    stored_orders_1 = {o.client_order_id: o for o in engine1.store.orders()}
    assert len(stored_orders_1) == 2
    assert len(engine1.store.fills()) == 2  # one per order (the resting one: 1 slice)
    assert stored_orders_1["lt1-btc-buy"].status is OrderStatus.FILLED
    assert stored_orders_1["lt1-btc-buy"].filled_qty == money("1")

    # Graceful teardown: drain the writer so every enqueued write lands before
    # the "restart" reopens the same file (mirrors the supervisor's
    # `_drain_store` ahead of `_teardown`).
    engine1.store.close()

    # --- "Restart": a second engine over the SAME store path ----------------- #
    engine2 = build_engine(config, db_path=db_path)
    broker2 = engine2.broker
    assert isinstance(broker2, PaperBroker)
    assert engine2.store is not None

    # Mirrors `_start_locked` exactly: tag context, restore the dedup map, heal
    # from persisted fills, THEN reconcile (so a genuinely-filled order is
    # terminal before the orphan rule runs).
    engine2.store.set_context(mode="paper", venue="paper")
    engine2.router.restore(engine2.store.orders())
    engine2.fill_sync.replay(
        sorted(
            (
                record.fill
                for record in engine2.store.stored_fills()
                if record.mode == "paper"
            ),
            key=lambda fill: fill.ts,
        )
    )
    recon = await reconcile(
        engine2.broker, engine2.router, engine2.tracker, event_bus=engine2.bus
    )
    # The resting order has no venue record on the fresh (paper) broker: it is
    # the lone orphan, closed-and-forgotten (leaf 03).
    assert recon.closed_orphans == 1
    assert engine2.router.get("lt1-eth-resting") is None
    assert engine2.router.get("lt1-btc-buy") is not None
    assert engine2.router.get("lt1-btc-buy").status is OrderStatus.FILLED

    # Paper-only step: fold the store's fills into the fresh tracker + perf so
    # the book survives the restart (see `_replay_paper_book`'s docstring).
    assert engine2.tracker.all_positions() == {}  # reconcile alone: no venue fills yet
    _replay_paper_book(engine2)
    assert engine2.tracker.position(BTC_USD) is not None
    assert engine2.tracker.position(BTC_USD).net_qty == money("1")
    assert engine2.tracker.position(ETH_USD) is not None
    assert engine2.tracker.position(ETH_USD).net_qty == money("1")

    # --- Lifetime 2: a fresh order that closes the BTC position -------------- #
    closing_order = _limit_order(
        "lt2-btc-sell", instrument=BTC_USD, side=OrderSide.SELL, qty="1", limit="32000"
    )
    await engine2.router.submit(closing_order)
    assert closing_order.status is OrderStatus.FILLED
    assert closing_order.filled_qty == money("1")

    engine2.store.flush()

    # --- The epic's invariants ------------------------------------------------ #

    # 1. Every venue id and every fill id, across BOTH lifetimes, is distinct
    #    (leaf 01 — no id collision between the two PaperBroker instances).
    stored_orders_2 = engine2.store.orders()
    venue_ids = [o.venue_order_id for o in stored_orders_2]
    assert len(venue_ids) == len(set(venue_ids)) == 3
    fill_ids = [f.fill_id for f in engine2.store.fills()]
    assert len(fill_ids) == len(set(fill_ids)) == 3  # BTC buy, ETH partial, BTC sell

    # 2. No fill is ever swallowed: tracker positions equal the signed fold of
    #    the store's fills, per instrument.
    signed_sums = _signed_fill_sums(engine2.store.fills())
    assert (
        engine2.tracker.position(BTC_USD).net_qty == signed_sums[BTC_USD] == money("0")
    )
    assert (
        engine2.tracker.position(ETH_USD).net_qty == signed_sums[ETH_USD] == money("1")
    )

    # 3. Every filled row is FILLED with filled_qty == qty; the lifetime-1
    #    resting row is CANCELLED (the persisted orphan close, leaf 03); no row
    #    is left open/submitted.
    by_cid = {o.client_order_id: o for o in stored_orders_2}
    assert len(by_cid) == 3
    assert by_cid["lt1-btc-buy"].status is OrderStatus.FILLED
    assert by_cid["lt1-btc-buy"].filled_qty == by_cid["lt1-btc-buy"].qty == money("1")
    assert by_cid["lt2-btc-sell"].status is OrderStatus.FILLED
    assert by_cid["lt2-btc-sell"].filled_qty == by_cid["lt2-btc-sell"].qty == money("1")
    assert by_cid["lt1-eth-resting"].status is OrderStatus.CANCELLED
    non_terminal = {
        OrderStatus.NEW,
        OrderStatus.SUBMITTED,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
    }
    assert all(o.status not in non_terminal for o in stored_orders_2)

    # 4. Fills stay the sole PnL source: PerformanceService.realised_pnl() over
    #    the whole run equals the same fold computed independently from the
    #    store's fills (grouped by instrument, Position.from_fills, summed).
    fills_by_instrument: dict[Instrument, list[Fill]] = {}
    for fill in engine2.store.fills():
        fills_by_instrument.setdefault(fill.instrument, []).append(fill)
    expected_realised = sum(
        (
            Position.from_fills(fills).realised_pnl
            for fills in fills_by_instrument.values()
        ),
        money("0"),
    )
    assert expected_realised > money("0")  # the BTC round trip is a real profit
    assert engine2.perf.realised_pnl() == expected_realised

    engine2.store.close()
