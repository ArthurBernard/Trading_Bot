"""Tests for the pure PnL-series helpers (:mod:`trading_bot.application.pnl_series`).

Prove the fill fold + the per-mode split against a known sequence, with money
exact end to end (no float) and no I/O:

* :func:`equity_series` folds fills in **timestamp order** into
  ``(ts_ms, realised_pnl, equity)`` points, ``equity = v0 + cumulative realised
  PnL``, reconciling to the domain :class:`~trading_bot.domain.position.Position`
  fold (so it can never diverge from the engine's ``perf.realised_pnl()``);
* :func:`by_mode` partitions tagged fills into ``{mode: [Fill, ...]}`` so live and
  testnet (fake money) become separate series.

Fynance-free (this is realised-PnL from fills), so it runs under the CI matrix
with dccd + fynance absent.
"""

from __future__ import annotations

from trading_bot.application.pnl_series import by_mode, equity_series
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide
from trading_bot.domain.position import Position
from trading_bot.storage.sqlite_store import StoredFill

BTC = Instrument(Symbol("BTC", "USD"))
ETH = Instrument(Symbol("ETH", "USD"))


def _fill(
    fid: str,
    side: OrderSide,
    qty: str,
    price: str,
    *,
    fee: str = "1",
    ts: int = 0,
    inst: Instrument = BTC,
) -> Fill:
    return Fill(fid, f"c-{fid}", inst, side, money(qty), money(price), money(fee), ts)


# --- equity_series --------------------------------------------------------- #


def test_empty_series_is_empty() -> None:
    """No fills → no points."""
    assert equity_series([], v0=money("100")) == []


def test_round_trip_folds_to_v0_plus_realised() -> None:
    """A buy→sell round trip: two points, equity = v0 + cumulative realised PnL."""
    fills = [
        _fill("F1", OrderSide.BUY, "1", "100", fee="1", ts=1),
        _fill("F2", OrderSide.SELL, "1", "110", fee="1", ts=2),
    ]
    points = equity_series(fills, v0=money("1000"))
    assert [p.ts_ms for p in points] == [1, 2]
    # After the buy: no close yet, only the -1 fee realised. equity = 1000 - 1.
    assert points[0].realised_pnl == money("-1")
    assert points[0].equity == money("999")
    # After the sell: +10 gross - 2 fees = +8 realised. equity = 1008.
    assert points[1].realised_pnl == money("8")
    assert points[1].equity == money("1008")


def test_series_is_sorted_by_timestamp() -> None:
    """Fills given out of order are folded in ascending ts (the curve is time-ordered)."""
    fills = [
        _fill("F2", OrderSide.SELL, "1", "110", fee="1", ts=2),
        _fill("F1", OrderSide.BUY, "1", "100", fee="1", ts=1),
    ]
    points = equity_series(fills, v0=money("0"))
    assert [p.ts_ms for p in points] == [1, 2]
    assert points[-1].realised_pnl == money("8")


def test_final_equity_matches_domain_position_fold() -> None:
    """The final realised PnL equals Position.from_fills over the same fills.

    The load-bearing invariant: the series reuses the domain Position fold, so its
    endpoint reconciles exactly to `Position.from_fills(...).realised_pnl` — the
    same value the engine's performance service reports (never a re-derivation).
    """
    fills = [
        _fill("F1", OrderSide.BUY, "2", "100", fee="1", ts=1),
        _fill("F2", OrderSide.SELL, "1", "120", fee="1", ts=2),
        _fill("F3", OrderSide.SELL, "1", "90", fee="1", ts=3),
    ]
    points = equity_series(fills, v0=money("500"))
    expected = Position.from_fills(fills).realised_pnl
    assert points[-1].realised_pnl == expected
    assert points[-1].equity == money("500") + expected


def test_multi_instrument_realised_is_additive() -> None:
    """Realised PnL sums across instruments (each fill's contribution is a delta)."""
    fills = [
        _fill("B1", OrderSide.BUY, "1", "100", fee="0", ts=1, inst=BTC),
        _fill("E1", OrderSide.BUY, "1", "10", fee="0", ts=2, inst=ETH),
        _fill("B2", OrderSide.SELL, "1", "110", fee="0", ts=3, inst=BTC),
        _fill("E2", OrderSide.SELL, "1", "12", fee="0", ts=4, inst=ETH),
    ]
    points = equity_series(fills, v0=money("0"))
    # BTC round trip +10, ETH round trip +2 → +12 total realised, zero fees.
    assert points[-1].realised_pnl == money("12")
    assert points[-1].equity == money("12")


def test_money_is_exact_decimal_not_float() -> None:
    """A dusty price folds exactly (no float rounding)."""
    fills = [
        _fill("F1", OrderSide.BUY, "0.1", "0.1", fee="0", ts=1),
        _fill("F2", OrderSide.SELL, "0.1", "0.3", fee="0", ts=2),
    ]
    points = equity_series(fills, v0=money("0"))
    # (0.3 - 0.1) * 0.1 = 0.02 exactly.
    assert points[-1].realised_pnl == money("0.02")


# --- by_mode --------------------------------------------------------------- #


def _stored(fid: str, mode: str, venue: str = "", ts: int = 0) -> StoredFill:
    return StoredFill(
        fill=_fill(fid, OrderSide.BUY, "1", "100", ts=ts), mode=mode, venue=venue
    )


def test_by_mode_partitions_and_preserves_order() -> None:
    """by_mode buckets by the mode tag, first-seen mode order, fills in input order."""
    stored = [
        _stored("P1", "paper"),
        _stored("T1", "testnet", "binance"),
        _stored("P2", "paper"),
        _stored("T2", "testnet", "binance"),
    ]
    buckets = by_mode(stored)
    assert list(buckets) == ["paper", "testnet"]  # first-seen order
    assert [f.fill_id for f in buckets["paper"]] == ["P1", "P2"]
    assert [f.fill_id for f in buckets["testnet"]] == ["T1", "T2"]


def test_by_mode_empty() -> None:
    """No stored fills → an empty mapping."""
    assert by_mode([]) == {}


def test_live_and_testnet_fold_from_v0_separately() -> None:
    """Two modes give two independent series, each anchored at the same v0.

    The core model: testnet (fake money) is never combined with live. Each mode's
    fills fold into their own curve from the strategy's v0.
    """
    stored = [
        StoredFill(_fill("T1", OrderSide.BUY, "1", "100", fee="0", ts=1), "testnet", "b"),
        StoredFill(_fill("T2", OrderSide.SELL, "1", "110", fee="0", ts=2), "testnet", "b"),
        StoredFill(_fill("L1", OrderSide.BUY, "1", "100", fee="0", ts=1), "live", "k"),
        StoredFill(_fill("L2", OrderSide.SELL, "1", "105", fee="0", ts=2), "live", "k"),
    ]
    buckets = by_mode(stored)
    testnet = equity_series(buckets["testnet"], v0=money("1000"))
    live = equity_series(buckets["live"], v0=money("1000"))
    assert testnet[-1].equity == money("1010")  # +10 from v0
    assert live[-1].equity == money("1005")  # +5 from v0, independent
