"""Tests for the :class:`~trading_bot.application.portfolio_runner.PortfolioRunner`.

These prove the multi-asset loop's contract — ``feed → signal_fn →
weights_to_signals → per-coin delta_to(position) → order → router → broker →
fill → tracker`` — end to end against the **real**
:class:`~trading_bot.brokers.paper.PaperBroker`, fully offline:

* **flat → N orders**: one rebalance from flat routes one leg per non-zero-weight
  coin, with sizes equal to ``weightᵢ × capital / priceᵢ`` (exact ``Decimal``) and
  sides matching the weight signs; the broker-confirmed fills (read off the shared
  tracker) match the intended per-coin targets;
* **delta on rebalance**: a second rebalance with *changed* weights routes the
  **delta** vs the now-non-flat positions (read from the shared tracker), not the
  absolute target — and a coin whose weight goes to ``0`` is targeted *flat* (full
  close);
* **idempotency**: re-submitting the same rebalance step (same ``step``) places no
  duplicate orders — the router dedups on the per-coin id;
* **risk gate**: a leg exceeding ``max_order`` raises
  :class:`~trading_bot.domain.errors.RiskLimitBreached`, never reaches the broker,
  and the **other** legs still route (the documented continue-other-legs policy);
* **reconciliation honesty**: after fills, ``tracker.position(coin)`` equals the
  routed cumulative qty per coin — broker-confirmed state, not local optimism.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from decimal import Decimal

import polars as pl
import pytest

from trading_bot.application import (
    EventBus,
    OrderRouter,
    PortfolioFeed,
    PortfolioRunner,
    PortfolioStrategy,
    PositionTracker,
    RiskManager,
)
from trading_bot.application.config import RiskConfig
from trading_bot.application.mark_cache import MarkCache
from trading_bot.brokers import PaperBroker
from trading_bot.domain import (
    CapitalEvent,
    CapitalEventType,
    Instrument,
    OrderSide,
    Symbol,
    money,
)
from trading_bot.domain.errors import RiskLimitBreached

BTC = Symbol("BTC", "USDT")
ETH = Symbol("ETH", "USDT")
UNIVERSE = (BTC, ETH)
CAPITAL = money("100000")

# Reference prices the hand-calc round-trips through exactly.
BTC_PRICE = money("50000")
ETH_PRICE = money("2500")


# --- helpers --------------------------------------------------------------- #


def _frame(close: float, *, time_ns: int = 1_700_000_000_000_000_000) -> pl.DataFrame:
    """A minimal one-row OHLC(V) frame whose latest close is ``close``."""
    return pl.DataFrame(
        {
            "time": [time_ns],
            "o": [close],
            "h": [close],
            "l": [close],
            "c": [close],
            "v": [1.0],
        }
    )


def _frames(
    btc_close: float = 50000.0, eth_close: float = 2500.0
) -> dict[Symbol, pl.DataFrame]:
    """The per-coin cross-section for one rebalance tick."""
    return {BTC: _frame(btc_close), ETH: _frame(eth_close)}


class _ListFeed:
    """A fake portfolio feed: yields canned per-coin cross-sections, with asof.

    Mirrors the iterable a :class:`~trading_bot.application.portfolio_feed.
    PortfolioFeed` is — an iterator of ``Mapping[Symbol, pl.DataFrame]`` — and
    exposes an ``asof_ms()`` so the runner reads the as-of from the feed (the
    real-feed path), not only by deriving it from the frames.
    """

    def __init__(
        self, ticks: list[Mapping[Symbol, pl.DataFrame]], *, asof: int
    ) -> None:
        self._ticks = ticks
        self._asof = asof

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._ticks)

    def asof_ms(self) -> int:
        return self._asof


def _weights_signal(weights: Mapping[Symbol, Decimal]):  # type: ignore[no-untyped-def]
    """A fake :data:`PortfolioSignalFn` returning a fixed weight vector."""

    def _fn(
        asof_ms: int, frames: Mapping[Symbol, pl.DataFrame]
    ) -> Mapping[Symbol, Decimal]:
        return dict(weights)

    return _fn


def _engine(
    *, risk: RiskConfig | None = None
) -> tuple[OrderRouter, PositionTracker, EventBus, PaperBroker]:
    """A real paper-broker engine: router + tracker + bus, optionally risk-gated.

    The broker fills LIMIT legs at their (close) limit price and emits a
    ``FillEvent`` per fill onto the bus the tracker subscribes to, so the loop
    closes (a tick's fills update the *next* tick's positions).
    """
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={
            "USDT": money("100000000"),
            "BTC": money("0"),
            "ETH": money("0"),
        },
        event_bus=bus,
    )
    risk_manager = (
        RiskManager(risk, position_tracker=tracker) if risk is not None else None
    )
    router = OrderRouter(broker, bus, risk_manager=risk_manager)
    return router, tracker, bus, broker


def _strategy(signal_fn, *, name: str = "book") -> PortfolioStrategy:  # type: ignore[no-untyped-def]
    return PortfolioStrategy(
        name=name,
        universe=UNIVERSE,
        signal_fn=signal_fn,
        capital=CAPITAL,
    )


# --- flat → N orders ------------------------------------------------------- #


async def test_one_rebalance_from_flat_routes_n_sized_orders() -> None:
    """From flat, a rebalance routes one leg per coin sized weight*capital/price.

    Weights ``+0.5`` BTC / ``-0.25`` ETH at prices 50000 / 2500 with capital
    100000 size to exactly ``+1.0`` BTC (BUY) and ``-10.0`` ETH (SELL). We assert
    the router's submitted orders' sizes/sides, then the **broker-confirmed**
    positions on the shared tracker.
    """
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, _bus, broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([_frames()], asof=1_700),
        router,
        tracker,
        event_bus=_bus,
    )

    result = await runner.rebalance(_frames())

    assert result.submitted == 2
    assert result.failed == 0

    # Router-side: the submitted orders carry the exact target sizes & sides.
    orders = router.tracked_orders()
    assert set(orders) == {"book-BTC/USDT-0", "book-ETH/USDT-0"}
    btc_order = orders["book-BTC/USDT-0"]
    eth_order = orders["book-ETH/USDT-0"]
    assert btc_order.side is OrderSide.BUY and btc_order.qty == Decimal("1")
    assert eth_order.side is OrderSide.SELL and eth_order.qty == Decimal("10")

    # Broker-confirmed: the shared tracker (fed off the bus) reflects the fills,
    # not local optimism.
    btc_pos = tracker.position(Instrument(BTC))
    eth_pos = tracker.position(Instrument(ETH))
    assert btc_pos is not None and btc_pos.net_qty == Decimal("1")
    assert eth_pos is not None and eth_pos.net_qty == Decimal("-10")


async def test_zero_weight_coin_routes_no_leg() -> None:
    """A coin with weight 0 (and flat) is on target → no leg for it."""
    weights = {BTC: money("0.5"), ETH: money("0")}
    router, tracker, _bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([_frames()], asof=1_700),
        router,
        tracker,
    )

    result = await runner.rebalance(_frames())

    assert result.submitted == 1  # only BTC traded
    assert set(router.tracked_orders()) == {"book-BTC/USDT-0"}
    # ETH never traded → no fill → no tracked position.
    assert tracker.position(Instrument(ETH)) is None


async def test_omitted_coin_is_targeted_flat() -> None:
    """A coin the signal *omits* (not in the weight vector) is covered as flat.

    The book covers the whole universe: an omitted coin defaults to weight 0, so
    once it holds a position it is fully closed on the next rebalance.
    """
    # First: open both. Second: signal omits ETH entirely → ETH closed flat.
    router, tracker, _bus, _broker = _engine()
    open_both = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    runner = PortfolioRunner(open_both, _ListFeed([], asof=1_700), router, tracker)
    await runner.rebalance(_frames())
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("-10")

    # Now a signal that only mentions BTC; ETH is omitted → targeted flat. A
    # distinct strategy name namespaces the new legs (the first runner's ids are
    # already tracked at the shared router).
    runner_2 = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("0.5")}), name="book2"),  # ETH omitted
        _ListFeed([], asof=1_701),
        router,
        tracker,
    )
    result = await runner_2.rebalance(_frames())

    # BTC already on target (+1) → no leg; ETH closed from -10 → +10 BUY leg.
    assert result.submitted == 1
    eth_close_leg = router.tracked_orders()["book2-ETH/USDT-0"]
    assert eth_close_leg.side is OrderSide.BUY and eth_close_leg.qty == Decimal("10")
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("0")


# --- delta on rebalance ---------------------------------------------------- #


async def test_second_rebalance_routes_delta_not_absolute() -> None:
    """A second rebalance routes the delta vs the now-non-flat shared positions.

    Tick 0: +0.5 BTC (+1) / -0.25 ETH (-10). Tick 1 (changed weights): +0.8 BTC
    (target +1.6, so delta +0.6 BUY) / +0.1 ETH (target +4, from -10 so delta +14
    BUY). The legs must be the *deltas*, not the absolute targets.
    """
    router, tracker, _bus, _broker = _engine()

    t0 = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    runner_0 = PortfolioRunner(t0, _ListFeed([], asof=1_700), router, tracker)
    await runner_0.rebalance(_frames())
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("-10")

    # A *different* runner (fresh step index 0) re-allocates the shared book.
    t1 = _strategy(
        _weights_signal({BTC: money("0.8"), ETH: money("0.1")}), name="book2"
    )
    runner_1 = PortfolioRunner(t1, _ListFeed([], asof=1_701), router, tracker)
    result = await runner_1.rebalance(_frames())

    assert result.submitted == 2
    btc_leg = router.tracked_orders()["book2-BTC/USDT-0"]
    eth_leg = router.tracked_orders()["book2-ETH/USDT-0"]
    # target +1.6, current +1 → +0.6 BUY
    assert btc_leg.side is OrderSide.BUY and btc_leg.qty == Decimal("0.6")
    # target +4, current -10 → +14 BUY
    assert eth_leg.side is OrderSide.BUY and eth_leg.qty == Decimal("14")

    # Broker-confirmed resulting positions are the new absolute targets.
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1.6")
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("4")


async def test_on_target_rebalance_submits_nothing() -> None:
    """Re-running the *same* weights against the resulting book is a no-op.

    After a rebalance lands the targets, an immediate identical rebalance finds
    every coin already on target (delta 0) and routes no leg.
    """
    router, tracker, _bus, broker = _engine()
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    runner = PortfolioRunner(strat, _ListFeed([], asof=1_700), router, tracker)

    await runner.rebalance(_frames())
    fills_after_1 = len(await broker.fills())

    # Same weights, same book → delta 0 everywhere.
    result_2 = await runner.rebalance(_frames())
    assert result_2.submitted == 0
    assert len(await broker.fills()) == fills_after_1


# --- idempotency ----------------------------------------------------------- #


async def test_resubmitting_same_step_does_not_double_route() -> None:
    """Re-submitting the same rebalance step places no duplicate venue orders.

    The per-coin ids ``f"{name}-{symbol}-{step}"`` make a re-run a no-op at the
    router. We capture the legs of step 0, then replay those exact orders through
    the same router and assert the broker placed no new fills.
    """
    router, tracker, _bus, broker = _engine()
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    runner = PortfolioRunner(strat, _ListFeed([], asof=1_700), router, tracker)

    await runner.rebalance(_frames())
    fills_after_1 = len(await broker.fills())
    tracked_after_1 = set(router.tracked_orders())
    submitted_orders = list(router.tracked_orders().values())

    # Replay the very same orders (same client-order-ids) through the router.
    for order in submitted_orders:
        again = await router.submit(order)
        assert again is order  # already-tracked order, never re-submits

    assert len(await broker.fills()) == fills_after_1
    assert set(router.tracked_orders()) == tracked_after_1


async def test_rerun_same_step_index_is_deterministic_noop() -> None:
    """A *fresh* runner replaying step 0's tick against the filled book is a no-op.

    Two runners (same name, same feed, fresh step indices) over the *shared*
    engine. Runner A's fills already moved the shared tracker to target, so
    runner B finds every coin on target (delta 0) and routes nothing — no new
    fills, no double-counting. (Even had a leg been built, its id matches A's and
    the router would dedup it; here it never gets that far because the position is
    already correct — reconciliation honesty at the position level.)
    """
    router, tracker, _bus, broker = _engine()
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))

    runner_a = PortfolioRunner(strat, _ListFeed([], asof=1_700), router, tracker)
    await runner_a.rebalance(_frames())
    fills_after_a = len(await broker.fills())

    runner_b = PortfolioRunner(strat, _ListFeed([], asof=1_700), router, tracker)
    result_b = await runner_b.rebalance(_frames())

    # On target everywhere → no legs, no new fills, positions unchanged.
    assert result_b.submitted == 0
    assert len(await broker.fills()) == fills_after_a
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("-10")


# --- risk gate ------------------------------------------------------------- #


async def test_risk_breach_on_one_leg_does_not_abort_others() -> None:
    """A leg over ``max_order`` raises and never fills, but other legs still route.

    With ``max_order = 5``, BTC's +1 leg passes but ETH's -10 leg breaches. The
    documented policy is *continue the other legs*: BTC routes and fills, ETH is
    recorded as a failure, and the rebalance is not aborted.
    """
    router, tracker, _bus, broker = _engine(risk=RiskConfig(max_order=money("5")))
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    runner = PortfolioRunner(strat, _ListFeed([], asof=1_700), router, tracker)

    result = await runner.rebalance(_frames())

    # BTC (+1) routed; ETH (-10) breached max_order and was recorded.
    assert result.submitted == 1
    assert result.failed == 1
    assert result.failures[0].symbol == ETH
    assert isinstance(result.failures[0].error, RiskLimitBreached)

    # BTC filled (broker-confirmed); ETH never reached the broker.
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")
    assert tracker.position(Instrument(ETH)) is None
    # The breaching leg is not tracked (a refused order leaves no record).
    assert "book-ETH/USDT-0" not in router.tracked_orders()
    assert "book-BTC/USDT-0" in router.tracked_orders()


async def test_kill_switch_fails_every_leg() -> None:
    """A tripped kill-switch refuses every leg → N failures, zero submitted."""
    risk = RiskConfig()
    router, tracker, _bus, broker = _engine(risk=risk)
    # Reach into the router's risk manager to trip it.
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
    )
    router._risk.trip("test halt")  # type: ignore[union-attr]

    result = await runner.rebalance(_frames())

    assert result.submitted == 0
    assert result.failed == 2
    assert {f.symbol for f in result.failures} == {BTC, ETH}
    assert await broker.fills() == []


# --- reconciliation honesty + run() loop ----------------------------------- #


async def test_run_drives_feed_and_tracker_matches_routed_qty() -> None:
    """``run`` over a feed routes each tick; final positions = routed cumulative.

    A two-tick feed: tick 0 opens, tick 1 (same runner, same weights) is on
    target → no new legs. The final broker-confirmed positions equal the routed
    cumulative quantity per coin.
    """
    router, tracker, _bus, broker = _engine()
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    feed = _ListFeed([_frames(), _frames()], asof=1_700)
    runner = PortfolioRunner(strat, feed, router, tracker, event_bus=_bus)

    total = await runner.run()

    # Tick 0 routed 2 legs; tick 1 found everything on target → 0.
    assert total == 2
    assert runner.step_index == 2  # both ticks consumed

    # Broker-confirmed positions equal the routed targets, read off the tracker.
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("-10")

    # And the signed fills per coin sum to the tracked net (no double counting).
    fills = await broker.fills()
    by_coin: dict[Symbol, Decimal] = {}
    for f in fills:
        signed = f.qty if f.side is OrderSide.BUY else -f.qty
        by_coin[f.instrument.symbol] = (
            by_coin.get(f.instrument.symbol, Decimal("0")) + signed
        )
    assert by_coin[BTC] == Decimal("1")
    assert by_coin[ETH] == Decimal("-10")


async def test_asof_derived_from_frames_when_feed_has_none() -> None:
    """When the feed exposes no ``asof_ms``, the as-of is derived from the frames.

    A bare list feed (no ``asof_ms``) → the runner derives the as-of from the
    frames' latest common ``time`` (ns → ms) and stamps it on the signals; the
    rebalance still routes correctly.
    """
    router, tracker, _bus, _broker = _engine()
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    # A plain list is iterable but has no asof_ms().
    runner = PortfolioRunner(strat, [_frames()], router, tracker)

    result = await runner.rebalance(_frames())

    assert result.submitted == 2
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")


async def test_money_read_off_frame_is_exact_decimal() -> None:
    """The latest close is read as exact ``Decimal`` (no float), driving sizing.

    A close with a fraction (50000.5) that a ``float`` would not represent
    exactly is read via ``money(str(...))``; the BTC target is sized off the exact
    value. We assert the leg's limit price is the exact decimal and the size is
    ``0.5 * 100000 / 50000.5`` exactly.
    """
    router, tracker, _bus, _broker = _engine()
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("0")}))
    runner = PortfolioRunner(strat, [_frames(btc_close=50000.5)], router, tracker)

    await runner.rebalance(_frames(btc_close=50000.5))

    btc_leg = router.tracked_orders()["book-BTC/USDT-0"]
    assert btc_leg.limit_price == Decimal("50000.5")
    expected = money(str(money("0.5") * CAPITAL / money("50000.5")))
    assert btc_leg.qty == abs(expected)


# --- rebalance_latest: single rebalance over the latest cross-section ------- #


async def test_rebalance_latest_rebalances_over_the_feeds_latest_cross_section() -> (
    None
):
    """`rebalance_latest` takes the feed's latest cross-section and rebalances once."""
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([_frames()], asof=1_700),
        router,
        tracker,
        event_bus=bus,
    )

    result = await runner.rebalance_latest()

    assert result is not None
    assert result.submitted == 2  # one leg per coin, from flat


