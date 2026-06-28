"""Feed market **bars** to a strategy as growing, *causal* windows.

A :class:`DataFeed` turns a source of OHLC(V) bars into an **iterator of causal
windows**: stepping it once yields a :class:`polars.DataFrame` containing every
bar *up to and including* the current step — ``frame[: t + 1]`` at step ``t``.
A strategy's :data:`~trading_bot.application.strategy.SignalFn` evaluated on that
window therefore only ever sees bars ``≤ t``. This is the load-bearing
invariant of the whole module:

    **Causality / no lookahead** — at step ``t`` the feed exposes only bars
    ``≤ t``; the window's last ``time`` is bar ``t``'s, never a later bar's.
    A backtest driven by a feed is, by construction, free of forward-looking
    leakage.

The bars-frame schema (the shape
:mod:`~trading_bot.application.strategy` consumes) is a :class:`polars.DataFrame`
with columns, rows ordered oldest→newest:

================  =========================================================
column            meaning
================  =========================================================
``time``          bar open time (epoch units as stored; dccd OHLC is ns)
``o`` ``h`` ``l`` open / high / low price of the bar
``c``             **close** price of the bar
``v``             traded volume over the bar
================  =========================================================

Sync vs async
-------------
:class:`DataFeed` is a **synchronous** ``Iterable[pl.DataFrame]`` — the natural
shape for a backtest loop (``for window in feed: ...``), and how the runner
(leaf 03) drives an offline replay. Iterating to exhaustion is finite for a
fixed historical frame. For a *live* feed, where bars arrive over time, the
poll-driven :meth:`DccdFeed.live_windows` async generator is provided as well; it
emits a window only when a **new closed bar** has appeared (never a partial,
still-forming bar — see :class:`DccdFeed`).

dccd column mapping
-------------------
``dccd.Client.read(..., data_type="ohlc")`` returns columns
``TS, open, high, low, close, volume, quote_volume, trades`` (``TS`` in
nanoseconds UTC, sorted ascending, deduplicated). :class:`DccdFeed` normalises
that to the strategy schema:

================  ===============
dccd column       schema column
================  ===============
``TS``            ``time``
``open``          ``o``
``high``          ``h``
``low``           ``l``
``close``         ``c``
``volume``        ``v``
================  ===============

(``quote_volume`` / ``trades`` are dropped.) The dccd coupling is kept **thin
and injectable**: :class:`DccdFeed` takes the client as a constructor argument
typed against the tiny :class:`_DccdClient` protocol, so tests pass a fake
client returning a canned frame and never need real dccd installed.

This module lives in the application layer: it depends on :mod:`polars` and may
import the pure domain. It performs no I/O of its own — only the injected dccd
client does (and only inside :class:`DccdFeed`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "DataFeed",
    "InMemoryFeed",
    "DccdFeed",
    "BARS_SCHEMA",
]

#: The columns a bars frame must carry for a strategy ``signal_fn`` (in order).
BARS_SCHEMA: tuple[str, ...] = ("time", "o", "h", "l", "c", "v")

#: dccd OHLC column -> bars-schema column. ``TS`` is nanoseconds UTC; the other
#: dccd OHLC columns (``quote_volume``, ``trades``) are not part of the schema
#: and are dropped on normalisation.
_DCCD_TO_SCHEMA: dict[str, str] = {
    "TS": "time",
    "open": "o",
    "high": "h",
    "low": "l",
    "close": "c",
    "volume": "v",
}


def _validate_bars_schema(frame: pl.DataFrame) -> None:
    """Raise :class:`ValueError` unless ``frame`` carries the bars schema.

    Every column in :data:`BARS_SCHEMA` must be present (extra columns are
    tolerated — a strategy may read any subset). The close column ``c`` is the
    one the built-in signal needs, so a frame missing it is explicitly rejected.
    """
    missing = [col for col in BARS_SCHEMA if col not in frame.columns]
    if missing:
        raise ValueError(
            f"bars frame missing required column(s) {missing}; "
            f"expected schema {list(BARS_SCHEMA)}, got {frame.columns}"
        )


@runtime_checkable
class DataFeed(Protocol):
    """A source of **causal bar windows** for a strategy.

    A ``DataFeed`` is a synchronous iterable: iterating it yields a growing
    window of bars, one bar advanced per step. The window at step ``t`` is the
    causal prefix ``frame[: t + 1]`` — all bars ``≤ t`` and **no later bar**, so
    a ``signal_fn`` evaluated on it can never look ahead.

    Implementations
    ---------------
    * :class:`InMemoryFeed` — replays a fixed in-memory frame (deterministic;
      the backbone of offline tests/backtests).
    * :class:`DccdFeed` — reads real stored bars via an injected dccd client and
      replays them (historical), plus a live poll mode.
    """

    def __iter__(self) -> Iterator[pl.DataFrame]:
        """Yield growing causal windows, one bar advanced per step."""
        ...

    def latest(self) -> pl.DataFrame:
        """Return the full bars frame currently known to the feed.

        For a historical/in-memory feed this is every bar; for a live feed it is
        every bar seen so far (closed bars only). The returned frame is itself a
        valid bars-schema frame — handy for warmup or a one-shot evaluation.
        """
        ...


def _replay(frame: pl.DataFrame) -> Iterator[pl.DataFrame]:
    """Yield ``frame[: t + 1]`` for ``t`` in ``0 .. height-1`` (causal prefixes).

    The shared replay engine behind :class:`InMemoryFeed` and
    :class:`DccdFeed`'s historical mode. An empty frame yields nothing.
    """
    height = frame.height
    for t in range(height):
        # frame[: t + 1] is bars 0..t inclusive — the causal window at step t.
        yield frame[: t + 1]


class InMemoryFeed:
    """A :class:`DataFeed` over a fixed, in-memory bars frame.

    Wraps a bars-schema frame (validated on construction) and replays it
    bar-by-bar: iterating yields the causal prefix ``frame[: t + 1]`` at each
    step ``t``. Deterministic and offline — the backbone of unit tests and
    reproducible backtests.

    Parameters
    ----------
    frame : polars.DataFrame
        The OHLC(V) bars, oldest→newest, carrying at least the
        :data:`BARS_SCHEMA` columns. Validated on construction.

    Raises
    ------
    ValueError
        If ``frame`` is missing any required schema column.

    Examples
    --------
    >>> import polars as pl
    >>> f = pl.DataFrame(
    ...     {"time": [1, 2, 3], "o": [1.0, 2, 3], "h": [1.0, 2, 3],
    ...      "l": [1.0, 2, 3], "c": [1.0, 2, 3], "v": [1.0, 1, 1]}
    ... )
    >>> feed = InMemoryFeed(f)
    >>> [w.height for w in feed]
    [1, 2, 3]
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        _validate_bars_schema(frame)
        self._frame = frame

    def __iter__(self) -> Iterator[pl.DataFrame]:
        """Yield ``frame[: t + 1]`` for each bar (causal windows)."""
        return _replay(self._frame)

    def latest(self) -> pl.DataFrame:
        """Return the full underlying bars frame."""
        return self._frame


