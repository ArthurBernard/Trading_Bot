"""Declare and load a strategy — config + a signal callable → domain ``Signal``.

A :class:`Strategy` is the engine's unit of *what to trade and when*: it pairs a
strategy's instrument with a **signal callable** (:data:`SignalFn`) that, given a
bars frame, returns a venue-neutral :class:`~trading_bot.domain.signal.Signal`.
The runner (leaf 03) drives a :class:`Strategy` on each new bar; this module only
declares the contract and how a strategy is loaded.

The signal contract
--------------------
A :data:`SignalFn` takes a **bars frame** and returns one
:class:`~trading_bot.domain.signal.Signal` for the strategy's instrument. The
bars frame is a :class:`polars.DataFrame` of OHLC(V) — the shape a dccd OHLC read
yields (leaf 02). The assumed columns are:

================  =========================================================
column            meaning
================  =========================================================
``time``          bar open time, seconds (or ms) since the Unix epoch (UTC)
``o`` ``h`` ``l`` open / high / low price of the bar
``c``             **close** price of the bar — the column signals read
``v``             traded volume over the bar
================  =========================================================

Only ``c`` (close) is required by the built-in example signal; a custom
:data:`SignalFn` may use any subset. Rows are ordered oldest→newest, so row
``-1`` is the most recent (current) bar. A signal computed at the current bar may
only look at rows ``≤`` the current one — **no lookahead**.

Loading — no arbitrary-file exec
--------------------------------
:func:`load_strategy` resolves a signal callable either from a passed callable
*or* from a safe ``"module:function"`` import string (via
:func:`importlib.import_module` + :func:`getattr`). This deliberately replaces
the legacy ``StrategyBot`` path, which built a module spec from an *arbitrary file
path* and ran :meth:`exec_module` on it (``legacy/strategy_manager.py``) — a code
sink. Here only an *already-importable* dotted module can be named; nothing is
executed from a loose file.

This module lives in the application layer: it may import the pure domain and
depends on :mod:`polars` (the bars frame type). The built-in
:func:`ma_crossover_signal` additionally needs :mod:`fynance`, but that is an
**optional** ``[triptych]`` dependency: it is imported *lazily*, inside the
signal callable, so importing this module never requires fynance — only
*calling* the built-in MA-crossover signal does (mirroring how
:mod:`trading_bot.domain.performance` defers its KPI imports). This module
performs no I/O of its own.
"""

from __future__ import annotations

import importlib
import warnings
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import polars as pl

from trading_bot.application.config import StrategyConfig
from trading_bot.domain.errors import InstrumentMismatch, SignalError
from trading_bot.domain.instrument import Instrument, Symbol, parse_kraken_pair
from trading_bot.domain.money import Money, money
from trading_bot.domain.signal import Signal

__all__ = [
    "SignalFn",
    "Strategy",
    "load_strategy",
    "ma_crossover_signal",
]

#: A strategy's signal callable: a bars frame (OHLC, polars) → a domain
#: :class:`~trading_bot.domain.signal.Signal` for the strategy's instrument.
SignalFn = Callable[[pl.DataFrame], Signal]

#: The close-price column the built-in signal reads from a bars frame.
_CLOSE_COL = "c"


def _bar_ts_ms(bars: pl.DataFrame) -> int:
    """Best-effort timestamp (ms since epoch, UTC) for the latest bar.

    Reads the ``time`` column of the last row if present, normalising a
    seconds-scale value to milliseconds (dccd OHLC times are seconds). Falls
    back to ``0`` when there is no usable time column, so a signal can still be
    built (``ts`` only needs to be non-negative).
    """
    if "time" not in bars.columns or bars.height == 0:
        return 0
    raw = bars["time"][-1]
    if raw is None:
        return 0
    val = int(raw)
    # Heuristic: a seconds-scale epoch (< ~1e12) is widened to milliseconds to
    # match Signal.ts / Fill.ts units; an already-ms value passes through.
    return val if val >= 1_000_000_000_000 else val * 1_000