async def test_rebalance_latest_returns_none_on_empty_feed() -> None:
    """`rebalance_latest` returns None when the feed yields no cross-section yet."""
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("0.5")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
    )

    assert await runner.rebalance_latest() is None


# --- rebalance_latest: bounded reads via feed.latest() (A-7) --------------- #


class _LatestFeed:
    """A feed exposing ``latest()`` + a counting ``__iter__`` (the real-feed shape).

    Mirrors :class:`~trading_bot.application.portfolio_feed.PortfolioFeed`, which
    exposes ``latest()`` (the full aligned cross-section, one store read) *and*
    ``asof_ms()``. Counts both ``latest()`` calls and how many bars ``__iter__``
    would walk, so a test can assert ``rebalance_latest`` reads the latest window
    directly and does **not** drain the whole feed (O(total bars)).
    """

    def __init__(
        self, window: Mapping[Symbol, pl.DataFrame], *, n_bars: int, asof: int
    ) -> None:
        self._window = window
        self._n_bars = n_bars  # how many growing prefixes a full drain would yield
        self._asof = asof
        self.latest_calls = 0
        self.bars_iterated = 0

    def latest(self) -> Mapping[Symbol, pl.DataFrame]:
        self.latest_calls += 1
        return self._window

    def __iter__(self):  # type: ignore[no-untyped-def]
        # A full drain would yield ``n_bars`` growing causal prefixes — count each
        # so a test can prove ``rebalance_latest`` did NOT take this path.
        for _ in range(self._n_bars):
            self.bars_iterated += 1
            yield self._window

    def asof_ms(self) -> int:
        return self._asof


