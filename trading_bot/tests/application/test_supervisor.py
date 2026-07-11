"""Tests for the :class:`StrategySupervisor` — per-strategy lifecycle + modes.

Offline: a paper config + a fake dccd client (canned bars). Proves the supervisor
splits a config into independently-managed units, starts/steps/stops them in their
own engine, switches modes (paper ↔ testnet), and **gates real money** (``live``
needs an explicit confirmation). Async tests run un-decorated (``asyncio_mode =
"auto"``).
"""

from __future__ import annotations

import asyncio

import polars as pl
import pytest

from trading_bot.application.accounting import Violation
from trading_bot.application.config import (
    AppConfig,
    DataSourceConfig,
    PortfolioStrategyConfig,
    SignalRefConfig,
    StrategyConfig,
)
from trading_bot.application.events import FillEvent, LogEvent
from trading_bot.application.strategy_runner import StrategyRunner
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.domain.capital import CapitalEvent, CapitalEventType
from trading_bot.domain.errors import (
    ConfigError,
    LiveCapitalOpsDeferred,
    LiveTradingNotEnabled,
    WithdrawalTooLarge,
)
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderStatus, OrderType
from trading_bot.storage.sqlite_store import SqliteStore


def _dccd_ohlc(closes: list[float], *, span_s: int = 60) -> pl.DataFrame:
    span_ns = span_s * 1_000_000_000
    ts = [i * span_ns for i in range(len(closes))]
    return pl.DataFrame(
        {
            "TS": ts,
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1.0] * len(closes),
            "quote_volume": list(closes),
            "trades": [1] * len(closes),
        }
    )


class _FakeDccdClient:
    """A canned offline dccd client keyed by symbol (no network)."""

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames

    def read(
        self, exchange, symbol, data_type="ohlc", span=None, start_ns=None, end_ns=None
    ):  # noqa: ANN001, ANN201
        return self._frames[symbol]

    def backfill(self, *a, **k):  # noqa: ANN002, ANN003, ANN201  # pragma: no cover
        return None


def _trend() -> list[float]:
    """A close series that trends up then down (the MA crosses both ways)."""
    return [100.0 + i for i in range(20)] + [119.0 - i for i in range(1, 21)]


def _config(*, with_broker: bool = True) -> AppConfig:
    """A paper config: one BTC/USD MA-crossover strategy (+ an optional broker)."""
    raw: dict = {
        "mode": "paper",
        "strategies": [
            {
                "name": "btc-ma",
                "symbol": "BTC/USD",
                "data": {"exchange": "kraken", "span": 60},
                "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                "reference_qty": "2",
                "lookback": 6,
            }
        ],
    }
    if with_broker:
        raw["brokers"] = [{"name": "kraken", "exchange": "kraken"}]
    return AppConfig.model_validate(raw)


def _supervisor() -> StrategySupervisor:
    client = _FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    return StrategySupervisor(_config(), dccd_client=client)


def test_splits_config_into_units() -> None:
    """The supervisor splits the config into one (stopped, paper) unit per strategy."""
    sup = _supervisor()
    assert sup.names() == ["btc-ma"]
    [status] = sup.status()
    assert status.name == "btc-ma"
    assert status.kind == "strategy"
    assert status.mode == "paper"
    assert status.running is False
    assert status.realised_pnl is None


async def test_start_step_stop_lifecycle() -> None:
    """start builds a per-unit engine; step re-evaluates over latest data; stop tears down."""
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    sup = _supervisor()

    await sup.start("btc-ma")
    assert sup.status("btc-ma")[0].running is True

    # One re-evaluation over the latest (trend-down tail → short) data trades.
    order = await sup.step("btc-ma")
    assert order is not None  # delta != 0 from flat → an order was routed

    await sup.stop("btc-ma")
    status = sup.status("btc-ma")[0]
    assert status.running is False
    # Stopped → nothing to step.
    assert await sup.step("btc-ma") is None


async def test_set_mode_paper_testnet_roundtrip() -> None:
    """paper ↔ testnet switch needs no confirmation and updates the unit's mode."""
    sup = _supervisor()
    await sup.set_mode("btc-ma", "testnet")
    assert sup.status("btc-ma")[0].mode == "testnet"
    await sup.set_mode("btc-ma", "paper")
    assert sup.status("btc-ma")[0].mode == "paper"


async def test_set_mode_live_requires_explicit_confirmation() -> None:
    """Switching to live (real money) without confirmation is refused."""
    sup = _supervisor()
    with pytest.raises(LiveTradingNotEnabled):
        await sup.set_mode("btc-ma", "live")  # no confirm → refused
    assert sup.status("btc-ma")[0].mode == "paper"  # unchanged

    # With the deliberate acknowledgement the mode flips (the engine is only built
    # on start, which still enforces credentials + risk limits).
    await sup.set_mode("btc-ma", "live", confirm_live=True)
    assert sup.status("btc-ma")[0].mode == "live"


async def test_testnet_without_a_broker_is_refused() -> None:
    """A paper-only unit with no configured broker cannot go testnet/live.

    And a refused switch changes **nothing**: the mode is validated (sliced) before
    the unit is mutated, so a ConfigError leaves the unit on its previous mode.
    """
    sup = StrategySupervisor(
        _config(with_broker=False),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    with pytest.raises(ConfigError, match="no matching broker"):
        await sup.set_mode("btc-ma", "testnet")
    # The refused switch left the unit on paper (config-validation is atomic).
    assert sup.status("btc-ma")[0].mode == "paper"


def test_status_includes_the_strategys_exchange() -> None:
    """Each unit reports the exchange it's for (a strategy's ``data.exchange``)."""
    sup = _supervisor()
    assert sup.status("btc-ma")[0].exchange == "kraken"


async def test_set_mode_refused_when_no_broker_for_that_exchange() -> None:
    """testnet/live needs a broker **matching the unit's exchange**, not just any broker.

    The strategy is on Kraken (`data.exchange`), but only a Binance broker is
    configured — switching it to testnet must be refused (per-exchange routing).
    """
    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "brokers": [{"name": "bn", "exchange": "binance"}],  # no kraken broker
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                }
            ],
        }
    )
    sup = StrategySupervisor(
        cfg, dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    )
    assert sup.status("btc-ma")[0].exchange == "kraken"
    with pytest.raises(ConfigError, match="no matching broker"):
        await sup.set_mode("btc-ma", "testnet")


async def test_unknown_strategy_is_a_config_error() -> None:
    """Operating on an unknown name raises a clear error."""
    sup = _supervisor()
    with pytest.raises(ConfigError, match="unknown strategy"):
        await sup.start("nope")


async def test_start_all_step_all_shutdown() -> None:
    """The daemon's boot/tick/teardown: start every unit, step the running ones, stop."""
    pytest.importorskip("fynance")
    sup = _supervisor()

    await sup.start_all()
    assert all(s.running for s in sup.status())

    stepped = await sup.step_all()
    assert stepped == 1  # the one running unit stepped once

    await sup.shutdown()
    assert not any(s.running for s in sup.status())
    assert await sup.step_all() == 0  # nothing running → nothing stepped


# --- aggregate read accessors (Overview page) ------------------------------ #


def _two_venue_config() -> AppConfig:
    """A paper config with two strategies, on Kraken and on Binance."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "brokers": [
                {"name": "kraken", "exchange": "kraken"},
                {"name": "binance", "exchange": "binance"},
            ],
            "strategies": [
                {
                    "name": "btc-kraken",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
                {
                    "name": "eth-binance",
                    "symbol": "ETH/USDT",
                    "data": {"exchange": "binance", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
            ],
        }
    )


def _two_venue_client() -> _FakeDccdClient:
    """Offline dccd client for the two-venue config's symbols.

    Injected so `start()` never imports the real dccd (absent in CI). The bars are
    only read on a `step`; these KPI/positions tests seed fills directly, so canned
    data suffices.
    """
    return _FakeDccdClient(
        {"BTC/USD": _dccd_ohlc(_trend()), "ETH/USDT": _dccd_ohlc(_trend())}
    )


def _seed_fills(sup: StrategySupervisor, name: str, symbol: Symbol) -> None:
    """Emit a buy→sell round trip on the running unit's engine bus.

    Drives the unit's own tracker + performance service (both subscribed to the
    engine bus) exactly as a broker's confirmed fills would — the aggregate
    accessors then reflect that engine truth.
    """
    inst = Instrument(symbol)
    bus = sup._units[name].engine.bus  # noqa: SLF001 — seed the wired bus
    bus.emit(
        FillEvent(
            Fill(
                f"{name}-F1",
                f"{name}-c1",
                inst,
                OrderSide.BUY,
                money("1"),
                money("100"),
                money("1"),
                1,
            )
        )
    )
    bus.emit(
        FillEvent(
            Fill(
                f"{name}-F2",
                f"{name}-c2",
                inst,
                OrderSide.SELL,
                money("1"),
                money("110"),
                money("1"),
                2,
            )
        )
    )


async def _seeded_two_venue_supervisor() -> StrategySupervisor:
    """Two running paper units (Kraken + Binance), each with a seeded round trip."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    _seed_fills(sup, "btc-kraken", Symbol("BTC", "USD"))
    _seed_fills(sup, "eth-binance", Symbol("ETH", "USDT"))
    return sup


