"""Tests for the triptych entrypoint — :func:`run_app` / :func:`build_runners`.

These prove the entrypoint's contract — *one config runs the whole declared
system* — fully **offline**: a 2-strategy :class:`AppConfig`, a **fake dccd
client** returning canned OHLC bars, the default
:class:`~trading_bot.brokers.paper.PaperBroker` (the engine's real data path),
and no network.

What is verified
----------------
* **multi-strategy run** — both strategies run concurrently through the
  :class:`~trading_bot.application.orchestrator.Orchestrator`, each submits
  orders, and the :class:`~trading_bot.application.run_app.RunReport` reports per
  strategy + the aggregate PnL / fees;
* **independent positions / PnL = the fills** — each strategy's final position
  and the aggregate realised PnL are recomputed **independently** from the
  broker's own fills (``Position.from_fills``), the PnL source of truth, and must
  agree exactly;
* **signal resolution** — a builtin name (``ma_crossover`` + ``params``) *and* a
  ``module:function`` reference both resolve to a usable ``SignalFn``;
* **config errors** — a strategy missing its ``signal`` or its ``data`` source is
  a clear :class:`~trading_bot.domain.errors.ConfigError`.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import polars as pl
import pytest

from trading_bot.application.config import AppConfig
from trading_bot.application.run_app import (
    RunReport,
    build_runners,
    run_app,
)
from trading_bot.application.service_factory import build_engine
from trading_bot.domain.errors import ConfigError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.position import Position
from trading_bot.domain.signal import Signal

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


# --- a fake dccd client (canned bars, no network) -------------------------- #


def _dccd_ohlc(closes: list[float], *, start_ns: int = 0, span_s: int = 60) -> pl.DataFrame:
    """Build a canned dccd OHLC frame (dccd columns) from a list of closes.

    dccd's ``read`` returns ``TS, open, high, low, close, volume, ...`` with
    ``TS`` in nanoseconds; :class:`DccdFeed` normalises that to the bars schema.
    Only ``close`` drives the MA-crossover signal; ``o/h/l`` track it.
    """
    n = len(closes)
    span_ns = span_s * 1_000_000_000
    ts = [start_ns + i * span_ns for i in range(n)]
    return pl.DataFrame(
        {
            "TS": ts,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1.0] * n,
            "quote_volume": [c for c in closes],
            "trades": [1] * n,
        }
    )


class _FakeDccdClient:
    """A canned, offline dccd client: ``read`` returns a per-symbol OHLC frame.

    Keyed by the ``symbol`` string ``feed_for`` passes through (the strategy's
    ``symbol``), so each strategy reads its own series. Never touches a network
    and needs no dccd install — it satisfies the
    :class:`~trading_bot.application.data_provider.DccdClient` protocol
    structurally.
    """

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames
        self.reads: list[tuple[str, str]] = []

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        self.reads.append((exchange, symbol))
        return self._frames[symbol]

    def backfill(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start: str = "last",
    ) -> None:  # pragma: no cover - not exercised (backfill=False)
        return None


def custom_flat_signal(bars: pl.DataFrame) -> Signal:
    """A module:function signal: always flat for ETH/USD (tests the import path)."""
    return Signal.exposure(ETH_USD, money("0"), ts=0)


# --- the canonical 2-strategy offline config ------------------------------- #


def _trend_up_then_down(n_up: int = 20, n_down: int = 20, *, base: float = 100.0) -> list[float]:
    """A close series that trends up then down (crosses an MA both ways)."""
    up = [base + i for i in range(n_up)]
    top = base + n_up - 1
    down = [top - i for i in range(1, n_down + 1)]
    return up + down


def _two_strategy_config() -> AppConfig:
    """A realistic 2-strategy paper config: BTC + ETH MA-crossover, dccd feeds."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
                {
                    "name": "eth-ma",
                    "symbol": "ETH/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 4, "slow": 8}},
                    "reference_qty": "3",
                    "lookback": 8,
                },
            ],
        }
    )


def _fake_client_for(config: AppConfig) -> _FakeDccdClient:
    """A fake dccd client serving each strategy a distinct trend-up-then-down."""
    return _FakeDccdClient(
        {
            "BTC/USD": _dccd_ohlc(_trend_up_then_down(base=100.0)),
            "ETH/USD": _dccd_ohlc(_trend_up_then_down(base=50.0)),
        }
    )


# --- the multi-strategy end-to-end run ------------------------------------- #