async def test_rebalance_latest_reads_latest_window_once_not_whole_feed() -> None:
    """`rebalance_latest` uses `feed.latest()` once and never drains O(n) bars."""
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, bus, _broker = _engine()
    feed = _LatestFeed(_frames(), n_bars=10_000, asof=1_700)
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)), feed, router, tracker, event_bus=bus
    )

    result = await runner.rebalance_latest()

    assert result is not None
    assert result.submitted == 2  # one leg per coin, from flat
    # Bounded read: exactly one latest() call and NOT a single bar iterated — the
    # O(total bars) per-tick drain is gone.
    assert feed.latest_calls == 1
    assert feed.bars_iterated == 0, "rebalance_latest must not drain the whole feed"


async def test_rebalance_latest_returns_none_when_latest_window_is_empty() -> None:
    """A `latest()`-capable feed with an empty window yields None (no rebalance)."""
    router, tracker, bus, _broker = _engine()
    feed = _LatestFeed({}, n_bars=0, asof=1_700)
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("0.5")})),
        feed,
        router,
        tracker,
        event_bus=bus,
    )

    assert await runner.rebalance_latest() is None
    assert feed.latest_calls == 1
    assert feed.bars_iterated == 0


# --- rebalance_latest: the idle-tick freshness gate ------------------------- #

