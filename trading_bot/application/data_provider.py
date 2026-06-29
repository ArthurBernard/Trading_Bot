"""Build a :class:`~trading_bot.application.data_feed.DataFeed` from a strategy's
declared dccd data source — the **config → feed** glue (library import).

:func:`feed_for` is the single seam between a validated
:class:`~trading_bot.application.config.StrategyConfig` and a live
:class:`~trading_bot.application.data_feed.DccdFeed`. It resolves the dccd read a
strategy needs (``exchange`` / ``symbol`` / ``span`` / optional history start),
optionally **drives collection** first (``backfill``), and returns a feed that
replays the stored bars as causal windows.

Resolved integration decision (the ADR records the *why*)
--------------------------------------------------------
dccd is used as an **in-process library**, not a separate service/daemon/IPC:

* ``dccd.Client.read`` produces the bars a :class:`DccdFeed` replays (read path);
* ``dccd.Client.backfill`` can *drive* collection so the read has data to surface
  (the orchestrator role — trading_bot driving dccd).

The dccd coupling stays **thin and injectable**: this module imports ``dccd``
only to construct the real client lazily (when none is injected), and types the
client against :class:`~trading_bot.application.data_feed._DccdClientFull` so a
test fake stands in without dccd installed.

``start`` → ``start_ns``
------------------------
:attr:`~trading_bot.application.config.DataSourceConfig.start` is the optional
history start. :func:`_resolve_start_ns` maps it to the nanosecond bound
``DccdFeed`` forwards to ``client.read``:

* ``None`` → ``None`` (read from the dataset's start);
* an ``int`` → passed through unchanged (already an epoch-nanosecond marker);
* a ``str`` → parsed as an ISO-8601 date or datetime (``"2024-01-01"`` or
  ``"2024-01-01T00:00:00"``; a trailing ``"Z"`` is accepted), interpreted as UTC
  when no offset is given, and converted to epoch nanoseconds.

This module lives in the application layer: it may import :mod:`dccd` and the
data-feed primitives. It performs no I/O of its own beyond constructing the
client; all reading/normalisation/causality stays inside :class:`DccdFeed`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import polars as pl

from trading_bot.application.data_feed import DccdFeed

if TYPE_CHECKING:
    from typing import Any

    from trading_bot.application.config import StrategyConfig
    from trading_bot.application.data_feed import DataFeed

__all__ = [
    "feed_for",
    "DccdClient",
    "ResamplingDccdClient",
]

#: dccd OHLC columns the resampler aggregates over (the source-frame schema).
_OHLC_COLS: tuple[str, ...] = (
    "TS",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
)


@runtime_checkable
class DccdClient(Protocol):
    """The slice of ``dccd.Client`` :func:`feed_for` needs.

    Extends the read-only :class:`~trading_bot.application.data_feed._DccdClient`
    (used by :class:`DccdFeed`) with the ``backfill`` method that lets
    trading_bot *drive* dccd's collection (the orchestrator role). Kept tiny so a
    test fake stands in without importing dccd; the real ``dccd.Client``
    satisfies it structurally.
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

    def backfill(
        self,
        exchange: str,
        symbol: str,
        data_type: str = ...,
        span: int | None = ...,
        start: str = ...,
    ) -> Any:
        """Drive dccd to collect/store bars for ``exchange``/``symbol``."""
        ...


