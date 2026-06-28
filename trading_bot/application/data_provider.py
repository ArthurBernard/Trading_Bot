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
]


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
