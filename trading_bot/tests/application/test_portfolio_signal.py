"""Tests for :mod:`trading_bot.application.portfolio`.

These prove the multi-asset signal contract and its safe by-reference loader:

* :func:`weights_to_signals` sizes a weight vector into explicit-quantity
  :class:`~trading_bot.domain.signal.Signal`\\ s with **exact** ``Decimal``
  arithmetic (``qty = weight * capital / price``): a hand-calc round-trips to the
  precise target quantities, a ``0`` weight is flat, and a missing/non-positive
  price (or non-positive capital) raises;
* single-name leverage (``|w| > 1``) is **allowed** — there is no ``[-1, 1]``
  rejection on the way to a target-quantity signal;
* :func:`load_portfolio_signal` resolves a ``"module:function"`` ref to a
  callable and raises a clear :class:`~trading_bot.domain.errors.ConfigError` on
  a no-``":"`` ref, a missing attribute, an unimportable module, or a
  non-callable target;
* the in-repo fake :func:`~trading_bot.tests.fixtures.fake_book.fixed_weights`
  round-trips through :func:`weights_to_signals` to the exact target quantities a
  hand-calc gives.
"""

from __future__ import annotations

import pytest

from trading_bot.application.portfolio import (
    PortfolioStrategy,
    load_portfolio_signal,
    weights_to_signals,
)
from trading_bot.domain.errors import ConfigError, SignalError
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.signal import SignalMode
from trading_bot.tests.fixtures import fake_book

BTC = Symbol("BTC", "USDT")
ETH = Symbol("ETH", "USDT")

PRICES = {BTC: money("50000"), ETH: money("2500")}
CAPITAL = money("100000")


def _by_symbol(signals: list) -> dict[Symbol, object]:
    """Index a list of signals by their instrument's symbol."""
    return {sig.instrument.symbol: sig for sig in signals}


# --- weights_to_signals: exact sizing -------------------------------------- #


def test_weights_to_signals_exact_quantities() -> None:
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    signals = weights_to_signals(
        weights, prices=PRICES, capital=CAPITAL, asof_ms=1_700
    )
    assert len(signals) == 2
    by_sym = _by_symbol(signals)

    btc_sig = by_sym[BTC]
    # 0.5 * 100000 / 50000 = +1.0 BTC (exact Decimal).
    assert btc_sig.mode is SignalMode.TARGET_QTY
    assert btc_sig.target == money("1")
    assert btc_sig.instrument == Instrument(BTC)
    assert btc_sig.ts == 1_700

    eth_sig = by_sym[ETH]
    # -0.25 * 100000 / 2500 = -10.0 ETH (exact Decimal).
    assert eth_sig.mode is SignalMode.TARGET_QTY
    assert eth_sig.target == money("-10")
    assert eth_sig.ts == 1_700


def test_zero_weight_is_flat_target() -> None:
    signals = weights_to_signals(
        {BTC: money("0")}, prices=PRICES, capital=CAPITAL, asof_ms=0
    )
    assert len(signals) == 1
    assert signals[0].mode is SignalMode.TARGET_QTY
    assert signals[0].target == money("0")


def test_leverage_weight_allowed_no_bound() -> None:
    # |w| = 2.5 > 1: single-name leverage. Allowed — no [-1, 1] rejection.
    # 2.5 * 100000 / 50000 = +5.0 BTC.
    signals = weights_to_signals(
        {BTC: money("2.5")}, prices=PRICES, capital=CAPITAL, asof_ms=1
    )
    assert signals[0].target == money("5")

    # And a leveraged short likewise.
    short = weights_to_signals(
        {BTC: money("-2")}, prices=PRICES, capital=CAPITAL, asof_ms=1
    )
    # -2 * 100000 / 50000 = -4.0 BTC.
    assert short[0].target == money("-4")