class ResamplingDccdClient:
    """A dccd client that **resamples** a finer stored span up to a coarser one.

    The live daily-bars seam for the portfolio. dccd's store serves bars at the
    span they were collected at, and the Binance store the LS1 universe lives in
    holds only **1-minute** bars — so a plain ``client.read(span=86400)`` against
    it returns *zero* daily rows (dccd does not resample). Daily bars are a
    *consumer* responsibility (this mirrors ``fynance_research.data.load_ohlc``,
    which resamples 1m→1d via polars ``group_by_dynamic(every="1d")``).

    This adapter wraps a real :class:`DccdClient`: a ``read`` for the
    ``daily_span`` is satisfied by reading the wrapped client at the finer
    ``source_span`` (default ``60`` = 1m) and aggregating each calendar day's
    minute bars into one daily OHLCV bar —

    ===========  =========================================================
    column       aggregation over the day's minute bars
    ===========  =========================================================
    ``open``     ``first`` (the day's opening minute)
    ``high``     ``max``
    ``low``      ``min``
    ``close``    ``last`` (the day's closing minute)
    ``volume``   ``sum``
    ===========  =========================================================

    via polars ``group_by_dynamic(every="1d")`` on a ``time`` derived from the
    ``TS`` column. The grouping is **left-closed / left-labelled** (polars'
    default), so each daily bar is stamped at the **day's open** and aggregates
    only that day's minutes — never a future minute (the **causality** the whole
    feed depends on). The **last (still-forming) day is dropped** whenever the
    source's latest minute does not reach that day's final minute boundary, so a
    partial day never enters the cross-section; a day whose source data runs to
    its end is kept. Only the OHLCV columns are aggregated — every price stays the
    *exact* :class:`~decimal.Decimal`-friendly value dccd reported (no ``float``
    coercion); only the ``time``/``TS`` math is integer/datetime.

    A read whose ``span`` is **not** the ``daily_span`` is forwarded to the
    wrapped client unchanged (so the same adapter can serve a mixed config). The
    adapter is **injectable**: the offline tests pass a fake *daily* client
    directly and never need it; a live portfolio reading a 1m store wraps its real
    client in this.

    Parameters
    ----------
    inner : DccdClient
        The wrapped real (or fake) client read at ``source_span``.
    daily_span : int, optional
        The coarse span (seconds) a ``read`` for which triggers resampling.
        Defaults to ``86400`` (one day). A read at any other span is passed
        through to ``inner`` unchanged.
    source_span : int, optional
        The fine span (seconds) the wrapped client is read at and aggregated up
        from. Defaults to ``60`` (one minute). Must be ``> 0`` and ``<
        daily_span``.
    every : str, optional
        The polars ``group_by_dynamic`` bucket width. Defaults to ``"1d"``
        (matching the daily ``daily_span``). Override only to resample to a
        different coarse bar.

    Raises
    ------
    ValueError
        If ``source_span`` is not a positive value strictly smaller than
        ``daily_span``.

    """

    def __init__(
        self,
        inner: DccdClient,
        *,
        daily_span: int = 86_400,
        source_span: int = 60,
        every: str = "1d",
    ) -> None:
        if source_span <= 0:
            raise ValueError(
                f"source_span must be positive seconds, got {source_span}"
            )
        if source_span >= daily_span:
            raise ValueError(
                f"source_span ({source_span}) must be finer than daily_span "
                f"({daily_span}) to resample up"
            )
        self._inner = inner
        self._daily_span = daily_span
        self._source_span = source_span
        self._every = every

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        """Read bars, resampling source→daily when ``span`` is the daily span.

        For ``span == daily_span`` the wrapped client is read at
        ``source_span`` (forwarding the same ``exchange`` / ``symbol`` /
        ``data_type`` / ``start_ns`` / ``end_ns`` bounds) and the result is
        aggregated to daily via :meth:`_resample`. Any other ``span`` is passed
        straight through to the wrapped client.
        """
        if span != self._daily_span:
            return self._inner.read(
                exchange, symbol, data_type, span, start_ns, end_ns
            )
        raw = self._inner.read(
            exchange, symbol, data_type, self._source_span, start_ns, end_ns
        )
        return self._resample(raw)

    def backfill(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start: str = "last",
    ) -> object:
        """Forward a backfill to the wrapped client at the **source** span.

        Backfilling collects the *stored* (fine) bars the resample reads from, so
        a daily-span backfill request is driven at ``source_span``; any other
        span is forwarded unchanged.
        """
        drive_span = self._source_span if span == self._daily_span else span
        return self._inner.backfill(exchange, symbol, data_type, drive_span, start)

    def _resample(self, raw: pl.DataFrame) -> pl.DataFrame:
        """Aggregate fine ``raw`` OHLCV bars up to the coarse bar (causal).

        Groups the source frame by calendar day (``group_by_dynamic`` over a
        ``time`` derived from ``TS``), taking ``open=first, high=max, low=min,
        close=last, volume=sum`` over each day's minutes — left-closed /
        left-labelled so a day's bar is stamped at the day's open and aggregates
        only that day's minutes (never a future one). The still-forming last day
        (whose source minutes do not reach the next day boundary) is dropped, so
        no partial day enters the result. Returns a dccd-shaped OHLC frame (the
        same column set the source had), sorted oldest→newest. An empty source
        yields an empty (but correctly-shaped) frame.

        OHLC prices are carried through polars' aggregation without any ``float``
        coercion — whatever exact value dccd stored is what comes out.
        """
        if raw.height == 0:
            return raw

        every_ns = self._daily_span * 1_000_000_000
        daily = (
            raw.with_columns(pl.from_epoch("TS", time_unit="ns").alias("dt"))
            .group_by_dynamic("dt", every=self._every, closed="left", label="left")
            .agg(
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                pl.col("volume").sum(),
            )
            .sort("dt")
            .with_columns(pl.col("dt").dt.timestamp("ns").alias("TS"))
        )

        # Drop the still-forming last day: keep a day only when the source data
        # reaches at least its closing boundary (its last source minute is at or
        # beyond the next day's open). This keeps the cross-section to *closed*
        # days only — a partial last day is never emitted (causality).
        # newest source minute's open time (raw is non-empty here)
        last_ts = int(raw["TS"].max())  # type: ignore[arg-type]
        daily = daily.filter(
            (pl.col("TS") + every_ns - self._source_span * 1_000_000_000) <= last_ts
        )

        # Re-attach the dccd OHLC columns the source carried but we did not
        # aggregate (quote_volume / trades), zero-filled, so the result keeps the
        # source schema the normaliser expects. Only ever present if the source
        # had them.
        extras: list[pl.Expr] = []
        if "quote_volume" in raw.columns:
            extras.append(pl.lit(0.0).alias("quote_volume"))
        if "trades" in raw.columns:
            extras.append(pl.lit(0).alias("trades"))
        if extras:
            daily = daily.with_columns(extras)

        keep = [c for c in _OHLC_COLS if c in daily.columns]
        return daily.select(keep)