async def test_run_app_two_strategies_independent_positions_and_pnl() -> None:
    """`run_app` over a 2-strategy config: both trade, positions/PnL = the fills.

    Verification on real data: the engine's reported per-strategy positions and
    the aggregate realised PnL are recomputed **independently** from the paper
    broker's own fills (``Position.from_fills`` per instrument) and must agree
    exactly — fills are the PnL source of truth.
    """
    pytest.importorskip("fynance")  # ma_crossover signals evaluate fynance.sma
    config = _two_strategy_config()
    client = _fake_client_for(config)

    # Build the engine ourselves so we can read the broker's fills back after the
    # run, then drive the same engine the entrypoint would (build_runners + orch).
    report = await run_app(config, dccd_client=client)

    assert isinstance(report, RunReport)
    assert len(report.strategies) == 2
    names = {s.name for s in report.strategies}
    assert names == {"btc-ma", "eth-ma"}

    # Both strategies actually traded (the trend crosses their MAs each way).
    by_name = {s.name: s for s in report.strategies}
    assert by_name["btc-ma"].orders_submitted > 0
    assert by_name["eth-ma"].orders_submitted > 0
    assert report.total_orders == sum(s.orders_submitted for s in report.strategies)

    # Positions are independent per instrument.
    assert by_name["btc-ma"].instrument == BTC_USD
    assert by_name["eth-ma"].instrument == ETH_USD
    assert by_name["btc-ma"].position is not None
    assert by_name["eth-ma"].position is not None

    # Each strategy read its OWN symbol's series (independent feeds).
    read_symbols = {sym for _exch, sym in client.reads}
    assert read_symbols == {"BTC/USD", "ETH/USD"}


async def test_run_app_positions_match_independent_fill_computation() -> None:
    """Each strategy's position == ``Position.from_fills`` over its own fills.

    Drives the engine directly (so we hold the broker) and compares every
    reported per-strategy position to an independent fold of exactly that
    instrument's broker fills.
    """
    pytest.importorskip("fynance")  # ma_crossover signals evaluate fynance.sma
    config = _two_strategy_config()
    client = _fake_client_for(config)

    engine = build_engine(config, db_path=None)
    runners = build_runners(config, engine, dccd_client=client)
    assert len(runners) == 2

    from trading_bot.application.orchestrator import Orchestrator

    orch = Orchestrator(event_bus=engine.bus)
    orch.add_all(runners)
    results = await orch.run()

    # The broker is the paper broker; read its confirmed fills (source of truth).
    fills = await engine.broker.fills()
    assert fills, "the run should have produced fills"

    by_instrument: dict[Instrument, list[Fill]] = {}
    for fill in fills:
        by_instrument.setdefault(fill.instrument, []).append(fill)

    # Independent per-instrument position fold must equal the engine's tracker.
    for instrument, inst_fills in by_instrument.items():
        expected = Position.from_fills(inst_fills)
        tracked = engine.tracker.position(instrument)
        assert tracked is not None
        assert tracked.net_qty == expected.net_qty
        assert tracked.realised_pnl == expected.realised_pnl
        assert tracked.fees_paid == expected.fees_paid

    # Aggregate realised PnL = sum over instruments of the fills-based PnL.
    expected_total = sum(
        (Position.from_fills(fs).realised_pnl for fs in by_instrument.values()),
        money("0"),
    )
    assert engine.perf.realised_pnl() == expected_total

    # And both runners completed (the orchestrator returned a count for each).
    assert set(results) == set(runners)


async def test_run_app_module_function_signal_resolves() -> None:
    """A ``module:function`` signal reference resolves and runs (no exec sink)."""
    config = AppConfig.model_validate(
        {
            "mode": "paper",
            "strategies": [
                {
                    "name": "eth-custom",
                    "symbol": "ETH/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {
                        "ref": "trading_bot.tests.application.test_run_app:custom_flat_signal"
                    },
                    "reference_qty": "1",
                }
            ],
        }
    )
    client = _FakeDccdClient({"ETH/USD": _dccd_ohlc(_trend_up_then_down(base=50.0))})

    report = await run_app(config, dccd_client=client)

    # The custom signal is always flat → no order, flat (None) position.
    assert len(report.strategies) == 1
    assert report.strategies[0].orders_submitted == 0
    assert report.strategies[0].position is None


async def test_run_app_max_steps_caps_each_runner() -> None:
    """`max_steps` bounds every runner's feed (fewer/zero orders than uncapped)."""
    config = _two_strategy_config()
    client = _fake_client_for(config)

    # Cap below the warmup lookback so no order can fire — a clean, deterministic
    # bound (the feed is truncated to 3 windows, both lookbacks are >= 6).
    report = await run_app(config, dccd_client=client, max_steps=3)

    assert report.total_orders == 0
    for strat in report.strategies:
        assert strat.orders_submitted == 0


