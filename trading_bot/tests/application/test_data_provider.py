"""Tests for :mod:`trading_bot.application.data_provider`.

These prove :func:`feed_for` is correct *config→feed glue* over the dccd library
import, all offline against an **injected fake client** (no real dccd / network):

* a :class:`StrategyConfig` with a data source yields a
  :class:`~trading_bot.application.data_feed.DccdFeed` that replays the expected
  **causal prefixes** (window count + last-ts, no lookahead);
* the data-source fields (``exchange`` / ``span``) and ``strategy.symbol`` are
  forwarded to the dccd ``read`` call, and ``start`` is resolved to ``start_ns``
  (int passthrough; ISO date/datetime parsed to ns);
* ``backfill=True`` calls the client's ``backfill`` **before** the first ``read``
  (call order recorded on the fake);
* ``strategy.data is None`` raises a clear error;
* (``-m network``) a real-data check: if dccd ``inventory()`` reports stored
  OHLC, ``feed_for`` over it reads and replays a few causal prefixes; skip with a
  clear reason if nothing is stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import polars as pl
import pytest

from trading_bot.application.config import StrategyConfig
from trading_bot.application.data_feed import BARS_SCHEMA, DataFeed, DccdFeed
from trading_bot.application.data_provider import feed_for

# --- helpers --------------------------------------------------------------- #


def _dccd_ohlc(closes: list[float], *, start_ns: int, span_ns: int) -> pl.DataFrame:
    """A frame mimicking ``dccd.Client.read(..., 'ohlc')`` (its column names)."""
    n = len(closes)
    return pl.DataFrame(
        {
            "TS": [start_ns + span_ns * i for i in range(n)],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * n,
            "quote_volume": [10.0] * n,
            "trades": [5] * n,
        }
    )


class _FakeDccdClient:
    """A fake dccd client recording ``read``/``backfill`` calls *in order*.

    ``read`` returns a canned frame (honouring ``end_ns`` so the live/cutoff path
    works); ``backfill`` records its args and returns a stub dict. A shared
    ``calls`` log records the *method name* of every call so a test can assert
    backfill precedes the first read.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame
        self.read_calls: list[dict[str, object]] = []
        self.backfill_calls: list[dict[str, object]] = []
        self.calls: list[str] = []  # ordered method names ("backfill" / "read")

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        self.calls.append("read")
        self.read_calls.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "data_type": data_type,
                "span": span,
                "start_ns": start_ns,
                "end_ns": end_ns,
            }
        )
        frame = self._frame
        if end_ns is not None:
            frame = frame.filter(pl.col("TS") <= end_ns)
        return frame

    def backfill(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start: str = "last",
    ) -> dict[str, Any]:
        self.calls.append("backfill")
        self.backfill_calls.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "data_type": data_type,
                "span": span,
                "start": start,
            }
        )
        return {"status": "ok"}


def _strategy(
    *,
    symbol: str = "BTC/USDT",
    exchange: str = "binance",
    span: int = 60,
    start: str | int | None = None,
    with_data: bool = True,
) -> StrategyConfig:
    """A StrategyConfig with (or without) a dccd data source."""
    payload: dict[str, Any] = {"name": "ma", "symbol": symbol}
    if with_data:
        data: dict[str, Any] = {"exchange": exchange, "span": span}
        if start is not None:
            data["start"] = start
        payload["data"] = data
    return StrategyConfig.model_validate(payload)


# --- feed_for: builds a DccdFeed that replays causal prefixes --------------- #


def test_feed_for_returns_dccd_feed_replaying_causal_prefixes() -> None:
    """feed_for(cfg, client=fake) → a DccdFeed replaying causal windows."""
    span = 60
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([5.0, 6, 7, 8], start_ns=10**18, span_ns=span_ns)
    client = _FakeDccdClient(raw)
    cfg = _strategy(symbol="BTC/USDT", exchange="binance", span=span)

    feed = feed_for(cfg, client=client)

    assert isinstance(feed, DccdFeed)
    assert isinstance(feed, DataFeed)  # runtime-checkable protocol

    windows = list(feed)
    assert len(windows) == 4
    for t, window in enumerate(windows):
        assert list(window.columns) == list(BARS_SCHEMA)
        assert window.height == t + 1
        # Causal: last time is bar t's TS, never a later one (no lookahead).
        assert window["time"][-1] == raw["TS"][t]
        assert window["time"].max() == raw["TS"][t]


def test_feed_for_forwards_exchange_symbol_span_to_read() -> None:
    """The data-source fields + symbol are forwarded to the dccd read."""
    raw = _dccd_ohlc([1.0, 2, 3], start_ns=1, span_ns=1)
    client = _FakeDccdClient(raw)
    cfg = _strategy(symbol="XBT/USD", exchange="kraken", span=300)

    list(feed_for(cfg, client=client))  # drive one read

    call = client.read_calls[0]
    assert call["exchange"] == "kraken"
    assert call["symbol"] == "XBT/USD"
    assert call["span"] == 300
    assert call["data_type"] == "ohlc"
    # No start declared => no start_ns bound.
    assert call["start_ns"] is None


def test_feed_for_passes_int_start_through_as_start_ns() -> None:
    """An int ``start`` (already epoch-ns) is forwarded unchanged as start_ns."""
    raw = _dccd_ohlc([1.0, 2], start_ns=1, span_ns=1)
    client = _FakeDccdClient(raw)
    cfg = _strategy(start=1_700_000_000_000_000_000)

    list(feed_for(cfg, client=client))

    assert client.read_calls[0]["start_ns"] == 1_700_000_000_000_000_000