_DAY_NS = 86_400 * 1_000_000_000


def _dccd_ohlc(
    closes: list[float], *, start_ns: int, span_ns: int = _DAY_NS
) -> pl.DataFrame:
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


class _CountingDccdClient:
    """A fake dccd client, keyed by venue symbol, counting full vs tail reads.

    ``start_ns is None`` means a **full** (whole-history) read — the cost
    :class:`~trading_bot.application.portfolio_feed.PortfolioFeed`'s
    ``_read_all``/``latest`` pays every call; a non-``None`` ``start_ns`` means
    the freshness gate's bounded **tail** probe. A 2-coin universe's one full
    evaluation increments ``full_reads`` by 2 (one read per coin), likewise for
    one tail probe.
    """

    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self.frames = frames
        self.full_reads = 0
        self.tail_reads = 0
        self.raise_on_tail = False

    def read(
        self,
        exchange: str,
        symbol: str,
        data_type: str = "ohlc",
        span: int | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> pl.DataFrame:
        if start_ns is None:
            self.full_reads += 1
        else:
            self.tail_reads += 1
            if self.raise_on_tail:
                raise RuntimeError("tail read boom")
        frame = self.frames[symbol]
        if start_ns is not None:
            frame = frame.filter(pl.col("TS") >= start_ns)
        if end_ns is not None:
            frame = frame.filter(pl.col("TS") <= end_ns)
        return frame


def _counting_client(
    btc_closes: list[float], eth_closes: list[float], *, start_ns: int = 0
) -> _CountingDccdClient:
    return _CountingDccdClient(
        {
            BTC.to_venue_symbol("binance"): _dccd_ohlc(btc_closes, start_ns=start_ns),
            ETH.to_venue_symbol("binance"): _dccd_ohlc(eth_closes, start_ns=start_ns),
        }
    )


def _portfolio_feed_over(client: _CountingDccdClient) -> PortfolioFeed:
    return PortfolioFeed(list(UNIVERSE), exchange="binance", client=client, span=86_400)


def _weights_signal_spy(weights: Mapping[Symbol, Decimal], calls: list[int]):  # type: ignore[no-untyped-def]
    """Like `_weights_signal`, but records every `asof_ms` it is called with."""

    def _fn(
        asof_ms: int, frames: Mapping[Symbol, pl.DataFrame]
    ) -> Mapping[Symbol, Decimal]:
        calls.append(asof_ms)
        return dict(weights)

    return _fn


async def test_rebalance_latest_gate_skips_full_read_when_no_new_common_date() -> None:
    """A second tick over an unchanged store skips the full read and the signal.

    The first-ever tick has no baseline, so it always runs the full path. A
    second tick where the tail probe finds nothing newer than that baseline
    must return ``None`` **without** a second full read and **without**
    invoking ``signal_fn``.
    """
    client = _counting_client([100.0], [50.0])  # a single common day only
    feed = _portfolio_feed_over(client)
    calls: list[int] = []
    strat = _strategy(
        _weights_signal_spy({BTC: money("0.5"), ETH: money("-0.25")}, calls)
    )
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(strat, feed, router, tracker, event_bus=bus)

    first = await runner.rebalance_latest()
    assert first is not None
    assert first.submitted == 2
    assert client.full_reads == 2  # one _read_all() over the 2-coin universe
    assert client.tail_reads == 0  # first-ever tick: no baseline yet
    assert len(calls) == 1
    assert runner.last_asof_ms == 0  # day 0's asof
    assert runner.last_eval_ms is not None

    second = await runner.rebalance_latest()
    assert second is None  # nothing new -> the gate skips
    assert client.full_reads == 2  # NOT read again
    assert client.tail_reads == 2  # exactly one probe over the 2-coin universe
    assert len(calls) == 1  # the signal was never invoked on the skipped tick


async def test_rebalance_latest_gate_falls_through_when_new_common_date_appears() -> (
    None
):
    """A tail probe that finds a newer common date runs the full path once more."""
    client = _counting_client([100.0], [50.0])
    feed = _portfolio_feed_over(client)
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(strat, feed, router, tracker, event_bus=bus)

    await runner.rebalance_latest()
    assert client.full_reads == 2
    assert runner.last_asof_ms == 0

    # A new common day lands for both coins.
    client.frames = {
        BTC.to_venue_symbol("binance"): _dccd_ohlc([100.0, 101.0], start_ns=0),
        ETH.to_venue_symbol("binance"): _dccd_ohlc([50.0, 51.0], start_ns=0),
    }

    result = await runner.rebalance_latest()

    assert result is not None
    assert client.full_reads == 4  # exactly one more full read (2 coins)
    assert client.tail_reads == 2  # exactly one probe (2 coins), saw the advance
    assert runner.last_asof_ms == _DAY_NS // 1_000_000  # day 1's asof


async def test_rebalance_latest_gate_falls_through_on_tail_probe_error() -> None:
    """A tail-read failure is conservative: fall through to the full path."""
    client = _counting_client([100.0], [50.0])
    feed = _portfolio_feed_over(client)
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(strat, feed, router, tracker, event_bus=bus)

    await runner.rebalance_latest()
    assert client.full_reads == 2

    client.raise_on_tail = True
    result = await runner.rebalance_latest()

    # Fell through to the full path (correctness over speed); same weights on
    # unchanged prices -> already on target -> nothing new submitted.
    assert result is not None
    assert result.submitted == 0
    assert client.tail_reads == 1  # the probe raised on the first coin it tried
    assert client.full_reads == 4  # still ran the full path despite the error


async def test_rebalance_latest_last_asof_and_last_eval_update_on_skip_and_full() -> (
    None
):
    """``last_asof_ms``/``last_eval_ms`` update correctly on both paths."""
    client = _counting_client([100.0], [50.0])
    feed = _portfolio_feed_over(client)
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(strat, feed, router, tracker, event_bus=bus)

    assert runner.last_asof_ms is None
    assert runner.last_eval_ms is None

    await runner.rebalance_latest()
    asof_after_full = runner.last_asof_ms
    eval_after_full = runner.last_eval_ms
    assert asof_after_full == 0
    assert eval_after_full is not None

    await runner.rebalance_latest()  # the skip path (nothing new)
    assert runner.last_asof_ms == asof_after_full  # unchanged on a skip
    assert runner.last_eval_ms is not None
    assert runner.last_eval_ms >= eval_after_full  # still updated on a skip


async def test_rebalance_latest_offloads_the_full_read_off_the_event_loop() -> None:
    """A slow, synchronous `latest()` does not block a concurrent coroutine.

    Proves the `asyncio.to_thread` offload actually parallelises: while the
    feed's blocking read sleeps, a concurrent loop gets many chances to run —
    it would not if the read ran synchronously on the event loop.
    """

    class _SlowFeed:
        def latest(self) -> Mapping[Symbol, pl.DataFrame]:
            time.sleep(0.15)  # a slow synchronous "dccd read + alignment"
            return _frames()

    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(strat, _SlowFeed(), router, tracker, event_bus=bus)  # type: ignore[arg-type]

    ticks = 0
    stop = False

    async def _concurrent_loop() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(_concurrent_loop())
    await runner.rebalance_latest()
    stop = True
    ticker.cancel()

    # The concurrent loop must have gotten several chances to run while the
    # slow synchronous read was in flight on its own thread.
    assert ticks >= 5


# --- capital_provider: lazy sizing base per rebalance ----------------------- #


def _cap_provider(tmp_path, policy: str, realised: dict, *, genesis: str = "100000"):  # type: ignore[no-untyped-def]  # noqa: ANN001
    """A real store-backed CapitalService closure the runner sizes against.

    Returns ``(store, provider)`` — ``provider`` folds the ledger live and
    applies ``policy`` against the mutable ``realised["v"]`` on every call, so a
    ledger write (a deposit) or a realised-PnL move is reflected on the next tick
    with no runner rebuild.
    """
    from trading_bot.application.capital_service import CapitalService
    from trading_bot.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "ledger.sqlite"))
    cap = CapitalService(store, "book", "paper")
    cap.ensure_genesis(money(genesis))
    return store, (lambda: cap.sizing_base(policy, realised["v"]))


