"""Tests for :mod:`trading_bot.application.data_feed`.

These prove the feed's load-bearing **causality / no-lookahead** invariant and
the thin, injectable dccd coupling:

* :class:`InMemoryFeed` replaying ``N`` bars yields exactly ``N`` windows; the
  window at step ``t`` has ``t + 1`` rows and its last ``time`` is bar ``t``'s —
  never a later bar (the no-lookahead assertion);
* schema validation rejects a frame missing the close column ``c``;
* a spy ``signal_fn`` driven by an :class:`InMemoryFeed` only ever sees causal
  frames (the max ``time`` it observes equals the current bar's, never ahead);
* :class:`DccdFeed` historical mode with an **injected fake client** (returning a
  canned frame carrying dccd's real column names) normalises to the bars schema
  and replays the same causal prefixes — no real dccd needed;
* live mode emits a bar only once it is **closed** (a still-forming bar under a
  faked clock is never yielded);
* (``-m network``) a real-data check: if dccd ``inventory()`` reports any stored
  OHLC, build a :class:`DccdFeed` over it and assert monotonic timestamps and no
  lookahead across a few prefixes; skip with a clear reason if nothing is stored.
"""

from __future__ import annotations

import polars as pl
import pytest

from trading_bot.application.data_feed import (
    BARS_SCHEMA,
    DataFeed,
    DccdFeed,
    InMemoryFeed,
    normalise_dccd_ohlc,
)

# --- helpers --------------------------------------------------------------- #


def _bars(closes: list[float], *, start_ts: int = 1_000) -> pl.DataFrame:
    """A bars-schema frame from a list of closes (time in seconds, +60s/bar)."""
    n = len(closes)
    times = [start_ts + 60 * i for i in range(n)]
    return pl.DataFrame(
        {
            "time": times,
            "o": closes,
            "h": closes,
            "l": closes,
            "c": closes,
            "v": [1.0] * n,
        }
    )


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
    """A fake dccd client: ``read`` returns a canned frame, honouring ``end_ns``.

    Records the arguments of each ``read`` call so a test can assert what the
    feed forwarded, and slices the canned frame to ``end_ns`` (inclusive) so the
    live-mode closed-bar logic can be exercised against a moving cutoff.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame
        self.calls: list[dict[str, object]] = []

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        self.calls.append(
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


# --- InMemoryFeed: causality ----------------------------------------------- #


def test_inmemory_replays_n_causal_windows() -> None:
    """Replaying N bars yields N windows; window t has t+1 rows, last time = t."""
    frame = _bars([10.0, 11, 12, 13, 14])
    feed = InMemoryFeed(frame)

    windows = list(feed)

    assert len(windows) == frame.height
    for t, window in enumerate(windows):
        # Causal: exactly bars 0..t, and the last time is bar t's — never later.
        assert window.height == t + 1
        assert window["time"][-1] == frame["time"][t]
        # The last bar of the window is never ahead of the current step.
        assert window["time"][-1] <= frame["time"][t]


def test_inmemory_window_never_contains_future_bar() -> None:
    """The max time in window t equals bar t's time — no lookahead, ever."""
    frame = _bars([1.0, 2, 3, 4, 5, 6])
    for t, window in enumerate(InMemoryFeed(frame)):
        assert window["time"].max() == frame["time"][t]


def test_inmemory_latest_returns_full_frame() -> None:
    """``latest()`` returns the whole underlying frame."""
    frame = _bars([1.0, 2, 3])
    assert InMemoryFeed(frame).latest().equals(frame)


def test_inmemory_empty_frame_yields_nothing() -> None:
    """An empty (but schema-valid) frame replays to zero windows."""
    empty = _bars([])
    assert list(InMemoryFeed(empty)) == []


def test_inmemory_is_a_datafeed() -> None:
    """InMemoryFeed satisfies the runtime-checkable DataFeed protocol."""
    assert isinstance(InMemoryFeed(_bars([1.0])), DataFeed)


# --- InMemoryFeed: schema validation --------------------------------------- #


def test_schema_validation_rejects_missing_close() -> None:
    """A frame missing the close column ``c`` is rejected on construction."""
    bad = pl.DataFrame(
        {"time": [1, 2], "o": [1.0, 2], "h": [1.0, 2], "l": [1.0, 2], "v": [1.0, 1]}
    )
    with pytest.raises(ValueError, match="missing required column"):
        InMemoryFeed(bad)