async def test_kpi_strategy_level_has_one_row_per_unit() -> None:
    """`kpi("strategy")` returns a row per running unit with its own PnL + ratios."""
    sup = await _seeded_two_venue_supervisor()
    rows = sup.kpi("strategy")
    assert {r.strategy for r in rows} == {"btc-kraken", "eth-binance"}
    by_name = {r.strategy: r for r in rows}
    # Each round trip: +10 gross - 2 fees = +8 realised.
    assert by_name["btc-kraken"].realised_pnl == money("8")
    assert by_name["btc-kraken"].fees_paid == money("2")
    assert by_name["btc-kraken"].exchange == "kraken"
    # Per-strategy ratios are floats (computed off the unit's curve) when fynance is
    # available; they degrade to None without it (the dashboard stays functional).
    pytest.importorskip("fynance")
    assert isinstance(by_name["btc-kraken"].sharpe, float)


async def test_kpi_exchange_level_folds_per_venue() -> None:
    """`kpi("exchange")` folds units per venue (PnL/fees summed; ratios None)."""
    sup = await _seeded_two_venue_supervisor()
    rows = sup.kpi("exchange")
    by_venue = {r.exchange: r for r in rows}
    assert set(by_venue) == {"kraken", "binance"}
    assert by_venue["kraken"].realised_pnl == money("8")
    assert by_venue["binance"].realised_pnl == money("8")
    # Aggregate ratios are None (a combined curve lands in a later leaf).
    assert by_venue["kraken"].sharpe is None
    assert by_venue["kraken"].strategy is None


async def test_kpi_total_sums_all_units() -> None:
    """`kpi("total")` is a single row summing every unit (ratios None)."""
    sup = await _seeded_two_venue_supervisor()
    [total] = sup.kpi("total")
    assert total.key == "total"
    assert total.realised_pnl == money("16")  # 8 + 8
    assert total.fees_paid == money("4")  # 2 + 2
    assert total.sharpe is None
    assert total.exchange is None


async def test_positions_carry_strategy_and_exchange_tags() -> None:
    """`positions()` rows carry the owning strategy + its venue (group-by keys)."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    # Seed a net-long book (a buy only, no close) so the position is non-flat.
    inst = Instrument(Symbol("BTC", "USD"))
    sup._units["btc-kraken"].engine.bus.emit(  # noqa: SLF001
        FillEvent(
            Fill(
                "k-F1",
                "k-c1",
                inst,
                OrderSide.BUY,
                money("3"),
                money("100"),
                money("1"),
                1,
            )
        )
    )
    rows = sup.positions()
    [row] = rows
    assert row.strategy == "btc-kraken"
    assert row.exchange == "kraken"
    assert row.instrument == "BTC/USD"
    assert row.base == "BTC"
    assert row.net_qty == money("3")


async def test_open_orders_carry_strategy_and_exchange_tags() -> None:
    """`open_orders()` rows are tagged with strategy + exchange across the units."""
    pytest.importorskip("fynance")
    client = _FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    sup = StrategySupervisor(_config(), dccd_client=client)
    await sup.start("btc-ma")
    await sup.step("btc-ma")  # routes an order into the unit's router
    rows = sup.open_orders()
    # The paper broker fills market orders immediately (terminal), so there may be
    # no *open* order; but every row that exists must carry the tags.
    for row in rows:
        assert row.strategy == "btc-ma"
        assert row.exchange == "kraken"


def test_aggregate_accessors_empty_when_nothing_running() -> None:
    """An all-stopped supervisor aggregates to empty lists (total is a zero row)."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    assert sup.positions() == []
    assert sup.open_orders() == []
    assert sup.kpi("strategy") == []
    assert sup.kpi("exchange") == []
    [total] = sup.kpi("total")  # total is always one row, even when empty
    assert total.realised_pnl == money("0")


def test_kpi_rejects_an_unknown_level() -> None:
    """An unknown KPI level is a clear ValueError (the API maps it to 422)."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    with pytest.raises(ValueError, match="unknown KPI level"):
        sup.kpi("bogus")  # type: ignore[arg-type]


# --- dynamic membership: add_unit / remove_unit / manifest ----------------- #


def _portfolio_entry(name: str = "demo1") -> PortfolioStrategyConfig:
    """A deployable portfolio entry pointing at an existing signal ref."""
    return PortfolioStrategyConfig(
        name=name,
        venue="binance",
        universe=["BTC/USDT", "ETH/USDT"],
        signal=SignalRefConfig(ref="strategies.demo.signal:portfolio_signal"),
        capital=money("100000"),
        data=DataSourceConfig(exchange="binance", span=86400),
    )


def test_add_unit_appends_a_stopped_unit() -> None:
    """`add_unit` deploys a new **stopped** unit reflected in status() + names()."""
    sup = _supervisor()
    name = sup.add_unit(_portfolio_entry())
    assert name == "demo1"
    assert sup.names() == ["btc-ma", "demo1"]
    [st] = [s for s in sup.status() if s.name == "demo1"]
    assert st.kind == "portfolio"
    assert st.exchange == "binance"
    assert st.running is False  # never auto-started (paper-safe)


def test_add_unit_rejects_a_duplicate_name() -> None:
    """A name already managed is a ConfigError; nothing is added."""
    sup = _supervisor()
    with pytest.raises(ConfigError, match="duplicate"):
        sup.add_unit(StrategyConfig(name="btc-ma", symbol="ETH/USD"))
    assert sup.names() == ["btc-ma"]  # unchanged


async def test_add_unit_bad_signal_ref_surfaces_on_start() -> None:
    """A deployment with an unimportable signal ref adds (paper-safe) but fails to start.

    Adding never resolves the signal (paper-safe, no import), so a bad
    ``module:function`` ref lands as a stopped unit; the clear error surfaces when
    it is *started* (where the runner resolves + imports the signal).
    """
    sup = _supervisor()
    sup.add_unit(
        PortfolioStrategyConfig(
            name="pf",
            venue="binance",
            universe=["BTC/USDT", "ETH/USDT"],
            signal=SignalRefConfig(ref="nonexistent.module:sig"),
            capital=money("100000"),
            data=DataSourceConfig(exchange="binance", span=86400),
        )
    )
    assert "pf" in sup.names()  # added stopped (deploy is paper-safe)
    with pytest.raises(ConfigError):
        await sup.start("pf")  # the runner resolves the ref here → clear error


def test_add_unit_non_paper_seed_needs_a_matching_broker() -> None:
    """A non-paper seed with no matching broker for the venue is rejected atomically.

    The base config is ``live`` with a Kraken broker only; deploying a Binance
    portfolio (no matching broker) must raise and leave the supervisor untouched.
    """
    base = AppConfig.model_validate(
        {
            "mode": "live",
            "live_enabled": True,
            "brokers": [{"name": "k", "exchange": "kraken"}],
        }
    )
    sup = StrategySupervisor(base)
    with pytest.raises(ConfigError, match="no matching broker"):
        sup.add_unit(_portfolio_entry())
    assert sup.names() == []  # nothing added; base restored


def test_manifest_reflects_the_current_units() -> None:
    """`manifest()` returns the AppConfig reconstructed from the live units."""
    sup = _supervisor()
    sup.add_unit(_portfolio_entry())
    man = sup.manifest()
    assert [s.name for s in man.strategies] == ["btc-ma"]
    assert [p.name for p in man.portfolios] == ["demo1"]


async def test_remove_unit_stops_and_drops() -> None:
    """`remove_unit` stops a running unit, drops it, and forgets its config."""
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    sup = _supervisor()
    await sup.start("btc-ma")
    assert sup.status("btc-ma")[0].running is True
    sup.remove_unit("btc-ma")
    assert sup.names() == []
    assert sup.manifest().strategies == []
    # It is truly gone — operating on it is now an unknown-strategy error.
    with pytest.raises(ConfigError, match="unknown strategy"):
        await sup.start("btc-ma")


def test_remove_unit_unknown_is_a_config_error() -> None:
    """Removing an unmanaged name is a clear ConfigError."""
    sup = _supervisor()
    with pytest.raises(ConfigError, match="unknown strategy"):
        sup.remove_unit("nope")


# --- paper start-replay: a persisted book survives a restart --------------- #


def _config_with_store(db_path: str) -> AppConfig:
    """A paper BTC/USD strategy whose engine persists to (and restores from) ``db_path``."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db_path},
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                }
            ],
        }
    )