@dataclass(frozen=True, slots=True)
class Strategy:
    """A declared strategy: an instrument and the signal callable that drives it.

    Immutable. Build one directly, or via :func:`load_strategy` from a
    :class:`~trading_bot.application.config.StrategyConfig`. The runner calls
    :meth:`evaluate` on each new bar to obtain the strategy's target
    :class:`~trading_bot.domain.signal.Signal`.

    Parameters
    ----------
    name : str
        Logical id of the strategy instance (matches the config's ``name``).
    instrument : Instrument
        The instrument the strategy trades. :meth:`evaluate` enforces that the
        signal callable returns a :class:`~trading_bot.domain.signal.Signal` for
        *this* instrument.
    signal_fn : SignalFn
        The signal callable: a bars frame → a domain ``Signal``.
    reference_qty : Decimal or None, optional
        The max position size (base units) a fractional-exposure signal is a
        fraction of. Carried here so the runner can resolve a
        :data:`~trading_bot.domain.signal.SignalMode.EXPOSURE` signal into a
        quantity. ``None`` (default) when the strategy emits explicit-quantity
        signals or the runner supplies the scale elsewhere. Must be positive
        when given.
    lookback : int, optional
        Warmup: the minimum number of bars required before the signal is
        meaningful (e.g. the slow MA window). Until that many bars are present
        :meth:`evaluate` returns a **flat** signal rather than calling
        ``signal_fn`` — see :meth:`evaluate`. Must be non-negative. Default ``0``
        (no warmup).

    """

    name: str
    instrument: Instrument
    signal_fn: SignalFn
    reference_qty: Money | None = None
    lookback: int = 0

    def __post_init__(self) -> None:
        """Validate ``reference_qty`` (>0) and ``lookback`` (>=0)."""
        if self.reference_qty is not None and self.reference_qty <= 0:
            raise SignalError(
                f"reference_qty must be positive, got {self.reference_qty}"
            )
        if self.lookback < 0:
            raise SignalError(f"lookback must be non-negative, got {self.lookback}")

    def evaluate(self, bars: pl.DataFrame) -> Signal:
        """Evaluate the strategy on ``bars`` and return its target signal.

        Warmup choice: if fewer than :attr:`lookback` bars are supplied, return
        a **flat** ``Signal.exposure(instrument, 0)`` rather than calling
        ``signal_fn`` or raising. Flat is the *safe* default — an undertrained
        signal that has not seen its full window should hold no position, never
        guess a direction. (Raising was the alternative; flat keeps the runner's
        loop simple and never opens a position on partial data.)

        Otherwise it calls ``signal_fn(bars)`` and validates the returned signal
        is for :attr:`instrument`, raising :class:`InstrumentMismatch` if a
        custom callable returns a signal for the wrong instrument.

        Parameters
        ----------
        bars : polars.DataFrame
            The OHLC(V) bars frame (oldest→newest); see the module docstring for
            the assumed schema.

        Returns
        -------
        Signal
            The strategy's target signal for :attr:`instrument` (flat during
            warmup).

        Raises
        ------
        InstrumentMismatch
            If ``signal_fn`` returns a signal for a different instrument.

        """
        if bars.height < self.lookback:
            return Signal.exposure(
                self.instrument, money("0"), ts=_bar_ts_ms(bars)
            )

        signal = self.signal_fn(bars)
        if signal.instrument != self.instrument:
            raise InstrumentMismatch(
                str(self.instrument), str(signal.instrument)
            )
        return signal


def _instrument_from_symbol(symbol: str) -> Instrument:
    """Build an :class:`Instrument` from a config ``symbol`` string.

    Accepts a canonical ``BASE/QUOTE`` (e.g. ``"BTC/USD"``) or a Kraken pair
    string (e.g. ``"XXBTZUSD"``); both route through
    :func:`~trading_bot.domain.instrument.parse_kraken_pair`, which honours an
    explicit ``/`` separator. Falls back to a 4/4-ish split error message via the
    parser.
    """
    sym: Symbol = parse_kraken_pair(symbol)
    return Instrument(sym)


def load_strategy(
    config: StrategyConfig, signal_fn: SignalFn | str
) -> Strategy:
    """Build a :class:`Strategy` from a config and a resolvable signal callable.

    The instrument is built from ``config.symbol`` (canonical ``BASE/QUOTE`` or a
    Kraken pair string). ``signal_fn`` is resolved as:

    * a **callable** — used directly;
    * a ``"module:function"`` **string** — :func:`importlib.import_module` the
      module, then :func:`getattr` the function. Only an already-importable
      dotted module may be named; unlike the legacy path nothing is exec'd from a
      loose file.

    Parameters
    ----------
    config : StrategyConfig
        The strategy declaration (``name`` + ``symbol``).
    signal_fn : SignalFn or str
        The signal callable, or a ``"module:function"`` import reference.

    Returns
    -------
    Strategy
        The loaded strategy (``reference_qty``/``lookback`` left at defaults).

    Raises
    ------
    SignalError
        If ``signal_fn`` is a string that does not resolve to a callable
        (malformed reference, missing module, missing attribute, or the
        attribute is not callable).

    """
    if callable(signal_fn):
        resolved: SignalFn = signal_fn
    else:
        resolved = _resolve_ref(signal_fn)

    instrument = _instrument_from_symbol(config.symbol)
    return Strategy(name=config.name, instrument=instrument, signal_fn=resolved)