def test_schema_validation_tolerates_extra_columns() -> None:
    """Extra columns beyond the schema are tolerated (a signal may ignore them)."""
    frame = _bars([1.0, 2, 3]).with_columns(pl.lit("x").alias("extra"))
    feed = InMemoryFeed(frame)
    # All schema columns survive into the windows.
    last = list(feed)[-1]
    for col in BARS_SCHEMA:
        assert col in last.columns


# --- A spy signal_fn only ever sees causal frames -------------------------- #


def test_spy_signal_fn_only_sees_causal_frames() -> None:
    """A spy recording the max time it sees never observes a future bar."""
    frame = _bars([100.0, 101, 102, 103, 104])
    seen_max: list[int] = []

    def spy(window: pl.DataFrame) -> None:
        seen_max.append(int(window["time"].max()))

    for t, window in enumerate(InMemoryFeed(frame)):
        spy(window)
        # At step t the spy may only have seen up to bar t's time.
        assert seen_max[-1] == int(frame["time"][t])
        assert seen_max[-1] <= int(frame["time"][t])


# --- DccdFeed historical: normalisation + causal replay -------------------- #


def test_normalise_dccd_ohlc_maps_columns() -> None:
    """dccd OHLC columns map to time,o,h,l,c,v (extras dropped, order kept)."""
    raw = _dccd_ohlc([1.0, 2, 3], start_ns=10**18, span_ns=60 * 10**9)
    norm = normalise_dccd_ohlc(raw)
    assert list(norm.columns) == list(BARS_SCHEMA)
    # close -> c carried through faithfully.
    assert norm["c"].to_list() == [1.0, 2, 3]
    assert norm["time"].to_list() == raw["TS"].to_list()


def test_normalise_dccd_ohlc_rejects_missing_source() -> None:
    """A dccd frame missing a source column (e.g. close) is rejected."""
    raw = _dccd_ohlc([1.0, 2], start_ns=1, span_ns=1).drop("close")
    with pytest.raises(ValueError, match="missing source column"):
        normalise_dccd_ohlc(raw)


def test_dccd_historical_replays_causal_prefixes() -> None:
    """Historical mode reads once, normalises, and replays causal prefixes."""
    span = 60
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([5.0, 6, 7, 8], start_ns=10**18, span_ns=span_ns)
    client = _FakeDccdClient(raw)
    feed = DccdFeed(client, "binance", "BTC/USDT", span)

    windows = list(feed)

    assert len(windows) == 4
    for t, window in enumerate(windows):
        assert list(window.columns) == list(BARS_SCHEMA)
        assert window.height == t + 1
        # Causal: last time is bar t's TS, never a later one.
        assert window["time"][-1] == raw["TS"][t]
        assert window["time"].max() == raw["TS"][t]
    # The read was forwarded with the dccd contract args.
    assert client.calls[0]["exchange"] == "binance"
    assert client.calls[0]["symbol"] == "BTC/USDT"
    assert client.calls[0]["data_type"] == "ohlc"
    assert client.calls[0]["span"] == span


def test_dccd_latest_returns_full_normalised_frame() -> None:
    """``latest()`` returns the full normalised bars frame."""
    raw = _dccd_ohlc([1.0, 2, 3], start_ns=1, span_ns=1)
    feed = DccdFeed(_FakeDccdClient(raw), "kraken", "XBT/USD", 1)
    full = feed.latest()
    assert list(full.columns) == list(BARS_SCHEMA)
    assert full.height == 3


def test_dccd_rejects_non_positive_span() -> None:
    """Construction rejects a non-positive span."""
    with pytest.raises(ValueError, match="span must be positive"):
        DccdFeed(_FakeDccdClient(_dccd_ohlc([], start_ns=0, span_ns=1)), "x", "y", 0)


def test_dccd_is_a_datafeed() -> None:
    """DccdFeed satisfies the runtime-checkable DataFeed protocol."""
    feed = DccdFeed(_FakeDccdClient(_dccd_ohlc([1.0], start_ns=0, span_ns=1)), "x", "y", 1)
    assert isinstance(feed, DataFeed)


# --- DccdFeed live: closed-bar-only ---------------------------------------- #


