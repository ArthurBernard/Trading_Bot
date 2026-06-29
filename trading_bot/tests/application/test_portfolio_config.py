"""Tests for portfolio config + its wiring into :func:`run_app` (leaf 04).

These prove a **portfolio strategy** is declarable in
:class:`~trading_bot.application.config.AppConfig` and runnable through
:func:`~trading_bot.application.run_app.run_app`, alongside the single-instrument
strategies, fully **offline** — a fake *daily* dccd client serving canned bars
per coin, the in-repo fake portfolio signal
(:func:`trading_bot.tests.fixtures.fake_book.fixed_weights`), and the default
:class:`~trading_bot.brokers.paper.PaperBroker` (the engine's real data path).

What is verified
----------------
* **config validators** — a valid ``portfolios:`` entry validates; an empty
  universe, a duplicate coin in a universe, and a non-positive capital are
  rejected;
* **offline end-to-end through run_app** — the per-coin routed orders and final
  shared-tracker positions match the intended ``weight × capital / price`` target
  read back from the broker-confirmed fills (the PnL source of truth);
* **overlap detection** — a portfolio universe sharing a coin with a
  single-instrument strategy, or with another portfolio, is a clear
  :class:`~trading_bot.domain.errors.ConfigError`;
* **backward compat** — a config with no ``portfolios:`` runs exactly as before;
* **the resample-on-read adapter**
  (:class:`~trading_bot.application.data_provider.ResamplingDccdClient`) — canned
  1-minute bars aggregate to causal, OHLCV-correct daily bars (and a real-store
  ``-m network`` check).

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest
from pydantic import ValidationError

from trading_bot.application.config import AppConfig, PortfolioStrategyConfig
from trading_bot.application.data_provider import ResamplingDccdClient
from trading_bot.application.run_app import (
    PortfolioCoinReport,
    PortfolioReport,
    RunReport,
    build_portfolio_runners,
    run_app,
)
from trading_bot.application.service_factory import build_engine
from trading_bot.domain.errors import ConfigError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.position import Position
from trading_bot.tests.fixtures import fake_book

BTC = Symbol("BTC", "USDT")
ETH = Symbol("ETH", "USDT")
BTC_USDT = Instrument(BTC)
ETH_USDT = Instrument(ETH)

_FAKE_SIGNAL = "trading_bot.tests.fixtures.fake_book:fixed_weights"

# --- canned daily bars + a fake daily dccd client -------------------------- #

_DAY_NS = 86_400 * 1_000_000_000


def _dccd_ohlc(closes: list[float], *, start_ns: int = 0) -> pl.DataFrame:
    """A canned **daily** dccd OHLC frame (dccd's column names) from closes.

    Mirrors what a daily dccd ``read`` would return: ``TS`` (ns) one day apart,
    plus open/high/low/close/volume + the dropped quote_volume/trades. The fake
    portfolio signal ignores the bars, but the runner reads each coin's latest
    *close* to size + price its leg, so the closes are what matter.
    """
    n = len(closes)
    return pl.DataFrame(
        {
            "TS": [start_ns + _DAY_NS * i for i in range(n)],
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1.0] * n,
            "quote_volume": [10.0] * n,
            "trades": [5] * n,
        }
    )


class _FakeDailyDccdClient:
    """A canned daily dccd client keyed by the venue pair string.

    The :class:`~trading_bot.application.portfolio_feed.PortfolioFeed` reads each
    coin via ``client.read(exchange, venue_pair, ...)``; this fake returns the
    canned frame for that pair (honouring an ``end_ns`` cutoff) and records the
    calls. No real dccd / network — it satisfies the
    :class:`~trading_bot.application.data_provider.DccdClient` protocol
    structurally. It already serves **daily** bars, so no resampling is needed
    (the injectable seam: offline tests bypass ``ResamplingDccdClient``).
    """

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames
        self.reads: list[tuple[str, str, int | None]] = []

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        self.reads.append((exchange, symbol, span))
        frame = self._frames[symbol]
        if end_ns is not None:
            frame = frame.filter(pl.col("TS") <= end_ns)
        return frame

    def backfill(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start: str = "last",
    ) -> None:  # pragma: no cover - not exercised (backfill=False)
        return None


def _daily_client(closes_by_pair: dict[str, list[float]]) -> _FakeDailyDccdClient:
    """A fake daily client serving each venue pair a constant-ish daily series."""
    return _FakeDailyDccdClient(
        {pair: _dccd_ohlc(closes) for pair, closes in closes_by_pair.items()}
    )


def _one_portfolio_config(
    *,
    universe: list[str] | None = None,
    capital: str = "100000",
    extra_strategies: list[dict] | None = None,
    extra_portfolios: list[dict] | None = None,
) -> AppConfig:
    """A paper config with one portfolio (the fake signal, BTC+ETH/USDT, daily)."""
    portfolio = {
        "name": "fake-pf",
        "universe": universe if universe is not None else ["BTC/USDT", "ETH/USDT"],
        "signal": {"ref": _FAKE_SIGNAL},
        "capital": capital,
        "data": {"exchange": "binance", "span": 86400},
    }
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "strategies": extra_strategies or [],
            "portfolios": [portfolio, *(extra_portfolios or [])],
        }
    )


# --- config validators ----------------------------------------------------- #


def test_valid_portfolio_config_validates() -> None:
    """A well-formed portfolio entry validates with exact-Decimal money."""
    cfg = _one_portfolio_config(capital="100000")
    assert len(cfg.portfolios) == 1
    pf = cfg.portfolios[0]
    assert isinstance(pf, PortfolioStrategyConfig)
    assert pf.name == "fake-pf"
    assert pf.universe == ["BTC/USDT", "ETH/USDT"]
    assert pf.signal.ref == _FAKE_SIGNAL
    # Capital parses exactly from the YAML scalar — Decimal, never float.
    assert pf.capital == Decimal("100000")
    assert isinstance(pf.capital, Decimal)
    assert pf.data.span == 86400
    assert pf.venue == "binance"
    assert pf.gross_cap is None


def test_capital_is_exact_decimal_from_scalar() -> None:
    """A fractional capital keeps its exact decimal meaning (no float drift)."""
    cfg = _one_portfolio_config(capital="100000.10")
    assert cfg.portfolios[0].capital == Decimal("100000.10")


def test_empty_universe_rejected() -> None:
    """An empty universe is a validation error."""
    with pytest.raises(ValidationError, match="non-empty"):
        _one_portfolio_config(universe=[])


def test_duplicate_coin_in_universe_rejected() -> None:
    """A duplicate coin within a universe is rejected (even by alias spelling)."""
    with pytest.raises(ValidationError, match="duplicate instrument"):
        _one_portfolio_config(universe=["BTC/USDT", "BTC/USDT"])


def test_duplicate_coin_by_alias_in_universe_rejected() -> None:
    """Two spellings of the same coin (BTC vs XBT) collide as a duplicate."""
    with pytest.raises(ValidationError, match="duplicate instrument"):
        _one_portfolio_config(universe=["BTC/USDT", "XBT/USDT"])


def test_non_positive_capital_rejected() -> None:
    """A zero / negative capital is rejected."""
    with pytest.raises(ValidationError, match="capital must be positive"):
        _one_portfolio_config(capital="0")
    with pytest.raises(ValidationError, match="capital must be positive"):
        _one_portfolio_config(capital="-5")


def test_non_positive_gross_cap_rejected() -> None:
    """A non-positive gross_cap is rejected (None is allowed)."""
    with pytest.raises(ValidationError, match="gross_cap must be positive"):
        AppConfig.model_validate(
            {
                "portfolios": [
                    {
                        "name": "p",
                        "universe": ["BTC/USDT"],
                        "signal": {"ref": _FAKE_SIGNAL},
                        "capital": "1000",
                        "data": {"exchange": "binance", "span": 86400},
                        "gross_cap": "0",
                    }
                ]
            }
        )


def test_unparseable_universe_pair_rejected() -> None:
    """A malformed pair string in the universe is caught at config time."""
    with pytest.raises(ValidationError, match="not a valid pair"):
        _one_portfolio_config(universe=["NOTAPAIR"])


def test_gross_cap_carried_onto_strategy() -> None:
    """A declared gross_cap reaches the built PortfolioStrategy (exact Decimal)."""
    cfg = AppConfig.model_validate(
        {
            "portfolios": [
                {
                    "name": "p",
                    "universe": ["BTC/USDT", "ETH/USDT"],
                    "signal": {"ref": _FAKE_SIGNAL},
                    "capital": "100000",
                    "data": {"exchange": "binance", "span": 86400},
                    "gross_cap": "1.5",
                }
            ]
        }
    )
    engine = build_engine(cfg, db_path=None)
    client = _daily_client({"BTCUSDT": [50000.0], "ETHUSDT": [2500.0]})
    runners = build_portfolio_runners(cfg, engine, dccd_client=client)
    assert runners[0].strategy.gross_cap == money("1.5")


# --- backward compat ------------------------------------------------------- #


def test_config_without_portfolios_defaults_empty() -> None:
    """A config with no portfolios validates and has an empty portfolios list."""
    cfg = AppConfig.model_validate({"strategies": []})
    assert cfg.portfolios == []


async def test_run_app_no_portfolios_unchanged() -> None:
    """run_app over a portfolio-free config: report has no portfolios, runs fine."""
    cfg = AppConfig()  # no strategies, no portfolios
    report = await run_app(cfg)
    assert isinstance(report, RunReport)
    assert report.strategies == []
    assert report.portfolios == []
    assert report.total_orders == 0
    assert report.realised_pnl == money("0")


# --- offline end-to-end through run_app ------------------------------------ #


async def test_run_app_portfolio_orders_match_intended_targets() -> None:
    """run_app over one portfolio: per-coin orders/positions == weight×capital/price.

    Verification on real data (offline): the fake signal returns a fixed weight
    vector; with a flat constant price series the runner rebalances to the target
    on the first tick and holds, so the final shared-tracker position per coin
    must equal ``weight × capital / price``. That target is also recomputed
    independently from the **broker-confirmed fills** (``Position.from_fills``),
    the PnL source of truth.
    """
    # Constant closes == the fake_book reference prices, so target qty is exact:
    #   BTC: +0.5 * 100000 / 50000 = +1.0 ; ETH: -0.25 * 100000 / 2500 = -10.0
    btc_price, eth_price = 50000.0, 2500.0
    n_days = 4
    cfg = _one_portfolio_config(capital="100000")
    client = _daily_client(
        {
            "BTCUSDT": [btc_price] * n_days,
            "ETHUSDT": [eth_price] * n_days,
        }
    )

    # Build the engine ourselves so we can read the broker's fills back.
    engine = build_engine(cfg, db_path=None)
    runners = build_portfolio_runners(cfg, engine, dccd_client=client)
    assert len(runners) == 1

    from trading_bot.application.orchestrator import Orchestrator

    orch = Orchestrator(event_bus=engine.bus)
    orch.add_all(runners)  # type: ignore[arg-type]
    await orch.run()

    # Intended targets (exact Decimal arithmetic, mirroring weights_to_signals).
    intended = {
        BTC_USDT: fake_book.WEIGHTS[BTC] * money("100000") / money(str(btc_price)),
        ETH_USDT: fake_book.WEIGHTS[ETH] * money("100000") / money(str(eth_price)),
    }
    assert intended[BTC_USDT] == money("1")
    assert intended[ETH_USDT] == money("-10")

    # The shared tracker's final per-coin net position == the intended target.
    for instrument, target in intended.items():
        tracked = engine.tracker.position(instrument)
        assert tracked is not None
        assert tracked.net_qty == target

    # And recomputed independently from the broker-confirmed fills.
    fills = await engine.broker.fills()
    assert fills, "the rebalance should have produced fills"
    by_instrument: dict[Instrument, list[Fill]] = {}
    for fill in fills:
        by_instrument.setdefault(fill.instrument, []).append(fill)
    for instrument, target in intended.items():
        expected = Position.from_fills(by_instrument[instrument])
        assert expected.net_qty == target
        tracked = engine.tracker.position(instrument)
        assert tracked is not None
        assert tracked.net_qty == expected.net_qty
        assert tracked.realised_pnl == expected.realised_pnl


async def test_run_app_report_shape_per_portfolio() -> None:
    """The RunReport carries a PortfolioReport per portfolio with per-coin coins."""
    cfg = _one_portfolio_config(capital="100000")
    client = _daily_client({"BTCUSDT": [50000.0] * 4, "ETHUSDT": [2500.0] * 4})

    report = await run_app(cfg, dccd_client=client)

    assert report.strategies == []
    assert len(report.portfolios) == 1
    pf = report.portfolios[0]
    assert isinstance(pf, PortfolioReport)
    assert pf.name == "fake-pf"
    # Two legs (BTC long, ETH short) on the first rebalance; held after.
    assert pf.orders_submitted == 2
    assert report.total_orders == 2
    # One coin report per universe member, in universe order, each with a position.
    assert [c.instrument for c in pf.coins] == [BTC_USDT, ETH_USDT]
    for coin in pf.coins:
        assert isinstance(coin, PortfolioCoinReport)
        assert coin.position is not None
    assert pf.coins[0].position.net_qty == money("1")  # type: ignore[union-attr]
    assert pf.coins[1].position.net_qty == money("-10")  # type: ignore[union-attr]


async def test_run_app_portfolio_alongside_strategy() -> None:
    """A portfolio runs alongside a single-instrument strategy (disjoint coins)."""
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    cfg = _one_portfolio_config(
        extra_strategies=[
            {
                "name": "ltc-ma",
                "symbol": "LTC/USDT",
                "data": {"exchange": "binance", "span": 60},
                "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                "reference_qty": "5",
                "lookback": 6,
            }
        ]
    )
    # The portfolio reads BTC/ETH daily; the strategy reads LTC (single-coin feed
    # keyed by the strategy's symbol string).
    pf_client = _daily_client({"BTCUSDT": [50000.0] * 30, "ETHUSDT": [2500.0] * 30})
    # Give the LTC strategy a trending series so it trades; one shared client must
    # answer both the portfolio reads (BTCUSDT/ETHUSDT) and the strategy read
    # (LTC/USDT — the raw symbol string feed_for passes).
    ltc_trend = [100.0 + i for i in range(15)] + [115.0 - i for i in range(1, 16)]
    pf_client._frames["LTC/USDT"] = _dccd_ohlc(ltc_trend)

    report = await run_app(cfg, dccd_client=pf_client)

    assert len(report.strategies) == 1
    assert len(report.portfolios) == 1
    assert report.strategies[0].name == "ltc-ma"
    assert report.portfolios[0].name == "fake-pf"
    # Both books traded, disjoint instruments.
    assert report.portfolios[0].orders_submitted == 2
    assert report.strategies[0].orders_submitted > 0


async def test_run_app_max_steps_caps_portfolio() -> None:
    """max_steps bounds the portfolio runner's rebalance ticks."""
    cfg = _one_portfolio_config(capital="100000")
    client = _daily_client({"BTCUSDT": [50000.0] * 10, "ETHUSDT": [2500.0] * 10})

    # Cap at one tick: the first rebalance opens both legs, then we stop.
    report = await run_app(cfg, dccd_client=client, max_steps=1)
    assert report.portfolios[0].orders_submitted == 2

    # Cap at zero ticks: nothing rebalances, no orders.
    report0 = await run_app(cfg, dccd_client=client, max_steps=0)
    assert report0.portfolios[0].orders_submitted == 0
    for coin in report0.portfolios[0].coins:
        assert coin.position is None


# --- overlap detection ----------------------------------------------------- #


def test_portfolio_overlaps_strategy_is_config_error() -> None:
    """A portfolio coin shared with a single-instrument strategy → ConfigError."""
    cfg = _one_portfolio_config(
        extra_strategies=[
            {
                "name": "btc-ma",
                "symbol": "BTC/USDT",  # already owned by the portfolio
                "data": {"exchange": "binance", "span": 60},
                "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
            }
        ]
    )
    with pytest.raises(ConfigError) as exc_info:
        # run_app rejects up front; assert via the sync overlap guard.
        from trading_bot.application.run_app import _reject_commingled

        _reject_commingled(cfg)

    msg = str(exc_info.value)
    assert "BTC/USDT" in msg
    assert "btc-ma" in msg
    assert "fake-pf" in msg
    assert "commingle" in msg


def test_two_portfolios_overlap_is_config_error() -> None:
    """Two portfolios sharing a coin → ConfigError (same shared tracker)."""
    cfg = _one_portfolio_config(
        extra_portfolios=[
            {
                "name": "other-pf",
                "universe": ["ETH/USDT", "LTC/USDT"],  # ETH overlaps fake-pf
                "signal": {"ref": _FAKE_SIGNAL},
                "capital": "50000",
                "data": {"exchange": "binance", "span": 86400},
            }
        ]
    )
    from trading_bot.application.run_app import _reject_commingled

    with pytest.raises(ConfigError, match="commingle") as exc_info:
        _reject_commingled(cfg)
    msg = str(exc_info.value)
    assert "ETH/USDT" in msg
    assert "fake-pf" in msg
    assert "other-pf" in msg


async def test_run_app_surfaces_portfolio_strategy_overlap() -> None:
    """run_app surfaces a portfolio↔strategy overlap as a ConfigError."""
    cfg = _one_portfolio_config(
        extra_strategies=[
            {
                "name": "eth-ma",
                "symbol": "ETH/USDT",  # owned by the portfolio
                "data": {"exchange": "binance", "span": 60},
                "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
            }
        ]
    )
    with pytest.raises(ConfigError, match="commingle"):
        await run_app(cfg, dccd_client=_FakeDailyDccdClient({}))


def test_disjoint_portfolio_and_strategy_build_fine() -> None:
    """Disjoint coins across a portfolio + strategy build without error."""
    cfg = _one_portfolio_config(
        extra_strategies=[
            {
                "name": "ltc-ma",
                "symbol": "LTC/USDT",  # disjoint from BTC/ETH
                "data": {"exchange": "binance", "span": 60},
                "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
            }
        ]
    )
    from trading_bot.application.run_app import _reject_commingled

    _reject_commingled(cfg)  # does not raise


# --- the resample-on-read adapter (canned) --------------------------------- #

_MIN_NS = 60 * 1_000_000_000
_DAY_MIN = 1440


def _canned_1m(
    days: list[dict[str, float]], *, partial_last_minutes: int = 0
) -> pl.DataFrame:
    """Build canned 1-minute dccd bars from a per-day OHLCV spec.

    Each ``days`` entry is a full calendar day of 1m bars synthesised so its daily
    aggregate is known: the day's first minute opens at ``open``, one minute hits
    ``high``, one hits ``low``, the last minute closes at ``close``, and every
    minute carries ``vol`` so the daily volume is ``1440 * vol``. An optional
    ``partial_last_minutes`` appends a trailing *incomplete* day (which the
    resampler must drop).
    """
    ts: list[int] = []
    o: list[float] = []
    h: list[float] = []
    low: list[float] = []
    c: list[float] = []
    v: list[float] = []
    minute = 0
    for spec in days:
        for m in range(_DAY_MIN):
            ts.append(minute * _MIN_NS)
            # open on first minute, close on last; high/low planted mid-day.
            o.append(spec["open"] if m == 0 else spec["close"])
            h.append(spec["high"] if m == 1 else spec["close"])
            low.append(spec["low"] if m == 2 else spec["close"])
            c.append(spec["close"])
            v.append(spec["vol"])
            minute += 1
    for m in range(partial_last_minutes):
        ts.append(minute * _MIN_NS)
        o.append(999.0)
        h.append(999.0)
        low.append(999.0)
        c.append(999.0)
        v.append(7.0)
        minute += 1
    return pl.DataFrame(
        {
            "TS": ts,
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "volume": v,
            "quote_volume": [0.0] * len(ts),
            "trades": [1] * len(ts),
        }
    )


class _InnerFromFrame:
    """A minimal dccd client returning a fixed 1m frame (honours end_ns)."""

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame
        self.span_seen: int | None = None

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        self.span_seen = span
        frame = self._frame
        if end_ns is not None:
            frame = frame.filter(pl.col("TS") <= end_ns)
        return frame

    def backfill(self, *a: object, **k: object) -> None:  # pragma: no cover
        return None


def test_resample_daily_ohlcv_is_correct_and_causal() -> None:
    """Canned 1m → daily: o=first, h=max, l=min, c=last, v=sum; partial day dropped."""
    days = [
        {"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "vol": 1.0},
        {"open": 200.0, "high": 222.0, "low": 188.0, "close": 210.0, "vol": 2.0},
    ]
    # Two full days + a 3-minute partial third day (must be dropped).
    frame = _canned_1m(days, partial_last_minutes=3)
    inner = _InnerFromFrame(frame)
    client = ResamplingDccdClient(inner)

    daily = client.read("binance", "BTC-USDT", "ohlc", 86400)

    # The adapter read the *source* (1m) span, not the daily span.
    assert inner.span_seen == 60
    # Only the two CLOSED days survive (the partial day is dropped — causality).
    assert daily.height == 2
    # OHLCV correctness per day.
    assert daily["open"].to_list() == [100.0, 200.0]
    assert daily["high"].to_list() == [110.0, 222.0]
    assert daily["low"].to_list() == [90.0, 188.0]
    assert daily["close"].to_list() == [105.0, 210.0]
    assert daily["volume"].to_list() == [1440.0 * 1.0, 1440.0 * 2.0]
    # Causal: each daily bar is stamped at the DAY'S OPEN (left-labelled), in ns.
    assert daily["TS"].to_list() == [0, _DAY_NS]
    # The dccd OHLC schema is preserved (so the normaliser downstream is happy).
    assert set(daily.columns) >= {
        "TS",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }


def test_resample_passes_through_non_daily_span() -> None:
    """A read whose span != daily_span is forwarded unchanged (no resampling)."""
    frame = _canned_1m(
        [{"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "vol": 1.0}]
    )
    inner = _InnerFromFrame(frame)
    client = ResamplingDccdClient(inner)

    out = client.read("binance", "BTC-USDT", "ohlc", 60)  # the source span itself
    assert inner.span_seen == 60
    # Pass-through returns the raw 1m frame untouched (1440 rows, not aggregated).
    assert out.height == _DAY_MIN


def test_resample_keeps_last_day_when_complete() -> None:
    """A final day whose 1m data reaches its end is KEPT (not dropped)."""
    days = [
        {"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "vol": 1.0},
        {"open": 200.0, "high": 222.0, "low": 188.0, "close": 210.0, "vol": 2.0},
    ]
    frame = _canned_1m(days, partial_last_minutes=0)  # both days complete
    client = ResamplingDccdClient(_InnerFromFrame(frame))
    daily = client.read("binance", "BTC-USDT", "ohlc", 86400)
    assert daily.height == 2  # both kept


def test_resample_rejects_bad_source_span() -> None:
    """source_span must be a positive value finer than daily_span."""
    inner = _InnerFromFrame(pl.DataFrame())
    with pytest.raises(ValueError, match="finer"):
        ResamplingDccdClient(inner, daily_span=86400, source_span=86400)
    with pytest.raises(ValueError, match="positive"):
        ResamplingDccdClient(inner, source_span=0)


def test_resample_empty_source_yields_empty() -> None:
    """An empty source frame resamples to an empty (correctly-shaped) frame."""
    empty = pl.DataFrame(
        schema={
            "TS": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "quote_volume": pl.Float64,
            "trades": pl.Int64,
        }
    )
    client = ResamplingDccdClient(_InnerFromFrame(empty))
    out = client.read("binance", "BTC-USDT", "ohlc", 86400)
    assert out.height == 0


# --- Verification on real data (opt-in) ------------------------------------ #


@pytest.mark.network
def test_resample_adapter_against_real_binance_1m_store() -> None:
    """ResamplingDccdClient over the REAL dccd Binance 1m store: daily OHLC sanity.

    Wraps an inner client that reads the real 1-minute parquet for one coin
    (``BTC-USDT``, year 2024 — fully within the store so every day is closed),
    resamples to daily via the adapter, and asserts the daily bar count + OHLC
    sanity (h≥o≥l, h≥c≥l, v>0) and that the resampled daily aggregate matches an
    **independent** ``group_by_dynamic`` aggregation of the same raw 1m bars
    (mirroring ``fynance_research.data.load_ohlc``).

    Skips with a clear how-to-sync reason when the store is absent (sync the alt
    *-USDT 1m pairs via the dccd daemon — see ../fynance-research/DEPLOY_LS1.md
    §3; they land at ~/data/arthurserver/binance/ohlc/<PAIR>/1m/).
    """
    from pathlib import Path

    base = Path.home() / "data" / "arthurserver" / "binance" / "ohlc" / "BTC-USDT" / "1m"
    files = sorted(base.glob("2024.parquet")) or sorted(base.glob("*.parquet"))
    if not files:
        pytest.skip(
            "no dccd Binance 1m store for BTC-USDT at "
            "~/data/arthurserver/binance/ohlc/BTC-USDT/1m; sync the *-USDT 1m "
            "pairs via the dccd daemon (DEPLOY_LS1.md §3)"
        )

    raw = (
        pl.concat([pl.read_parquet(f) for f in files])
        .unique(subset="TS", keep="last")
        .sort("TS")
    )

    class _Inner:
        def read(
            self,
            exchange: str,
            symbol: str,
            data_type: str = "ohlc",
            span: int | None = None,
            start_ns: int | None = None,
            end_ns: int | None = None,
        ) -> pl.DataFrame:
            frame = raw
            if end_ns is not None:
                frame = frame.filter(pl.col("TS") <= end_ns)
            return frame

        def backfill(self, *a: object, **k: object) -> None:  # pragma: no cover
            return None

    daily = ResamplingDccdClient(_Inner()).read("binance", "BTC-USDT", "ohlc", 86400)

    # Independent reference aggregation (fynance_research-style).
    ref = (
        raw.with_columns(pl.from_epoch("TS", time_unit="ns").alias("dt"))
        .group_by_dynamic("dt", every="1d", closed="left", label="left")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum(),
        )
        .sort("dt")
    )

    assert daily.height > 300, f"too few daily bars from real 1m: {daily.height}"
    # Daily count matches the independent reference (or is at most one fewer — the
    # adapter drops a still-forming final day the naive ref would keep).
    assert ref.height - daily.height in (0, 1), (
        f"resampled {daily.height} vs ref {ref.height} daily bars"
    )

    # OHLC sanity on every resampled daily bar.
    sane = daily.filter(
        (pl.col("high") >= pl.col("open"))
        & (pl.col("open") >= pl.col("low"))
        & (pl.col("high") >= pl.col("close"))
        & (pl.col("close") >= pl.col("low"))
        & (pl.col("volume") > 0)
    )
    assert sane.height == daily.height, "some daily bar violated OHLC sanity"

    # Value-for-value match against the reference on the overlapping prefix.
    n = daily.height
    for col in ("open", "high", "low", "close", "volume"):
        assert daily[col][:n].to_list() == ref[col][:n].to_list(), f"{col} mismatch"

    # Causal stamping: each daily TS is midnight UTC (day-open, left-labelled).
    assert all(ts % _DAY_NS == 0 for ts in daily["TS"].to_list())