def _resolve_ref(ref: str) -> SignalFn:
    """Resolve a ``"module:function"`` string to a callable, safely.

    Raises :class:`SignalError` (never lets a bare ``ImportError`` /
    ``AttributeError`` escape) with a message naming the offending reference.
    """
    if ":" not in ref:
        raise SignalError(
            f"signal_fn reference {ref!r} must be 'module:function'"
        )
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise SignalError(
            f"signal_fn reference {ref!r} must be 'module:function'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SignalError(
            f"cannot import module {module_name!r} for signal_fn {ref!r}: {exc}"
        ) from exc
    try:
        fn = getattr(module, attr)
    except AttributeError as exc:
        raise SignalError(
            f"module {module_name!r} has no attribute {attr!r} "
            f"for signal_fn {ref!r}"
        ) from exc
    if not callable(fn):
        raise SignalError(
            f"signal_fn {ref!r} resolved to a non-callable {type(fn).__name__}"
        )
    return fn  # type: ignore[no-any-return]


def ma_crossover_signal(
    instrument: Instrument, *, fast: int = 10, slow: int = 30
) -> SignalFn:
    """Build a moving-average-crossover :data:`SignalFn` for ``instrument``.

    A small built-in example (for tests / docs). The returned callable computes
    a fast and a slow **simple** moving average over the close column (``c``)
    using :func:`fynance.sma`, then emits a fractional-exposure signal on the
    sign of ``fast_ma - slow_ma`` at the latest bar:

    * ``+1`` (long) when the fast MA is above the slow MA;
    * ``-1`` (short) when it is below;
    * ``0`` (flat) when they are equal (or there is no data).

    **Causality.** :func:`fynance.sma` is a trailing average (a shrinking window
    for the first ``w-1`` bars, never reaching forward), and only the *last*
    element of each MA is read, so the signal at bar ``t`` depends solely on
    bars ``≤ t`` — no lookahead. The signal's ``ts`` is taken from the latest
    bar's ``time`` (see the module docstring schema).

    Parameters
    ----------
    instrument : Instrument
        The instrument the produced signals are for.
    fast : int, optional
        Fast MA window (bars). Must be ``>= 1`` and ``< slow``. Default ``10``.
    slow : int, optional
        Slow MA window (bars). Must be ``> fast``. Default ``30``.

    Returns
    -------
    SignalFn
        A callable ``bars -> Signal.exposure(instrument, {-1,0,+1})``.

    Raises
    ------
    ValueError
        If the windows are not ``1 <= fast < slow``.
    ImportError
        Raised by the returned callable (not here) when it is *evaluated* in an
        environment without the optional ``fynance`` dependency. fynance is
        imported lazily inside the callable, so building the ``SignalFn`` — and
        importing this module — never requires it; only running the signal does.

    """
    if fast < 1 or slow <= fast:
        raise ValueError(
            f"need 1 <= fast < slow, got fast={fast}, slow={slow}"
        )

    def _signal(bars: pl.DataFrame) -> Signal:
        # fynance is an optional [triptych] dependency: import it here, when the
        # signal is actually evaluated, so importing this module stays free of it
        # (mirrors domain.performance's lazy KPI imports). A missing fynance
        # surfaces as a clear ImportError at evaluation time, not at import time.
        import fynance as fy

        ts = _bar_ts_ms(bars)
        if bars.height == 0 or _CLOSE_COL not in bars.columns:
            return Signal.exposure(instrument, money("0"), ts=ts)

        closes = bars[_CLOSE_COL].to_numpy().astype(np.float64)
        # fy.sma shrinks the window for the first w-1 bars (still causal) and
        # only warns that the window exceeds the series — benign here, so silence
        # it: we read only the trailing MA value.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            fast_ma = fy.sma(closes, w=fast)
            slow_ma = fy.sma(closes, w=slow)
        spread = float(fast_ma[-1] - slow_ma[-1])

        if spread > 0:
            target = money("1")
        elif spread < 0:
            target = money("-1")
        else:
            target = money("0")
        return Signal.exposure(instrument, target, ts=ts)

    return _signal
