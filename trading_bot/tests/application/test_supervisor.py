"""Tests for the :class:`StrategySupervisor` — per-strategy lifecycle + modes.

Offline: a paper config + a fake dccd client (canned bars). Proves the supervisor
splits a config into independently-managed units, starts/steps/stops them in their
own engine, switches modes (paper ↔ testnet), and **gates real money** (``live``
needs an explicit confirmation). Async tests run un-decorated (``asyncio_mode =
"auto"``).
"""

from __future__ import annotations

import polars as pl
import pytest

from trading_bot.application.config import (
    AppConfig,
    DataSourceConfig,
    PortfolioStrategyConfig,
    SignalRefConfig,
    StrategyConfig,
)
from trading_bot.application.events import FillEvent
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.domain.errors import ConfigError, LiveTradingNotEnabled
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide


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

    def read(self, exchange, symbol, data_type="ohlc", span=None, start_ns=None, end_ns=None):  # noqa: ANN001, ANN201
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
            Fill(f"{name}-F1", f"{name}-c1", inst, OrderSide.BUY,
                 money("1"), money("100"), money("1"), 1)
        )
    )
    bus.emit(
        FillEvent(
            Fill(f"{name}-F2", f"{name}-c2", inst, OrderSide.SELL,
                 money("1"), money("110"), money("1"), 2)
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
            Fill("k-F1", "k-c1", inst, OrderSide.BUY,
                 money("3"), money("100"), money("1"), 1)
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


def _portfolio_entry(name: str = "alloc1") -> PortfolioStrategyConfig:
    """A deployable portfolio entry pointing at an existing signal ref."""
    return PortfolioStrategyConfig(
        name=name,
        venue="binance",
        universe=["BTC/USDT", "ETH/USDT"],
        signal=SignalRefConfig(ref="strategies.alloc1.signal:alloc1_portfolio_signal"),
        capital=money("100000"),
        data=DataSourceConfig(exchange="binance", span=86400),
    )


def test_add_unit_appends_a_stopped_unit() -> None:
    """`add_unit` deploys a new **stopped** unit reflected in status() + names()."""
    sup = _supervisor()
    name = sup.add_unit(_portfolio_entry())
    assert name == "alloc1"
    assert sup.names() == ["btc-ma", "alloc1"]
    [st] = [s for s in sup.status() if s.name == "alloc1"]
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
    assert [p.name for p in man.portfolios] == ["alloc1"]


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
        Fill("SF2", "sc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2)
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
        Fill("PF2", "pc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2)
    )
    # A testnet round trip on the SAME instrument (fake money — must be ignored by
    # the paper replay): would otherwise add +18.
    store.set_context(mode="testnet", venue="binance")
    store.record_fill(
        Fill("TF1", "tc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 3)
    )
    store.record_fill(
        Fill("TF2", "tc2", inst, OrderSide.SELL, money("1"), money("120"), money("1"), 4)
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
        Fill("PF2", "pc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2)
    )
    # Testnet round trip on the same book (fake money, separate series): +18.
    store.set_context(mode="testnet", venue="binance")
    store.record_fill(
        Fill("TF1", "tc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 3)
    )
    store.record_fill(
        Fill("TF2", "tc2", inst, OrderSide.SELL, money("1"), money("120"), money("1"), 4)
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
            Fill(f"{db}-F1", f"{db}-c1", inst, OrderSide.BUY,
                 money("1"), money("100"), money("0"), 1)
        )
        store.record_fill(
            Fill(f"{db}-F2", f"{db}-c2", inst, OrderSide.SELL,
                 money("1"), money("110"), money("0"), 2)
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
