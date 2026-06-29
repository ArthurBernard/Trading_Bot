"""Tests for :mod:`trading_bot.application.portfolio_feed`.

These prove :class:`PortfolioFeed` is a correct **multi-instrument, common-index,
causal** feed over a universe of coins, all offline against an **injected fake
dccd client** returning canned daily bars per coin (no real dccd / network):

* **common-index alignment** — coins with mismatched date ranges yield only the
  *intersection* of their bar dates; a coin missing the latest day means that day
  is never emitted (the freshness gate; the cross-section is never computed on a
  partial universe);
* **causality / no lookahead** — at step ``t`` no coin's window contains a
  timestamp ``> t``; the windows grow monotonically, one common date per step;
* the per-coin window at the final step carries ``≥`` the configured lookback
  rows when the fixture provides them (≥ 200 daily closes for an SMA-200 trend);
* :meth:`PortfolioFeed.asof_ms` equals the latest common date's close in ms;
* a lagging coin is **logged, never raised**.

A separate ``-m network`` test verifies the same properties on the **real** dccd
Binance store (10 LS1 coins resampled 1m→1d), skipping with a clear how-to-sync
reason when the store is absent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import polars as pl
import pytest

from trading_bot.application.portfolio_feed import PortfolioFeed
from trading_bot.domain.instrument import Symbol

# --- helpers --------------------------------------------------------------- #

_DAY_NS = 86_400 * 1_000_000_000


def _dccd_ohlc(closes: list[float], *, start_ns: int, span_ns: int = _DAY_NS) -> pl.DataFrame:
    """A frame mimicking ``dccd.Client.read(..., 'ohlc')`` (dccd's column names)."""
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
    """A fake dccd client keyed by ``symbol`` → a canned daily OHLC frame.

    ``read`` looks the canned frame up by the pair string the feed passes
    (honouring ``end_ns`` so a cutoff read still works) and records each call's
    args so a test can assert what the feed forwarded. No real dccd needed.
    """

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames
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
        frame = self._frames[symbol]
        if end_ns is not None:
            frame = frame.filter(pl.col("TS") <= end_ns)
        return frame


def _client_for(
    universe: list[Symbol],
    frames_by_coin: Mapping[Symbol, pl.DataFrame],
    *,
    exchange: str = "binance",
) -> _FakeDccdClient:
    """Build a fake client keyed by each coin's venue pair string."""
    return _FakeDccdClient(
        {sym.to_venue_symbol(exchange): frames_by_coin[sym] for sym in universe}
    )


# --- construction validation ----------------------------------------------- #


def test_rejects_empty_universe() -> None:
    """An empty universe is a clear ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        PortfolioFeed([], exchange="binance", client=_FakeDccdClient({}))


def test_rejects_duplicate_universe() -> None:
    """A universe with a duplicate symbol is rejected."""
    btc = Symbol("BTC", "USDT")
    with pytest.raises(ValueError, match="duplicate"):
        PortfolioFeed([btc, btc], exchange="binance", client=_FakeDccdClient({}))


def test_rejects_blank_exchange() -> None:
    """A blank exchange is rejected."""
    with pytest.raises(ValueError, match="exchange"):
        PortfolioFeed(
            [Symbol("BTC", "USDT")], exchange="  ", client=_FakeDccdClient({})
        )


def test_rejects_non_positive_span() -> None:
    """A non-positive span is rejected."""
    with pytest.raises(ValueError, match="span"):
        PortfolioFeed(
            [Symbol("BTC", "USDT")],
            exchange="binance",
            span=0,
            client=_FakeDccdClient({}),
        )


# --- common-index alignment + freshness gate ------------------------------- #


def test_aligns_on_intersection_of_dates() -> None:
    """Mismatched ranges yield only the intersection of bar dates."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    # BTC: days 0..4; ETH: days 2..6. Intersection = days 2,3,4 (3 dates).
    frames = {
        btc: _dccd_ohlc([1.0, 2, 3, 4, 5], start_ns=0),
        eth: _dccd_ohlc([10.0, 20, 30, 40, 50], start_ns=2 * _DAY_NS),
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    latest = feed.latest()
    assert latest[btc].height == 3
    assert latest[eth].height == 3
    # The common dates are days 2,3,4 (in ns).
    expected = [2 * _DAY_NS, 3 * _DAY_NS, 4 * _DAY_NS]
    assert latest[btc]["time"].to_list() == expected
    assert latest[eth]["time"].to_list() == expected

    windows = list(feed)
    assert len(windows) == 3  # one step per common date


def test_coin_missing_latest_day_not_emitted() -> None:
    """A coin missing the latest day → that day is never emitted (freshness gate)."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    # BTC has 5 days (0..4); ETH lags by one (only days 0..3).
    frames = {
        btc: _dccd_ohlc([1.0, 2, 3, 4, 5], start_ns=0),
        eth: _dccd_ohlc([10.0, 20, 30, 40], start_ns=0),
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    windows = list(feed)
    # Day 4 is BTC-only -> dropped. Only the 4 common days (0..3) are emitted.
    assert len(windows) == 4
    final = windows[-1]
    # The stale day (day 4) must NOT appear in any coin's final window.
    assert final[btc]["time"].max() == 3 * _DAY_NS
    assert final[eth]["time"].max() == 3 * _DAY_NS
    # The fresh coin's day-4 bar is never forward-filled into the cross-section.
    assert 4 * _DAY_NS not in final[btc]["time"].to_list()


def test_lagging_coin_logs_not_raises(caplog: pytest.LogCaptureFixture) -> None:
    """A coin lagging the universe is logged (warning), never raised."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    frames = {
        btc: _dccd_ohlc([1.0, 2, 3, 4, 5], start_ns=0),  # to day 4
        eth: _dccd_ohlc([10.0, 20, 30], start_ns=0),  # to day 2 (lags)
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    with caplog.at_level(logging.WARNING, logger="trading_bot.application.portfolio_feed"):
        windows = list(feed)  # does not raise

    assert len(windows) == 3  # common days 0..2
    assert any("lags the universe" in r.message for r in caplog.records)


def test_no_common_dates_yields_nothing() -> None:
    """Non-overlapping coins yield no windows and asof_ms is None."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    frames = {
        btc: _dccd_ohlc([1.0, 2], start_ns=0),  # days 0,1
        eth: _dccd_ohlc([10.0, 20], start_ns=10 * _DAY_NS),  # days 10,11
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    assert list(feed) == []
    assert feed.asof_ms() is None


# --- causality / no lookahead ---------------------------------------------- #


def test_windows_are_causal_and_grow_monotonically() -> None:
    """At step t no coin's window has a bar > day t; windows grow by one date."""
    btc, eth, ltc = Symbol("BTC", "USDT"), Symbol("ETH", "USDT"), Symbol("LTC", "USDT")
    universe = [btc, eth, ltc]
    # All three share days 0..5 (plus tails that drop out by inner-join).
    frames = {
        btc: _dccd_ohlc([float(i) for i in range(6)], start_ns=0),
        eth: _dccd_ohlc([float(i) for i in range(8)], start_ns=0),  # extra tail
        ltc: _dccd_ohlc([float(i) for i in range(6)], start_ns=0),
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    common = sorted({0 * _DAY_NS + _DAY_NS * i for i in range(6)})  # days 0..5
    windows = list(feed)
    assert len(windows) == 6

    prev_height: dict[Symbol, int] = {sym: 0 for sym in universe}
    for t, window in enumerate(windows):
        for sym in universe:
            f = window[sym]
            # Causal: every coin's window holds exactly common dates 0..t, and
            # its last time is common-date t's — never a later bar (no lookahead).
            assert f.height == t + 1
            assert f["time"].to_list() == common[: t + 1]
            assert f["time"].max() == common[t]
            # Monotonic growth: each window is one date longer than the last.
            assert f.height == prev_height[sym] + 1
            prev_height[sym] = f.height


def test_final_window_has_at_least_lookback_rows() -> None:
    """The per-coin final window has ≥ the configured lookback rows."""
    lookback = 200
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    n = lookback + 30  # comfortably above lookback
    frames = {
        btc: _dccd_ohlc([float(i) for i in range(n)], start_ns=0),
        eth: _dccd_ohlc([float(i) for i in range(n)], start_ns=0),
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    final = list(feed)[-1]
    for sym in universe:
        assert final[sym].height >= lookback


# --- asof timestamp -------------------------------------------------------- #


def test_asof_ms_is_latest_common_date_in_ms() -> None:
    """asof_ms equals the latest common date's close, ns → ms."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    frames = {
        btc: _dccd_ohlc([1.0, 2, 3, 4], start_ns=0),  # days 0..3
        eth: _dccd_ohlc([10.0, 20, 30], start_ns=0),  # days 0..2 (lags one)
    }
    feed = PortfolioFeed(universe, exchange="binance", client=_client_for(universe, frames))

    # Latest common date is day 2 (ETH stops there). asof = its ns // 1e6.
    expected_ms = (2 * _DAY_NS) // 1_000_000
    assert feed.asof_ms() == expected_ms


# --- forwarding to the dccd read ------------------------------------------- #


def test_forwards_exchange_span_symbol_to_read() -> None:
    """Each coin's read gets the exchange, span and its venue pair string."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    frames = {
        btc: _dccd_ohlc([1.0, 2], start_ns=0),
        eth: _dccd_ohlc([10.0, 20], start_ns=0),
    }
    client = _client_for(universe, frames)
    feed = PortfolioFeed(universe, exchange="binance", span=86_400, client=client)

    list(feed)  # drive the reads

    symbols_read = {c["symbol"] for c in client.calls}
    assert symbols_read == {"BTCUSDT", "ETHUSDT"}
    assert all(c["exchange"] == "binance" for c in client.calls)
    assert all(c["span"] == 86_400 for c in client.calls)
    assert all(c["data_type"] == "ohlc" for c in client.calls)


def test_symbol_for_override_renders_store_keys() -> None:
    """A symbol_for override matches a store keyed by a different convention."""
    btc, eth = Symbol("BTC", "USDT"), Symbol("ETH", "USDT")
    universe = [btc, eth]
    # Store keyed dccd-dir-style "BTC-USDT" (hyphen) rather than "BTCUSDT".
    client = _FakeDccdClient(
        {
            "BTC-USDT": _dccd_ohlc([1.0, 2, 3], start_ns=0),
            "ETH-USDT": _dccd_ohlc([10.0, 20, 30], start_ns=0),
        }
    )
    feed = PortfolioFeed(
        universe,
        exchange="binance",
        client=client,
        symbol_for=lambda s: f"{s.base}-{s.quote}",
    )

    windows = list(feed)
    assert len(windows) == 3
    assert {c["symbol"] for c in client.calls} == {"BTC-USDT", "ETH-USDT"}


# --- Verification on real data (opt-in) ------------------------------------ #

# The 10 LS1 coins (all *-USDT, daily) — the universe the real-data check drains.
_LS1_COINS = ["BTC", "ETH", "BCH", "LTC", "XRP", "XLM", "DOGE", "DOT", "TRX", "ZEC"]


@pytest.mark.network
def test_real_binance_daily_portfolio_feed_is_causal_and_gated() -> None:
    """Real dccd Binance store: drain a 10-coin daily PortfolioFeed and check it.

    Asserts (a) every emitted rebalance date has a closed bar for ALL 10 coins
    (inner-join freshness gate), (b) the final window has ≥ 200 daily closes per
    coin, and (c) no window contains a future bar (causality).

    The Binance store holds **1-minute** bars (``span 60``); dccd's ``read`` is
    keyed by span and does **not** resample, so daily bars are resampled here the
    same way the rest of the triptych does (``fynance_research.data.load_ohlc`` —
    polars ``group_by_dynamic(every="1d")``, OHLCV-correct). The resampling
    client is then injected, so this exercises the real common-index alignment +
    causality on real prices.

    Skips with a clear how-to-sync reason when the store is absent (per
    ``../fynance-research/DEPLOY_LS1.md`` §3: the alt USDT pairs sync to
    ``~/data/arthurserver/binance/ohlc/<PAIR>/1m/``).
    """
    from pathlib import Path

    root = Path.home() / "data" / "arthurserver" / "binance" / "ohlc"
    if not root.is_dir():
        pytest.skip(
            "no dccd Binance store at ~/data/arthurserver/binance/ohlc; sync the "
            "10 LS1 *-USDT daily pairs via the dccd daemon (DEPLOY_LS1.md §3) — "
            "they land at ~/data/arthurserver/binance/ohlc/<PAIR>/1m/"
        )

    universe = [Symbol(c, "USDT") for c in _LS1_COINS]

    # Resample-on-read client: reads the 1m parquet for a coin and aggregates to
    # daily, returning dccd-shaped OHLC columns (mirrors fynance_research.data).
    agg = [
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
    ]

    class _ResampleClient:
        def read(
            self,
            exchange: str,
            symbol: str,
            data_type: str = "ohlc",
            span: int | None = None,
            start_ns: int | None = None,
            end_ns: int | None = None,
        ) -> pl.DataFrame:
            base = root / symbol / "1m"
            files = sorted(base.glob("*.parquet"))
            if not files:
                pytest.skip(f"no 1m parquet for {symbol} under {base}")
            raw = (
                pl.concat([pl.read_parquet(f) for f in files])
                .unique(subset="TS", keep="last")
                .sort("TS")
            )
            daily = (
                raw.with_columns(pl.from_epoch("TS", time_unit="ns").alias("dt"))
                .group_by_dynamic("dt", every="1d")
                .agg(*agg)
                .sort("dt")
                .with_columns(
                    [
                        pl.col("dt").dt.timestamp("ns").alias("TS"),
                        pl.lit(0.0).alias("quote_volume"),
                        pl.lit(0).alias("trades"),
                    ]
                )
                .select(
                    "TS", "open", "high", "low", "close", "volume",
                    "quote_volume", "trades",
                )
            )
            if end_ns is not None:
                daily = daily.filter(pl.col("TS") <= end_ns)
            return daily

    feed = PortfolioFeed(
        universe,
        exchange="binance",
        client=_ResampleClient(),
        span=86_400,
        symbol_for=lambda s: f"{s.base}-{s.quote}",  # store dirs are "BTC-USDT"
    )

    latest = feed.latest()
    # All coins present, same number of common dates each.
    heights = {sym: latest[sym].height for sym in universe}
    assert len(set(heights.values())) == 1, f"unequal common-date counts: {heights}"
    common_dates = next(iter(latest.values()))["time"].to_list()
    assert len(common_dates) > 200, f"too few common daily dates: {len(common_dates)}"

    # (a) every emitted rebalance date has a closed bar for ALL 10 coins.
    common_set = set(common_dates)
    for sym in universe:
        assert set(latest[sym]["time"].to_list()) == common_set

    # Drain and check causality on a sampled set of steps (full drain is ~2000
    # steps × 10 coins — sample to keep the test brisk while still proving it).
    windows = list(feed)
    assert len(windows) == len(common_dates)
    sorted_common = sorted(common_dates)
    step_indices = sorted(
        {0, 1, len(windows) // 2, len(windows) - 2, len(windows) - 1}
    )
    for t in step_indices:
        window = windows[t]
        for sym in universe:
            f = window[sym]
            # (c) no future bar: the window's last time is common-date t's.
            assert f.height == t + 1
            assert f["time"].max() == sorted_common[t]
            assert f["time"][-1] == sorted_common[t]

    # (b) the final window has ≥ 200 daily closes per coin.
    final = windows[-1]
    for sym in universe:
        assert final[sym].height >= 200, f"{sym} final window < 200 closes"

    # asof is the latest common date in ms.
    assert feed.asof_ms() == sorted_common[-1] // 1_000_000