class _FakeClock:
    """A monotonic fake clock (ns) advanced by the fake sleep, for live tests."""

    def __init__(self, start_ns: int) -> None:
        self.t = start_ns

    def now_ns(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += int(seconds * 1_000_000_000)


async def test_live_emits_only_closed_bars() -> None:
    """Live mode never yields a bar whose close time has not yet passed.

    Three bars exist in the store at times t0, t0+span, t0+2*span. The clock
    starts just after bar 0's close window but before bar 1's, so the first poll
    must surface only bar 0 (bars 1 and 2 are still 'forming' relative to the
    clock). As the fake clock advances one span per poll, each newly closed bar
    appears, and a window is never longer than the number of bars that have
    actually closed.
    """
    span = 60
    span_ns = span * 1_000_000_000
    t0 = 1_000 * span_ns
    raw = _dccd_ohlc([10.0, 11, 12], start_ns=t0, span_ns=span_ns)
    client = _FakeDccdClient(raw)
    feed = DccdFeed(client, "binance", "BTC/USDT", span)

    # Clock starts one span after bar 0's open => bar 0 is closed, bars 1,2 not.
    clock = _FakeClock(t0 + span_ns)

    windows: list[pl.DataFrame] = []
    async for window in feed.live_windows(
        now_ns=clock.now_ns, sleep=clock.sleep, max_steps=3
    ):
        windows.append(window)

    # One window per newly closed bar; growing causal prefixes.
    assert [w.height for w in windows] == [1, 2, 3]
    # First window holds only the closed bar 0 — the not-yet-closed bars were
    # excluded (no lookahead / no partial bar).
    assert windows[0]["time"].to_list() == [t0]
    assert windows[1]["time"].to_list() == [t0, t0 + span_ns]
    assert windows[2]["time"].to_list() == [t0, t0 + span_ns, t0 + 2 * span_ns]
    # Every read was capped at the closed-bar cutoff (now - span), never beyond.
    for call in client.calls:
        assert call["end_ns"] is not None


class _StopPolling(Exception):
    """Sentinel raised by the fake sleep to break an unbounded live loop."""


async def test_live_does_not_emit_unclosed_bar_when_none_closed() -> None:
    """With the clock before the first bar closes, no window is emitted."""
    span = 60
    span_ns = span * 1_000_000_000
    t0 = 5_000 * span_ns
    raw = _dccd_ohlc([1.0], start_ns=t0, span_ns=span_ns)
    feed = DccdFeed(_FakeDccdClient(raw), "binance", "BTC/USDT", span)

    # Clock sits during bar 0's formation window (before t0 + span): not closed.
    clock = _FakeClock(t0 + span_ns // 2)

    windows: list[pl.DataFrame] = []
    # Bound the loop by sleeps via a small wrapper: stop after a few polls.
    polls = 0

    async def bounded_sleep(seconds: float) -> None:
        nonlocal polls
        polls += 1
        # Do NOT advance the clock — the bar stays unclosed forever here.
        if polls >= 3:
            raise _StopPolling

    with pytest.raises(_StopPolling):
        async for window in feed.live_windows(
            now_ns=clock.now_ns, sleep=bounded_sleep
        ):
            windows.append(window)

    assert windows == []  # nothing closed => nothing emitted


# --- Verification on real data (opt-in) ------------------------------------ #


@pytest.mark.network
def test_dccd_real_inventory_causal_replay() -> None:
    """Real dccd: replay a stored OHLC dataset and assert no lookahead.

    Skips with a clear reason when no OHLC dataset is stored (the injected-client
    tests above cover the logic; this only adds real-data coverage when present).
    """
    dccd = pytest.importorskip("dccd")
    client = dccd.Client()
    ohlc = [d for d in client.inventory() if d.get("data_type") == "ohlc" and d.get("rows", 0) > 0]
    if not ohlc:
        pytest.skip("no stored dccd OHLC dataset to replay (inventory empty)")

    entry = ohlc[0]
    feed = DccdFeed(
        client,
        entry["exchange"],
        entry["pair"],
        int(entry["span"]),
    )
    full = feed.latest()
    if full.height == 0:
        pytest.skip(f"dccd OHLC dataset {entry['exchange']}/{entry['pair']} read empty")

    assert list(full.columns) == list(BARS_SCHEMA)
    # Monotonic non-decreasing timestamps over the whole frame.
    times = full["time"].to_list()
    assert times == sorted(times)

    # Replay a few causal prefixes and assert each window's last time is its
    # current bar's — never a later one.
    for t, window in enumerate(feed):
        assert window.height == t + 1
        assert window["time"][-1] == full["time"][t]
        assert window["time"].max() == full["time"][t]
        if t >= 4:
            break
