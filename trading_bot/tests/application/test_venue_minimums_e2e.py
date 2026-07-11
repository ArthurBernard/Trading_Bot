"""E2E lock: the venue-minimums chain never submits a sub-minimum order.

Full-chain proof for ``doc/dev/plans/venue-minimums/00-plan.md`` (leaf 04):
resolver (leaf 01) -> order-prep policy (leaf 02) -> strict paper (leaf 03),
wired exactly as :func:`~trading_bot.application.service_factory.build_engine`
would (a real :class:`~trading_bot.brokers.paper.PaperBroker` in ``strict``
mode, routed through a real :class:`~trading_bot.application.order_router.
OrderRouter` and persisted to a real, scratch
:class:`~trading_bot.storage.sqlite_store.SqliteStore`), but with an injected
fake :class:`~trading_bot.application.instrument_specs.InstrumentSpecResolver`
carrying realistic (Binance-shaped) specs so the scenario is deterministic and
fully offline.

One :class:`~trading_bot.application.portfolio_runner.PortfolioRunner` over a
4-coin universe runs **two** rebalance ticks. A position in the fourth coin is
pre-seeded (a broker-confirmed fill emitted directly on the bus — not one of
the two graded ticks), so tick 1 already exercises every leg type the pinned
policy defines, in one pass:

* BTC — comfortably above its venue minimum -> ``submit`` unchanged;
* ETH — within ``min_order_ratio`` of its minimum -> ``round_up`` to it exactly;
* SOL — far below its minimum (the audit's dust shape) -> ``skip``, one info
  log, no order;
* ADA — a sell that would oversell the pre-seeded long -> capped ``submit`` at
  exactly the held quantity.

Tick 2 (same weights, same prices) proves convergence: the on-target BTC leg
and the now-dust ETH residual submit nothing (no re-round-up, no flip to
submit), the SOL dust leg skips again (stable, not a flip to submit), and ADA
— now flat, so no longer capped — continues selling toward its target on the
*same* side as tick 1 (progress, not oscillation). Throughout, every order that
reaches the store clears its venue minimum on the lot grid, and zero orders
are ever rejected (the upstream policy makes a strict-paper reject
structurally unreachable, since the routed order always carries a bare
instrument with no minimum of its own — see the module docstring of
:mod:`trading_bot.application.portfolio_runner`).
"""

from __future__ import annotations

# Built-in
import pathlib
from collections.abc import Mapping
from decimal import Decimal

# Third-party
import polars as pl

# Local
from trading_bot.application import (
    EventBus,
    FillEvent,
    LogEvent,
    OrderEvent,
    OrderRouter,
    PortfolioRunner,
    PortfolioStrategy,
    PositionTracker,
)
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import Fill, Instrument, OrderSide, Symbol, money
from trading_bot.storage.sqlite_store import SqliteStore

BTC = Symbol("BTC", "USDT")
ETH = Symbol("ETH", "USDT")
SOL = Symbol("SOL", "USDT")
ADA = Symbol("ADA", "USDT")
UNIVERSE = (BTC, ETH, SOL, ADA)
CAPITAL = money("100000")

BTC_PRICE = money("50000")
ETH_PRICE = money("2500")
SOL_PRICE = money("100")
ADA_PRICE = money("50")

#: Realistic (Binance-shaped) venue specs — a min_notional-bound leg (BTC), a
#: min_qty-bound round-up leg (ETH), a notional-bound dust leg (SOL) and a
#: min_qty-bound spot-sell-cap leg (ADA).
BTC_SPEC = Instrument(
    BTC, qty_precision=5, min_qty=money("0.00001"), min_notional=money("10")
)
ETH_SPEC = Instrument(
    ETH, qty_precision=4, min_qty=money("0.01"), min_notional=money("10")
)
SOL_SPEC = Instrument(
    SOL, qty_precision=2, min_qty=money("0.01"), min_notional=money("5")
)
ADA_SPEC = Instrument(ADA, qty_precision=2, min_qty=money("1"), min_notional=money("5"))
SPECS: dict[Symbol, Instrument] = {
    BTC: BTC_SPEC,
    ETH: ETH_SPEC,
    SOL: SOL_SPEC,
    ADA: ADA_SPEC,
}


# --- fakes / helpers --------------------------------------------------------- #


class _FakeResolver:
    """A fake :class:`InstrumentSpecResolver`: fixed realistic specs, no I/O."""

    degraded: set[str] = set()

    def __init__(self, specs: Mapping[Symbol, Instrument]) -> None:
        self._specs = dict(specs)

    async def resolve(self, exchange: str, symbol: Symbol) -> Instrument:
        return self._specs[symbol]