async def test_capital_provider_overrides_static_capital() -> None:
    """A provider base supersedes the strategy's static ``capital`` for sizing.

    BTC weight ``0.5`` at price 50000 with a provider base of 200000 sizes to
    ``0.5 * 200000 / 50000 = 2`` (vs 1 against the static 100000).
    """
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("0.5")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        capital_provider=lambda: money("200000"),
    )
    await runner.rebalance(_frames())
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("2")  # type: ignore[union-attr]


async def test_fixed_policy_sizes_on_contributed_after_profit(tmp_path) -> None:  # noqa: ANN001
    """`fixed` sizes on C even after realised profit — the base never grows."""
    realised = {"v": money("0")}
    _store, provider = _cap_provider(tmp_path, "fixed", realised)
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("1")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        capital_provider=provider,
    )
    await runner.rebalance(_frames())  # base 100000 → BTC qty 2
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("2")  # type: ignore[union-attr]

    # A big realised profit does NOT move a fixed base → still target 2 → no leg.
    realised["v"] = money("50000")
    result = await runner.rebalance(_frames())
    assert result.submitted == 0
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("2")  # type: ignore[union-attr]


async def test_compound_policy_reinvests_and_shrinks(tmp_path) -> None:  # noqa: ANN001
    """`compound` sizes on C + R: a profit grows the base, a loss shrinks it."""
    realised = {"v": money("0")}
    _store, provider = _cap_provider(tmp_path, "compound", realised)
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("1")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        capital_provider=provider,
    )
    await runner.rebalance(_frames())  # base 100000 → BTC qty 2
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("2")  # type: ignore[union-attr]

    # A +50000 realised profit → compound base 150000 → target 3 → top up +1.
    realised["v"] = money("50000")
    result = await runner.rebalance(_frames())
    assert result.submitted == 1
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("3")  # type: ignore[union-attr]

    # A loss shrinks the base: -80000 → base 70000 → target 1.4 → sell down.
    realised["v"] = money("-30000")  # 100000 - 30000 = 70000
    await runner.rebalance(_frames())
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1.4")  # type: ignore[union-attr]