def _resolve_start_ns(start: str | int | None) -> int | None:
    """Map a :attr:`DataSourceConfig.start` value to an epoch-ns bound.

    ``None`` → ``None``; an ``int`` is passed through (already epoch-ns); a
    ``str`` is parsed as an ISO-8601 date/datetime (UTC when no offset) and
    converted to nanoseconds.

    Raises
    ------
    ValueError
        If a string ``start`` is not a parseable ISO-8601 date/datetime.
    """
    if start is None:
        return None
    if isinstance(start, int):
        return start
    text = start.strip()
    # Accept a trailing 'Z' (UTC) which datetime.fromisoformat rejects pre-3.11
    # consistently across forms.
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed: datetime | date
        if "T" in iso or " " in iso or ":" in iso:
            parsed = datetime.fromisoformat(iso)
        else:
            parsed = date.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"data source start {start!r} is not an ISO-8601 date/datetime "
            "(e.g. '2024-01-01' or '2024-01-01T00:00:00') or an epoch-ns int"
        ) from exc
    if isinstance(parsed, datetime):
        dt = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    else:
        dt = datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
    # int(timestamp) seconds → nanoseconds; keep sub-second precision exact.
    return int(round(dt.timestamp() * 1_000_000_000))


def feed_for(
    strategy: StrategyConfig,
    *,
    client: DccdClient | None = None,
    backfill: bool = False,
    data_path: str | None = None,
) -> DataFeed:
    """Build a :class:`DccdFeed` from a strategy's declared dccd data source.

    Thin config→feed glue: it resolves the dccd read from ``strategy.data`` /
    ``strategy.symbol``, optionally drives collection first, and returns a feed
    that replays the stored bars as causal windows. All
    normalisation/causality/replay stays inside :class:`DccdFeed`.

    Parameters
    ----------
    strategy : StrategyConfig
        The strategy whose bars to feed. Its
        :attr:`~trading_bot.application.config.StrategyConfig.data` source
        (:class:`~trading_bot.application.config.DataSourceConfig`) is
        **required** — a dccd feed needs a declared dataset.
    client : DccdClient or None, optional
        The dccd client to read/backfill through. ``None`` (default) lazily
        constructs a real ``dccd.Client`` (importing dccd only then). Injecting a
        fake keeps the offline tests dccd-free.
    backfill : bool, optional
        When ``True``, call ``client.backfill(...)`` **before** building the feed
        — driving dccd to collect/store the data so the subsequent read has bars
        to surface (the orchestrator role). Default ``False`` (read whatever is
        already stored). The backfill uses the data source's ``start`` (or
        dccd's ``"last"`` default when no start is declared).
    data_path : str or None, optional
        Path forwarded to the real ``dccd.Client`` constructor (its
        ``config_path``) when ``client is None``. Ignored when a client is
        injected. Typically the engine passes
        :attr:`~trading_bot.application.config.StorageConfig.data_path`.

    Returns
    -------
    DataFeed
        A :class:`DccdFeed` over the strategy's stored bars (historical replay,
        plus the live poll mode).

    Raises
    ------
    ValueError
        If ``strategy.data`` is ``None`` (no declared data source), or if the
        data source ``start`` is an unparseable string.

    Examples
    --------
    >>> from trading_bot.application.config import StrategyConfig
    >>> cfg = StrategyConfig.model_validate({
    ...     "name": "ma", "symbol": "BTC/USD",
    ...     "data": {"exchange": "kraken", "span": 60},
    ... })
    >>> feed = feed_for(cfg, client=my_fake_client)  # doctest: +SKIP
    >>> for window in feed:  # doctest: +SKIP
    ...     ...  # each window is a causal prefix of the stored bars
    """
    data = strategy.data
    if data is None:
        raise ValueError(
            f"strategy {strategy.name!r} has no data source (data is None); "
            "a dccd feed needs a declared DataSourceConfig (exchange/span/...)"
        )

    exchange = data.exchange
    symbol = strategy.symbol
    span = data.span
    data_type = data.data_type
    start_ns = _resolve_start_ns(data.start)

    if client is None:
        client = _make_client(data_path)

    if backfill:
        # Drive dccd to collect/store the data before reading it (orchestrator
        # role). dccd's backfill takes a string ``start`` ("last" by default);
        # pass the declared start through as a string when set.
        start = "last" if data.start is None else str(data.start)
        client.backfill(exchange, symbol, data_type, span, start)

    return DccdFeed(client, exchange, symbol, span, start_ns=start_ns)


def _make_client(data_path: str | None) -> DccdClient:
    """Lazily construct a real ``dccd.Client`` (importing dccd only here).

    ``data_path`` is forwarded as the client's ``config_path``. Isolated so the
    dccd import never runs when a client is injected (the offline-test path).
    """
    from dccd import Client  # local import: dccd only required for the real path

    # dccd.Client is untyped; it satisfies the DccdClient protocol structurally.
    client: DccdClient = Client(data_path)
    return client
