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

from trading_bot.application.portfolio import (
    PortfolioSignalFn,
    as_portfolio_signal,
)
from trading_bot.domain.instrument import Symbol
from trading_bot.domain.money import Money

if TYPE_CHECKING:
    import polars as pl


def _target_weights(venue: str) -> object:
    """Call the research LS1 oracle for ``venue``, importing ``fynance_research`` lazily.

    Imports ``fynance_research.strategies.ls1_live.target_weights`` only when the
    signal is *evaluated* (not when this module is imported / the config is
    resolved), so the research dependency is needed only to actually run LS1.
    ``venue`` selects the book: ``"binance"`` (USDT pairs, the validated default)
    or ``"kraken"`` (USD pairs). Returns whatever the oracle returns — a
    ``{pair: weight}`` mapping or a ``(mapping, asof)`` tuple; the generic adapter
    handles either shape (and normalises the ``BTC-USDT`` / ``BTC-USD`` keys).
    """
    from fynance_research.strategies.ls1_live import (  # noqa: PLC0415
        target_weights,
    )

    return target_weights(venue)


def _venue_signal(venue: str) -> PortfolioSignalFn:
    """Build the adapted, contract-shaped LS1 signal for ``venue`` (lazy oracle).

    The generic :func:`~trading_bot.application.portfolio.as_portfolio_signal`
    bound to an argument-free closure over :func:`_target_weights` — so the
    research import stays lazy and the venue's pair keys (USDT or USD) are
    normalised to canonical :class:`~trading_bot.domain.instrument.Symbol`\\ s.
    """
    return as_portfolio_signal(lambda: _target_weights(venue))


def ls1_portfolio_signal(
    asof_ms: int, frames: Mapping[Symbol, "pl.DataFrame"]
) -> Mapping[Symbol, Money]:
    """The LS1 weight-vector signal, as a :data:`PortfolioSignalFn`.

    LS1 on **Binance** (USDT pairs) — the validated default venue. A module-level,
    parameter-free :data:`~trading_bot.application.portfolio.PortfolioSignalFn` a
    portfolio config resolves by reference (``configs/ls1.yaml``). The generic
    :func:`~trading_bot.application.portfolio.as_portfolio_signal` adapter bound to
    the lazily-imported research oracle, so the ``{"BTC-USDT": w}`` pair-string
    keys are normalised to canonical
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
    return _ADAPTED_BINANCE(asof_ms, frames)


def ls1_kraken_signal(
    asof_ms: int, frames: Mapping[Symbol, "pl.DataFrame"]
) -> Mapping[Symbol, Money]:
    """LS1 on **Kraken** (USD pairs) — same strategy, the original validation venue.

    The Kraken counterpart of :func:`ls1_portfolio_signal`: it calls the research
    oracle with ``venue="kraken"`` (USD-quoted, ~10 ``-USD`` pairs) and the generic
    adapter normalises the ``BTC-USD`` keys to canonical ``Symbol``\\ s. A portfolio
    config resolves it by reference (``configs/ls1_kraken.yaml``). See
    :func:`ls1_portfolio_signal` for the parameter/return contract.
    """
    return _ADAPTED_KRAKEN(asof_ms, frames)


#: The adapted, contract-shaped LS1 signals per venue (built once at import; the
#: research import inside each stays lazy until the signal is evaluated).
_ADAPTED_BINANCE = _venue_signal("binance")
_ADAPTED_KRAKEN = _venue_signal("kraken")