async def test_deposit_grows_next_rebalance_no_rebuild(tmp_path) -> None:  # noqa: ANN001
    """A recorded DEPOSIT grows the *next* rebalance's targets — same runner instance."""
    realised = {"v": money("0")}
    store, provider = _cap_provider(tmp_path, "fixed", realised)
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("1")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        capital_provider=provider,
    )
    await runner.rebalance(_frames())  # base 100000 → BTC qty 2
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("2")  # type: ignore[union-attr]

    # Record a +100000 deposit into the ledger — contributed jumps to 200000.
    store.record_capital_event(
        CapitalEvent("D1", "book", CapitalEventType.DEPOSIT, money("100000"), ts=10)
    )
    # The SAME runner instance sizes the next tick on 200000 → target 4 → +2.
    result = await runner.rebalance(_frames())
    assert result.submitted == 1
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("4")  # type: ignore[union-attr]


# --- venue-minimum order preparation (leaf 02: round-up-or-skip) ------------ #


class _FakeResolver:
    """A fake :class:`InstrumentSpecResolver`: canned specs, optional degraded set.

    Mirrors the resolver's contract — ``resolve`` never raises, an unknown
    symbol yields a bare instrument, ``degraded`` names exchanges whose fetch
    failed — while recording every ``(exchange, symbol)`` call.
    """

    def __init__(
        self,
        specs: Mapping[Symbol, Instrument] | None = None,
        *,
        degraded: set[str] | None = None,
    ) -> None:
        self._specs = dict(specs or {})
        self.degraded = degraded or set()
        self.calls: list[tuple[str, Symbol]] = []

    async def resolve(self, exchange: str, symbol: Symbol) -> Instrument:
        self.calls.append((exchange, symbol))
        return self._specs.get(symbol, Instrument(symbol))


def _capture_logs(bus: EventBus) -> list:
    """Subscribe to ``bus`` and collect every :class:`LogEvent` emitted."""
    from trading_bot.application.events import LogEvent

    events: list[LogEvent] = []

    def _handler(event) -> None:  # type: ignore[no-untyped-def]
        if isinstance(event, LogEvent):
            events.append(event)

    bus.subscribe(_handler)
    return events


#: Real-shaped venue specs: BTC on a 5-USDT notional minimum (Binance shape),
#: ETH on a plain min_qty.
_BTC_SPEC = Instrument(
    BTC, qty_precision=5, min_qty=money("0.00001"), min_notional=money("5")
)
_ETH_SPEC = Instrument(ETH, qty_precision=4, min_qty=money("0.0001"))


async def test_dust_leg_is_skipped_with_one_info_event() -> None:
    """A dust leg routes nothing and logs exactly one info line with the reason.

    BTC weight 0.00000002 sizes to 0.00000004 BTC (~0.002 USDT) — far below the
    5-USDT notional minimum: skipped. ETH (20 ETH) is unaffected.
    """
    weights = {BTC: money("0.00000002"), ETH: money("0.5")}
    router, tracker, bus, _broker = _engine()
    logs = _capture_logs(bus)
    resolver = _FakeResolver({BTC: _BTC_SPEC, ETH: _ETH_SPEC})
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=resolver,
        exchange="binance",
        min_order_ratio=money("0.5"),
    )

    result = await runner.rebalance(_frames())

    assert result.submitted == 1
    assert result.failed == 0
    assert set(router.tracked_orders()) == {"book-ETH/USDT-0"}  # no BTC order
    skips = [e for e in logs if "skipped" in e.message and "BTC/USDT" in e.message]
    assert len(skips) == 1
    assert skips[0].level == "info"
    assert resolver.calls and resolver.calls[0][0] == "binance"