def _seed_store(db_path: str) -> None:
    """Persist a buy→sell round trip on BTC/USD to a fresh store (+8 realised)."""
    from trading_bot.storage.sqlite_store import SqliteStore

    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db_path)
    store.record_fill(
        Fill("SF1", "sc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.record_fill(
        Fill(
            "SF2", "sc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2
        )
    )


async def test_paper_start_replays_the_stored_book(tmp_path) -> None:  # noqa: ANN001
    """A paper unit started over a seeded store restores its tracker + realised PnL.

    The end-to-end win: a freshly-built paper engine holds no venue state (its
    startup reconcile resets the tracker to empty), so without the replay a
    restarted paper unit would show an empty book. `start` replays the store's
    fills into the engine's tracker + performance service, so the position and
    realised PnL survive the restart.
    """
    db = str(tmp_path / "book.sqlite")
    _seed_store(db)  # a buy→sell round trip: net flat, +10 gross - 2 fees = +8

    sup = StrategySupervisor(
        _config_with_store(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")

    engine = sup._units["btc-ma"].engine  # noqa: SLF001
    inst = Instrument(Symbol("BTC", "USD"))
    position = engine.tracker.position(inst)
    assert position is not None  # the book was restored, not empty
    assert position.net_qty == money("0")  # bought 1, sold 1 → flat
    assert position.realised_pnl == money("8")  # +10 gross - 2 fees
    assert engine.perf.realised_pnl() == money("8")
    assert engine.perf.fees_paid() == money("2")
    # And the supervisor's status surfaces it.
    assert sup.status("btc-ma")[0].realised_pnl == money("8")


async def test_paper_start_replay_does_not_double_count_on_restart(tmp_path) -> None:  # noqa: ANN001
    """Stopping and re-starting a paper unit restores the same book (no double-count)."""
    db = str(tmp_path / "book.sqlite")
    _seed_store(db)
    sup = StrategySupervisor(
        _config_with_store(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")
    await sup.stop("btc-ma")
    await sup.start("btc-ma")  # a fresh engine, replays the same fills once

    engine = sup._units["btc-ma"].engine  # noqa: SLF001
    assert engine.perf.realised_pnl() == money("8")  # not 16
    assert engine.perf.fees_paid() == money("2")  # not 4


async def test_paper_start_replays_only_paper_tagged_fills(tmp_path) -> None:  # noqa: ANN001
    """A store with mixed-mode fills replays ONLY its paper fills into a paper engine.

    Fake / real money must never commingle into the paper simulator's book: a
    testnet round trip on the same instrument as a large open paper position would
    otherwise realise a spurious close against the wrong entry. The paper unit's
    replay filters on the storage `mode` tag, so its realised PnL is the paper
    fold alone.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    # A paper round trip: +8 realised.
    store.set_context(mode="paper", venue="")
    store.record_fill(
        Fill("PF1", "pc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.record_fill(
        Fill(
            "PF2", "pc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2
        )
    )
    # A testnet round trip on the SAME instrument (fake money — must be ignored by
    # the paper replay): would otherwise add +18.
    store.set_context(mode="testnet", venue="binance")
    store.record_fill(
        Fill("TF1", "tc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 3)
    )
    store.record_fill(
        Fill(
            "TF2", "tc2", inst, OrderSide.SELL, money("1"), money("120"), money("1"), 4
        )
    )

    sup = StrategySupervisor(
        _config_with_store(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")
    engine = sup._units["btc-ma"].engine  # noqa: SLF001
    # Only the paper fold — the testnet fills were not commingled.
    assert engine.perf.realised_pnl() == money("8")  # not 26
    assert engine.perf.fees_paid() == money("2")  # not 4


# --- pnl_series: per-mode realised-PnL / equity curve over time ------------- #


async def test_pnl_series_paper_matches_engine_truth(tmp_path) -> None:  # noqa: ANN001
    """A running paper unit's pnl_series folds its store's fills to the engine's truth.

    The load-bearing reconciliation: the derived curve's final equity equals
    `v0 + engine.perf.realised_pnl()` exactly (same fold, same v0), is non-empty,
    and monotonic in ts.
    """
    db = str(tmp_path / "book.sqlite")
    _seed_store(db)  # a buy→sell round trip: +8 realised
    sup = StrategySupervisor(
        _config_with_store(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")

    result = sup.pnl_series("btc-ma")
    v0 = result["v0"]
    series = result["series"]
    assert "paper" in series
    paper = series["paper"]
    assert paper  # non-empty
    # Monotonic in ts (ascending, time-ordered).
    ts_list = [row[0] for row in paper]
    assert ts_list == sorted(ts_list)
    # The final equity reconciles to the running engine's realised PnL exactly.
    engine = sup._units["btc-ma"].engine  # noqa: SLF001
    final_equity = paper[-1][2]
    assert final_equity == v0 + engine.perf.realised_pnl()
    # And v0 is the unit's configured starting capital.
    assert v0 == sup._units["btc-ma"].config.starting_capital  # noqa: SLF001


async def test_pnl_series_splits_live_and_testnet(tmp_path) -> None:  # noqa: ANN001
    """Fills tagged under two modes yield two separate series, each anchored at v0.

    Directly stores fills under two modes (paper→testnet is free; both fake
    money) so no venue/network is needed, then asserts pnl_series returns a
    per-mode split — testnet is never combined into the paper series.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    # Paper round trip: +8 realised.
    store.set_context(mode="paper", venue="")
    store.record_fill(
        Fill("PF1", "pc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.record_fill(
        Fill(
            "PF2", "pc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2
        )
    )
    # Testnet round trip on the same book (fake money, separate series): +18.
    store.set_context(mode="testnet", venue="binance")
    store.record_fill(
        Fill("TF1", "tc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 3)
    )
    store.record_fill(
        Fill(
            "TF2", "tc2", inst, OrderSide.SELL, money("1"), money("120"), money("1"), 4
        )
    )

    # Read via a stopped unit (reads the configured db_path store).
    sup = StrategySupervisor(
        _config_with_store(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    result = sup.pnl_series("btc-ma")
    v0 = result["v0"]
    series = result["series"]
    assert set(series) == {"paper", "testnet"}
    # Each mode folds from the SAME v0, independently.
    assert series["paper"][-1][2] == v0 + money("8")
    assert series["testnet"][-1][2] == v0 + money("18")
    # The current end points reflect each mode's equity.
    assert result["current"]["paper"]["equity"] == v0 + money("8")
    assert result["current"]["testnet"]["equity"] == v0 + money("18")


def test_pnl_series_unknown_strategy_raises() -> None:
    """pnl_series on an unmanaged name is a clear ConfigError (the API maps to 404)."""
    sup = _supervisor()
    with pytest.raises(ConfigError, match="unknown strategy"):
        sup.pnl_series("nope")


def test_pnl_series_no_fills_is_empty() -> None:
    """A unit that persists nothing (no db_path) has empty series (not an error)."""
    sup = _supervisor()  # the default config has no storage.db_path
    result = sup.pnl_series("btc-ma")
    assert result["series"] == {}
    assert result["current"] == {}
    assert result["v0"] == sup._units["btc-ma"].config.starting_capital  # noqa: SLF001


async def test_combined_equity_series_sums_v0_and_merges_fills(tmp_path) -> None:  # noqa: ANN001
    """combined_equity_series merges two strategies' paper fills, summing their v0."""
    from trading_bot.storage.sqlite_store import SqliteStore

    inst = Instrument(Symbol("BTC", "USD"))
    db_a = str(tmp_path / "a.sqlite")
    db_b = str(tmp_path / "b.sqlite")
    for db in (db_a, db_b):
        store = SqliteStore(db, mode="paper", venue="kraken")
        store.record_fill(
            Fill(
                f"{db}-F1",
                f"{db}-c1",
                inst,
                OrderSide.BUY,
                money("1"),
                money("100"),
                money("0"),
                1,
            )
        )
        store.record_fill(
            Fill(
                f"{db}-F2",
                f"{db}-c2",
                inst,
                OrderSide.SELL,
                money("1"),
                money("110"),
                money("0"),
                2,
            )
        )
    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "a",
                    "symbol": "BTC/USD",
                    "storage": {"db_path": db_a},  # ignored here — set per-unit below
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
            ],
            "storage": {"db_path": db_a},
        }
    )
    sup = StrategySupervisor(
        cfg, dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    )
    combined = sup.combined_equity_series(["a"], mode="paper")
    assert combined  # non-empty
    # Single strategy 'a': +10 gross (no fees) from its v0.
    v0 = sup._units["a"].config.starting_capital  # noqa: SLF001
    assert combined[-1][2] == v0 + money("10")


async def test_combined_equity_series_ignores_zero_fill_in_mode_units(  # noqa: ANN001
    tmp_path,
) -> None:
    """A unit with no fills in the mode contributes neither fills nor v0 (A-6).

    Two stopped paper units: 'traded' has a paper round trip persisted; 'idle' has
    an empty store. The combined equity anchor and returns path must reflect ONLY
    the traded unit's v0 — anchoring on the idle unit's capital would inflate the
    base and skew the aggregate KPI ratios.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    inst = Instrument(Symbol("BTC", "USD"))
    db_traded = str(tmp_path / "traded.sqlite")
    db_idle = str(tmp_path / "idle.sqlite")
    # 'traded': a +10 paper round trip. 'idle': an empty store (no fills at all).
    store = SqliteStore(db_traded, mode="paper", venue="kraken")
    store.record_fill(
        Fill("F1", "c1", inst, OrderSide.BUY, money("1"), money("100"), money("0"), 1)
    )
    store.record_fill(
        Fill("F2", "c2", inst, OrderSide.SELL, money("1"), money("110"), money("0"), 2)
    )
    SqliteStore(db_idle, mode="paper", venue="kraken")  # created empty

    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "starting_capital": "1000",  # each unit's equity anchor
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "traded",
                    "symbol": "BTC/USD",
                    "db_path": db_traded,  # per-unit isolated store (has fills)
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
                {
                    "name": "idle",
                    "symbol": "BTC/USD",
                    "db_path": db_idle,  # per-unit isolated store (empty)
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
            ],
            "storage": {"db_path": db_traded},
        }
    )
    sup = StrategySupervisor(
        cfg, dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    )

    combined = sup.combined_equity_series(["traded", "idle"], mode="paper")

    assert combined  # the traded unit produced a curve
    v0_traded = sup._units["traded"].config.starting_capital  # noqa: SLF001
    # Anchor = ONLY the traded unit's v0 (1000), not the sum of both units' v0
    # (2000). Final equity is v0 + 10 gross; the idle unit's v0 never enters the
    # base. Pre-fix, the aggregate anchored at 2000 (both v0s), inflating the base.
    assert combined[0][2] == v0_traded  # first point anchors on one v0, not two
    assert combined[-1][2] == v0_traded + money("10")

    # Regression guard: the idle unit alone yields an empty curve (nothing to fold).
    assert sup.combined_equity_series(["idle"], mode="paper") == []


# --- aggregate ratio KPIs (exchange / total on the combined curve) --------- #


def _kpi_ratio_supervisor(db_path: str) -> StrategySupervisor:
    """A running paper unit whose store holds a multi-fill book (a real curve).

    Records a spread of paper round trips to ``db_path`` (varied prices, so the
    derived equity curve has non-zero dispersion — a Sharpe/Sortino/Calmar is
    defined on it), then starts the unit so its engine reads that store. The unit
    is running, so ``combined_equity_series`` reads the live ``engine.store``.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db_path, mode="paper", venue="kraken")
    # A varied round-trip book (5 buys, 5 sells at different prices) so the equity
    # curve moves up and down — a ratio is defined (a flat/monotone curve is not).
    prices = [(100, 108), (108, 104), (104, 112), (112, 106), (106, 115)]
    for i, (buy_px, sell_px) in enumerate(prices):
        store.record_fill(
            Fill(
                f"B{i}",
                f"cB{i}",
                inst,
                OrderSide.BUY,
                money("1"),
                money(str(buy_px)),
                money("0"),
                2 * i + 1,
            )
        )
        store.record_fill(
            Fill(
                f"S{i}",
                f"cS{i}",
                inst,
                OrderSide.SELL,
                money("1"),
                money(str(sell_px)),
                money("0"),
                2 * i + 2,
            )
        )
    cfg = AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db_path},
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                }
            ],
        }
    )
    return StrategySupervisor(
        cfg, dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})
    )


