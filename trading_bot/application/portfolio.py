"""The multi-asset analogue of :mod:`~trading_bot.application.strategy`.

Where a single-instrument :data:`~trading_bot.application.strategy.SignalFn`
returns *one* :class:`~trading_bot.domain.signal.Signal` for *one* instrument, a
:data:`PortfolioSignalFn` returns a **weight vector** — a target *portfolio
allocation* across a whole universe of symbols at once. This is the contract a
multi-asset strategy speaks: "I want this signed fraction of capital in BTC,
that fraction in ETH, ...", said once, venue-neutrally, before any order is
shaped.

The portfolio signal contract
------------------------------
A :data:`PortfolioSignalFn` takes ``(asof_ms, frames)`` — the as-of timestamp
(ms since the Unix epoch, UTC) and a per-coin mapping of **causal** OHLC(V)
frames (at step ``t`` each frame holds only bars ``≤ t``, never a future bar) —
and returns a :class:`Mapping` ``{Symbol: weight}``. Each weight is a **signed
fraction of capital**: ``+0.5`` means *target a long worth half the capital*,
``-0.25`` means *target a short worth a quarter*. ``Σ|w|`` (the gross exposure)
*may* be capped by the signal itself (see :attr:`PortfolioStrategy.gross_cap`),
but **the engine does not re-normalise** the vector — a signal that returns
``Σ|w| = 2`` is taken at face value as 2× gross leverage. A symbol the signal
omits (or maps to ``0``) targets a flat position. A weight with ``|w| > 1`` is
*allowed* (it is leverage on a single name, not an error): the weight→quantity
sizing below carries its own scale, so there is no ``[-1, 1]`` rejection here
(unlike a fractional :data:`~trading_bot.domain.signal.SignalMode.EXPOSURE`
:class:`~trading_bot.domain.signal.Signal`, which *is* bounded).

Sizing — weight → quantity
--------------------------
:func:`weights_to_signals` is the pure helper the runner (leaf 03) uses to turn
a weight vector into per-instrument :class:`~trading_bot.domain.signal.Signal`\\
s. For each symbol::

    qty = weight * capital / price

and the result is an *explicit-quantity* signal
(:meth:`~trading_bot.domain.signal.Signal.target_qty`) — it carries its own
scale, so the runner can diff it against a live position with no
``reference_qty``. Everything stays exact :class:`~decimal.Decimal` (prices,
weights, capital, quantities); never ``float``. The helper takes the latest
``price`` per coin as an **explicit mapping** (the runner reads the latest close
off each frame and passes it in) rather than pulling it from a frame itself —
that keeps the helper pure, frame-agnostic and unit-testable.

Loading — no arbitrary-file exec
--------------------------------
:func:`load_portfolio_signal` resolves a ``"module:function"`` string to a
:data:`PortfolioSignalFn` via :func:`importlib.import_module` + :func:`getattr`,
exactly like :func:`~trading_bot.application.strategy.load_strategy` — only an
*already-importable* dotted module may be named, **never** an ``exec`` of a
loose file. There is no builtin portfolio-signal registry yet, so a ``ref`` with
no ``":"`` is a clear :class:`~trading_bot.domain.errors.ConfigError` (it cannot
be a builtin name).

This module lives in the application layer: it may import the pure domain, holds
all money/quantities as :class:`~decimal.Decimal`, and performs no I/O of its
own.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from trading_bot.domain.errors import ConfigError, SignalError
from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    parse_binance_symbol,
)
from trading_bot.domain.money import Money, money
from trading_bot.domain.signal import Signal

if TYPE_CHECKING:
    import polars as pl

__all__ = [
    "PortfolioSignalFn",
    "PortfolioStrategy",
    "weights_to_signals",
    "load_portfolio_signal",
    "as_portfolio_signal",
]

#: A multi-asset strategy's signal callable: ``(asof_ms, {Symbol: frame})`` →
#: a weight vector ``{Symbol: weight}``. Each weight is a **signed fraction of
#: capital**; ``Σ|w|`` may be capped by the signal but the engine never
#: re-normalises it. The frames are per-coin **causal** OHLC(V) windows (at step
#: ``t`` only bars ``≤ t``). The analogue of
#: :data:`~trading_bot.application.strategy.SignalFn` for a whole universe.
PortfolioSignalFn = Callable[
    [int, Mapping[Symbol, "pl.DataFrame"]], Mapping[Symbol, Money]
]


@dataclass(frozen=True, slots=True)
class PortfolioStrategy:
    """A declared multi-asset strategy: a universe + a weight-vector signal.

    The portfolio analogue of :class:`~trading_bot.application.strategy.
    Strategy`. Immutable: build one directly or (later) from config. The runner
    (leaf 03) evaluates :attr:`signal_fn` on each step's per-coin frames to get a
    weight vector, then sizes it against :attr:`capital` via
    :func:`weights_to_signals`.

    Parameters
    ----------
    name : str
        Logical id of the strategy instance.
    universe : tuple of Symbol
        The canonical :class:`~trading_bot.domain.instrument.Symbol`\\ s the
        strategy allocates across. A tuple (ordered, hashable) so the strategy
        stays frozen/hashable.
    signal_fn : PortfolioSignalFn
        The weight-vector signal callable: ``(asof_ms, frames)`` → ``{Symbol:
        weight}``.
    capital : Money
        The capital base (in quote units) the weights are a fraction of — a
        weight ``w`` for a symbol targets a position worth ``w * capital``.
    gross_cap : Money or None, optional
        An optional declared gross-exposure cap (``Σ|w| ≤ gross_cap``), recorded
        for a signal/runner to honour. ``None`` (default) means uncapped. The
        engine does **not** enforce or re-normalise against this — it is the
        signal's documented budget, carried here for reference.

    """

    name: str
    universe: tuple[Symbol, ...]
    signal_fn: PortfolioSignalFn
    capital: Money
    gross_cap: Money | None = None


def weights_to_signals(
    weights: Mapping[Symbol, Money],
    *,
    prices: Mapping[Symbol, Money],
    capital: Money,
    asof_ms: int,
) -> list[Signal]:
    """Size a weight vector into per-instrument target-quantity signals.

    Pure helper (no I/O, no frame). For each ``(symbol, weight)`` in
    ``weights`` the target net quantity is::

        qty = weight * capital / price

    and the result is an explicit-quantity
    :meth:`~trading_bot.domain.signal.Signal.target_qty` for
    ``Instrument(symbol)`` at ``asof_ms``. A weight of ``0`` yields a flat
    (``0``) target. A weight with ``|w| > 1`` (single-name leverage) is allowed
    — there is no ``[-1, 1]`` rejection; the explicit-quantity signal carries
    its own scale.

    All arithmetic stays exact :class:`~decimal.Decimal`: ``capital`` and each
    ``price`` / ``weight`` are :class:`~trading_bot.domain.money.Money`, and the
    quotient is taken via :func:`~trading_bot.domain.money.money` on the exact
    string form — never through ``float``.

    Parameters
    ----------
    weights : Mapping[Symbol, Money]
        The target weight vector ``{Symbol: signed fraction of capital}``.
    prices : Mapping[Symbol, Money]
        The latest price (quote per base unit) per symbol — supplied explicitly
        by the caller (the runner reads each frame's latest close). Every symbol
        in ``weights`` must have a strictly-positive price here.
    capital : Money
        The capital base (quote units) the weights are a fraction of. Must be
        positive.
    asof_ms : int
        The signal timestamp (ms since the Unix epoch, UTC) stamped on every
        produced :class:`~trading_bot.domain.signal.Signal`. Must be
        non-negative.

    Returns
    -------
    list of Signal
        One explicit-quantity :class:`~trading_bot.domain.signal.Signal` per
        symbol in ``weights``, in iteration order of ``weights``.

    Raises
    ------
    ConfigError
        If ``capital`` is not strictly positive.
    SignalError
        If a symbol in ``weights`` has no price in ``prices`` or a
        non-positive one.

    """
    if capital <= 0:
        raise ConfigError(
            f"capital must be positive to size weights, got {capital}"
        )

    signals: list[Signal] = []
    for symbol, weight in weights.items():
        price = prices.get(symbol)
        if price is None:
            raise SignalError(
                f"no price for {symbol} to size its weight {weight}"
            )
        if price <= 0:
            raise SignalError(
                f"price for {symbol} must be positive, got {price}"
            )
        # Keep everything Decimal: (weight * capital) / price, exactly.
        qty = money(str(weight * capital / price))
        signals.append(
            Signal.target_qty(Instrument(symbol), qty, ts=asof_ms)
        )
    return signals


def load_portfolio_signal(ref: str) -> PortfolioSignalFn:
    """Resolve a ``"module:function"`` string to a :data:`PortfolioSignalFn`.

    The portfolio counterpart of
    :func:`~trading_bot.application.strategy.load_strategy`'s resolver, with the
    same safety guarantee: the module is imported with
    :func:`importlib.import_module` and the function pulled with
    :func:`getattr` — only an *already-importable* dotted module can be named,
    and nothing is ever ``exec``\\ 'd from a loose file.

    There is **no** builtin portfolio-signal registry yet, so a ``ref`` with no
    ``":"`` cannot resolve to a builtin name and is rejected outright.

    Parameters
    ----------
    ref : str
        A ``"module:function"`` import reference (e.g.
        ``"my_pkg.signals:momentum"``).

    Returns
    -------
    PortfolioSignalFn
        The resolved callable.

    Raises
    ------
    ConfigError
        If ``ref`` is not a ``"module:function"`` string, names a module that
        cannot be imported, has no such attribute, or resolves to a non-callable.

    """
    if ":" not in ref:
        raise ConfigError(
            f"portfolio signal reference {ref!r} must be 'module:function' "
            "(there is no builtin portfolio-signal registry yet)"
        )
    module_name, _, attr = ref.partition(":")
    if not module_name or not attr:
        raise ConfigError(
            f"portfolio signal reference {ref!r} must be 'module:function'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(
            f"cannot import module {module_name!r} for portfolio signal "
            f"{ref!r}: {exc}"
        ) from exc
    try:
        fn = getattr(module, attr)
    except AttributeError as exc:
        raise ConfigError(
            f"module {module_name!r} has no attribute {attr!r} "
            f"for portfolio signal {ref!r}"
        ) from exc
    if not callable(fn):
        raise ConfigError(
            f"portfolio signal {ref!r} resolved to a non-callable "
            f"{type(fn).__name__}"
        )
    return fn  # type: ignore[no-any-return]


def as_portfolio_signal(
    weights_callable: Callable[[], Any],
    *,
    symbol_parse: Callable[[str], Symbol] = parse_binance_symbol,
) -> PortfolioSignalFn:
    """Adapt a store-reading ``() -> {pair: weight}`` callable to a :data:`PortfolioSignalFn`.

    The generic bridge between a **research** weight oracle and this package's
    :data:`PortfolioSignalFn` contract. A research signal like
    ``fynance_research.strategies.ls1_live.target_weights`` is **argument-free**
    (it reads its own data store) and returns a plain ``{pair-string: weight}``
    mapping (e.g. ``{"BTC-USDT": +0.31, "ZEC-USDT": -0.18, ...}``) — sometimes
    paired with an as-of timestamp as ``(weights, asof)``. The
    :data:`PortfolioSignalFn` the runner speaks is, by contrast,
    ``(asof_ms, frames) -> {Symbol: weight}``.

    This adapter returns a :data:`PortfolioSignalFn` that, on each rebalance:

    * **ignores** the passed ``asof_ms`` / ``frames`` (the underlying callable
      reads its own store) — the frames still drive the runner's freshness gate
      and the latest closes it prices each leg at, so the engine stays the
      authority on *when* and *at what price* to trade;
    * calls ``weights_callable()`` and accepts **either** a bare ``{pair: weight}``
      mapping **or** a ``(mapping, asof)`` 2-tuple (the as-of is discarded — the
      engine's own as-of governs), defensively;
    * **normalises** each pair-string key to a canonical
      :class:`~trading_bot.domain.instrument.Symbol` via ``symbol_parse`` (default
      :func:`~trading_bot.domain.instrument.parse_binance_symbol`, so
      ``"BTC-USDT"`` → ``Symbol("BTC", "USDT")``), and each weight to exact
      :class:`~trading_bot.domain.money.Money` via ``money(str(...))`` — never
      ``float``.

    It is the **only** research-aware glue and it is fully generic: it hardcodes
    no universe, no strategy specifics, only key-normalisation and the return-shape
    handling. To wire a concrete research signal by config, expose a parameter-free
    module-level callable that returns ``as_portfolio_signal(target_weights)`` (or
    the adapted signal directly) and point the config's ``signal.ref`` at it. Such
    wrappers + their configs are **strategy-specific and kept local-only** under the
    gitignored ``strategies/`` tree (never committed to this engine repo); see
    ``strategies/README.md`` and ``doc/dev/09-go-live.md``.

    Parameters
    ----------
    weights_callable : Callable[[], Any]
        The argument-free weight oracle. Returns either a ``{pair: weight}``
        mapping or a ``(mapping, asof)`` tuple. Called once per rebalance.
    symbol_parse : Callable[[str], Symbol], optional
        How a pair-string key is parsed to a canonical
        :class:`~trading_bot.domain.instrument.Symbol`. Defaults to
        :func:`~trading_bot.domain.instrument.parse_binance_symbol` (handles the
        ``"BTC-USDT"`` hyphen form *and* the bare ``"BTCUSDT"`` form). A
        :class:`Symbol` key is passed through unchanged (already canonical).

    Returns
    -------
    PortfolioSignalFn
        A ``(asof_ms, frames) -> {Symbol: Money}`` callable.

    Raises
    ------
    SignalError
        If ``weights_callable()`` returns neither a mapping nor a
        ``(mapping, asof)`` tuple, or a key cannot be parsed to a
        :class:`Symbol`.

    """

    def _signal(
        asof_ms: int, frames: Mapping[Symbol, pl.DataFrame]
    ) -> Mapping[Symbol, Money]:
        raw = weights_callable()
        weights = _unwrap_weights(raw)
        return _normalise_weight_keys(weights, symbol_parse)

    return _signal


def _unwrap_weights(raw: Any) -> Mapping[Any, Any]:
    """Accept a bare ``{pair: weight}`` mapping or a ``(mapping, asof)`` tuple.

    The research oracle may return the weight vector alone or paired with its
    as-of timestamp; either way the engine's own as-of governs, so the as-of
    component (when present) is discarded and only the mapping is kept.
    """
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], Mapping):
        return raw[0]
    raise SignalError(
        "weight oracle must return a {pair: weight} mapping or a "
        f"(mapping, asof) tuple, got {type(raw).__name__}"
    )


def _normalise_weight_keys(
    weights: Mapping[Any, Any],
    symbol_parse: Callable[[str], Symbol],
) -> dict[Symbol, Money]:
    """Normalise pair-string keys → canonical :class:`Symbol`, weights → ``Money``.

    A :class:`Symbol` key is passed through (already canonical); a string key is
    parsed via ``symbol_parse``. Each weight is taken as exact
    :class:`~trading_bot.domain.money.Money` through ``money(str(...))`` — never
    ``float``.
    """
    out: dict[Symbol, Money] = {}
    for key, weight in weights.items():
        if isinstance(key, Symbol):
            symbol = key
        else:
            try:
                symbol = symbol_parse(str(key))
            except ValueError as exc:
                raise SignalError(
                    f"weight oracle key {key!r} is not a parseable pair: {exc}"
                ) from exc
        out[symbol] = money(str(weight))
    return out
