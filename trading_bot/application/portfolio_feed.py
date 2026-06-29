"""A multi-instrument **causal** feed: N coins on a common daily date index.

Where :class:`~trading_bot.application.data_feed.DccdFeed` replays *one* coin's
bars as growing causal windows, :class:`PortfolioFeed` does the same for a whole
**universe** at once — and adds the property a cross-sectional signal lives or
dies on: every emitted rebalance date is one on which **every** coin has that
day's *closed* bar. The cross-section is never computed on a partial universe.

Why a common index, inner-join, never forward-fill
--------------------------------------------------
A :data:`~trading_bot.application.portfolio.PortfolioSignalFn` reads a per-coin
mapping of frames and returns a weight *vector*: the weight it gives BTC is a
function of where BTC sits **relative to** ETH, LTC, …, *as of the same day*. If
one coin's bar for today is missing or stale and a neighbour's is fresh, the
cross-section silently shifts (today's BTC rank computed against yesterday's ZEC)
— a quiet, hard-to-spot corruption of the signal. So :class:`PortfolioFeed`:

* **aligns on a common date index** — an *inner join* on the bar timestamp. A
  rebalance date is emitted only when it is present for *all* N coins;
* **never forward-fills** a stale close into the cross-section. A coin missing
  the latest day simply means that day is not emitted (the freshness gate). A
  coin lagging the others is **logged, never raised** — the feed degrades to the
  largest common-complete prefix rather than fabricating a bar.

Causality is inherited bar-for-bar from the single-coin path
------------------------------------------------------------
The per-coin read goes through the *same* dccd path the single-coin feed uses
(:class:`~trading_bot.application.data_feed.DccdFeed`, reading via the injected
:class:`~trading_bot.application.data_provider.DccdClient`) — this module adds no
new read logic and no new dccd coupling. Each coin's bars arrive already
normalised to the bars schema (``time, o, h, l, c, v``), oldest→newest. After the
inner-join restriction to the common dates, iterating yields, at step ``t``, a
``Mapping[Symbol, pl.DataFrame]`` where each coin's frame is the causal prefix up
to and including common-date ``t`` — **no coin's window ever contains a bar dated
> t** (the no-lookahead invariant), and the windows grow monotonically. So a
signal evaluated at step ``t`` sees, for each coin, every closed bar ``≤ t`` (≥
the configured lookback once enough history has accrued, e.g. ≥ 200 closes for an
SMA-200 trend) and nothing later.

Daily bars / dccd's span
------------------------
The default ``span`` is one day (``86400`` s). dccd's ``read`` is keyed by span:
it returns the dataset stored *at that span*. The offline tests inject a fake
client whose ``read`` returns canned **daily** bars per coin; a live caller
points the injected client at a store that serves daily bars at that span (see
the real-data verification in the tests for how the Binance 1-minute store is
resampled to daily before this feed consumes it).

This module lives in the application layer: it depends on :mod:`polars`, reuses
the data-feed / data-provider use-cases, and performs no I/O of its own beyond
the injected dccd client's reads (which happen inside the reused
:class:`DccdFeed`). It introduces no ``float`` arithmetic — it only aligns and
slices frames on the integer ``time`` column, leaving each bar's OHLC values
exactly as dccd reported them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

import polars as pl

from trading_bot.application.data_feed import DccdFeed
from trading_bot.domain.instrument import Symbol

if TYPE_CHECKING:
    from trading_bot.application.data_provider import DccdClient

__all__ = [
    "PortfolioFeed",
]

logger = logging.getLogger(__name__)

#: Default bar width: one day in seconds (dccd's ``span`` for daily bars).
_DAILY_SPAN: int = 86_400


class PortfolioFeed:
    """A causal, common-index feed over a universe of coins' daily bars.

    Reads each coin's bars through the *same* single-coin dccd path
    (:class:`~trading_bot.application.data_feed.DccdFeed` over the injected
    :class:`~trading_bot.application.data_provider.DccdClient`), aligns them on a
    **common date index** (inner join on the bar ``time``), and replays the
    aligned cross-section as growing causal windows — one common date advanced per
    step. The freshness gate falls out of the inner join: a date is emitted only
    when *every* coin has that day's closed bar; a coin lagging the others is
    **logged, never raised**, and its missing tail days are simply not emitted
    (the cross-section is never forward-filled onto a stale close).

    The dccd coupling is **injectable**: pass a ``client`` (a fake in offline
    tests, the real ``dccd.Client`` live) and nothing here imports dccd.

    Parameters
    ----------
    universe : Sequence[Symbol]
        The canonical :class:`~trading_bot.domain.instrument.Symbol`\\ s to feed,
        in the order the per-coin mapping iterates. Must be non-empty and free of
        duplicates.
    exchange : str
        The venue key each coin's bars are stored under (e.g. ``"binance"``),
        forwarded to ``client.read``. Must be non-empty.
    client : DccdClient or None
        The dccd client to read through. ``None`` lazily constructs a real
        ``dccd.Client`` (importing dccd only then, via the data-provider path).
        Injecting a fake keeps the offline tests dccd-free.
    span : int, optional
        Bar width in **seconds** forwarded to ``client.read``. Defaults to
        ``86400`` (daily). Must be ``> 0``.
    start_ns, end_ns : int or None, optional
        Optional inclusive nanosecond bounds forwarded to every coin's read.
    data_type : str, optional
        The dccd data kind to read. Defaults to ``"ohlc"``.
    data_path : str or None, optional
        Forwarded to the real ``dccd.Client`` constructor (its ``config_path``)
        when ``client is None``; ignored when a client is injected.
    symbol_for : Callable[[Symbol], str] or None, optional
        How a canonical :class:`Symbol` is rendered to the pair string the store
        is keyed by. ``None`` (default) renders it for ``exchange`` via
        :meth:`~trading_bot.domain.instrument.Symbol.to_venue_symbol`.

    Raises
    ------
    ValueError
        If ``universe`` is empty or has duplicates, ``exchange`` is blank, or
        ``span`` is not positive.

    Examples
    --------
    >>> from trading_bot.domain.instrument import Symbol
    >>> feed = PortfolioFeed(  # doctest: +SKIP
    ...     [Symbol("BTC", "USDT"), Symbol("ETH", "USDT")],
    ...     exchange="binance",
    ...     client=my_fake_client,
    ... )
    >>> for frames in feed:  # doctest: +SKIP
    ...     ...  # frames[Symbol("BTC","USDT")] is the causal window up to day t
    """

    def __init__(
        self,
        universe: Sequence[Symbol],
        *,
        exchange: str,
        client: DccdClient | None = None,
        span: int = _DAILY_SPAN,
        start_ns: int | None = None,
        end_ns: int | None = None,
        data_type: str = "ohlc",
        data_path: str | None = None,
        symbol_for: Callable[[Symbol], str] | None = None,
    ) -> None:
        coins = tuple(universe)
        if not coins:
            raise ValueError("universe must be a non-empty sequence of Symbol")
        if len(set(coins)) != len(coins):
            raise ValueError(f"universe has duplicate symbols: {coins}")
        if not exchange or not exchange.strip():
            raise ValueError("exchange must be a non-empty string")
        if span <= 0:
            raise ValueError(f"span must be positive seconds, got {span}")

        self._universe = coins
        self._exchange = exchange
        self._span = span
        self._data_type = data_type

        if client is None:
            client = _make_client(data_path)
        self._client = client

        # Default symbol rendering: canonical Symbol -> the venue's pair code the
        # store is keyed by (e.g. BTC/USDT -> "BTCUSDT" on binance). A callable
        # override lets a caller match a store keyed by a different convention
        # (e.g. "BTC-USDT").
        render: Callable[[Symbol], str] = (
            symbol_for
            if symbol_for is not None
            else (lambda sym: sym.to_venue_symbol(exchange))
        )

        # One single-coin DccdFeed per coin — the *reused* read/normalise path.
        # Nothing in this module re-implements the dccd read; each coin's bars
        # arrive already normalised to the bars schema, oldest→newest.
        self._feeds: dict[Symbol, DccdFeed] = {
            sym: DccdFeed(
                client,
                exchange,
                render(sym),
                span,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            for sym in coins
        }

    @property
    def universe(self) -> tuple[Symbol, ...]:
        """The coins this feed allocates across, in mapping order."""
        return self._universe

    def _read_all(self) -> dict[Symbol, pl.DataFrame]:
        """Read every coin's full normalised bars frame (oldest→newest).

        Delegates each coin's read+normalise to its single-coin
        :class:`DccdFeed` (the reused dccd path) — this module adds no read
        logic of its own.
        """
        return {sym: feed.latest() for sym, feed in self._feeds.items()}

    def _common_dates(self, frames: Mapping[Symbol, pl.DataFrame]) -> list[int]:
        """The sorted intersection of every coin's bar timestamps (the gate).

        A rebalance date survives only when *all* coins carry that day's closed
        bar (inner join on ``time``). A coin whose latest bar lags the universe
        maximum is logged (never raised) — its missing tail days are excluded
        from the result, so the cross-section is never computed on a partial or
        stale universe.
        """
        date_sets = {sym: set(f["time"].to_list()) for sym, f in frames.items()}
        common: set[int] = set.intersection(*date_sets.values()) if date_sets else set()

        # Freshness diagnostics: a coin whose newest bar is behind the universe
        # maximum is lagging — log it (the day it lacks is simply not emitted).
        maxima = {
            sym: (max(dates) if dates else None) for sym, dates in date_sets.items()
        }
        present = [m for m in maxima.values() if m is not None]
        if present:
            universe_max = max(present)
            for sym, latest in maxima.items():
                if latest is None:
                    logger.warning(
                        "portfolio feed: %s has no bars; it cannot enter the "
                        "cross-section", sym,
                    )
                elif latest < universe_max:
                    logger.warning(
                        "portfolio feed: %s lags the universe (latest bar %d < "
                        "%d); the cross-section stops at the last common date "
                        "(stale day not emitted, never forward-filled)",
                        sym, latest, universe_max,
                    )

        return sorted(common)

    def _aligned(
        self, frames: Mapping[Symbol, pl.DataFrame]
    ) -> tuple[list[int], dict[Symbol, pl.DataFrame]]:
        """Restrict every coin to the common dates, oldest→newest.

        Returns the sorted common dates and, per coin, that coin's bars filtered
        to exactly those dates (same row order across coins, so step ``t`` is the
        same calendar day for every coin).
        """
        dates = self._common_dates(frames)
        if not dates:
            return [], {sym: f.clear() for sym, f in frames.items()}
        keep = set(dates)
        aligned = {
            sym: f.filter(pl.col("time").is_in(keep)).sort("time")
            for sym, f in frames.items()
        }
        return dates, aligned

    def __iter__(self) -> Iterator[Mapping[Symbol, pl.DataFrame]]:
        """Read once, align, and yield growing causal cross-sections.

        At step ``t`` yields ``{Symbol: window}`` where each window is the coin's
        bars over common dates ``0 .. t`` inclusive — every coin's last ``time``
        is common-date ``t``'s, never a later one (no lookahead), and the windows
        grow one common date per step.
        """
        _dates, aligned = self._aligned(self._read_all())
        n = next(iter(aligned.values())).height if aligned else 0
        for t in range(n):
            # frame[: t + 1] is common dates 0..t inclusive — the causal window.
            yield {sym: f[: t + 1] for sym, f in aligned.items()}

    def latest(self) -> Mapping[Symbol, pl.DataFrame]:
        """Return the full aligned per-coin frames (every common date).

        Each coin's frame holds exactly the common dates (the freshness-gated
        cross-section), oldest→newest — the multi-coin analogue of
        :meth:`DccdFeed.latest`. Handy for a one-shot evaluation or warmup.
        """
        _dates, aligned = self._aligned(self._read_all())
        return aligned

    def asof_ms(self) -> int | None:
        """The latest common date's close, in **milliseconds** since the epoch.

        dccd stamps bars in nanoseconds; the portfolio signal contract speaks
        milliseconds, so the latest common ``time`` is converted ns → ms. Returns
        ``None`` when there is no common date (an empty or non-overlapping
        universe), so a caller can skip rather than rebalance on nothing.
        """
        dates = self._common_dates(self._read_all())
        if not dates:
            return None
        return dates[-1] // 1_000_000


def _make_client(data_path: str | None) -> DccdClient:
    """Lazily construct a real dccd client (importing dccd only here).

    Reuses :func:`trading_bot.application.data_provider._make_client` so the
    single, audited dccd-construction seam is shared — this module never imports
    dccd directly.
    """
    from trading_bot.application.data_provider import _make_client as make

    return make(data_path)