# --- config errors --------------------------------------------------------- #


def test_build_runners_missing_signal_is_config_error() -> None:
    """A strategy with no ``signal`` raises a clear :class:`ConfigError`."""
    config = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "no-signal",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                }
            ]
        }
    )
    engine = build_engine(config, db_path=None)
    with pytest.raises(ConfigError, match="no signal"):
        build_runners(config, engine, dccd_client=_FakeDccdClient({}))


def test_build_runners_missing_data_is_config_error() -> None:
    """A strategy with no ``data`` source raises a clear :class:`ConfigError`."""
    config = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "no-data",
                    "symbol": "BTC/USD",
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                }
            ]
        }
    )
    engine = build_engine(config, db_path=None)
    with pytest.raises(ConfigError, match="no data source"):
        build_runners(config, engine, dccd_client=_FakeDccdClient({}))


def test_build_runners_unknown_builtin_signal_is_config_error() -> None:
    """An unknown builtin signal name raises a clear :class:`ConfigError`."""
    config = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "bad-builtin",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "does_not_exist"},
                }
            ]
        }
    )
    engine = build_engine(config, db_path=None)
    with pytest.raises(ConfigError, match="unknown builtin signal"):
        build_runners(config, engine, dccd_client=_FakeDccdClient({}))


def test_build_runners_bad_builtin_params_is_config_error() -> None:
    """Builtin params the factory rejects (fast >= slow) → a :class:`ConfigError`."""
    config = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "bad-params",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 10, "slow": 5}},
                }
            ]
        }
    )
    engine = build_engine(config, db_path=None)
    with pytest.raises(ConfigError, match="cannot build builtin signal"):
        build_runners(config, engine, dccd_client=_FakeDccdClient({}))


# --- same-instrument commingling: detect & reject -------------------------- #


def _same_symbol_config(second_symbol: str = "BTC/USD") -> AppConfig:
    """A 2-strategy config where both strategies trade the same instrument."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "strategies": [
                {
                    "name": "btc-fast",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "1",
                },
                {
                    "name": "btc-slow",
                    "symbol": second_symbol,
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 4, "slow": 8}},
                    "reference_qty": "1",
                },
            ],
        }
    )


def test_build_runners_same_symbol_is_config_error() -> None:
    """Two strategies on the same symbol → a clear :class:`ConfigError`.

    The shared per-instrument tracker / performance view has no per-strategy
    attribution, so two strategies on one instrument would commingle. The config
    is rejected up front, naming the duplicated symbol and both strategies.
    """
    config = _same_symbol_config()
    engine = build_engine(config, db_path=None)
    with pytest.raises(ConfigError) as exc_info:
        build_runners(config, engine, dccd_client=_FakeDccdClient({}))

    msg = str(exc_info.value)
    assert "BTC/USD" in msg
    assert "btc-fast" in msg
    assert "btc-slow" in msg
    assert "commingle" in msg


def test_build_runners_equivalent_symbol_spellings_commingle() -> None:
    """Two spellings of the *same* instrument (BTC/USD vs XBT/USD) are caught.

    Detection is by the normalised :class:`Symbol`, not the raw string, so a
    Kraken-style ``XBT`` alias of ``BTC`` does not slip past the guard.
    """
    config = _same_symbol_config(second_symbol="XBT/USD")
    engine = build_engine(config, db_path=None)
    with pytest.raises(ConfigError, match="commingle"):
        build_runners(config, engine, dccd_client=_FakeDccdClient({}))


def test_run_app_same_symbol_rejected() -> None:
    """`run_app` surfaces the same-symbol commingling as a :class:`ConfigError`."""
    import asyncio

    config = _same_symbol_config()
    with pytest.raises(ConfigError, match="commingle"):
        asyncio.run(run_app(config, dccd_client=_FakeDccdClient({})))


def test_build_runners_distinct_symbols_build_fine() -> None:
    """Distinct symbols build a runner each — the guard only rejects duplicates."""
    config = _two_strategy_config()
    engine = build_engine(config, db_path=None)
    runners = build_runners(config, engine, dccd_client=_fake_client_for(config))
    assert len(runners) == 2
    instruments = {r.strategy.instrument for r in runners}
    assert instruments == {BTC_USD, ETH_USD}


def test_run_app_empty_config_runs_no_strategies() -> None:
    """A config with no strategies yields an empty, well-formed report."""
    import asyncio

    config = AppConfig()
    report = asyncio.run(run_app(config))
    assert report.strategies == []
    assert report.total_orders == 0
    assert report.realised_pnl == money("0")


# --- reconcile-on-startup (the "reconcile, don't assume" invariant) -------- #


async def test_run_app_reconciles_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_app` reconciles the engine to the broker before running.

    Spies the ``reconcile`` the entrypoint calls and asserts it is awaited exactly
    once with the engine's own broker / router / tracker (the wired collaborators)
    — so a restart converges to the venue's truth *before* the first order.
    ``reconcile``'s actual convergence is proven in ``tests/hardening/``; this
    asserts the wiring.
    """
    import importlib

    run_app_mod = importlib.import_module("trading_bot.application.run_app")
    from trading_bot.application.order_router import OrderRouter
    from trading_bot.application.position_tracker import PositionTracker
    from trading_bot.application.reconcile import ReconResult
    from trading_bot.brokers.paper import PaperBroker

    calls: list[tuple[object, object, object]] = []

    async def _spy(broker, router, tracker, *, since_ms=None, event_bus=None):  # noqa: ANN001, ANN202
        calls.append((broker, router, tracker))
        return ReconResult(0, 0, 0, 0, 0)

    monkeypatch.setattr(run_app_mod, "reconcile", _spy)

    await run_app(AppConfig())  # paper, no strategies

    assert len(calls) == 1
    broker, router, tracker = calls[0]
    assert isinstance(broker, PaperBroker)
    assert isinstance(router, OrderRouter)
    assert isinstance(tracker, PositionTracker)