def test_missing_price_raises() -> None:
    with pytest.raises(SignalError):
        weights_to_signals(
            {ETH: money("0.5")},  # ETH price omitted below
            prices={BTC: money("50000")},
            capital=CAPITAL,
            asof_ms=0,
        )


@pytest.mark.parametrize("bad_price", ["0", "-1", "-50000"])
def test_non_positive_price_raises(bad_price: str) -> None:
    with pytest.raises(SignalError):
        weights_to_signals(
            {BTC: money("0.5")},
            prices={BTC: money(bad_price)},
            capital=CAPITAL,
            asof_ms=0,
        )


@pytest.mark.parametrize("bad_capital", ["0", "-100000"])
def test_non_positive_capital_raises(bad_capital: str) -> None:
    with pytest.raises(ConfigError):
        weights_to_signals(
            {BTC: money("0.5")},
            prices=PRICES,
            capital=money(bad_capital),
            asof_ms=0,
        )


def test_empty_weights_yields_no_signals() -> None:
    assert weights_to_signals({}, prices=PRICES, capital=CAPITAL, asof_ms=0) == []


# --- PortfolioStrategy ----------------------------------------------------- #


def test_portfolio_strategy_is_frozen_and_hashable() -> None:
    strat = PortfolioStrategy(
        name="p",
        universe=(BTC, ETH),
        signal_fn=fake_book.fixed_weights,
        capital=CAPITAL,
    )
    assert strat.gross_cap is None
    # Frozen: attribute assignment is rejected.
    with pytest.raises(Exception):
        strat.name = "other"  # type: ignore[misc]
    # Hashable (frozen + tuple universe).
    assert hash(strat) == hash(strat)


def test_portfolio_strategy_records_gross_cap() -> None:
    strat = PortfolioStrategy(
        name="p",
        universe=(BTC,),
        signal_fn=fake_book.fixed_weights,
        capital=CAPITAL,
        gross_cap=money("1.5"),
    )
    assert strat.gross_cap == money("1.5")


# --- load_portfolio_signal ------------------------------------------------- #


def test_load_portfolio_signal_resolves_callable() -> None:
    ref = "trading_bot.tests.fixtures.fake_book:fixed_weights"
    fn = load_portfolio_signal(ref)
    assert callable(fn)
    assert fn is fake_book.fixed_weights


@pytest.mark.parametrize(
    "ref",
    [
        "no_colon_here",
        ":missing_module",
        "trading_bot.tests.fixtures.fake_book:",
        "trading_bot.does.not.exist:fn",
        "trading_bot.tests.fixtures.fake_book:nonexistent_attr",
    ],
)
def test_load_portfolio_signal_bad_ref_raises_config_error(ref: str) -> None:
    with pytest.raises(ConfigError):
        load_portfolio_signal(ref)


def test_load_portfolio_signal_non_callable_raises() -> None:
    # WEIGHTS is a dict on the module -> resolvable but not callable.
    with pytest.raises(ConfigError):
        load_portfolio_signal(
            "trading_bot.tests.fixtures.fake_book:WEIGHTS"
        )


# --- fixture round-trip ---------------------------------------------------- #


def test_fake_fixture_weights_round_trip_to_exact_quantities() -> None:
    """The fake's fixed weights size to the exact hand-calc target quantities."""
    weights = fake_book.fixed_weights(asof_ms=0, frames={})
    signals = weights_to_signals(
        weights,
        prices=fake_book.PRICES,
        capital=fake_book.CAPITAL,
        asof_ms=42,
    )
    by_sym = _by_symbol(signals)

    # Hand-calc: 0.5 * 100000 / 50000 = +1 BTC ; -0.25 * 100000 / 2500 = -10 ETH.
    assert by_sym[BTC].target == money("1")
    assert by_sym[ETH].target == money("-10")
    assert all(s.mode is SignalMode.TARGET_QTY for s in signals)
    assert all(s.ts == 42 for s in signals)