def _frame(close: Decimal) -> pl.DataFrame:
    """A minimal one-row OHLC(V) frame whose latest close is ``close``."""
    c = float(close)
    return pl.DataFrame(
        {
            "time": [1_700_000_000_000_000_000],
            "o": [c],
            "h": [c],
            "l": [c],
            "c": [c],
            "v": [1.0],
        }
    )


def _frames() -> dict[Symbol, pl.DataFrame]:
    """The fixed per-coin cross-section both rebalance ticks see (unchanged)."""
    return {
        BTC: _frame(BTC_PRICE),
        ETH: _frame(ETH_PRICE),
        SOL: _frame(SOL_PRICE),
        ADA: _frame(ADA_PRICE),
    }


def _weights_signal(weights: Mapping[Symbol, Decimal]):  # type: ignore[no-untyped-def]
    """A fake :data:`PortfolioSignalFn` returning a fixed weight vector."""

    def _fn(
        asof_ms: int, frames: Mapping[Symbol, pl.DataFrame]
    ) -> Mapping[Symbol, Decimal]:
        return dict(weights)

    return _fn


def _strict_paper_engine(
    db_path: pathlib.Path,
) -> tuple[EventBus, PositionTracker, OrderRouter, SqliteStore]:
    """A real strict-paper engine (router + tracker + bus) with a scratch store.

    Mirrors what :func:`~trading_bot.application.service_factory.build_engine`
    wires (strict :class:`~trading_bot.brokers.paper.PaperBroker`, a real
    :class:`~trading_bot.application.order_router.OrderRouter`, a real
    :class:`~trading_bot.storage.sqlite_store.SqliteStore` attached to the
    bus), built by hand here to inject the fake spec resolver and keep the
    scenario fully offline.
    """
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={
            "USDT": money("100000000"),
            "BTC": money("0"),
            "ETH": money("0"),
            "SOL": money("0"),
            "ADA": money("1000"),
        },
        event_bus=bus,
        strict=True,
    )
    router = OrderRouter(broker, bus)
    store = SqliteStore(db_path)
    store.attach(bus)
    return bus, tracker, router, store


def _capture(bus: EventBus, cls: type) -> list:  # type: ignore[type-arg]
    """Subscribe to ``bus`` and collect every emitted instance of ``cls``."""
    events: list = []  # type: ignore[type-arg]

    def _handler(event: object) -> None:
        if isinstance(event, cls):
            events.append(event)

    bus.subscribe(_handler)
    return events


# --- the scenario ------------------------------------------------------------ #