async def test_kpi_total_ratios_come_from_the_combined_curve(tmp_path) -> None:  # noqa: ANN001
    """`kpi("total")` / `kpi("exchange")` ratios are non-null on a real combined curve."""
    pytest.importorskip("fynance")  # the ratios need the research dependency
    sup = _kpi_ratio_supervisor(str(tmp_path / "book.sqlite"))
    await sup.start("btc-ma")

    [total] = sup.kpi("total")
    # The combined curve has dispersion, so every ratio estimates to a real float.
    assert isinstance(total.sharpe, float)
    assert isinstance(total.sortino, float)
    assert isinstance(total.calmar, float)
    assert isinstance(total.max_drawdown, float)

    [exchange] = sup.kpi("exchange")
    assert exchange.exchange == "kraken"
    assert isinstance(exchange.sharpe, float)


async def test_kpi_aggregate_ratios_degrade_without_fynance(  # noqa: ANN001
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without fynance the aggregate ratios are None (never raise)."""
    sup = _kpi_ratio_supervisor(str(tmp_path / "book.sqlite"))
    await sup.start("btc-ma")

    # Make every ratio wrapper behave as if fynance were absent.
    import trading_bot.application.supervisor as sup_mod
    from trading_bot.domain.performance import PerformanceDependencyError

    def _no_fynance(*_args: object, **_kwargs: object) -> float:
        raise PerformanceDependencyError("sharpe")

    for name in ("sharpe", "sortino", "calmar", "max_drawdown"):
        monkeypatch.setattr(sup_mod, name, _no_fynance)

    [total] = sup.kpi("total")
    assert total.sharpe is None
    assert total.sortino is None
    assert total.calmar is None
    assert total.max_drawdown is None
    # PnL/fees still surface (the fold is money-exact, dependency-free).
    # Round trips: (108-100)+(104-108)+(112-104)+(106-112)+(115-106) = 15, no fees.
    assert total.realised_pnl == money("15")


# --- per-strategy store isolation: two portfolios, two stores --------------- #


def _fake_portfolio_signal(asof_ms, frames):  # noqa: ANN001, ANN201, ARG001
    """A no-op portfolio signal (offline, CI-safe — never evaluated in these tests).

    Referenced by ``_two_portfolio_config`` so ``start()`` can resolve a portfolio
    signal **without** importing the gitignored ``strategies/`` (absent in CI). The
    units are never stepped, so the empty target is never used — fills are seeded
    onto the engine bus directly.
    """
    return {}


def _two_portfolio_config(db_a: str, db_b: str) -> AppConfig:
    """A manifest with two portfolios, each declaring its OWN ``db_path``.

    Reproduces the deploy-two-portfolios-in-one-manifest shape (demo-binance +
    demo-kraken): both paper, disjoint universes/venues, each with a per-strategy
    store path so their books never commingle. The signal is a local no-op ref (so it
    resolves offline, no ``strategies/`` import); the units are never stepped.
    """
    ref = "trading_bot.tests.application.test_supervisor:_fake_portfolio_signal"
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": "./var/should-not-be-used.sqlite"},
            "portfolios": [
                {
                    "name": "pf-a",
                    "venue": "binance",
                    "universe": ["BTC/USDT"],
                    "capital": "100000",
                    "signal": {"ref": ref},
                    "data": {"exchange": "binance", "span": 86400},
                    "db_path": db_a,
                },
                {
                    "name": "pf-b",
                    "venue": "kraken",
                    "universe": ["BTC/USD"],
                    "capital": "100000",
                    "signal": {"ref": ref},
                    "data": {"exchange": "kraken", "span": 86400},
                    "db_path": db_b,
                },
            ],
        }
    )


def _two_portfolio_client() -> _FakeDccdClient:
    """Offline dccd client for the two-portfolio config (so ``start`` needs no dccd)."""
    return _FakeDccdClient(
        {"BTC/USDT": _dccd_ohlc(_trend()), "BTC/USD": _dccd_ohlc(_trend())}
    )


def _seed_portfolio_fills(
    sup: StrategySupervisor, name: str, symbol: Symbol, *, exit_price: str
) -> None:
    """Emit a buy@100 → sell@``exit_price`` round trip on ``name``'s engine bus."""
    inst = Instrument(symbol)
    bus = sup._units[name].engine.bus  # noqa: SLF001 — seed the wired bus
    bus.emit(
        FillEvent(
            Fill(
                f"{name}-F1",
                f"{name}-c1",
                inst,
                OrderSide.BUY,
                money("1"),
                money("100"),
                money("1"),
                1,
            )
        )
    )
    bus.emit(
        FillEvent(
            Fill(
                f"{name}-F2",
                f"{name}-c2",
                inst,
                OrderSide.SELL,
                money("1"),
                money(exit_price),
                money("1"),
                2,
            )
        )
    )


async def test_per_strategy_db_path_isolates_the_slice(tmp_path) -> None:  # noqa: ANN001
    """Each portfolio's sliced config points at its OWN ``db_path`` (not the global)."""
    db_a = str(tmp_path / "a.sqlite")
    db_b = str(tmp_path / "b.sqlite")
    sup = StrategySupervisor(
        _two_portfolio_config(db_a, db_b), dccd_client=_two_portfolio_client()
    )
    # The per-strategy db_path overrode the global storage.db_path on each slice.
    assert sup._units["pf-a"].config.storage.db_path == db_a  # noqa: SLF001
    assert sup._units["pf-b"].config.storage.db_path == db_b  # noqa: SLF001


async def test_per_strategy_stores_do_not_commingle_fills(tmp_path) -> None:  # noqa: ANN001
    """Two portfolios with their own db_path keep pnl_series disjoint (no commingling).

    The whole point of the isolation fix: seed a disjoint paper round trip onto each
    unit's engine bus (each writes to its OWN store), then assert each unit's
    ``pnl_series`` folds only its own fills — pf-a's +8 never leaks into pf-b's +18.
    """
    db_a = str(tmp_path / "a.sqlite")
    db_b = str(tmp_path / "b.sqlite")
    sup = StrategySupervisor(
        _two_portfolio_config(db_a, db_b), dccd_client=_two_portfolio_client()
    )
    await sup.start("pf-a")
    await sup.start("pf-b")
    # Disjoint books: pf-a on BTC/USDT (+8), pf-b on BTC/USD (+18).
    _seed_portfolio_fills(sup, "pf-a", Symbol("BTC", "USDT"), exit_price="110")
    _seed_portfolio_fills(sup, "pf-b", Symbol("BTC", "USD"), exit_price="120")

    pa = sup.pnl_series("pf-a")
    pb = sup.pnl_series("pf-b")
    v0 = money("100000")
    # Each series folds ONLY its own fills — no cross-contamination.
    assert pa["series"]["paper"][-1][2] == v0 + money("8")
    assert pb["series"]["paper"][-1][2] == v0 + money("18")
    # And each unit's live engine holds only its own instrument.
    a_insts = {
        str(i.symbol)
        for i in sup._units["pf-a"].engine.tracker.all_positions()  # noqa: SLF001
    }
    b_insts = {
        str(i.symbol)
        for i in sup._units["pf-b"].engine.tracker.all_positions()  # noqa: SLF001
    }
    assert a_insts == {"BTC/USDT"}
    assert b_insts == {"BTC/USD"}


async def test_per_strategy_replay_on_restart_stays_isolated(tmp_path) -> None:  # noqa: ANN001
    """After a stop→start (which replays each store), every unit's book is its own only.

    The restart path is where commingling used to bite: ``_replay_paper_book`` folds
    the store's fills back into a fresh engine. With per-strategy stores each unit
    replays only its own persisted fills, so pf-a's restored book is BTC/USDT +8 and
    pf-b's is BTC/USD +18 — never each other's.
    """
    db_a = str(tmp_path / "a.sqlite")
    db_b = str(tmp_path / "b.sqlite")
    sup = StrategySupervisor(
        _two_portfolio_config(db_a, db_b), dccd_client=_two_portfolio_client()
    )
    await sup.start("pf-a")
    await sup.start("pf-b")
    _seed_portfolio_fills(sup, "pf-a", Symbol("BTC", "USDT"), exit_price="110")
    _seed_portfolio_fills(sup, "pf-b", Symbol("BTC", "USD"), exit_price="120")

    # Restart both — each engine is rebuilt and replays its OWN store's fills.
    await sup.stop("pf-a")
    await sup.stop("pf-b")
    await sup.start("pf-a")
    await sup.start("pf-b")

    ea = sup._units["pf-a"].engine  # noqa: SLF001
    eb = sup._units["pf-b"].engine  # noqa: SLF001
    assert {str(i.symbol) for i in ea.tracker.all_positions()} == {"BTC/USDT"}
    assert {str(i.symbol) for i in eb.tracker.all_positions()} == {"BTC/USD"}
    assert ea.perf.realised_pnl() == money("8")
    assert eb.perf.realised_pnl() == money("18")


# --- concurrency: per-unit lock serialises lifecycle vs stepping (A-3) ------- #


class _BarrierRunner(StrategyRunner):
    """A ``StrategyRunner`` whose ``step_latest`` suspends on an injected barrier.

    Bypasses the real ``__init__`` (no engine wiring) so a test can install it as a
    unit's ``runner`` directly. When stepped it signals ``entered`` and then blocks
    on ``release`` — the deterministic seam that parks a ``step`` **mid-flight** so
    a concurrent ``stop`` / ``set_mode`` fires while the step is suspended (the A-3
    race), instead of hoping the scheduler happens to interleave. Being a real
    ``StrategyRunner`` subclass, it satisfies :meth:`StrategySupervisor.step`'s
    ``isinstance`` dispatch.
    """

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        # Deliberately do NOT call super().__init__ — no engine to wire here.
        self._entered = entered
        self._release = release

    async def step_latest(self):  # type: ignore[override]  # noqa: ANN201
        self._entered.set()  # announce the step is now in flight
        await self._release.wait()  # park here until the test lets it finish
        return None


async def test_step_racing_a_concurrent_stop_does_not_crash_or_corrupt() -> None:
    """A ``step`` parked mid-flight while a ``stop`` tears the same unit down is safe.

    Reproduces A-3 deterministically: install a barrier runner, launch a ``step``
    that suspends inside ``step_latest``, then ``stop`` the unit while the step is
    in flight. The step must complete without raising (no ``None.step_latest()`` /
    ``AssertionError``), and the unit must be left cleanly torn down.
    """
    sup = _supervisor()
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001

    entered, release = asyncio.Event(), asyncio.Event()
    unit.runner = _BarrierRunner(entered, release)

    step_task = asyncio.create_task(sup.step("btc-ma"))
    await asyncio.wait_for(entered.wait(), timeout=1.0)  # step is now mid-flight

    # Tear the unit down while the step is suspended. `stop` takes the unit lock,
    # but `step` already released it (it holds only a local runner snapshot), so
    # `stop` does not block on the parked step.
    await asyncio.wait_for(sup.stop("btc-ma"), timeout=1.0)

    release.set()  # let the in-flight step resume and finish
    assert await asyncio.wait_for(step_task, timeout=1.0) is None  # no exception

    status = sup.status("btc-ma")[0]
    assert status.running is False  # cleanly torn down
    assert unit.runner is None
    assert unit.engine is None
    # A follow-up step on the stopped unit is a quiet no-op (consistent state).
    assert await sup.step("btc-ma") is None


async def test_step_racing_a_concurrent_set_mode_does_not_crash_or_corrupt() -> None:
    """A ``step`` parked mid-flight while ``set_mode`` rebuilds the same unit is safe.

    ``set_mode`` stops → re-slices → starts under the unit lock. The in-flight step
    (holding a snapshot of the *old* runner) must finish without raising, and the
    unit must end consistently in the new mode with a freshly-built engine. The
    switch is a paper → paper restart: it exercises the full teardown → re-slice →
    rebuild critical section offline (a real testnet/live build needs venue
    credentials, out of scope here — the race is the same whatever the target mode).
    """
    pytest.importorskip("fynance")  # set_mode restarts → rebuilds a real engine
    sup = _supervisor()
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001
    old_engine = unit.engine

    entered, release = asyncio.Event(), asyncio.Event()
    unit.runner = _BarrierRunner(entered, release)

    step_task = asyncio.create_task(sup.step("btc-ma"))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    # Restart under the lock (paper → paper still tears down + rebuilds the engine).
    set_mode_task = asyncio.create_task(sup.set_mode("btc-ma", "paper"))
    release.set()  # let the parked step resume; set_mode proceeds too
    assert await asyncio.wait_for(step_task, timeout=1.0) is None  # no exception
    await asyncio.wait_for(set_mode_task, timeout=2.0)

    status = sup.status("btc-ma")[0]
    assert status.mode == "paper"  # consistent end state
    assert status.running is True
    assert unit.engine is not None
    assert unit.engine is not old_engine  # a genuinely fresh engine


async def test_two_concurrent_starts_build_the_engine_exactly_once(monkeypatch) -> None:  # noqa: ANN001
    """Two ``start``s racing on one unit build its engine exactly once (no double-build).

    The concrete A-3 corruption ("double-build an engine"): ``start``'s idempotency
    guard (``if unit.running: return``) sits *before* its awaits, so on the old,
    un-locked code two concurrent ``start``s both pass the guard, both build an
    engine, and the second silently clobbers the first (a leaked engine / duplicated
    reconcile). Deterministic seam: ``reconcile`` is patched to park on a barrier the
    first time, so the first ``start`` is suspended mid-build when the second is
    launched. The per-unit lock serialises them — the second waits, then sees
    ``running`` and returns — so ``build_engine`` runs exactly once.
    """
    import trading_bot.application.supervisor as sup_mod

    build_calls = 0
    real_build = sup_mod.build_engine

    def _counting_build(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal build_calls
        build_calls += 1
        return real_build(*args, **kwargs)

    gate = asyncio.Event()
    first = True
    real_reconcile = sup_mod.reconcile

    async def _gated_reconcile(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal first
        if first:
            first = False
            await gate.wait()  # park the first start mid-build
        return await real_reconcile(*args, **kwargs)

    monkeypatch.setattr(sup_mod, "build_engine", _counting_build)
    monkeypatch.setattr(sup_mod, "reconcile", _gated_reconcile)

    sup = _supervisor()
    t1 = asyncio.create_task(sup.start("btc-ma"))
    # Let the first start reach (and park at) the reconcile seam.
    await asyncio.sleep(0)
    t2 = asyncio.create_task(sup.start("btc-ma"))
    await asyncio.sleep(0)  # give the second start a chance to (try to) proceed

    gate.set()  # release the parked first start
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)

    assert build_calls == 1  # exactly one engine built — no double-build
    assert sup.status("btc-ma")[0].running is True


async def test_independent_units_step_concurrently_the_lock_is_not_global() -> None:
    """Two different units step at the same time — the lock is per-unit, not global.

    Both units' steps park on their barriers simultaneously; if the supervisor took
    a single global lock, the second step could never enter while the first is
    parked. That both report ``entered`` before either is released proves the locks
    are independent (concurrent stepping of distinct strategies is preserved).
    """
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")

    e1, r1 = asyncio.Event(), asyncio.Event()
    e2, r2 = asyncio.Event(), asyncio.Event()
    sup._units["btc-kraken"].runner = _BarrierRunner(e1, r1)  # noqa: SLF001
    sup._units["eth-binance"].runner = _BarrierRunner(e2, r2)  # noqa: SLF001

    t1 = asyncio.create_task(sup.step("btc-kraken"))
    t2 = asyncio.create_task(sup.step("eth-binance"))

    # Both must be able to be in flight at once (a global lock would serialise them,
    # so the second `entered` would never fire while the first is parked).
    await asyncio.wait_for(asyncio.gather(e1.wait(), e2.wait()), timeout=1.0)

    r1.set()
    r2.set()
    assert await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0) == [None, None]


async def test_remove_unit_fully_tears_down_no_residual_handles() -> None:
    """`remove_unit` leaves no residual engine / runner / store handle on the unit.

    A-10 routes `remove_unit` through the shared `_teardown`, so a removed unit is
    torn down exactly as `stop` tears one down: engine, runner and running flag are
    all cleared (and the unit is dropped from the registry + base config).
    """
    pytest.importorskip("fynance")
    sup = _supervisor()
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001 — grab the handle before removal
    assert unit.engine is not None  # it really was running

    sup.remove_unit("btc-ma")

    # The unit object itself is fully torn down (no leaked engine/runner handle) ...
    assert unit.running is False
    assert unit.runner is None
    assert unit.engine is None
    # ... and it is gone from the registry + manifest.
    assert sup.names() == []
    assert sup.manifest().strategies == []


# --- capital: status fields, v0 repoint, KPI isolation ---------------------- #


def _config_alloc(
    db_path: str, *, allocation: str = "100", policy: str = "fixed"
) -> AppConfig:
    """A paper BTC/USD strategy declaring an ``allocation`` + ``capital_policy``."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db_path},
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                    "allocation": allocation,
                    "capital_policy": policy,
                }
            ],
        }
    )


async def test_status_exposes_capital_fields(tmp_path) -> None:  # noqa: ANN001
    """A started unit with an ``allocation`` surfaces the capital view on its status."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = StrategySupervisor(
        _config_alloc(db, allocation="100", policy="compound"),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")  # seeds the genesis via build_runners

    status = sup.status("btc-ma")[0]
    assert status.allocation == money("100")
    assert status.contributed == money("100")  # only the genesis so far
    assert status.capital_policy == "compound"
    # No fills / flat book yet → total_value = contributed + 0 + 0.
    assert status.total_value == money("100")
    # The genesis landed once in the ledger.
    events = sup._units["btc-ma"].engine.store.capital_events(strategy="btc-ma")  # type: ignore[union-attr]  # noqa: SLF001
    assert len(events) == 1 and events[0].event_id == "btc-ma:funding"


def test_status_capital_fields_none_for_legacy_unit() -> None:
    """A strategy with no ``allocation`` reports None capital fields (unchanged)."""
    sup = _supervisor()  # the default config declares no allocation / no store
    [status] = sup.status()
    assert status.allocation is None
    assert status.contributed is None
    assert status.unrealised is None
    assert status.total_value is None
    assert status.capital_policy == "fixed"  # the field default


def test_v0_anchors_at_allocation(tmp_path) -> None:  # noqa: ANN001
    """`pnl_series`' v0 (the KPI anchor) repoints to the genesis allocation."""
    db = str(tmp_path / "book.sqlite")
    sup = StrategySupervisor(
        _config_alloc(db, allocation="250"),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    assert sup.pnl_series("btc-ma")["v0"] == money("250")


def test_v0_legacy_unit_stays_starting_capital() -> None:
    """A legacy unit (no allocation) keeps ``starting_capital`` as its anchor."""
    sup = _supervisor()
    result = sup.pnl_series("btc-ma")
    assert result["v0"] == sup._units["btc-ma"].config.starting_capital  # noqa: SLF001


def test_deposit_never_distorts_the_fill_only_kpi_curve(tmp_path) -> None:  # noqa: ANN001
    """The guardrail: a DEPOSIT moves total_value but never the fill-only equity curve.

    A deposit is a capital movement, not a return — so the per-mode ``series``
    (the KPI equity curve) and the ``v0`` anchor must be byte-identical before and
    after it, while ``total_value`` (contributed + realised) moves by the deposit.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    store.set_context(mode="paper", venue="kraken")
    # Genesis funding of 100 + a paper round trip realising +8.
    store.record_capital_event(
        CapitalEvent(
            "btc-ma:funding", "btc-ma", CapitalEventType.FUNDING, money("100"), 0
        )
    )
    store.record_fill(
        Fill("PF1", "pc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.record_fill(
        Fill(
            "PF2", "pc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2
        )
    )

    # A stopped unit reads back from the configured db_path store.
    sup = StrategySupervisor(
        _config_alloc(db, allocation="100"),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )

    before = sup.pnl_series("btc-ma")
    assert before["v0"] == money("100")  # anchored at the genesis
    series_before = before["series"]["paper"]
    # total_value = contributed(100) + realised(8) + unrealised(None→0).
    assert before["current"]["paper"]["total_value"] == money("108")

    # Record a mid-run DEPOSIT of 50 → contributed jumps to 150.
    store.record_capital_event(
        CapitalEvent("D1", "btc-ma", CapitalEventType.DEPOSIT, money("50"), ts=100)
    )

    after = sup.pnl_series("btc-ma")
    # The fill-only KPI curve + anchor are UNCHANGED — a deposit is not a return.
    assert after["v0"] == money("100")
    assert after["series"]["paper"] == series_before
    # But total_value moved by the deposit: 150 + 8 = 158.
    assert after["current"]["paper"]["total_value"] == money("158")
    # And the status view agrees (contributed grew, realised curve did not).
    status = sup.status("btc-ma")[0]
    assert status.contributed == money("150")
    assert status.total_value == money("158")


# --- control plane: deposit / withdraw / set_policy ------------------------- #


def _seed_book(db: str, *, allocation: str, fills: tuple[Fill, ...] = ()) -> None:
    """Seed a paper store with the genesis funding + any ``fills`` (before start)."""
    store = SqliteStore(db)
    store.set_context(mode="paper", venue="kraken")
    store.record_capital_event(
        CapitalEvent(
            "btc-ma:funding",
            "btc-ma",
            CapitalEventType.FUNDING,
            money(allocation),
            0,
        )
    )
    for fill in fills:
        store.record_fill(fill)
    store.close()


async def _started_alloc_unit(
    db: str,
    *,
    allocation: str = "100",
    policy: str = "fixed",
    fills: tuple[Fill, ...] = (),
) -> StrategySupervisor:
    """A started paper BTC/USD unit with an ``allocation`` (+ optional seeded book).

    Any ``fills`` are written to the store *before* start so the unit's engine
    replays them into its tracker / perf (a paper book that survives a restart).
    """
    if fills:
        _seed_book(db, allocation=allocation, fills=fills)
    sup = StrategySupervisor(
        _config_alloc(db, allocation=allocation, policy=policy),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")
    return sup


async def test_deposit_idempotent_by_op_id(tmp_path) -> None:  # noqa: ANN001
    """A re-sent ``op_id`` is a no-op (one ledger event); distinct op_ids accumulate."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)

    b1 = await sup.deposit("btc-ma", "50", op_id="op-1")
    assert b1["contributed"] == money("150")
    # Same op_id again → no new event, identical breakdown (idempotent replay).
    b2 = await sup.deposit("btc-ma", "50", op_id="op-1")
    assert b2["contributed"] == money("150")
    assert [e["event_id"] for e in b2["events"]].count("op-1") == 1
    # A distinct op_id accumulates.
    b3 = await sup.deposit("btc-ma", "50", op_id="op-2")
    assert b3["contributed"] == money("200")


async def test_deposit_money_precision_is_exact(tmp_path) -> None:  # noqa: ANN001
    """Depositing ``0.1`` three times moves contributed by exactly ``0.3`` (no float)."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)

    breakdown: dict[str, object] = {}
    for i in range(3):
        breakdown = await sup.deposit("btc-ma", "0.1", op_id=f"d{i}")
    assert breakdown["contributed"] == money("100.3")


async def test_deposit_rejects_a_non_positive_amount(tmp_path) -> None:  # noqa: ANN001
    """A zero / negative deposit amount is refused (the direction lives in the type)."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)
    with pytest.raises(ValueError, match="strictly positive"):
        await sup.deposit("btc-ma", "0", op_id="bad")


async def test_deposit_on_a_stopped_unit_persists_and_seeds_genesis(
    tmp_path,  # noqa: ANN001
) -> None:
    """A deposit on a never-started unit writes to its db store + seeds the genesis."""
    db = str(tmp_path / "book.sqlite")
    sup = StrategySupervisor(
        _config_alloc(db, allocation="100"),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )  # never started — the stopped-unit (db_path) write path
    b = await sup.deposit("btc-ma", "50", op_id="op-1")
    assert b["contributed"] == money("150")
    ids = {e["event_id"] for e in b["events"]}
    assert "btc-ma:funding" in ids and "op-1" in ids


async def test_withdraw_over_withdrawable_raises_with_the_figure(
    tmp_path,  # noqa: ANN001
) -> None:
    """Withdrawing more than withdrawable raises, carrying the exact figure; nothing moves."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)  # flat book → withdrawable == contributed(100)

    with pytest.raises(WithdrawalTooLarge) as exc:
        await sup.withdraw("btc-ma", "150", op_id="w1")
    assert exc.value.withdrawable == money("100")
    assert exc.value.requested == money("150")
    # Nothing moved — the refused op left the ledger untouched.
    assert sup.capital_breakdown("btc-ma")["contributed"] == money("100")


async def test_withdraw_reduces_contributed(tmp_path) -> None:  # noqa: ANN001
    """A valid withdrawal drops contributed (and the withdrawable) by the amount."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)

    b = await sup.withdraw("btc-ma", "30", op_id="w1")
    assert b["contributed"] == money("70")
    assert b["withdrawable"] == money("70")


async def test_withdraw_idempotent_replay_skips_the_guard(tmp_path) -> None:  # noqa: ANN001
    """Re-sending a withdrawal's ``op_id`` is a no-op — the replay never re-trips the guard."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)

    b1 = await sup.withdraw("btc-ma", "60", op_id="w1")
    assert b1["contributed"] == money("40")  # withdrawable was 100
    # Replay: 60 now exceeds the reduced withdrawable (40), but a replay is a
    # no-op — it must NOT raise, and must return the unchanged breakdown.
    b2 = await sup.withdraw("btc-ma", "60", op_id="w1")
    assert b2["contributed"] == money("40")
    assert [e["event_id"] for e in b2["events"]].count("w1") == 1


async def test_withdrawable_fully_invested_is_zero(tmp_path) -> None:  # noqa: ANN001
    """A unit whose open position consumes all its cash has ~zero withdrawable."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    # BUY 2 @ 50 → the whole 100 genesis is committed to the open position.
    buy = Fill("F1", "c1", inst, OrderSide.BUY, money("2"), money("50"), money("0"), 1)
    sup = await _started_alloc_unit(db, fills=(buy,))

    b = sup.capital_breakdown("btc-ma")
    assert b["total_value"] == money("100")  # 100 + realised(0) + unrealised(0)
    assert b["withdrawable"] == money("0")  # committed |2|*50 = 100 → nothing free
    with pytest.raises(WithdrawalTooLarge):
        await sup.withdraw("btc-ma", "1", op_id="w1")


async def test_withdrawable_never_negative(tmp_path) -> None:  # noqa: ANN001
    """Committed capital beyond total value floors withdrawable at zero (never negative)."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    # BUY 3 @ 50 → committed 150 > total_value 100 ⇒ withdrawable floored at 0.
    buy = Fill("F1", "c1", inst, OrderSide.BUY, money("3"), money("50"), money("0"), 1)
    sup = await _started_alloc_unit(db, fills=(buy,))
    assert sup.capital_breakdown("btc-ma")["withdrawable"] == money("0")


async def test_partly_invested_withdrawable_is_the_free_cash(
    tmp_path,  # noqa: ANN001
) -> None:
    """Withdrawable is total value net of the capital committed to the open book."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    # BUY 1 @ 50 → 50 committed, 50 free of the 100 genesis.
    buy = Fill("F1", "c1", inst, OrderSide.BUY, money("1"), money("50"), money("0"), 1)
    sup = await _started_alloc_unit(db, fills=(buy,))

    assert sup.capital_breakdown("btc-ma")["withdrawable"] == money("50")
    b = await sup.withdraw("btc-ma", "50", op_id="w1")
    assert b["contributed"] == money("50")


async def test_set_policy_is_hot_on_the_running_unit(tmp_path) -> None:  # noqa: ANN001
    """A policy flip is hot: the SAME running provider reflects it, no restart."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    # A round trip realising +10, so fixed vs compound sizing bases differ.
    buy = Fill("F1", "c1", inst, OrderSide.BUY, money("1"), money("100"), money("0"), 1)
    sell = Fill(
        "F2", "c2", inst, OrderSide.SELL, money("1"), money("110"), money("0"), 2
    )
    sup = await _started_alloc_unit(db, policy="fixed", fills=(buy, sell))

    unit = sup._units["btc-ma"]  # noqa: SLF001
    provider = unit.runner._capital_provider  # type: ignore[union-attr]  # noqa: SLF001
    assert provider is not None
    # fixed: sizing base is contributed (100), realised PnL not reinvested.
    assert provider() == money("100")

    b = await sup.set_policy("btc-ma", "compound")
    assert b["policy"] == "compound"
    # Hot: the SAME provider now reinvests realised (+10) → 110, no rebuild.
    assert provider() == money("110")
    # The shared config entry (hence the persisted manifest) reflects it too.
    assert sup.manifest().strategies[0].capital_policy == "compound"


async def test_set_policy_rejects_an_unknown_policy(tmp_path) -> None:  # noqa: ANN001
    """An unrecognised policy is refused (nothing changes)."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)
    with pytest.raises(ConfigError, match="unknown capital policy"):
        await sup.set_policy("btc-ma", "bogus")
    assert sup.manifest().strategies[0].capital_policy == "fixed"


async def test_live_mode_capital_ops_are_refused(tmp_path) -> None:  # noqa: ANN001
    """Deposit / withdraw on a live unit is refused (deferred to real-key enablement)."""
    db = str(tmp_path / "book.sqlite")
    sup = StrategySupervisor(
        _config_alloc(db, allocation="100"),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.set_mode("btc-ma", "live", confirm_live=True)  # stopped → just flips mode
    with pytest.raises(LiveCapitalOpsDeferred):
        await sup.deposit("btc-ma", "50", op_id="op-1")
    with pytest.raises(LiveCapitalOpsDeferred):
        await sup.withdraw("btc-ma", "10", op_id="op-2")


async def test_capital_breakdown_lists_the_ledger(tmp_path) -> None:  # noqa: ANN001
    """The breakdown carries the genesis + deposit events as the UI's audit trail."""
    pytest.importorskip("fynance")
    db = str(tmp_path / "book.sqlite")
    sup = await _started_alloc_unit(db)
    await sup.deposit("btc-ma", "25", op_id="dep-1", note="top up")

    b = sup.capital_breakdown("btc-ma")
    kinds = {(e["type"], e["event_id"]) for e in b["events"]}
    assert ("funding", "btc-ma:funding") in kinds
    assert ("deposit", "dep-1") in kinds
    dep = next(e for e in b["events"] if e["event_id"] == "dep-1")
    assert dep["amount"] == money("25") and dep["note"] == "top up"
    assert b["allocation"] == money("100")
    assert b["contributed"] == money("125")
    assert b["policy"] == "fixed"


def test_capital_breakdown_unknown_unit_raises() -> None:
    """A breakdown for an unknown unit raises (the API maps it to 404)."""
    sup = _supervisor()
    with pytest.raises(ConfigError):
        sup.capital_breakdown("nope")


# --- accounting guardrail: startup check, TTL cache, alert-on-new-only ------- #


def _accounting_config(db_path: str) -> AppConfig:
    """A single paper BTC/USD strategy storing to ``db_path`` (no allocation)."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db_path},
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                }
            ],
        }
    )


def _seed_duplicate_venue_orders(db_path: str) -> None:
    """Persist two OPEN orders sharing one ``venue_order_id`` (a warn violation).

    Left OPEN (not FILLED) and with no fills recorded, so the only violation
    ``check_book`` raises against them is ``duplicate_venue_ids`` — the
    reconcile pass that runs during ``start`` closes them as orphans (no venue
    reports them open), but that only flips their status; the shared
    ``venue_order_id`` survives, so the violation still fires after startup.
    """
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db_path, mode="paper", venue="kraken")
    for cid in ("dup-a", "dup-b"):
        order = Order(
            client_order_id=cid,
            instrument=inst,
            side=OrderSide.BUY,
            qty=money("1"),
            type=OrderType.LIMIT,
            limit_price=money("100"),
        )
        order.status = OrderStatus.OPEN
        order.venue_order_id = "VID-DUP"
        store.upsert_order(order)


async def test_startup_check_runs_and_stores_report(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Starting over a store with a seeded inconsistency populates the unit's
    accounting holder and alerts the violation once, at the right level.
    """
    import trading_bot.application.supervisor as sup_mod

    db = str(tmp_path / "book.sqlite")
    _seed_duplicate_venue_orders(db)

    captured: list[LogEvent] = []
    real_build_engine = sup_mod.build_engine

    def _capturing_build_engine(config, **kwargs):  # noqa: ANN001, ANN003, ANN202
        engine = real_build_engine(config, **kwargs)
        engine.bus.subscribe(captured.append)
        return engine

    monkeypatch.setattr(sup_mod, "build_engine", _capturing_build_engine)

    sup = StrategySupervisor(
        _accounting_config(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")

    unit = sup._units["btc-ma"]  # noqa: SLF001
    assert unit.accounting is not None
    assert [v.kind for v in unit.accounting.violations] == ["duplicate_venue_ids"]
    assert unit.accounting.computed_at_ms is not None

    accounting_events = [
        e
        for e in captured
        if isinstance(e, LogEvent) and e.message.startswith("accounting:")
    ]
    assert len(accounting_events) == 1
    assert accounting_events[0].level == "warning"
    assert "VID-DUP" in accounting_events[0].message


async def test_ttl_recompute(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Two calls within the TTL compute once; past the TTL, a call recomputes."""
    import trading_bot.application.supervisor as sup_mod

    db = str(tmp_path / "book.sqlite")
    now = [1_000_000]
    sup = StrategySupervisor(
        _accounting_config(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
        clock=lambda: now[0],
    )
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001
    # Reset the cache the real startup check just populated so the TTL story
    # below starts clean (only the patched calls below are counted).
    unit.accounting = None  # noqa: SLF001

    calls = 0
    real_check_book = sup_mod.check_book

    def _counting_check_book(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        return real_check_book(*args, **kwargs)

    monkeypatch.setattr(sup_mod, "check_book", _counting_check_book)

    sup._accounting_of(unit)  # noqa: SLF001 — no cache yet: computes once
    assert calls == 1
    sup._accounting_of(unit)  # noqa: SLF001 — still within the 60s TTL: cached
    assert calls == 1

    now[0] += 61_000  # past the TTL
    sup._accounting_of(unit)  # noqa: SLF001 — stale: recomputes
    assert calls == 2


async def test_alerts_only_on_new_violations(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A stable violation set stays silent; only new/resolved ones alert once."""
    import trading_bot.application.supervisor as sup_mod

    db = str(tmp_path / "book.sqlite")
    now = [0]
    sup = StrategySupervisor(
        _accounting_config(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
        clock=lambda: now[0],
    )
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001
    assert unit.engine is not None

    stable = Violation(
        kind="duplicate_venue_ids",
        severity="warn",
        subject="VID-1",
        detail="venue_order_id 'VID-1' is shared by 2 order rows.",
        measured="2",
        expected="1",
    )
    drift = Violation(
        kind="position_drift",
        severity="error",
        subject="BTC/USD",
        detail="BTC/USD: tracked net qty disagrees with its fills.",
        measured="5",
        expected="3",
    )
    # Prime the cache with `stable` already known, so the recomputes below only
    # exercise the diff against it (not the "everything is new" first-ever call).
    unit.accounting = sup_mod._AccountingState(  # noqa: SLF001
        violations=[stable], computed_at_ms=now[0]
    )

    captured: list[LogEvent] = []
    unit.engine.bus.subscribe(captured.append)

    plan = iter([[stable], [stable, drift], [drift]])
    monkeypatch.setattr(sup_mod, "check_book", lambda *a, **k: next(plan))  # noqa: ARG005

    now[0] += 61_000
    sup._accounting_of(unit)  # noqa: SLF001 — same set as cached: silent
    assert captured == []

    now[0] += 61_000
    sup._accounting_of(unit)  # noqa: SLF001 — `drift` newly appeared: one alert
    assert len(captured) == 1
    assert captured[0].level == "error"
    assert captured[0].message.startswith("accounting:")

    now[0] += 61_000
    sup._accounting_of(unit)  # noqa: SLF001 — `stable` resolved, `drift` stable
    assert len(captured) == 2
    assert captured[1].level == "info"
    assert "resolved" in captured[1].message


async def test_mode_filtering(tmp_path) -> None:  # noqa: ANN001
    """A fill recorded under a different mode in the same store is excluded."""
    import trading_bot.application.supervisor as sup_mod

    db = str(tmp_path / "book.sqlite")
    sup = StrategySupervisor(
        _accounting_config(db),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())}),
    )
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001
    assert unit.engine is not None
    assert unit.accounting is not None
    assert unit.accounting.violations == []  # empty paper book -> clean startup

    # A fill recorded under "testnet" in the SAME underlying file. Unfiltered,
    # it would drift BTC/USD from zero (no tracked position sees it) — proving
    # the mode filter, not just an accidentally-clean book.
    foreign_store = SqliteStore(db, mode="testnet", venue="kraken")
    inst = Instrument(Symbol("BTC", "USD"))
    foreign_store.record_fill(
        Fill(
            "foreign-F1",
            "foreign-c1",
            inst,
            OrderSide.BUY,
            money("1"),
            money("100"),
            money("0"),
            1,
        )
    )

    violations = sup_mod.StrategySupervisor._compute_accounting_report(  # noqa: SLF001
        unit, unit.engine
    )
    assert violations == []