def test_feed_for_parses_iso_date_start_to_start_ns() -> None:
    """An ISO date string ``start`` is parsed (UTC midnight) to epoch-ns."""
    raw = _dccd_ohlc([1.0, 2], start_ns=1, span_ns=1)
    client = _FakeDccdClient(raw)
    cfg = _strategy(start="2024-01-01")

    list(feed_for(cfg, client=client))

    expected = int(
        datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1_000_000_000
    )
    assert client.read_calls[0]["start_ns"] == expected


def test_feed_for_parses_iso_datetime_start_to_start_ns() -> None:
    """An ISO datetime (with 'Z') ``start`` is parsed as UTC to epoch-ns."""
    raw = _dccd_ohlc([1.0, 2], start_ns=1, span_ns=1)
    client = _FakeDccdClient(raw)
    cfg = _strategy(start="2024-03-15T12:30:00Z")

    list(feed_for(cfg, client=client))

    expected = int(
        datetime(2024, 3, 15, 12, 30, tzinfo=timezone.utc).timestamp()
        * 1_000_000_000
    )
    assert client.read_calls[0]["start_ns"] == expected


def test_feed_for_rejects_unparseable_string_start() -> None:
    """A non-ISO string ``start`` raises a clear ValueError."""
    cfg = _strategy(start="not-a-date")
    with pytest.raises(ValueError, match="ISO-8601"):
        feed_for(cfg, client=_FakeDccdClient(_dccd_ohlc([], start_ns=0, span_ns=1)))


# --- feed_for: backfill drives collection BEFORE reading ------------------- #


def test_feed_for_backfill_runs_before_read() -> None:
    """backfill=True calls client.backfill before the first read (order matters)."""
    span = 60
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([1.0, 2, 3], start_ns=10**18, span_ns=span_ns)
    client = _FakeDccdClient(raw)
    cfg = _strategy(symbol="ETH/USDT", exchange="binance", span=span)

    feed = feed_for(cfg, client=client, backfill=True)

    # backfill is invoked eagerly, before any read.
    assert client.calls[0] == "backfill"
    assert client.backfill_calls  # was called
    bf = client.backfill_calls[0]
    assert bf["exchange"] == "binance"
    assert bf["symbol"] == "ETH/USDT"
    assert bf["span"] == span
    assert bf["data_type"] == "ohlc"
    assert bf["start"] == "last"  # no declared start => dccd's default

    # Reading (driving the feed) happens after — backfill precedes the first read.
    list(feed)
    assert client.calls.index("backfill") < client.calls.index("read")


def test_feed_for_no_backfill_does_not_call_backfill() -> None:
    """Default (backfill=False) never drives collection — read-only."""
    raw = _dccd_ohlc([1.0, 2], start_ns=1, span_ns=1)
    client = _FakeDccdClient(raw)

    list(feed_for(_strategy(), client=client))

    assert client.backfill_calls == []
    assert "backfill" not in client.calls


def test_feed_for_backfill_forwards_declared_start() -> None:
    """A declared ``start`` is forwarded to backfill as a string."""
    raw = _dccd_ohlc([1.0], start_ns=1, span_ns=1)
    client = _FakeDccdClient(raw)
    cfg = _strategy(start="2024-01-01")

    feed_for(cfg, client=client, backfill=True)

    assert client.backfill_calls[0]["start"] == "2024-01-01"


# --- feed_for: missing data source is a clear error ------------------------ #


def test_feed_for_requires_data_source() -> None:
    """A strategy with no data source raises a clear ValueError."""
    cfg = _strategy(with_data=False)
    assert cfg.data is None
    with pytest.raises(ValueError, match="no data source"):
        feed_for(cfg, client=_FakeDccdClient(_dccd_ohlc([], start_ns=0, span_ns=1)))


# --- Verification on real data (opt-in) ------------------------------------ #


@pytest.mark.network
async def test_feed_for_real_inventory_causal_replay() -> None:
    """Real dccd: feed_for over a stored OHLC dataset replays with no lookahead.

    Skips with a clear reason when no OHLC dataset is stored (the injected-client
    tests above cover the logic; this only adds real-data coverage when present).
    The dccd ``Client`` is an **async context manager** — ``inventory()`` / ``read``
    require it entered (``async with``), which builds the store.
    """
    dccd = pytest.importorskip("dccd")
    async with dccd.Client() as client:
        ohlc = [
            d
            for d in client.inventory()
            if d.get("data_type") == "ohlc" and d.get("rows", 0) > 0
        ]
        if not ohlc:
            pytest.skip("no stored dccd OHLC dataset to replay (inventory empty)")

        entry = ohlc[0]
        cfg = StrategyConfig.model_validate(
            {
                "name": "real",
                "symbol": entry["pair"],
                "data": {"exchange": entry["exchange"], "span": int(entry["span"])},
            }
        )
        feed = feed_for(cfg, client=client)
        full = feed.latest()
        if full.height == 0:
            pytest.skip(
                f"dccd OHLC dataset {entry['exchange']}/{entry['pair']} read empty"
            )

        assert list(full.columns) == list(BARS_SCHEMA)
        times = full["time"].to_list()
        assert times == sorted(times)  # monotonic non-decreasing

        for t, window in enumerate(feed):
            assert window.height == t + 1
            assert window["time"][-1] == full["time"][t]
            assert window["time"].max() == full["time"][t]
            if t >= 4:
                break