async def test_close_to_min_leg_submits_bumped_qty() -> None:
    """A leg within min_order_ratio of the minimum submits the bumped quantity.

    BTC weight 0.00003 sizes to 0.00006 BTC at 50000 (3 USDT) — 60% of the
    5-USDT notional minimum (0.0001 BTC): rounded UP to exactly 0.0001.
    """
    weights = {BTC: money("0.00003")}
    router, tracker, bus, _broker = _engine()
    logs = _capture_logs(bus)
    resolver = _FakeResolver({BTC: _BTC_SPEC})
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=resolver,
        exchange="binance",
    )

    result = await runner.rebalance(_frames())

    assert result.submitted == 1
    order = router.tracked_orders()["book-BTC/USDT-0"]
    assert order.side is OrderSide.BUY
    assert order.qty == Decimal("0.00010")  # 5 / 50000, on the 1e-5 lot grid
    bumps = [e for e in logs if "rounded up" in e.message]
    assert len(bumps) == 1 and bumps[0].level == "info"
    # The broker-confirmed fill matches the bumped quantity (no sub-minimum
    # order ever reached the venue).
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("0.00010")  # type: ignore[union-attr]


async def test_degraded_resolver_warns_once_across_ticks() -> None:
    """A degraded venue fetch produces ONE warning per runner lifetime, not per tick."""
    weights = {BTC: money("0.5"), ETH: money("0.25")}
    router, tracker, bus, _broker = _engine()
    logs = _capture_logs(bus)
    resolver = _FakeResolver(degraded={"binance"})  # bare specs: permissive
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=resolver,
        exchange="binance",
    )

    await runner.rebalance(_frames())
    await runner.rebalance(_frames())

    warnings = [e for e in logs if e.level == "warning" and "degraded" in e.message]
    assert len(warnings) == 1
    # Permissive fallback: the legs themselves still routed (tick 1: 2 legs;
    # tick 2: on target -> 0).
    assert set(router.tracked_orders()) == {"book-BTC/USDT-0", "book-ETH/USDT-0"}


async def test_normal_legs_are_regression_unchanged_under_resolver() -> None:
    """Bare resolved specs leave a normal rebalance byte-identical to today.

    Same weights as ``test_one_rebalance_from_flat_routes_n_sized_orders``:
    with a resolver returning bare instruments (no minimums, no lot) the
    submitted quantities are the exact unquantized Decimals of the legacy path.
    """
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=_FakeResolver(),  # every symbol resolves bare
        exchange="binance",
    )

    result = await runner.rebalance(_frames())

    assert result.submitted == 2
    orders = router.tracked_orders()
    btc_order = orders["book-BTC/USDT-0"]
    eth_order = orders["book-ETH/USDT-0"]
    assert btc_order.side is OrderSide.BUY and btc_order.qty == Decimal("1")
    assert eth_order.side is OrderSide.SELL and eth_order.qty == Decimal("10")
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")  # type: ignore[union-attr]
    assert tracker.position(Instrument(ETH)).net_qty == Decimal("-10")  # type: ignore[union-attr]


async def test_sell_leg_capped_at_held_position_via_runner() -> None:
    """A long-reducing sell leg is capped at the held qty end to end.

    Tick 0 opens +1 BTC. Tick 1 targets a large short (-2 weight → target -4):
    the sell delta (-5) is capped at the held +1 — the venue-shaped spot cap —
    and the routed order sells exactly 1.
    """
    router, tracker, bus, _broker = _engine()
    resolver = _FakeResolver({BTC: _BTC_SPEC})
    open_long = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("0.5")})),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=resolver,
        exchange="binance",
    )
    await open_long.rebalance(_frames())
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("1")  # type: ignore[union-attr]

    flip_short = PortfolioRunner(
        _strategy(_weights_signal({BTC: money("-2")}), name="book2"),
        _ListFeed([], asof=1_701),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=resolver,
        exchange="binance",
    )
    result = await flip_short.rebalance(_frames())

    assert result.submitted == 1
    order = router.tracked_orders()["book2-BTC/USDT-0"]
    assert order.side is OrderSide.SELL
    assert order.qty == Decimal("1.00000")  # capped at the held +1, on the grid
    assert tracker.position(Instrument(BTC)).net_qty == Decimal("0")  # type: ignore[union-attr]


async def test_resolver_requires_exchange() -> None:
    """Constructing a runner with a resolver but no exchange fails loudly."""
    router, tracker, bus, _broker = _engine()
    try:
        PortfolioRunner(
            _strategy(_weights_signal({BTC: money("0.5")})),
            _ListFeed([], asof=1_700),
            router,
            tracker,
            event_bus=bus,
            spec_resolver=_FakeResolver(),
        )
    except ValueError as exc:
        assert "exchange" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("expected ValueError for a resolver with no exchange")


# --- mark cache publish ------------------------------------------------------ #