async def test_run_app_can_skip_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reconcile_on_start=False`` skips the startup reconcile (opt-out honoured)."""
    import importlib

    run_app_mod = importlib.import_module("trading_bot.application.run_app")

    calls: list[object] = []

    async def _spy(*a: object, **k: object) -> None:
        calls.append(a)

    monkeypatch.setattr(run_app_mod, "reconcile", _spy)

    await run_app(AppConfig(), reconcile_on_start=False)
    assert calls == []


async def test_run_app_startup_reconcile_converges_to_venue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a restart (empty engine, venue ahead) converges via ``run_app``.

    Verification on real broker state: a :class:`PaperBroker` is left holding an
    open, partially-filled order whose fill the freshly-built tracker never saw
    (the order is placed **directly** on the broker, before the tracker subscribes
    — modelling a restart where the engine maps are empty but the venue is ahead).
    With the default ``reconcile_on_start=True``, ``run_app`` must ingest the
    venue-open order into the router and rebuild the BTC position from the venue's
    confirmed fill before the (strategy-less) run.
    """
    import importlib

    run_app_mod = importlib.import_module("trading_bot.application.run_app")
    from trading_bot.application.events import EventBus
    from trading_bot.application.order_router import OrderRouter
    from trading_bot.application.performance_service import PerformanceService
    from trading_bot.application.position_tracker import PositionTracker
    from trading_bot.application.risk import RiskManager
    from trading_bot.application.service_factory import Engine
    from trading_bot.brokers.paper import PaperBroker
    from trading_bot.domain.order import Order, OrderSide, OrderType

    bus = EventBus()
    broker = PaperBroker(
        prices={BTC_USD: money("30000")},
        starting_balances={"USD": money("100000000")},
        event_bus=bus,
    )
    # Leave an open, partially-filled order ON THE VENUE — placed directly on the
    # broker so the not-yet-built engine tracker never sees its fill.
    broker.arm_partial(money("0.5"))
    await broker.place_order(
        Order(
            client_order_id="pre-existing",
            instrument=BTC_USD,
            side=OrderSide.BUY,
            qty=money("4"),
            type=OrderType.LIMIT,
            limit_price=money("30000"),
        )
    )

    # Now wire a FRESH (empty) engine over that same broker/bus — the restart: the
    # tracker subscribes only now, after the fill above was already emitted.
    config = AppConfig()
    tracker = PositionTracker(event_bus=bus)
    perf = PerformanceService(v0=config.starting_capital, event_bus=bus)
    risk = RiskManager(config.risk, position_tracker=tracker)
    router = OrderRouter(broker, bus, risk_manager=risk)
    engine = Engine(
        config=config,
        bus=bus,
        broker=broker,
        router=router,
        tracker=tracker,
        perf=perf,
        risk=risk,
        store=None,
    )
    assert router.tracked_orders() == {}
    assert tracker.all_positions() == {}

    monkeypatch.setattr(
        run_app_mod, "build_engine", lambda cfg, db_path=None: engine
    )
    await run_app(config)  # reconcile_on_start defaults True

    # The venue-open order is now tracked (ingested), and the BTC position was
    # rebuilt from the venue's confirmed fill — the engine converged to the venue.
    assert router.get("pre-existing") is not None
    assert BTC_USD in tracker.all_positions()
