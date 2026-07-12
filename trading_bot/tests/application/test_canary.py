"""Tests for the canary scenario and its exact paper oracle.

The canary (:mod:`trading_bot.application.canary`) is the platform's
deterministic self-test: a sequential round-trip plus two free probes over a
dedicated, explicitly-funded engine, with every expectation vs observation
recorded exactly. These tests drive the **real** engine assembly both ways the
leaf allows — the factory (:func:`~trading_bot.application.service_factory.
build_engine`, funded via ``AppConfig.paper_starting_balances``) and a
hand-wired :class:`~trading_bot.application.service_factory.Engine` (the
deliberately-broken variants) — and assert:

* the full scenario is green end to end on a fresh strict paper engine, with
  the **exact** oracle: realised PnL == −Σ fees, flat, balances moved by the
  fees only, both round-trip rows ``FILLED`` in the store;
* a broker that fills the far-off cancel probe is **detected** and the run
  stops placing (no round-trip orders ever reach the venue);
* a broken idempotency path (router + venue dedup both disabled) is detected
  the same way;
* the oracle catches a seeded imbalance (a fill dropped from the store);
* the printed evidence table is stable and exact.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

# Built-in
import pathlib
import sqlite3
from decimal import Decimal

# Third-party
import pytest

# Local
from trading_bot.application import (
    AppConfig,
    CanaryReport,
    EventBus,
    OrderRouter,
    PerformanceService,
    PositionTracker,
    RiskConfig,
    RiskManager,
    build_engine,
    paper_oracle,
    run_canary,
)
from trading_bot.application.order_fill_sync import OrderFillSync
from trading_bot.application.service_factory import Engine
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import Instrument, Order, OrderStatus, Symbol, money
from trading_bot.storage.sqlite_store import SqliteStore

#: A spec-carrying instrument shaped like the real Binance BTC/USDT venue spec
#: (1e-5 lot, 5-quote-unit min-notional), so strict paper quantizes and gates
#: exactly like the live venue would.
INSTRUMENT = Instrument(
    Symbol("BTC", "USDT"),
    price_precision=2,
    qty_precision=5,
    min_qty=money("0.00001"),
    min_notional=money("5"),
)
MARK = money("50000")
QTY = money("0.001")


def _factory_engine(tmp_path: pathlib.Path, *, strict: bool = True) -> Engine:
    """A factory-built, funded, strict-paper canary engine with a mark injected."""
    config = AppConfig(
        paper_strict=strict,
        paper_starting_balances={"USDT": Decimal("1000")},
    )
    engine = build_engine(config, db_path=tmp_path / "canary.sqlite")
    broker = engine.broker
    assert isinstance(broker, PaperBroker)
    broker.set_price(INSTRUMENT, MARK)
    return engine


def _hand_engine(
    tmp_path: pathlib.Path,
    *,
    broker_cls: type[PaperBroker] = PaperBroker,
    router_cls: type[OrderRouter] = OrderRouter,
) -> Engine:
    """A hand-wired engine (the factory's exact shape) with injectable classes.

    Mirrors :func:`~trading_bot.application.service_factory.build_engine`'s
    wiring order — broker on the bus, tracker/perf subscribed, risk-gated
    router, fill sync before the store attaches — but lets a test swap in a
    deliberately broken broker/router subclass. Deterministic ids
    (``id_token``) and the broker's default deterministic clock.
    """
    bus = EventBus()
    broker = broker_cls(
        prices={INSTRUMENT: MARK},
        starting_balances={"USDT": money("1000")},
        strict=True,
        event_bus=bus,
        id_token="canarytest",
    )
    tracker = PositionTracker(event_bus=bus)
    perf = PerformanceService(event_bus=bus)
    risk = RiskManager(
        RiskConfig(),
        position_tracker=tracker,
        daily_pnl_provider=perf.realised_pnl_since,
    )
    router = router_cls(broker, bus, risk_manager=risk)
    fill_sync = OrderFillSync(router, bus)
    store = SqliteStore(tmp_path / "canary.sqlite")
    store.attach(bus)
    return Engine(
        config=AppConfig(),
        bus=bus,
        broker=broker,
        router=router,
        fill_sync=fill_sync,
        tracker=tracker,
        perf=perf,
        risk=risk,
        store=store,
    )


def _names(report: CanaryReport) -> list[str]:
    """The report's check names, in order."""
    return [check.name for check in report.checks]


# --- the full scenario, green end to end ------------------------------------ #


async def test_full_scenario_paper_engine_all_green(tmp_path: pathlib.Path) -> None:
    """The whole canary passes on a fresh, funded, strict factory engine.

    Every check green, in the pinned order (probes before money), and the
    exact paper accounting holds: realised PnL == −Σ fees == −0.10, cost ==
    2 × the per-leg fee, quote balance down by exactly the fees, base flat.
    """
    engine = _factory_engine(tmp_path)

    report = await run_canary(engine, INSTRUMENT, qty=QTY)

    assert report.passed, report.to_text()
    for check in report.checks:
        assert check.passed, f"{check.name}: {check.expected} != {check.observed}"
    assert _names(report) == [
        "cancel_probe.resting",
        "cancel_probe.cancelled",
        "cancel_probe.balances_unmoved",
        "cancel_probe.cancelled_persisted",
        "idempotency.deduped",
        "roundtrip.buy_filled",
        "roundtrip.buy_in_store",
        "roundtrip.buy_in_tracker",
        "roundtrip.sell_filled",
        "roundtrip.sell_in_store",
        "roundtrip.flat",
        "oracle.realised_pnl_is_minus_fees",
        "oracle.position_flat",
        "oracle.buy_filled_in_store",
        "oracle.sell_filled_in_store",
        "oracle.quote_delta_is_minus_fees",
        "oracle.base_delta_zero",
        "oracle.fill_count",
    ]

    # Exact accounting: fee per leg = 50000 * 0.001 * 10bps = 0.05.
    assert engine.perf.fees_paid() == money("0.1")
    assert engine.perf.realised_pnl() == money("-0.1")
    assert report.cost == money("0.1")  # == 2 x the per-leg fee
    # Balances moved by the fees only (quote), and the base is exactly flat.
    assert report.initial_balances == {"USDT": money("1000")}
    assert report.final_balances["USDT"] == money("999.9")
    assert report.final_balances["BTC"] == money("0")

    # The store agrees: probe CANCELLED, both round-trip rows FILLED.
    store = engine.store
    assert store is not None
    assert store.get_order(report.probe_cid).status is OrderStatus.CANCELLED
    assert store.get_order(report.buy_cid).status is OrderStatus.FILLED
    assert store.get_order(report.sell_cid).status is OrderStatus.FILLED
    store.close()


async def test_probe_qty_scales_up_to_stay_venue_legal(
    tmp_path: pathlib.Path,
) -> None:
    """A round-trip qty legal at the mark but sub-notional at the probe price.

    ``0.0001 * 50000 = 5`` clears the venue minimum at the mark, but at the
    50%-below probe price (``25000``) the same quantity is worth only ``2.5``
    — a strict venue would reject the probe. The canary scales the probe's
    quantity up to the smallest venue-legal size at the probe price
    (``5 / 25000 = 0.0002``) and the run stays green.
    """
    engine = _factory_engine(tmp_path)

    report = await run_canary(engine, INSTRUMENT, qty=money("0.0001"))

    assert report.passed, report.to_text()
    store = engine.store
    assert store is not None
    probe_row = store.get_order(report.probe_cid)
    assert probe_row is not None
    assert probe_row.qty == money("0.0002")
    assert probe_row.status is OrderStatus.CANCELLED
    store.close()


# --- caller wiring errors are refused before any order ----------------------- #


async def test_canary_refuses_engine_without_store() -> None:
    """No dedicated store, no canary: the run is refused before any order.

    ``build_engine`` attaches a store only for an explicit ``db_path`` (no
    default), so a store-less engine is exactly the "pointed at nothing of its
    own" misuse the canary must make impossible.
    """
    engine = build_engine(AppConfig())
    with pytest.raises(ValueError, match="dedicated store"):
        await run_canary(engine, INSTRUMENT, qty=QTY)


async def test_canary_validates_qty_and_offset(tmp_path: pathlib.Path) -> None:
    """A non-positive qty or an out-of-range probe offset is refused early."""
    engine = _factory_engine(tmp_path)
    with pytest.raises(ValueError, match="strictly positive"):
        await run_canary(engine, INSTRUMENT, qty=money("0"))
    with pytest.raises(ValueError, match="probe_offset_pct"):
        await run_canary(engine, INSTRUMENT, qty=QTY, probe_offset_pct=money("100"))
    # Nothing was placed by the refused runs.
    assert await engine.broker.fills() == []
    assert engine.store is not None
    engine.store.close()


# --- failure detection: cancel probe ----------------------------------------- #


async def test_cancel_probe_failure_detected_and_run_stops(
    tmp_path: pathlib.Path,
) -> None:
    """A broker that fills the far-off probe order fails the check — and the
    run places nothing further.

    The permissive (non-strict) simulator is exactly such a broker: it fills
    every limit order at its limit price regardless of the mark. The canary
    must catch it on the very first check and never reach the round-trip.
    """
    engine = _factory_engine(tmp_path, strict=False)

    report = await run_canary(engine, INSTRUMENT, qty=QTY)

    assert not report.passed
    assert _names(report) == ["cancel_probe.resting"]
    assert not report.checks[0].passed
    assert "status=filled" in report.checks[0].observed

    # No further orders were placed: one venue fill (the mis-filled probe),
    # one store row, and the round-trip ids never reached router or store.
    broker = engine.broker
    assert isinstance(broker, PaperBroker)
    assert len(await broker.fills()) == 1
    store = engine.store
    assert store is not None
    store.flush()
    assert len(store.orders()) == 1
    assert engine.router.get(report.buy_cid) is None
    assert engine.router.get(report.sell_cid) is None
    # It still snapshotted and reported what it measured.
    assert report.final_balances != {}
    assert report.cost is not None
    store.close()


# --- failure detection: idempotency probe ------------------------------------ #


class _NoDedupRouter(OrderRouter):
    """A deliberately broken router: forgets an id right before re-submitting it."""

    async def submit(self, order: Order) -> Order:
        self._orders.pop(order.client_order_id, None)
        return await super().submit(order)


class _NoDedupBroker(PaperBroker):
    """A deliberately broken strict broker: forgets its venue-side dedup too."""

    async def place_order(self, order: Order) -> str:
        self._venue_id_by_cid.pop(order.client_order_id, None)
        return await super().place_order(order)


async def test_idempotency_failure_detected_and_run_stops(
    tmp_path: pathlib.Path,
) -> None:
    """A duplicate client-order-id that creates a second live order is caught.

    With both dedup layers disabled (router map and strict venue-side dedup),
    the probe's re-submission opens a second resting venue order — the check
    must fail on the identity *and* the venue open-order count, and the
    round-trip must never run.
    """
    engine = _hand_engine(
        tmp_path, broker_cls=_NoDedupBroker, router_cls=_NoDedupRouter
    )

    report = await run_canary(engine, INSTRUMENT, qty=QTY)

    assert not report.passed
    names = _names(report)
    assert names[-1] == "idempotency.deduped"
    assert all(name.startswith("cancel_probe.") for name in names[:-1])
    dedup_check = report.checks[-1]
    assert not dedup_check.passed
    assert "a different order returned" in dedup_check.observed
    assert "venue_open=1" in dedup_check.observed

    # The round-trip never ran: no fills at all, no buy/sell rows.
    broker = engine.broker
    assert isinstance(broker, PaperBroker)
    assert await broker.fills() == []
    assert engine.router.get(report.buy_cid) is None
    assert engine.router.get(report.sell_cid) is None
    # Best-effort cleanup cancelled the duplicate: nothing left resting.
    assert await broker.open_orders() == []
    assert engine.store is not None
    engine.store.close()


# --- the oracle catches a seeded imbalance ------------------------------------ #


async def test_oracle_catches_seeded_imbalance(tmp_path: pathlib.Path) -> None:
    """Dropping one persisted fill turns the oracle red.

    After a green run, delete one round-trip fill row from the store (the kind
    of imbalance a lost execution would leave): the fill-count check and the
    realised-PnL-vs-store-fees cross-check must both fail.
    """
    engine = _factory_engine(tmp_path)
    report = await run_canary(engine, INSTRUMENT, qty=QTY)
    assert report.passed

    store = engine.store
    assert store is not None
    store.flush()
    conn = sqlite3.connect(tmp_path / "canary.sqlite")
    try:
        fill_id = conn.execute("SELECT fill_id FROM fills LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM fills WHERE fill_id = ?", (fill_id,))
        conn.commit()
    finally:
        conn.close()

    checks = {check.name: check for check in paper_oracle(engine, report)}
    assert not checks["oracle.fill_count"].passed
    assert checks["oracle.fill_count"].observed == "fills=1"
    assert not checks["oracle.realised_pnl_is_minus_fees"].passed
    store.close()


# --- report printing: stable, exact strings ----------------------------------- #


async def test_report_printing_stable_and_exact(tmp_path: pathlib.Path) -> None:
    """Two identical deterministic runs print the identical evidence table.

    A fixed ``id_token``, the simulator's deterministic clock and a fixed
    ``run_id`` make the whole run reproducible; the printed table carries no
    timestamps or minted ids, so it must match to the character — and selected
    lines are pinned exactly.
    """
    texts = []
    for sub in ("a", "b"):
        subdir = tmp_path / sub
        subdir.mkdir()
        engine = _hand_engine(subdir)
        report = await run_canary(engine, INSTRUMENT, qty=QTY, run_id="print")
        assert report.passed, report.to_text()
        texts.append(report.to_text())
        assert engine.store is not None
        engine.store.close()

    assert texts[0] == texts[1]
    lines = texts[0].splitlines()
    assert lines[0] == (
        "canary — exchange=paper mode=paper instrument=BTC/USDT qty=0.001"
    )
    assert lines[1] == (
        "[PASS] cancel_probe.resting: "
        "expected=status=open filled_qty=0 venue_fills=0 | "
        "observed=status=open filled_qty=0 venue_fills=0"
    )
    assert lines[5] == (
        "[PASS] idempotency.deduped: "
        "expected=original order returned venue_open=0 venue_fills=0 "
        "store_orders=1 | "
        "observed=original order returned venue_open=0 venue_fills=0 "
        "store_orders=1"
    )
    # Strict paper quantizes 0.001 to the 1e-5 lot (0.00100): numerically equal
    # values pass even when the rendered exponents differ — exact, not fuzzy.
    assert lines[6] == (
        "[PASS] roundtrip.buy_filled: "
        "expected=status=filled filled_qty=0.001 | "
        "observed=status=filled filled_qty=0.00100"
    )
    assert lines[12] == (
        "[PASS] oracle.realised_pnl_is_minus_fees: "
        "expected=realised_pnl=-0.10000 | observed=realised_pnl=-0.10000"
    )
    assert lines[-2] == "cost: 0.10000"
    assert lines[-1] == "result: PASS"