async def test_rebalance_publishes_marks_with_the_ticks_asof() -> None:
    """A rebalance publishes every traded symbol's close + this tick's asof.

    The engine's :class:`~trading_bot.application.mark_cache.MarkCache`, when
    threaded in, ends up holding exactly the ``prices`` the tick sized against
    (read via :meth:`PortfolioRunner._latest_closes`) keyed by symbol, each
    stamped with the same ``asof_ms`` :meth:`PortfolioRunner._derive_asof_ms`
    derives for the tick's signals — and ``source == "bar_close"`` (this
    runner never publishes a fill-derived mark).
    """
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, bus, _broker = _engine()
    mark_cache = MarkCache()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([_frames()], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        mark_cache=mark_cache,
    )

    await runner.rebalance(_frames())

    expected_asof = 1_700_000_000_000_000_000 // 1_000_000
    btc_mark = mark_cache.get(BTC)
    eth_mark = mark_cache.get(ETH)
    assert btc_mark is not None
    assert btc_mark.price == BTC_PRICE
    assert btc_mark.asof_ms == expected_asof
    assert btc_mark.source == "bar_close"
    assert eth_mark is not None
    assert eth_mark.price == ETH_PRICE
    assert eth_mark.asof_ms == expected_asof


async def test_second_rebalance_updates_marks_in_place() -> None:
    """A later rebalance's closes and asof overwrite the prior tick's marks."""
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, bus, _broker = _engine()
    mark_cache = MarkCache()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([_frames()], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        mark_cache=mark_cache,
    )
    await runner.rebalance(_frames())

    second_time_ns = 1_700_000_060_000_000_000
    second_frames = {
        BTC: _frame(51000.0, time_ns=second_time_ns),
        ETH: _frame(2600.0, time_ns=second_time_ns),
    }
    await runner.rebalance(second_frames)

    btc_mark = mark_cache.get(BTC)
    eth_mark = mark_cache.get(ETH)
    assert btc_mark is not None
    assert btc_mark.price == money("51000")
    assert btc_mark.asof_ms == second_time_ns // 1_000_000
    assert eth_mark is not None
    assert eth_mark.price == money("2600")
    assert eth_mark.asof_ms == second_time_ns // 1_000_000


async def test_no_mark_cache_is_a_legacy_noop() -> None:
    """Omitting ``mark_cache`` (the default) rebalances exactly as before."""
    weights = {BTC: money("0.5"), ETH: money("-0.25")}
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([_frames()], asof=1_700),
        router,
        tracker,
        event_bus=bus,
    )

    result = await runner.rebalance(_frames())

    assert result.submitted == 2
    assert result.failed == 0


# --- leaf 02: per-unit event-trail logging (module loggers) ----------------- #

_RUNNER_LOGGER = "trading_bot.application.portfolio_runner"


async def test_mixed_rebalance_logs_summary_skip_and_submit_lines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mixed rebalance logs the summary, the skip's reason, and the submit's cid.

    BTC (weight 1e-5 → 0.00002 BTC ≈ 1 USDT) is skipped below the 5-USDT notional
    minimum; ETH (weight 0.5) submits. The pinned INFO lines land through the module
    logger: the summary with correct counters (which sum to ``legs``), the skip line
    carrying the ``LegDecision.reason``, and the submit line naming the cid.
    """
    weights = {BTC: money("0.00001"), ETH: money("0.5")}
    router, tracker, bus, _broker = _engine()
    resolver = _FakeResolver({BTC: _BTC_SPEC, ETH: _ETH_SPEC})
    runner = PortfolioRunner(
        _strategy(_weights_signal(weights)),
        _ListFeed([], asof=1_700),
        router,
        tracker,
        event_bus=bus,
        spec_resolver=resolver,
        exchange="binance",
    )

    with caplog.at_level(logging.INFO, logger=_RUNNER_LOGGER):
        result = await runner.rebalance(_frames())

    assert result.submitted == 1
    msgs = [r.getMessage() for r in caplog.records if r.name == _RUNNER_LOGGER]

    # The one summary line, with the exact pinned counters.
    summary = [m for m in msgs if " rebalance asof=" in m]
    assert len(summary) == 1
    assert summary[0].startswith("unit=book rebalance asof=")
    m = re.search(
        r"legs=(\d+) submitted=(\d+) round_up=(\d+) skipped=(\d+) on_target=(\d+)",
        summary[0],
    )
    assert m is not None
    legs, sub, ru, sk, ot = (int(g) for g in m.groups())
    assert (sub, ru, sk, ot) == (1, 0, 1, 0)
    assert sub + ru + sk + ot == legs  # the four counters partition the universe

    # The skip line carries the LegDecision.reason (Decimals + the binding min).
    skips = [m for m in msgs if "leg BTC/USDT skip:" in m]
    assert len(skips) == 1
    assert "venue minimum" in skips[0]

    # The submit line names the leg's deterministic cid.
    submits = [m for m in msgs if m.startswith("unit=book order submitted")]
    assert len(submits) == 1
    assert "cid=book-ETH/USDT-0" in submits[0]


async def test_idle_gated_tick_adds_no_unit_info_lines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A freshness-gate-skipped tick adds zero unit INFO lines (leaf 02 anti-spam).

    The first tick evaluates (genesis) and logs its summary; a second tick over an
    unchanged store is skipped by the freshness gate before ``rebalance`` runs — so
    it must add **no** INFO line from the runner at all.
    """
    client = _counting_client([100.0], [50.0])  # a single common day only
    feed = _portfolio_feed_over(client)
    strat = _strategy(_weights_signal({BTC: money("0.5"), ETH: money("-0.25")}))
    router, tracker, bus, _broker = _engine()
    runner = PortfolioRunner(strat, feed, router, tracker, event_bus=bus)

    with caplog.at_level(logging.INFO, logger=_RUNNER_LOGGER):
        first = await runner.rebalance_latest()
        assert first is not None  # first-ever tick evaluates
        assert any(
            " rebalance asof=" in r.getMessage()
            for r in caplog.records
            if r.name == _RUNNER_LOGGER
        )

        caplog.clear()
        second = await runner.rebalance_latest()
        assert second is None  # the gate skipped this tick

    idle_info = [
        r
        for r in caplog.records
        if r.name == _RUNNER_LOGGER and r.levelno >= logging.INFO
    ]
    assert idle_info == []