async def test_two_tick_rebalance_never_submits_below_venue_minimum(tmp_path) -> None:  # noqa: ANN001
    """A rebalance never submits a sub-minimum order; dust skipped, close-to-min
    rounded, spot sells capped — locking the whole resolver/policy/strict-paper
    chain (see the module docstring for the full scenario).
    """
    bus, tracker, router, store = _strict_paper_engine(tmp_path / "scratch.sqlite")
    order_events = _capture(bus, OrderEvent)
    log_events = _capture(bus, LogEvent)

    # Pre-seed ADA's held long (100 ADA) via a direct broker-confirmed fill on
    # the bus -- NOT one of the two graded rebalance ticks -- so tick 1 already
    # has a long to cap a sell against.
    seed = Fill(
        fill_id="SEED-1",
        client_order_id="seed-ada",
        instrument=Instrument(ADA),
        side=OrderSide.BUY,
        qty=money("100"),
        price=ADA_PRICE,
        fee=money("0"),
        ts=0,
    )
    bus.emit(FillEvent(fill=seed))
    seeded = tracker.position(Instrument(ADA))
    assert seeded is not None and seeded.net_qty == Decimal("100")

    # Weights crafted so ONE tick produces all four leg types:
    #   BTC -> target 1 BTC          (well above its venue minimum: submit)
    #   ETH -> target 0.006 ETH      (within min_order_ratio of 0.01: round_up)
    #   SOL -> target 0.01 SOL       (far below its 0.05 notional minimum: skip)
    #   ADA -> target -2000 ADA      (oversells the held +100: capped submit)
    weights = {
        BTC: money("0.5"),
        ETH: money("0.00015"),
        SOL: money("0.00001"),
        ADA: money("-1"),
    }
    strategy = PortfolioStrategy(
        name="e2e",
        universe=UNIVERSE,
        signal_fn=_weights_signal(weights),
        capital=CAPITAL,
    )
    runner = PortfolioRunner(
        strategy,
        [],  # feed unused: .rebalance() is driven directly, tick by tick
        router,
        tracker,
        event_bus=bus,
        spec_resolver=_FakeResolver(SPECS),
        exchange="binance",
    )

    # --- tick 1: above-min + round-up + dust + spot-sell-cap, all at once --- #
    result_1 = await runner.rebalance(_frames())
    store.flush()

    assert result_1.failed == 0
    assert result_1.submitted == 3  # BTC, ETH (rounded up), ADA (capped) -- SOL skipped

    stored_1 = {o.client_order_id: o for o in store.orders()}
    assert set(stored_1) == {"e2e-BTC/USDT-0", "e2e-ETH/USDT-0", "e2e-ADA/USDT-0"}

    btc_order = stored_1["e2e-BTC/USDT-0"]
    eth_order = stored_1["e2e-ETH/USDT-0"]
    ada_order = stored_1["e2e-ADA/USDT-0"]

    assert btc_order.side is OrderSide.BUY and btc_order.qty == Decimal("1")
    # The round-up leg's stored qty is the EXACT venue minimum, not a hair more.
    assert eth_order.side is OrderSide.BUY and eth_order.qty == Decimal("0.01")
    # The capped sell's qty is exactly the held quantity -- never an oversell.
    assert ada_order.side is OrderSide.SELL and ada_order.qty == Decimal("100")

    # Every order that reached the store clears its venue minimum, lot-quantized.
    for order in stored_1.values():
        spec = SPECS[order.instrument.symbol]
        assert spec.quantize_qty(order.qty) == order.qty, (
            f"{order.client_order_id} qty {order.qty} is not on the lot grid"
        )
        if spec.min_qty is not None:
            assert order.qty >= spec.min_qty
        if spec.min_notional is not None and order.limit_price is not None:
            assert order.qty * order.limit_price >= spec.min_notional

    # The dust leg: no order, exactly one info LogEvent naming it.
    assert "e2e-SOL/USDT-0" not in stored_1
    dust_logs_1 = [
        e for e in log_events if "SOL/USDT" in e.message and "skipped" in e.message
    ]
    assert len(dust_logs_1) == 1
    assert dust_logs_1[0].level == "info"

    bump_logs_1 = [e for e in log_events if "rounded up" in e.message]
    assert len(bump_logs_1) == 1
    assert bump_logs_1[0].level == "info"

    # Strict paper + the upstream policy: zero rejects reachable during the run.
    assert all(e.order.reject_reason is None for e in order_events)

    # --- tick 2: same weights, same prices -> convergence, no flip-flop ----- #
    logs_before_tick2 = len(log_events)
    result_2 = await runner.rebalance(_frames())
    store.flush()

    assert result_2.failed == 0
    assert result_2.submitted == 1  # only ADA continues; nothing else resubmits

    stored_2 = {o.client_order_id: o for o in store.orders()}
    # BTC is already on target (delta 0: no leg at all, not even a log).
    assert "e2e-BTC/USDT-1" not in stored_2
    # ETH's residual (target 0.006 vs the rounded-up 0.01 held) is itself dust:
    # it settles into skip -- it does NOT re-trigger another round_up, nor does
    # it flip to submit.
    assert "e2e-ETH/USDT-1" not in stored_2
    # SOL's dust leg is stable: still skip, not a flip to submit.
    assert "e2e-SOL/USDT-1" not in stored_2

    ada_order_2 = stored_2["e2e-ADA/USDT-1"]
    # ADA is now flat (fully sold in tick 1) so the spot-sell cap no longer
    # applies -- it continues selling toward the original target, on the SAME
    # side as tick 1 (progress, never a flip to BUY).
    assert ada_order_2.side is OrderSide.SELL
    assert ada_order.side is ada_order_2.side
    assert ada_order_2.qty == Decimal("2000")

    tick2_logs = log_events[logs_before_tick2:]
    sol_skips_2 = [
        e for e in tick2_logs if "SOL/USDT" in e.message and "skipped" in e.message
    ]
    assert len(sol_skips_2) == 1  # dust remains dust, no oscillation

    eth_skips_2 = [
        e for e in tick2_logs if "ETH/USDT" in e.message and "skipped" in e.message
    ]
    assert len(eth_skips_2) == 1  # settles into skip, exactly once

    bump_logs_2 = [e for e in tick2_logs if "rounded up" in e.message]
    assert bump_logs_2 == []  # never a second round-up on the same leg

    # Zero rejects across the WHOLE run (both ticks).
    assert all(e.order.reject_reason is None for e in order_events)

    store.close()
