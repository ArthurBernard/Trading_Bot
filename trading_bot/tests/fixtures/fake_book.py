"""A tiny in-repo fake :data:`PortfolioSignalFn` for the portfolio tests.

:func:`fixed_weights` is a deterministic, frame-agnostic portfolio signal over a
small two-name universe (``BTC/USDT``, ``ETH/USDT``): it ignores its inputs and
always returns the same weight vector. It exists so downstream leaves (the
by-reference loader test here, the runner in leaf 03) have an *importable*
``module:function`` portfolio signal to resolve and drive — never an
``exec``\\ 'd loose file.

The fixed weights are deliberately chosen so they round-trip *exactly* through
:func:`~trading_bot.application.portfolio.weights_to_signals` with the
hand-calc constants below — see :data:`UNIVERSE` / :data:`PRICES` /
:data:`CAPITAL` and the test that asserts the resulting target quantities.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from trading_bot.domain.instrument import Symbol
from trading_bot.domain.money import Money, money

if TYPE_CHECKING:
    import polars as pl

#: The fake's two-name universe (canonical symbols).
UNIVERSE: tuple[Symbol, ...] = (Symbol("BTC", "USDT"), Symbol("ETH", "USDT"))

#: A reference price per symbol the hand-calc / round-trip test uses.
PRICES: Mapping[Symbol, Money] = {
    Symbol("BTC", "USDT"): money("50000"),
    Symbol("ETH", "USDT"): money("2500"),
}

#: The capital base the hand-calc / round-trip test uses.
CAPITAL: Money = money("100000")

#: The fixed weight vector the fake always returns: long half the capital in
#: BTC, short a quarter in ETH. With :data:`PRICES` / :data:`CAPITAL` this sizes
#: to exactly ``+1.0`` BTC and ``-10.0`` ETH (the round-trip the test asserts).
WEIGHTS: Mapping[Symbol, Money] = {
    Symbol("BTC", "USDT"): money("0.5"),
    Symbol("ETH", "USDT"): money("-0.25"),
}


def fixed_weights(
    asof_ms: int, frames: Mapping[Symbol, "pl.DataFrame"]
) -> Mapping[Symbol, Money]:
    """Return a fixed weight vector, ignoring ``asof_ms`` / ``frames``.

    A deterministic :data:`~trading_bot.application.portfolio.PortfolioSignalFn`
    for tests: always :data:`WEIGHTS`, regardless of inputs.

    Parameters
    ----------
    asof_ms : int
        Ignored (a real signal would stamp it; this fake is constant).
    frames : Mapping[Symbol, polars.DataFrame]
        Ignored (a real signal would read causal bars; this fake is constant).

    Returns
    -------
    Mapping[Symbol, Money]
        The fixed :data:`WEIGHTS` vector.

    """
    return WEIGHTS