@runtime_checkable
class _DccdClient(Protocol):
    """The slice of ``dccd.Client`` :class:`DccdFeed` actually uses.

    Kept intentionally tiny so a test fake (or any read-only OHLC source) can
    stand in without importing dccd. Both methods are **synchronous** (matching
    ``dccd.Client``).
    """

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = ...,
        span: int | None = ...,
        start_ns: int | None = ...,
        end_ns: int | None = ...,
    ) -> pl.DataFrame:
        """Return stored bars as a polars frame (dccd OHLC columns)."""
        ...


def normalise_dccd_ohlc(frame: pl.DataFrame) -> pl.DataFrame:
    """Map a dccd OHLC frame to the bars schema ``time,o,h,l,c,v``.

    Renames the dccd columns per :data:`_DCCD_TO_SCHEMA` and selects exactly the
    schema columns (dropping ``quote_volume`` / ``trades``). The row order dccd
    guarantees (ascending ``TS``) is preserved, so the result is oldest→newest.

    Parameters
    ----------
    frame : polars.DataFrame
        A dccd OHLC read (columns ``TS, open, high, low, close, volume, ...``).

    Returns
    -------
    polars.DataFrame
        A bars-schema frame.

    Raises
    ------
    ValueError
        If a required dccd source column is absent (so the mapping cannot
        produce the full schema).
    """
    missing = [src for src in _DCCD_TO_SCHEMA if src not in frame.columns]
    if missing:
        raise ValueError(
            f"dccd OHLC frame missing source column(s) {missing}; "
            f"expected {list(_DCCD_TO_SCHEMA)}, got {frame.columns}"
        )
    renamed = frame.rename(_DCCD_TO_SCHEMA)
    return renamed.select(list(BARS_SCHEMA))


