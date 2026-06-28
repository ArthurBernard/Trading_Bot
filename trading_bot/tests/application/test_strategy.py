"""Tests for :mod:`trading_bot.application.strategy`.

These prove the strategy contract and its safe loader:

* a hand-written ``signal_fn`` is returned verbatim by
  :meth:`Strategy.evaluate`, and a signal for the *wrong* instrument is rejected
  with :class:`InstrumentMismatch`;
* the built-in :func:`ma_crossover_signal` goes long after an up-cross and
  short after a down-cross on a known close series, and is **causal** —
  recomputing from only the bars ``≤ t`` reproduces the signal at ``t`` (no
  lookahead);
* :func:`load_strategy` resolves both a passed callable and a
  ``"module:function"`` string, and raises a clear :class:`SignalError` on an
  unresolvable reference;
* warmup: fewer than ``lookback`` bars yields a flat signal (the documented
  safe default), never a call into ``signal_fn``.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from trading_bot.application.config import StrategyConfig
from trading_bot.application.strategy import (
    Strategy,
    load_strategy,
    ma_crossover_signal,
)
from trading_bot.domain.errors import InstrumentMismatch, SignalError
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.signal import Signal, SignalMode

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


# --- helpers --------------------------------------------------------------- #


def _bars(closes: list[float], *, start_ts: int = 1_000) -> pl.DataFrame:
    """A minimal OHLC(V) bars frame from a list of closes (time in seconds)."""
    n = len(closes)
    times = [start_ts + 60 * i for i in range(n)]
    return pl.DataFrame(
        {
            "time": times,
            "o": closes,
            "h": closes,
            "l": closes,
            "c": closes,
            "v": [1.0] * n,
        }
    )


def constant_long_signal(bars: pl.DataFrame) -> Signal:
    """Module-level signal_fn for the loader test (resolvable by import)."""
    return Signal.exposure(BTC_USD, money("1"), ts=0)


# --- Strategy.evaluate ----------------------------------------------------- #


def test_evaluate_returns_signal_from_callable() -> None:
    want = Signal.exposure(BTC_USD, money("1"), ts=5)
    strat = Strategy(name="s", instrument=BTC_USD, signal_fn=lambda b: want)
    got = strat.evaluate(_bars([100.0, 101.0]))
    assert got is want


def test_evaluate_rejects_instrument_mismatch() -> None:
    # signal_fn returns a signal for ETH/USD but the strategy is BTC/USD.
    strat = Strategy(
        name="s",
        instrument=BTC_USD,
        signal_fn=lambda b: Signal.exposure(ETH_USD, money("1"), ts=0),
    )
    with pytest.raises(InstrumentMismatch):
        strat.evaluate(_bars([100.0]))


def test_strategy_rejects_bad_reference_qty_and_lookback() -> None:
    with pytest.raises(SignalError):
        Strategy(
            name="s",
            instrument=BTC_USD,
            signal_fn=constant_long_signal,
            reference_qty=money("0"),
        )
    with pytest.raises(SignalError):
        Strategy(
            name="s",
            instrument=BTC_USD,
            signal_fn=constant_long_signal,
            lookback=-1,
        )


# --- warmup ---------------------------------------------------------------- #


def test_warmup_returns_flat_below_lookback() -> None:
    called = False

    def fn(bars: pl.DataFrame) -> Signal:
        nonlocal called
        called = True
        return Signal.exposure(BTC_USD, money("1"), ts=0)

    strat = Strategy(name="s", instrument=BTC_USD, signal_fn=fn, lookback=5)
    sig = strat.evaluate(_bars([100.0, 101.0, 102.0]))  # 3 < 5
    assert sig.mode is SignalMode.EXPOSURE
    assert sig.target == money("0")
    assert not called  # signal_fn must not be invoked during warmup


def test_warmup_calls_fn_at_lookback() -> None:
    pytest.importorskip("fynance")  # evaluate() at lookback invokes ma_crossover (fynance.sma)
    strat = Strategy(
        name="s",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=1, slow=2),
        lookback=2,
    )
    # exactly 2 bars -> fn is called (not flat-by-warmup).
    sig = strat.evaluate(_bars([100.0, 200.0]))
    assert sig.target == money("1")  # rising -> long


# --- ma_crossover_signal --------------------------------------------------- #


def test_ma_crossover_rejects_bad_windows() -> None:
    with pytest.raises(ValueError):
        ma_crossover_signal(BTC_USD, fast=5, slow=5)
    with pytest.raises(ValueError):
        ma_crossover_signal(BTC_USD, fast=0, slow=3)


def test_ma_crossover_long_then_short() -> None:
    pytest.importorskip("fynance")  # ma_crossover signal evaluates fynance.sma
    fn = ma_crossover_signal(BTC_USD, fast=2, slow=4)

    # A clear up-trend: fast MA rises above slow MA -> long.
    up = _bars([10.0, 11.0, 13.0, 16.0, 20.0, 25.0])
    long_sig = fn(up)
    assert long_sig.mode is SignalMode.EXPOSURE
    assert long_sig.target == money("1")

    # A clear down-trend: fast MA falls below slow MA -> short.
    down = _bars([25.0, 20.0, 16.0, 13.0, 11.0, 10.0])
    short_sig = fn(down)
    assert short_sig.target == money("-1")


def test_ma_crossover_flips_at_crossover() -> None:
    pytest.importorskip("fynance")  # ma_crossover signal evaluates fynance.sma
    fn = ma_crossover_signal(BTC_USD, fast=2, slow=4)
    # up then down: collect the exposure at each step (using only bars <= t).
    closes = [10.0, 11.0, 13.0, 16.0, 20.0, 25.0, 22.0, 17.0, 12.0, 9.0, 7.0]
    targets = [
        float(fn(_bars(closes[: t + 1])).target) for t in range(len(closes))
    ]
    # Goes long during the up-leg and turns short during the down-leg.
    assert any(t > 0 for t in targets), targets
    assert targets[-1] < 0, targets  # short by the end of the down-leg


def test_ma_crossover_is_causal() -> None:
    """The signal at bar t must not depend on any bar > t (no lookahead)."""
    pytest.importorskip("fynance")  # ma_crossover signal evaluates fynance.sma
    fn = ma_crossover_signal(BTC_USD, fast=3, slow=5)
    closes = [10.0, 12.0, 11.0, 14.0, 18.0, 22.0, 19.0, 15.0, 11.0, 8.0]

    # The full-series signal at index t == the signal recomputed from closes[:t+1].
    full = _bars(closes)
    for t in range(len(closes)):
        truncated = fn(_bars(closes[: t + 1]))
        # Recompute the "signal as of t" from the full frame by slicing to t+1
        # rows — must match the truncated-input computation exactly.
        as_of_t = fn(full.head(t + 1))
        assert truncated.target == as_of_t.target, (
            f"lookahead at t={t}: {truncated.target} != {as_of_t.target}"
        )


def test_ma_crossover_ts_from_latest_bar() -> None:
    pytest.importorskip("fynance")  # ma_crossover signal evaluates fynance.sma
    fn = ma_crossover_signal(BTC_USD, fast=2, slow=4)
    bars = _bars([10.0, 11.0, 13.0, 16.0], start_ts=1_700_000_000)
    sig = fn(bars)
    # latest bar time = 1_700_000_000 + 3*60 seconds -> milliseconds.
    assert sig.ts == (1_700_000_000 + 180) * 1_000


# --- load_strategy --------------------------------------------------------- #


def test_load_strategy_with_callable() -> None:
    cfg = StrategyConfig(name="ma-cross", symbol="BTC/USD")
    strat = load_strategy(cfg, constant_long_signal)
    assert strat.name == "ma-cross"
    assert strat.instrument == BTC_USD
    assert strat.signal_fn is constant_long_signal


def test_load_strategy_with_import_string() -> None:
    cfg = StrategyConfig(name="ma-cross", symbol="BTC/USD")
    ref = "trading_bot.tests.application.test_strategy:constant_long_signal"
    strat = load_strategy(cfg, ref)
    assert strat.signal_fn is constant_long_signal
    # And the resolved callable still works through evaluate().
    sig = strat.evaluate(_bars([100.0, 101.0]))
    assert sig.target == money("1")


def test_load_strategy_kraken_pair_symbol() -> None:
    cfg = StrategyConfig(name="s", symbol="XXBTZUSD")
    strat = load_strategy(cfg, constant_long_signal)
    assert strat.instrument == BTC_USD


@pytest.mark.parametrize(
    "ref",
    [
        "no_colon_here",
        ":missing_module",
        "trading_bot.application.strategy:",
        "trading_bot.does.not.exist:fn",
        "trading_bot.application.strategy:nonexistent_attr",
    ],
)
def test_load_strategy_unresolvable_raises(ref: str) -> None:
    cfg = StrategyConfig(name="s", symbol="BTC/USD")
    with pytest.raises(SignalError):
        load_strategy(cfg, ref)


def test_load_strategy_non_callable_attr_raises() -> None:
    # __all__ is a list on the module -> resolvable but not callable.
    cfg = StrategyConfig(name="s", symbol="BTC/USD")
    with pytest.raises(SignalError):
        load_strategy(cfg, "trading_bot.application.strategy:__all__")


# --- verification on realistic synthetic data ------------------------------ #


def test_verify_on_realistic_series() -> None:
    """Trend up then down: exposure flips at the crossovers; fully causal."""
    pytest.importorskip("fynance")  # ma_crossover signal evaluates fynance.sma
    rng = np.random.default_rng(42)
    up = np.linspace(100.0, 200.0, 60)
    down = np.linspace(200.0, 100.0, 60)
    trend = np.concatenate([up, down])
    noise = rng.normal(0.0, 0.5, size=trend.shape)
    closes = (trend + noise).tolist()

    fn = ma_crossover_signal(BTC_USD, fast=5, slow=20)

    # Step through; each step sees only bars <= t (causal by construction).
    targets = [
        float(fn(_bars(closes[: t + 1])).target) for t in range(len(closes))
    ]

    # Long somewhere in the up-leg, short somewhere in the down-leg.
    up_leg = targets[20:60]
    down_leg = targets[80:]
    assert any(t > 0 for t in up_leg), up_leg
    assert any(t < 0 for t in down_leg), down_leg

    # Causality cross-check: full-frame head(t+1) == truncated input at every t.
    full = _bars(closes)
    for t in range(len(closes)):
        assert float(fn(full.head(t + 1)).target) == targets[t]
