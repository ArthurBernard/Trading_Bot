"""LS1 portfolio signal — the thin config wrapper for the research weight oracle.

This is the *only* LS1-aware glue, and it is tiny: it points the generic
:func:`trading_bot.application.portfolio.as_portfolio_signal` adapter at the
research oracle ``fynance_research.strategies.ls1_live.target_weights`` (an
argument-free callable that reads dccd's Binance store itself and returns
``{"BTC-USDT": +0.31, "ZEC-USDT": -0.18, ...}``, ``Σ|w| ≤ 2``). See
``../fynance-research/DEPLOY_LS1.md`` for the strategy, the universe and the live
signal API.

Why a wrapper module (the config seam)
--------------------------------------
``trading_bot`` stays generic — it ships **no** research dependency and **no**
LS1 code in the engine. A portfolio config names its signal as a safe
``"module:function"`` reference that
:func:`trading_bot.application.portfolio.load_portfolio_signal` imports and
``getattr``\\ s; the resolved callable must already match the
:data:`~trading_bot.application.portfolio.PortfolioSignalFn` contract
(``(asof_ms, frames) -> {Symbol: weight}``). The research oracle does **not**
(it is argument-free and returns pair-strings), so this module exposes a
module-level :data:`ls1_portfolio_signal` that *is* the adapted, contract-shaped
callable. ``configs/ls1.yaml`` points its ``signal.ref`` at
``examples.ls1_signal:ls1_portfolio_signal``.

``fynance_research`` is imported **lazily**, inside the signal, so importing this
module (and resolving the config) never requires the research package — only
*evaluating* the signal at run time does. That keeps the offline test suite and
config validation free of the research dependency; install it editable from the
sibling repo to actually run LS1::

    pip install -e ../fynance-research

The adapter ignores the frames the runner passes (the oracle reads its own
store), but those frames still drive the runner's freshness gate and the
per-leg prices — so the engine remains the authority on *when* and *at what
price* each leg trades.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from trading_bot.application.portfolio import as_portfolio_signal
from trading_bot.domain.instrument import Symbol
from trading_bot.domain.money import Money

if TYPE_CHECKING:
    import polars as pl


def _target_weights() -> object:
    """Call the research LS1 oracle, importing ``fynance_research`` lazily.

    Imports ``fynance_research.strategies.ls1_live.target_weights`` only when the
    signal is *evaluated* (not when this module is imported / the config is
    resolved), so the research dependency is needed only to actually run LS1.
    Returns whatever the oracle returns — a ``{pair: weight}`` mapping or a
    ``(mapping, asof)`` tuple; the generic adapter handles either shape.
    """
    from fynance_research.strategies.ls1_live import (  # noqa: PLC0415
        target_weights,
    )

    return target_weights()


def ls1_portfolio_signal(
    asof_ms: int, frames: Mapping[Symbol, "pl.DataFrame"]
) -> Mapping[Symbol, Money]:
    """The LS1 weight-vector signal, as a :data:`PortfolioSignalFn`.

    A module-level, parameter-free
    :data:`~trading_bot.application.portfolio.PortfolioSignalFn` a portfolio
    config can resolve by reference. It is the generic
    :func:`~trading_bot.application.portfolio.as_portfolio_signal` adapter bound
    to :func:`_target_weights` (the lazily-imported research oracle), so the
    ``{"BTC-USDT": w}`` pair-string keys are normalised to canonical
    :class:`~trading_bot.domain.instrument.Symbol`\\ s and the weights to exact
    :class:`~trading_bot.domain.money.Money`.

    Parameters
    ----------
    asof_ms : int
        Ignored — the research oracle reads its own store and the engine's own
        as-of (from the freshness-gated feed) governs the rebalance timing.
    frames : Mapping[Symbol, polars.DataFrame]
        Ignored by the oracle, but the runner still uses them for the freshness
        gate and the per-leg prices.

    Returns
    -------
    Mapping[Symbol, Money]
        ``{Symbol: signed fraction of capital}`` with ``Σ|w| ≤ 2``.

    """
    return _ADAPTED(asof_ms, frames)


#: The adapted, contract-shaped LS1 signal (built once at import; the research
#: import inside it stays lazy).
_ADAPTED = as_portfolio_signal(_target_weights)