class DccdFeed:
    """A :class:`DataFeed` backed by stored bars read through a dccd client.

    Two modes share one causal-window contract:

    * **historical** (the default, used by :meth:`__iter__` / :meth:`latest`):
      read the dataset **once** via ``client.read(...)``, normalise dccd's
      columns to the bars schema, then replay it bar-by-bar exactly like
      :class:`InMemoryFeed`. Reproducible backtests over real stored bars.
    * **live** (:meth:`live_windows`): an async generator that polls
      ``client.read(...)`` on the span cadence and emits a window only when a
      **new closed bar** has appeared. A bar is treated as closed once its open
      time is at least one ``span`` in the past (``now - bar.time ≥ span``), so a
      still-forming, partial bar is never emitted — **no lookahead**. The dccd
      client is synchronous, so the poll is run via an injected ``sleep``/``now``
      pair (defaulting to wall-clock + ``asyncio.sleep``) that tests override
      with a fake clock.

    The dccd client is **injected** (typed against :class:`_DccdClient`) so tests
    pass a fake returning a canned frame; nothing here imports dccd.

    Parameters
    ----------
    client : _DccdClient
        A read-only OHLC source with dccd's ``read`` signature (the real
        ``dccd.Client``, or a test fake).
    exchange : str
        Exchange name passed straight to ``client.read``.
    symbol : str
        Pair/symbol passed straight to ``client.read``. dccd uses its own pair
        strings (e.g. ``"BTC/USDT"``); the caller passes whatever the stored
        dataset is keyed by. (Domain ``Symbol`` rendering lives in the runner.)
    span : int
        Bar width in **seconds** (dccd's ``span``); also the live close cadence.
    start_ns, end_ns : int or None, optional
        Optional inclusive nanosecond bounds forwarded to ``client.read``.

    Raises
    ------
    ValueError
        On construction if ``span`` is not positive.
    """

    def __init__(
        self,
        client: _DccdClient,
        exchange: str,
        symbol: str,
        span: int,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> None:
        if span <= 0:
            raise ValueError(f"span must be positive seconds, got {span}")
        self._client = client
        self._exchange = exchange
        self._symbol = symbol
        self._span = span
        self._start_ns = start_ns
        self._end_ns = end_ns

    def _read_normalised(self, *, end_ns: int | None = None) -> pl.DataFrame:
        """Read the dataset via the client and normalise to the bars schema.

        ``end_ns`` overrides the construction bound (used by live polling to cap
        the read at the latest closed bar); otherwise the construction
        ``end_ns`` applies.
        """
        raw = self._client.read(
            self._exchange,
            self._symbol,
            "ohlc",
            self._span,
            self._start_ns,
            self._end_ns if end_ns is None else end_ns,
        )
        return normalise_dccd_ohlc(raw)

    def __iter__(self) -> Iterator[pl.DataFrame]:
        """Read once (historical) and yield causal windows, bar by bar."""
        return _replay(self._read_normalised())

    def latest(self) -> pl.DataFrame:
        """Read and return the full normalised bars frame (historical snapshot)."""
        return self._read_normalised()

    async def live_windows(
        self,
        *,
        now_ns: Callable[[], int],
        sleep: Callable[[float], Awaitable[None]],
        max_steps: int | None = None,
    ) -> AsyncIterator[pl.DataFrame]:
        """Poll for new **closed** bars and yield a growing causal window each.

        On each poll it reads the dataset capped at the latest closed-bar
        boundary (``cutoff = now - span``, in ns), so a bar still being formed is
        excluded. When the read surfaces at least one bar beyond what was already
        emitted, the new full prefix (every closed bar so far) is yielded — a
        causal window, never a partial bar. It then sleeps one ``span`` before
        polling again.

        Parameters
        ----------
        now_ns : Callable[[], int]
            Returns the current time in nanoseconds UTC. Injected so tests drive
            a fake clock; live callers pass ``lambda: time.time_ns()``.
        sleep : Callable[[float], Awaitable[None]]
            Async sleep (seconds). Live callers pass ``asyncio.sleep``; tests
            pass a fake that advances the fake clock.
        max_steps : int or None, optional
            Stop after yielding this many windows (bounds the otherwise-infinite
            live loop — required for tests; ``None`` runs until cancelled).

        Yields
        ------
        polars.DataFrame
            A causal window of all closed bars known so far, each time a new
            closed bar appears.
        """
        # span in nanoseconds — the closed-bar cutoff and the poll cadence.
        span_ns = self._span * 1_000_000_000
        emitted = 0  # number of closed bars already yielded
        steps = 0
        while max_steps is None or steps < max_steps:
            cutoff_ns = now_ns() - span_ns  # a bar is closed only if time <= cutoff
            frame = self._read_normalised(end_ns=cutoff_ns)
            if frame.height > emitted:
                emitted = frame.height
                steps += 1
                yield frame
            await sleep(float(self._span))
