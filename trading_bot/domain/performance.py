"""Pure PnL / KPI performance functions over a fill + price sequence.

This module is the modern, pure replacement for the legacy ``_PnLI`` /
``_PnLR`` / ``_FullPnL`` objects (``trading_bot/legacy/performance.py``,
referenced never imported). It rebuilds a strategy's profit-and-loss from the
**source of truth** — an ordered sequence of :class:`~trading_bot.domain.fill.
Fill` records — marked against a price series, and exposes the KPI ratios
(Sharpe, Sortino, max drawdown, Calmar) by delegating to **fynance**.

Two-tier design (money vs. series)
----------------------------------
The legacy ``_PnLI`` carried a single wide table whose columns were
``['price', 'returns', 'volume', 'exchanged_volume', 'position', 'signal',
'delta_signal', 'fee', 'PnL', 'cumPnL', 'value']``. Here that table is decomposed
into small, individually-testable typed functions:

* **money boundary — exact :class:`~decimal.Decimal`.** :func:`position_series`,
  :func:`fee_series`, :func:`pnl`, :func:`cum_pnl` and :func:`equity_curve`
  return tuples of ``Decimal``. Quantities, prices and fees never round-trip
  through ``float`` — the PnL of a real book must reconcile to the cent.
* **KPI series — ``float`` numpy.** The risk ratios are statistical estimators
  over a returns/equity path where ``float64`` is both sufficient and what
  fynance consumes. :func:`equity_array` bridges the two: it converts an exact
  equity curve into a ``float64`` ``numpy`` array to feed the KPI wrappers.

PnL convention (ported from legacy ``_get_PnL``)
------------------------------------------------
The legacy mark-to-market step was ``PnL_t = volume_t * returns_t * position_t
- fee_t`` with ``returns_t = price_t - price_{t-1}`` (an **absolute** price
change, not a percentage) and ``position_t`` the *signed* net exposure held
*going into* step ``t``. We keep exactly that: at each mark, the open signed
position earns the absolute price move since the previous mark, then the step's
fee is subtracted. Concretely, for marks ``p_0 .. p_n`` and the signed net
position ``q_{t-1}`` held over the interval ``(t-1, t]``::

    pnl_t = q_{t-1} * (p_t - p_{t-1}) - fee_t          (t >= 1)
    pnl_0 = -fee_0

``cum_pnl`` is the running sum and ``equity = v0 + cum_pnl`` (legacy
``value = cumPnL + v0``).

The inputs
----------
The functions take an **ordered** ``Sequence[Fill]`` (one instrument; in
execution order — the same contract as :meth:`~trading_bot.domain.position.
Position.from_fills`) plus a **mark price series**: a ``Sequence[Money]`` of one
price per fill, the price at which the book is marked when that fill lands. The
price series and the fill series share one index, so ``returns[t]`` is the move
from mark ``t-1`` to mark ``t``. Pass ``v0`` (initial capital) to anchor the
equity curve; it defaults to ``money("0")`` so ``equity_curve`` is just the
cumulative PnL when no starting capital is given.

fynance, lazily
---------------
fynance is a heavy optional dependency (the ``[triptych]`` extra) and is **not
importable in every environment**. The PnL core above is pure numpy/Decimal and
never touches it. The KPI wrappers :func:`sharpe`, :func:`sortino`,
:func:`max_drawdown` and :func:`calmar` import fynance **lazily, inside the
function body**, so ``import trading_bot.domain.performance`` always succeeds.
When fynance is absent the wrappers raise :class:`PerformanceDependencyError`
(a :class:`~trading_bot.domain.errors.TradingBotError`).

The module is pure: no I/O, no async, deterministic in fill order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np

from trading_bot.domain.errors import TradingBotError
from trading_bot.domain.fill import Fill
from trading_bot.domain.money import Money, money

if TYPE_CHECKING:
    from numpy.typing import NDArray

__all__ = [
    "PerformanceDependencyError",
    "returns",
    "exchanged_volume",
    "position_series",
    "fee_series",
    "pnl",
    "cum_pnl",
    "equity_curve",
    "equity_array",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
]

_ZERO: Money = money("0")


class PerformanceDependencyError(TradingBotError):
    """A KPI wrapper needs fynance but it is not importable.

    The PnL core of :mod:`trading_bot.domain.performance` is pure numpy/Decimal,
    but the KPI ratios (Sharpe, Sortino, max drawdown, Calmar) delegate to
    fynance — an optional ``[triptych]`` dependency. This is raised when one of
    those wrappers is called in an environment where ``import fynance`` fails.

    Parameters
    ----------
    func : str
        Name of the KPI function that required fynance.

    """

    def __init__(self, func: str) -> None:
        self.func = func
        super().__init__(
            f"{func}() requires the optional 'fynance' dependency "
            "(install the 'triptych' extra: pip install trading_bot[triptych])"
        )


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def _check_aligned(fills: Sequence[Fill], prices: Sequence[Money]) -> None:
    """Require a non-empty fill series and a price series of equal length."""
    if len(fills) != len(prices):
        raise ValueError(
            f"prices and fills must have equal length, "
            f"got {len(prices)} prices for {len(fills)} fills"
        )


# --------------------------------------------------------------------------- #
# Pure PnL core — exact Decimal at the money boundary
# --------------------------------------------------------------------------- #


def returns(prices: Sequence[Money]) -> tuple[Money, ...]:
    """Per-step **absolute** price changes (legacy ``_get_returns``).

    Ported from ``_PnLI._get_returns``: ``returns[t] = price[t] - price[t-1]``
    with ``returns[0] = 0``. These are absolute moves in quote units, *not*
    percentage returns — they are multiplied by the held quantity to give a
    mark-to-market PnL.

    Parameters
    ----------
    prices : Sequence[Money]
        The mark price series, one price per step, in chronological order.

    Returns
    -------
    tuple of Decimal
        The first-difference series, same length as ``prices`` (the first
        element is ``0``). Empty input yields an empty tuple.

    """
    if not prices:
        return ()
    out = [_ZERO]
    for prev, cur in zip(prices[:-1], prices[1:], strict=True):
        out.append(cur - prev)
    return tuple(out)


def exchanged_volume(fills: Sequence[Fill]) -> tuple[Money, ...]:
    """Per-step traded base volume (legacy ``exchanged_volume`` column).

    The magnitude of base units that changed hands at each step — ``fill.qty``,
    unsigned. Ported from ``_PnLI._get_exch_vol`` (the per-timestamp sum of
    executed volume); with one fill per step this is simply each fill's ``qty``.

    Parameters
    ----------
    fills : Sequence[Fill]
        The ordered fills.

    Returns
    -------
    tuple of Decimal
        The exchanged base volume at each step.

    """
    return tuple(f.qty for f in fills)


def position_series(fills: Sequence[Fill], *, initial: Money = _ZERO) -> tuple[Money, ...]:
    """Signed net position held **going into** each step.

    The exposure carried over the interval ending at step ``t`` — i.e. the net
    position *after* folding fills ``0 .. t-1`` (legacy ``_get_pos``'s ``ex_pos``
    snapshot taken before the step's own fill). ``position[0] == initial`` (the
    legacy ``pos_init``), and ``position[t]`` accumulates each prior fill's
    signed quantity. This is the quantity that earns step ``t``'s price move in
    :func:`pnl`.

    Parameters
    ----------
    fills : Sequence[Fill]
        The ordered fills.
    initial : Decimal, optional
        The net position held before the first fill (legacy ``ex_pos[0]`` /
        ``pos_init``). Defaults to flat (``0``).

    Returns
    -------
    tuple of Decimal
        The signed net position before each step, same length as ``fills``.

    """
    out: list[Money] = []
    held = initial
    for f in fills:
        out.append(held)
        held = held + f.signed_qty
    return tuple(out)


def fee_series(fills: Sequence[Fill]) -> tuple[Money, ...]:
    """Per-step fee charged, in quote units (legacy ``_get_fee``).

    Each fill's ``fee`` directly (already in quote units in the modern
    :class:`~trading_bot.domain.fill.Fill`, like the legacy real-mode
    ``_PnLR._get_fee`` which summed the venue-reported ``fee`` column — as
    opposed to ``_PnLI`` which derived it from a percentage).

    Parameters
    ----------
    fills : Sequence[Fill]
        The ordered fills.

    Returns
    -------
    tuple of Decimal
        The fee paid at each step.

    """
    return tuple(f.fee for f in fills)


def pnl(
    fills: Sequence[Fill],
    prices: Sequence[Money],
    *,
    initial_position: Money = _ZERO,
) -> tuple[Money, ...]:
    """Per-step mark-to-market PnL (legacy ``_get_PnL``).

    At each step the signed net position held *going into* the step earns the
    absolute price move since the previous mark, then the step's fee is
    subtracted::

        pnl_t = position_t * returns_t - fee_t

    where ``position_t`` is :func:`position_series` (the exposure carried over
    the interval ``(t-1, t]``), ``returns_t`` is :func:`returns` (the absolute
    move ``price_t - price_{t-1}``, zero at ``t == 0``) and ``fee_t`` is
    :func:`fee_series`. This is exactly the legacy ``volume * returns * position
    - fee`` with ``volume * position`` collapsed into the signed held quantity.

    Parameters
    ----------
    fills : Sequence[Fill]
        The ordered fills (one instrument, execution order).
    prices : Sequence[Money]
        The mark price series, one price per fill, same length as ``fills``.
    initial_position : Decimal, optional
        Net position held before the first fill. Defaults to flat.

    Returns
    -------
    tuple of Decimal
        The per-step PnL, same length as ``fills``.

    Raises
    ------
    ValueError
        If ``prices`` and ``fills`` differ in length.

    """
    _check_aligned(fills, prices)
    pos = position_series(fills, initial=initial_position)
    rets = returns(prices)
    fees = fee_series(fills)
    return tuple(
        p * r - fee for p, r, fee in zip(pos, rets, fees, strict=True)
    )


def cum_pnl(
    fills: Sequence[Fill],
    prices: Sequence[Money],
    *,
    initial_position: Money = _ZERO,
) -> tuple[Money, ...]:
    """Cumulative (running-sum) PnL (legacy ``cumPnL = cumsum(PnL)``).

    Parameters
    ----------
    fills : Sequence[Fill]
        The ordered fills.
    prices : Sequence[Money]
        The mark price series aligned to ``fills``.
    initial_position : Decimal, optional
        Net position held before the first fill. Defaults to flat.

    Returns
    -------
    tuple of Decimal
        The running cumulative PnL, same length as ``fills``.

    Raises
    ------
    ValueError
        If ``prices`` and ``fills`` differ in length.

    """
    steps = pnl(fills, prices, initial_position=initial_position)
    out: list[Money] = []
    running = _ZERO
    for step in steps:
        running = running + step
        out.append(running)
    return tuple(out)


def equity_curve(
    fills: Sequence[Fill],
    prices: Sequence[Money],
    *,
    v0: Money = _ZERO,
    initial_position: Money = _ZERO,
) -> tuple[Money, ...]:
    """The equity (account value) curve (legacy ``value = cumPnL + v0``).

    The initial capital ``v0`` plus the cumulative PnL at each step.

    Parameters
    ----------
    fills : Sequence[Fill]
        The ordered fills.
    prices : Sequence[Money]
        The mark price series aligned to ``fills``.
    v0 : Decimal, optional
        Initial capital available to the strategy. Defaults to ``0`` (so the
        curve is the bare cumulative PnL).
    initial_position : Decimal, optional
        Net position held before the first fill. Defaults to flat.

    Returns
    -------
    tuple of Decimal
        The equity at each step, same length as ``fills``.

    Raises
    ------
    ValueError
        If ``prices`` and ``fills`` differ in length.

    """
    return tuple(
        v0 + c
        for c in cum_pnl(fills, prices, initial_position=initial_position)
    )


def equity_array(equity: Sequence[Money]) -> NDArray[np.float64]:
    """Convert an exact equity curve to a ``float64`` array for the KPI wrappers.

    The KPI ratios are statistical estimators where ``float64`` is sufficient
    and is what fynance consumes; this is the single, explicit money→float
    boundary. Each :class:`~decimal.Decimal` is converted via ``float()``.

    Parameters
    ----------
    equity : Sequence[Money]
        An equity (or any value) curve as exact decimals.

    Returns
    -------
    numpy.ndarray
        A 1-D ``float64`` array of the same values.

    """
    return np.array([float(v) for v in equity], dtype=np.float64)


# --------------------------------------------------------------------------- #
# KPI wrappers — lazily delegate to fynance
# --------------------------------------------------------------------------- #


def _as_float_array(equity: Sequence[Money] | NDArray[np.float64]) -> NDArray[np.float64]:
    """Coerce an equity curve (Decimal sequence or float array) to ``float64``."""
    if isinstance(equity, np.ndarray):
        return equity.astype(np.float64)
    return equity_array(equity)


def sharpe(
    equity: Sequence[Money] | NDArray[np.float64],
    *,
    rf: float = 0.0,
    period: int = 252,
    log: bool = False,
) -> float:
    """Annualised Sharpe ratio of an equity curve, via ``fynance.metrics.sharpe``.

    Delegates to fynance's :func:`fynance.metrics.sharpe`
    (signature ``sharpe(X, rf=0, period=252, log=False, axis=0, dtype=None,
    ddof=0)``): annualised excess return over annualised volatility.

    Parameters
    ----------
    equity : Sequence[Money] or numpy.ndarray
        The equity / value curve (exact decimals or a ``float64`` array).
    rf : float, optional
        Annualised risk-free rate. Default ``0``.
    period : int, optional
        Periods per year for annualisation. Default ``252`` (trading days).
    log : bool, optional
        Use log-returns instead of simple returns. Default ``False``.

    Returns
    -------
    float
        The Sharpe ratio.

    Raises
    ------
    PerformanceDependencyError
        If fynance is not importable.

    """
    try:
        import fynance.metrics as fy_metrics
    except ImportError as exc:
        raise PerformanceDependencyError("sharpe") from exc
    x = _as_float_array(equity)
    return float(fy_metrics.sharpe(x, rf=rf, period=period, log=log))


def sortino(
    equity: Sequence[Money] | NDArray[np.float64],
    *,
    rf: float = 0.0,
    period: int = 252,
    log: bool = False,
) -> float:
    """Annualised Sortino ratio of an equity curve, via ``fynance.metrics.sortino``.

    Delegates to fynance's :func:`fynance.metrics.sortino`
    (signature ``sortino(X, rf=0, period=252, log=False, axis=0, dtype=None,
    ddof=0)``): annualised excess return over *downside* deviation.

    Parameters
    ----------
    equity : Sequence[Money] or numpy.ndarray
        The equity / value curve.
    rf : float, optional
        Annualised risk-free rate. Default ``0``.
    period : int, optional
        Periods per year. Default ``252``.
    log : bool, optional
        Use log-returns. Default ``False``.

    Returns
    -------
    float
        The Sortino ratio.

    Raises
    ------
    PerformanceDependencyError
        If fynance is not importable.

    """
    try:
        import fynance.metrics as fy_metrics
    except ImportError as exc:
        raise PerformanceDependencyError("sortino") from exc
    x = _as_float_array(equity)
    return float(fy_metrics.sortino(x, rf=rf, period=period, log=log))


def max_drawdown(
    equity: Sequence[Money] | NDArray[np.float64],
    *,
    raw: bool = False,
) -> float:
    """Maximum drawdown of an equity curve, via ``fynance.metrics.mdd``.

    Delegates to fynance's :func:`fynance.metrics.mdd`
    (signature ``mdd(X, raw=False, axis=0, dtype=None)``): the worst
    peak-to-trough decline, as a fraction of the peak by default.

    Parameters
    ----------
    equity : Sequence[Money] or numpy.ndarray
        The equity / value curve (should be positive for the relative form).
    raw : bool, optional
        If ``True`` return the absolute decline; otherwise (default) the
        fractional drawdown.

    Returns
    -------
    float
        The maximum drawdown.

    Raises
    ------
    PerformanceDependencyError
        If fynance is not importable.

    """
    try:
        import fynance.metrics as fy_metrics
    except ImportError as exc:
        raise PerformanceDependencyError("max_drawdown") from exc
    x = _as_float_array(equity)
    return float(fy_metrics.mdd(x, raw=raw))


def calmar(
    equity: Sequence[Money] | NDArray[np.float64],
    *,
    period: int = 252,
) -> float:
    """Calmar ratio of an equity curve, via ``fynance.metrics.calmar``.

    Delegates to fynance's :func:`fynance.metrics.calmar`
    (signature ``calmar(X, period=252, axis=0, dtype=None, ddof=0)``):
    the compounded annual return over the maximum drawdown.

    Parameters
    ----------
    equity : Sequence[Money] or numpy.ndarray
        The equity / value curve.
    period : int, optional
        Periods per year. Default ``252``.

    Returns
    -------
    float
        The Calmar ratio.

    Raises
    ------
    PerformanceDependencyError
        If fynance is not importable.

    """
    try:
        import fynance.metrics as fy_metrics
    except ImportError as exc:
        raise PerformanceDependencyError("calmar") from exc
    x = _as_float_array(equity)
    return float(fy_metrics.calmar(x, period=period))
