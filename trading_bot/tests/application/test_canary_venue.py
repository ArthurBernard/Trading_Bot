"""Tests for the canary's **venue** path: the identity oracle + the settle loop.

The venue oracle (:func:`trading_bot.application.canary.venue_oracle`) asserts
the accounting identity — our recorded fills == the venue-reported fills, the
venue's balance deltas == what our fills imply (the fills math, never a
hardcoded zero), flat — plus a bounded cost. A real (REST) venue also fills
market orders *asynchronously* and keys its fill reports by its **own** order
reference, so :func:`~trading_bot.application.canary.run_canary` settles each
market leg by polling ``broker.fills(...)`` and attributing new fills to the
leg (see the module docstring's "Venue legs settle by polling").

These tests drive both against :class:`_RestVenueBroker`, a deliberately
REST-shaped fake: market orders execute **venue-side only** (recorded in its
``fills()`` view, balances mutated — nothing emitted on the bus), limit orders
rest, and every fill's ``client_order_id`` is the venue's own numeric order id
(never ours) — exactly the Binance ``myTrades`` shape the settle loop must
handle. Covered:

* the whole scenario is green end to end over the REST venue: legs settle by
  poll, the store's fills carry OUR client-order-ids while
  ``report.venue_fills`` carries the venue's, and every identity/cost check
  holds;
* every oracle branch turns red on the seeded violation it guards: a dropped
  store fill (fills mismatch), a tampered quote balance (quote delta), venue
  base dust our fills do not explain (base delta — expected rendered from the
  fills math), a tiny ``max_cost`` (cost bound);
* the base-delta expectation really is the **fills math, not zero**: a venue
  over-delivery within the domain fill tolerance leaves base dust that the
  check *passes* because our recorded fills imply exactly that dust;
* a venue that never reports the market execution times the settle loop out —
  the leg's check fails with what was observed and the abort cleanup cancels
  the resting order;
* opt-in (``-m network``): the REAL Binance testnet run — the CLI's testnet
  path end to end with the ``BINANCE_TESTNET_*`` keys, fake money, real API.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

# Built-in
import os
import pathlib
import sqlite3
from dataclasses import replace

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
    run_canary,
    venue_oracle,
)
from trading_bot.application import canary as canary_mod
from trading_bot.application.events import FillEvent
from trading_bot.application.order_fill_sync import OrderFillSync
from trading_bot.application.service_factory import Engine
from trading_bot.brokers.base import Capability
from trading_bot.domain import Instrument, Order, OrderStatus, Symbol, money
from trading_bot.domain.fill import Fill
from trading_bot.domain.money import Money
from trading_bot.domain.order import OrderSide, OrderType
from trading_bot.storage.sqlite_store import SqliteStore

#: The same Binance-BTC/USDT-shaped venue spec the paper canary tests use.
INSTRUMENT = Instrument(
    Symbol("BTC", "USDT"),
    price_precision=2,
    qty_precision=5,
    min_qty=money("0.00001"),
    min_notional=money("5"),
)
MARK = money("50000")
QTY = money("0.001")
MAX_COST = money("2")

#: Per-leg venue fee at the fake's default 10 bps: 0.001 * 50000 * 0.001 = 0.05.
LEG_FEE = money("0.05")


class _RestVenueBroker:
    """A REST-shaped fake venue: async fills, venue-keyed fill reports.

    Market orders execute immediately **venue-side**: the execution lands in
    the ``fills()`` view and moves the balances, but nothing is emitted on any
    bus — exactly like a real REST venue, which only *reports* executions when
    polled. Limit orders rest until cancelled. Every fill's
    ``client_order_id`` is the venue's own numeric order id (the Binance
    ``myTrades`` shape), never the caller's — the canary's settle attribution
    must not rely on client-order-id matching.

    Parameters
    ----------
    balances : dict of str to Decimal
        Starting free balances.
    fee_bps : Decimal, optional
        Fee rate in basis points, charged in **quote** on each fill.
    fill_market : bool, optional
        ``False`` makes market orders rest instead of executing — the
        "venue never reports the execution" failure mode.
    buy_overfill : Decimal, optional
        Extra base quantity a market BUY delivers beyond the requested size
        (venue lot rounding) — the within-tolerance dust case. Balances move
        by the *delivered* quantity.
    fee_in_base_on_buys : bool, optional
        Charge a market BUY's commission in the **base** asset (taken out of
        the delivered quantity), exactly like the real Binance venue — the
        base-dust identity case observed live on the testnet. Sells stay
        quote-fee'd.

    """

    name = "restvenue"

    def __init__(
        self,
        *,
        balances: dict[str, Money],
        fee_bps: Money = money("10"),
        fill_market: bool = True,
        buy_overfill: Money = money("0"),
        fee_in_base_on_buys: bool = False,
    ) -> None:
        self._balances: dict[str, Money] = {
            asset: money(amount) for asset, amount in balances.items()
        }
        self._fee_bps = money(fee_bps)
        self._fill_market = fill_market
        self._buy_overfill = money(buy_overfill)
        self._fee_in_base_on_buys = fee_in_base_on_buys
        self._prices: dict[Symbol, Money] = {INSTRUMENT.symbol: MARK}
        # venue_order_id -> the resting order's rebuild ingredients.
        self._open: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._seq = 0

    def capabilities(self) -> set[Capability]:
        return {
            Capability.PLACE_ORDER,
            Capability.CANCEL,
            Capability.OPEN_ORDERS,
            Capability.BALANCES,
            Capability.FILLS,
            Capability.TICKER,
        }

    async def ticker(self, instrument: Instrument) -> Money:
        return self._prices[instrument.symbol]

    async def balances(self) -> dict[str, Money]:
        return {
            asset: amount for asset, amount in self._balances.items() if amount != 0
        }

    async def open_orders(self) -> list[Order]:
        return list(self._open.values())

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        return list(self._fills)

    async def place_order(self, order: Order) -> str:
        self._seq += 1
        venue_num = str(1000 + self._seq)
        venue_id = f"BTCUSDT:{venue_num}"
        if order.type is OrderType.MARKET and self._fill_market:
            price = self._prices[order.instrument.symbol]
            base = order.instrument.symbol.base
            quote = order.instrument.symbol.quote
            zero = money("0")
            qty = order.qty
            if order.side is OrderSide.BUY:
                qty += self._buy_overfill
            base_fee = order.side is OrderSide.BUY and self._fee_in_base_on_buys
            # Base-denominated buy commission (the real Binance shape) is a
            # cut of the delivered base; otherwise the fee is quote on the
            # traded notional.
            fee = (
                qty * self._fee_bps / money("10000")
                if base_fee
                else qty * price * self._fee_bps / money("10000")
            )
            self._fills.append(
                Fill(
                    fill_id=f"V{self._seq}",
                    # The venue's own order reference — NOT the caller's cid.
                    client_order_id=venue_num,
                    # Bare instrument, like a real adapter's fill rebuild.
                    instrument=Instrument(order.instrument.symbol),
                    side=order.side,
                    qty=qty,
                    price=price,
                    fee=fee,
                    ts=self._seq,
                    fee_asset=base if base_fee else None,
                )
            )
            if order.side is OrderSide.BUY:
                self._balances[quote] = self._balances.get(quote, zero) - (
                    qty * price + (zero if base_fee else fee)
                )
                self._balances[base] = self._balances.get(base, zero) + (
                    qty - (fee if base_fee else zero)
                )
            else:
                self._balances[base] = self._balances.get(base, zero) - qty
                self._balances[quote] = (
                    self._balances.get(quote, zero) + qty * price - fee
                )
        else:
            self._open[venue_id] = order
        return venue_id

    async def cancel_order(self, venue_order_id: str) -> None:
        self._open.pop(venue_order_id, None)


def _venue_engine(tmp_path: pathlib.Path, broker: _RestVenueBroker) -> Engine:
    """A hand-wired engine (the factory's exact shape) over the REST fake.

    Mirrors ``build_engine``'s wiring order — tracker/perf subscribed, a
    risk-gated router, the fill sync before the store attaches — so the settle
    loop's re-emitted fills flow through the very plumbing a real venue run
    uses.
    """
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    perf = PerformanceService(event_bus=bus)
    risk = RiskManager(
        RiskConfig(),
        position_tracker=tracker,
        daily_pnl_provider=perf.realised_pnl_since,
    )
    router = OrderRouter(broker, bus, risk_manager=risk)
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


async def _green_run(
    tmp_path: pathlib.Path,
) -> tuple[Engine, CanaryReport]:
    """One full, green venue-canary run over the REST fake (asserted green)."""
    engine = _venue_engine(tmp_path, _RestVenueBroker(balances={"USDT": money("1000")}))
    report = await run_canary(
        engine,
        INSTRUMENT,
        qty=QTY,
        oracle=venue_oracle(MAX_COST),
        mode_label="testnet",
    )
    assert report.passed, report.to_text()
    return engine, report


# --- the full scenario, green end to end over a REST-shaped venue ------------ #


async def test_venue_scenario_identity_holds(tmp_path: pathlib.Path) -> None:
    """The whole canary passes over a REST venue: settle-by-poll + identity oracle.

    The venue reports fills asynchronously and keys them by its own order id;
    the run must still confirm both legs, persist them under OUR ids, and the
    identity oracle must hold with the exact venue-reported ids/qtys/prices.
    """
    engine, report = await _green_run(tmp_path)

    assert report.mode == "testnet"
    assert [check.name for check in report.checks] == [
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
        "oracle.fills_match_venue",
        "oracle.position_flat",
        "oracle.base_delta_matches_fills",
        "oracle.quote_delta_matches_fills",
        "oracle.cost_bounded",
    ]

    # The venue reported the two round-trip fills under ITS OWN references...
    assert len(report.venue_fills) == 2
    assert {fill.client_order_id for fill in report.venue_fills} == {"1002", "1003"}
    # ...while the store recorded the same executions under OUR leg ids — the
    # settle loop's attribution — with identical fill ids.
    store = engine.store
    assert store is not None
    store.flush()
    ours = [
        fill
        for fill in store.fills()
        if fill.client_order_id in {report.buy_cid, report.sell_cid}
    ]
    assert {fill.fill_id for fill in ours} == {
        fill.fill_id for fill in report.venue_fills
    }
    assert store.get_order(report.buy_cid).status is OrderStatus.FILLED
    assert store.get_order(report.sell_cid).status is OrderStatus.FILLED

    # Exact venue accounting: both legs at the same mark, so the cost is the
    # two fees; balances moved by exactly what the fills imply.
    assert report.cost == 2 * LEG_FEE
    assert report.initial_balances == {"USDT": money("1000")}
    assert report.final_balances == {"USDT": money("999.9")}
    store.close()


async def test_venue_scenario_green_with_base_denominated_buy_fee(
    tmp_path: pathlib.Path,
) -> None:
    """The identity holds when the venue charges the BUY fee in the base asset.

    Exactly the shape the real Binance testnet exhibits (a BTC/USDT market buy
    is commissioned in BTC): the venue's base balance ends with a fee-sized
    dust the fills math must explain via the fill's ``fee_asset`` — expected
    from the fills, not from zero — while the position stays flat (fees never
    fold into the position quantity).
    """
    broker = _RestVenueBroker(
        balances={"USDT": money("1000")}, fee_in_base_on_buys=True
    )
    engine = _venue_engine(tmp_path, broker)

    report = await run_canary(
        engine,
        INSTRUMENT,
        qty=QTY,
        oracle=venue_oracle(MAX_COST),
        mode_label="testnet",
    )

    assert report.passed, report.to_text()
    checks = {c.name: c for c in report.checks}
    # Buy fee: 0.001 * 10bps = 0.000001 BTC, taken from the delivered base.
    base_check = checks["oracle.base_delta_matches_fills"]
    assert base_check.expected == "base_delta=-0.000001"
    assert base_check.observed == "base_delta=-0.000001"
    # Quote moved by the sell fee only (the buy fee never touched quote).
    quote_check = checks["oracle.quote_delta_matches_fills"]
    assert quote_check.expected == "quote_delta=-0.050"
    assert report.cost is not None
    if engine.store is not None:
        engine.store.close()


# --- every oracle branch turns red on its seeded violation ------------------- #


async def test_oracle_detects_fills_mismatch(tmp_path: pathlib.Path) -> None:
    """Dropping one persisted fill breaks the fills identity, exactly reported."""
    engine, report = await _green_run(tmp_path)
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

    checks = {c.name: c for c in venue_oracle(MAX_COST)(engine, report)}

    assert not checks["oracle.fills_match_venue"].passed
    # The venue side still lists both executions; ours only one.
    assert checks["oracle.fills_match_venue"].expected.count("[id=") == 2
    assert checks["oracle.fills_match_venue"].observed.count("[id=") == 1
    store.close()


async def test_oracle_detects_quote_balance_mismatch(
    tmp_path: pathlib.Path,
) -> None:
    """A venue quote delta our fills do not explain fails the quote identity."""
    engine, report = await _green_run(tmp_path)

    report.final_balances["USDT"] = report.final_balances["USDT"] + money("0.01")
    checks = {c.name: c for c in venue_oracle(MAX_COST)(engine, report)}

    quote_check = checks["oracle.quote_delta_matches_fills"]
    assert not quote_check.passed
    assert quote_check.expected == "quote_delta=-0.100"
    assert quote_check.observed == "quote_delta=-0.090"
    if engine.store is not None:
        engine.store.close()


async def test_oracle_reports_unexplained_base_dust_exactly(
    tmp_path: pathlib.Path,
) -> None:
    """Venue base dust our fills do NOT imply fails — against the fills math.

    The expected side is computed from our recorded fills (buy qty − sell qty
    == 0 here), never assumed: the venue reporting a stray base remainder (a
    base-denominated fee, say) is an exact, reported identity violation.
    """
    engine, report = await _green_run(tmp_path)

    report.final_balances["BTC"] = money("-0.00000001")
    checks = {c.name: c for c in venue_oracle(MAX_COST)(engine, report)}

    base_check = checks["oracle.base_delta_matches_fills"]
    assert not base_check.passed
    assert base_check.expected == "base_delta=0.000"  # the fills math, exact
    assert base_check.observed == "base_delta=-1E-8"
    if engine.store is not None:
        engine.store.close()


async def test_oracle_cost_bound_breached(tmp_path: pathlib.Path) -> None:
    """A run whose venue-measured cost exceeds ``max_cost`` fails the bound."""
    engine, report = await _green_run(tmp_path)

    checks = {c.name: c for c in venue_oracle(money("0.05"))(engine, report)}

    cost_check = checks["oracle.cost_bounded"]
    assert not cost_check.passed
    assert cost_check.expected == "cost<=0.05"
    assert cost_check.observed == "cost=0.100"  # 2 legs x 0.05 fee
    if engine.store is not None:
        engine.store.close()


# --- base dust: the expectation is the fills math, not zero ------------------ #


async def test_scenario_reports_venue_overfill_dust_and_stops(
    tmp_path: pathlib.Path,
) -> None:
    """A venue over-delivery within fill tolerance is reported exactly, and the
    run stops before selling.

    The buy leg reads FILLED (the domain clamps within-tolerance over-fill
    dust), but the tracker folds the *delivered* quantity — so the in-tracker
    check records the venue's real behaviour exactly and the scenario refuses
    to continue on a book that does not hold what it requested.
    """
    engine = _venue_engine(
        tmp_path,
        _RestVenueBroker(
            balances={"USDT": money("1000")},
            # 1e-8 over 0.001: excess fraction 1e-5, far inside the 0.1%
            # domain fill tolerance — dust, not a material over-fill.
            buy_overfill=money("0.00000001"),
        ),
    )
    report = await run_canary(
        engine, INSTRUMENT, qty=QTY, oracle=None, mode_label="testnet"
    )

    assert not report.passed
    by_name = {c.name: c for c in report.checks}
    assert by_name["roundtrip.buy_filled"].passed
    assert not by_name["roundtrip.buy_in_tracker"].passed
    assert "0.00100001" in by_name["roundtrip.buy_in_tracker"].observed
    # The sell was never placed: the run stopped on the dusted book.
    assert engine.router.get(report.sell_cid) is None
    if engine.store is not None:
        engine.store.close()


async def test_base_dust_explained_by_fills_math_passes(
    tmp_path: pathlib.Path,
) -> None:
    """Venue base dust that our fills imply PASSES the base-delta identity.

    The pinned "compare against the fills math, not against zero" case, over
    directly-seeded evidence of a *completed* round trip whose recorded fills
    imply a base remainder (the buy delivered a within-tolerance hair more
    than the sell returned): the venue balances reporting exactly that dust
    must pass the base-delta check, whose expectation is computed from the
    fills — a hardcoded-zero oracle would wrongly fail it.
    """
    engine = _venue_engine(tmp_path, _RestVenueBroker(balances={"USDT": money("1000")}))
    buy_qty = money("0.00100001")
    sell_qty = money("0.001")
    dust = buy_qty - sell_qty
    report = CanaryReport(
        exchange="restvenue",
        mode="testnet",
        instrument=INSTRUMENT,
        qty=QTY,
        probe_cid="canary-dust-probe",
        buy_cid="canary-dust-buy",
        sell_cid="canary-dust-sell",
    )
    ours = [
        Fill(
            fill_id="V1",
            client_order_id=report.buy_cid,
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            qty=buy_qty,
            price=MARK,
            fee=money("0"),
            ts=1,
        ),
        Fill(
            fill_id="V2",
            client_order_id=report.sell_cid,
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            qty=sell_qty,
            price=MARK,
            fee=money("0"),
            ts=2,
        ),
    ]
    for fill in ours:
        engine.bus.emit(FillEvent(fill))  # store + tracker record our fills
    report.initial_balances = {"USDT": money("1000")}
    # The venue reports exactly what those fills imply: the base dust left
    # over, and the quote moved by the notional difference.
    report.final_balances = {
        "USDT": money("1000") - dust * MARK,
        "BTC": dust,
    }
    # Venue-side, the same executions under the venue's own references.
    report.venue_fills = [
        replace(ours[0], client_order_id="9001"),
        replace(ours[1], client_order_id="9002"),
    ]

    checks = {c.name: c for c in venue_oracle(MAX_COST)(engine, report)}

    base_check = checks["oracle.base_delta_matches_fills"]
    assert dust != 0
    assert base_check.passed
    assert base_check.expected == f"base_delta={dust}"  # the fills math, not 0
    assert base_check.observed == f"base_delta={dust}"
    assert checks["oracle.fills_match_venue"].passed
    assert checks["oracle.quote_delta_matches_fills"].passed
    # The dusted book is still reported honestly where it IS a violation:
    # the position is not flat.
    assert not checks["oracle.position_flat"].passed
    if engine.store is not None:
        engine.store.close()


# --- settle timeout: a venue that never reports the execution ---------------- #


async def test_settle_timeout_fails_leg_with_evidence(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A market order the venue never reports filled times out and fails the leg.

    The settle loop gives up after the timeout; the leg's check records the
    still-open order exactly, the run stops placing, and the abort cleanup
    cancels the resting order on the venue.
    """
    monkeypatch.setattr(canary_mod, "_VENUE_SETTLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(canary_mod, "_VENUE_SETTLE_POLL_S", 0.01)
    broker = _RestVenueBroker(balances={"USDT": money("1000")}, fill_market=False)
    engine = _venue_engine(tmp_path, broker)

    report = await run_canary(
        engine, INSTRUMENT, qty=QTY, oracle=venue_oracle(MAX_COST)
    )

    assert not report.passed
    assert report.checks[-1].name == "roundtrip.buy_filled"
    assert not report.checks[-1].passed
    assert "status=open filled_qty=0" in report.checks[-1].observed
    # The sell never ran, and the cleanup cancelled the resting buy venue-side.
    assert engine.router.get(report.sell_cid) is None
    assert await broker.open_orders() == []
    if engine.store is not None:
        engine.store.close()


# --- the REAL Binance testnet run (opt-in: ``-m network``, key-gated) -------- #


@pytest.mark.network
async def test_real_binance_testnet_canary() -> None:
    """The canary against the REAL Binance spot testnet — fake money, real API.

    Skips without testnet credentials (``BINANCE_TESTNET_API_KEY`` /
    ``BINANCE_TESTNET_API_SECRET``, falling back to ``BINANCE_API_KEY`` /
    ``BINANCE_API_SECRET`` — testnet.binance.vision rejects a mainnet key with
    ``-2015``). Runs the CLI's own testnet path — the factory-built,
    testnet-pinned engine, venue-side sizing, the full scenario, the identity
    oracle — and asserts the whole evidence table is green. This is the
    road-to-1.0 #1 validation vehicle: a real cancel, venue-checked
    idempotency, and balance reconciliation against venue-reported fills.
    """
    from trading_bot.interfaces.cli.main import _run_venue_canary

    key = os.environ.get("BINANCE_TESTNET_API_KEY") or os.environ.get("BINANCE_API_KEY")
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or os.environ.get(
        "BINANCE_API_SECRET"
    )
    if not key or not secret:
        pytest.skip("no Binance testnet credentials in the environment")

    report = await _run_venue_canary(
        exchange="binance",
        symbol="BTC/USDT",
        mode="testnet",
        max_cost=MAX_COST,
        # The CLI's venue default: inside Binance's PERCENT_PRICE_BY_SIDE
        # price band (a 50%-below probe is rejected with -1013 on the venue).
        probe_offset_pct=money("15"),
        db_path=None,
    )

    assert report.exchange == "binance"
    assert report.mode == "testnet"
    assert report.passed, report.to_text()
    assert len(report.venue_fills) >= 2  # both legs venue-confirmed
