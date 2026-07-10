"""Tests for the :class:`~trading_bot.application.strategy_runner.StrategyRunner`.

These prove the runner's contract — the live loop ``feed → signal → delta →
order → router → broker → fill → tracker`` — end to end against the real
:class:`~trading_bot.brokers.paper.PaperBroker`, fully offline:

* **end-to-end**: a known OHLC series (trend up then down) driven through an
  ``InMemoryFeed`` + the MA-crossover strategy + ``OrderRouter`` → ``PaperBroker``
  + ``PositionTracker``; the tracked position goes long after the up-cross and
  flat/short after the down-cross, orders match the signal deltas, money exact
  ``Decimal`` and a hand-reasoned final position;
* **delta == 0**: a step already on target submits no order;
* **warmup**: no order before ``lookback`` bars (the strategy returns flat, so the
  delta is zero);
* **idempotent re-run**: running the same sequence twice (same per-step
  client-order-ids) does not double-submit — the broker sees one order per step;
* **causality**: a spy ``signal_fn`` records the max bar ``time`` it ever saw and
  asserts it never exceeds the current step's bar time (no lookahead).

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from decimal import Decimal

import polars as pl
import pytest

from trading_bot.application import (
    DccdFeed,
    EventBus,
    InMemoryFeed,
    LogEvent,
    OrderRouter,
    PositionTracker,
    Strategy,
    StrategyRunner,
    ma_crossover_signal,
)
from trading_bot.brokers import PaperBroker
from trading_bot.domain import (
    Instrument,
    OrderSide,
    OrderType,
    Signal,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))


# --- helpers --------------------------------------------------------------- #


def _bars(closes: list[float], *, start_ts: int = 1_000) -> pl.DataFrame:
    """A minimal OHLC(V) bars frame from a list of closes (time in seconds)."""
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


def _wire_feed(
    strategy: Strategy,
    feed: object,
    *,
    mark: str = "100",
) -> tuple[StrategyRunner, PaperBroker, PositionTracker, EventBus]:
    """Build a fully wired runner over a fresh broker/tracker/router stack.

    The broker fills MARKET orders at the injected ``mark`` price and emits a
    ``FillEvent`` per fill onto the bus the tracker subscribes to, so the loop
    closes (a step's fills update the *next* step's position). Takes any
    ``DataFeed``-shaped ``feed`` — :func:`_wire` is the common case (an
    ``InMemoryFeed`` over a fixed frame).
    """
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        prices={BTC_USD: money(mark)},
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={"USD": money("10000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)
    runner = StrategyRunner(strategy, feed, router, tracker, event_bus=bus)  # type: ignore[arg-type]
    return runner, broker, tracker, bus


def _wire(
    strategy: Strategy,
    frame: pl.DataFrame,
    *,
    mark: str = "100",
) -> tuple[StrategyRunner, PaperBroker, PositionTracker, EventBus]:
    """Build a fully wired runner over an ``InMemoryFeed`` of ``frame``."""
    return _wire_feed(strategy, InMemoryFeed(frame), mark=mark)


# --- end-to-end: trend up then down ---------------------------------------- #


async def test_end_to_end_position_follows_signal() -> None:
    """The tracked position follows the MA-crossover signal, long then short.

    A close series that trends up (so the fast MA crosses *above* the slow MA →
    long) then down (fast crosses *below* → short) is driven through the whole
    stack. With ``reference_qty = 2``, an EXPOSURE signal of ``+1`` targets long
    2 and ``-1`` targets short 2. We assert the tracked net_qty is long after the
    up-leg and short after the down-leg, with exact ``Decimal`` money.
    """
    pytest.importorskip("fynance")  # ma_crossover_signal evaluates fynance.sma
    # 20 up then 20 down — comfortably past a fast=3/slow=6 crossover each way.
    up = [float(100 + i) for i in range(20)]
    down = [float(120 - i) for i in range(1, 21)]
    frame = _bars(up + down)

    strat = Strategy(
        name="ma",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    runner, broker, tracker, _bus = _wire(strat, frame, mark="100")

    await runner.run()

    # After the full up-then-down series the latest signal is short (fast < slow
    # on the descending tail), so the target net position is -2.
    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == Decimal("-2")

    # Every fill the broker confirmed is for BTC/USD at the mark, exact Decimal.
    fills = await broker.fills()
    assert fills, "the run must have traded"
    for f in fills:
        assert f.instrument == BTC_USD
        assert f.price == Decimal("100")

    # The deltas net out to the final target exactly: sum of signed fill qtys
    # (BUY +, SELL -) equals net_qty.
    signed = sum((f.qty if f.side is OrderSide.BUY else -f.qty) for f in fills)
    assert signed == pos.net_qty


async def test_position_goes_long_then_short_across_the_cross() -> None:
    """Mid-run the position is long on the up-leg before flipping short.

    Drives only the up-leg first (a separate runner) to pin that the position is
    long *before* the down-cross — proving the track follows the signal at the
    crossover, not just at the end.
    """
    pytest.importorskip("fynance")  # ma_crossover_signal evaluates fynance.sma
    up = [float(100 + i) for i in range(20)]
    frame_up = _bars(up)
    strat = Strategy(
        name="ma",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    runner, _broker, tracker, _bus = _wire(strat, frame_up, mark="100")
    await runner.run()

    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == Decimal("2")  # long after the up-cross


# --- delta == 0: no order -------------------------------------------------- #


async def test_no_order_when_already_on_target() -> None:
    """A step whose signal target equals the current position submits no order.

    A constant long-exposure signal first buys to the target, then — on a second
    identical window — is already on target (delta 0) and submits nothing.
    """

    def _always_long(bars: pl.DataFrame) -> Signal:
        return Signal.exposure(BTC_USD, money("1"), ts=0)

    strat = Strategy(
        name="hold",
        instrument=BTC_USD,
        signal_fn=_always_long,
        reference_qty=money("3"),
    )
    # Two identical bars: step 0 buys to long 3; step 1 is already on target.
    frame = _bars([100.0, 100.0])
    runner, broker, tracker, _bus = _wire(strat, frame, mark="100")

    n = await runner.run()
    assert n == 1  # only the first step traded
    assert len(await broker.fills()) == 1
    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == Decimal("3")


async def test_run_respects_max_steps() -> None:
    """``run(max_steps=k)`` processes at most ``k`` windows and stops."""

    def _always_long(bars: pl.DataFrame) -> Signal:
        return Signal.exposure(BTC_USD, money("1"), ts=0)

    strat = Strategy(
        name="cap",
        instrument=BTC_USD,
        signal_fn=_always_long,
        reference_qty=money("1"),
    )
    # 10 bars available, but cap at 1 window: only the first step runs.
    frame = _bars([100.0] * 10)
    runner, broker, _tracker, _bus = _wire(strat, frame, mark="100")

    n = await runner.run(max_steps=1)
    assert n == 1  # one order on the first (and only processed) step
    assert runner.step_index == 1  # only one window consumed
    assert len(await broker.fills()) == 1


async def test_step_returns_none_on_zero_delta() -> None:
    """``step`` returns ``None`` (no order) when the delta is zero, but advances."""

    def _flat(bars: pl.DataFrame) -> Signal:
        return Signal.exposure(BTC_USD, money("0"), ts=0)

    strat = Strategy(
        name="flat", instrument=BTC_USD, signal_fn=_flat, reference_qty=money("1")
    )
    runner, broker, _tracker, _bus = _wire(strat, _bars([100.0]), mark="100")

    order = await runner.step(_bars([100.0]))
    assert order is None
    assert await broker.fills() == []
    # The index still advanced (a no-order step consumes its slot).
    assert runner.step_index == 1


# --- cooperative stop ------------------------------------------------------ #


async def test_run_stops_when_stop_event_already_set() -> None:
    """``run(stop_event=...)`` set before the first step submits nothing.

    The cooperative stop is checked at the top of each iteration, *before* the
    step — so an already-set event means the loop exits immediately with zero
    orders, never tearing a submission mid-flight.
    """

    def _always_long(bars: pl.DataFrame) -> Signal:
        return Signal.exposure(BTC_USD, money("1"), ts=0)

    strat = Strategy(
        name="stop",
        instrument=BTC_USD,
        signal_fn=_always_long,
        reference_qty=money("1"),
    )
    runner, broker, _tracker, _bus = _wire(strat, _bars([100.0] * 5), mark="100")

    stop = asyncio.Event()
    stop.set()
    n = await runner.run(stop_event=stop)

    assert n == 0
    assert await broker.fills() == []
    assert runner.step_index == 0  # not even one window consumed


async def test_run_stops_between_steps_on_stop_event() -> None:
    """A stop set mid-feed halts the loop *between* steps (no partial submit).

    A spy feed sets the stop event after the first window is consumed; the loop
    must process exactly that first window and then exit at the next
    between-steps check — leaving exactly one fill and a consistent position.
    """
    frame = _bars([100.0] * 5)
    stop = asyncio.Event()

    class _StopAfterFirst:
        """Yields the causal windows but sets ``stop`` after the first one."""

        def __init__(self, inner: InMemoryFeed) -> None:
            self._inner = inner
            self.seen = 0

        def __iter__(self) -> Iterator[pl.DataFrame]:
            for window in self._inner:
                self.seen += 1
                yield window
                if self.seen == 1:
                    stop.set()  # request stop right after step 0 finished

        def latest(self) -> pl.DataFrame:
            return self._inner.latest()

    def _always_long(bars: pl.DataFrame) -> Signal:
        return Signal.exposure(BTC_USD, money("1"), ts=0)

    strat = Strategy(
        name="midstop",
        instrument=BTC_USD,
        signal_fn=_always_long,
        reference_qty=money("1"),
    )
    feed = _StopAfterFirst(InMemoryFeed(frame))
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        prices={BTC_USD: money("100")},
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={"USD": money("10000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)
    runner = StrategyRunner(strat, feed, router, tracker, event_bus=bus)  # type: ignore[arg-type]

    n = await runner.run(stop_event=stop)

    # Exactly one step ran (step 0 bought to long 1); the stop took effect at
    # the next between-steps check, so no further window was processed.
    assert n == 1
    assert runner.step_index == 1
    fills = await broker.fills()
    assert len(fills) == 1
    pos = tracker.position(BTC_USD)
    assert pos is not None and pos.net_qty == Decimal("1")


# --- warmup ---------------------------------------------------------------- #


async def test_no_orders_during_warmup() -> None:
    """No order is submitted before ``lookback`` bars are present.

    With ``lookback = 6`` the strategy returns a flat signal for the first 5
    windows (heights 1..5); against a flat position that is delta 0, so the
    broker sees no order until at least 6 bars exist — and even then only if the
    signal is non-flat. We use a flat-after-warmup close series to isolate warmup:
    no order ever, because the signal is flat throughout, *especially* below
    lookback.
    """
    # Closes that never cross (monotone) but we set lookback high to assert the
    # warmup window submits nothing.
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]  # only 5 bars, < lookback 6
    strat = Strategy(
        name="warm",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=2, slow=4),
        reference_qty=money("1"),
        lookback=6,
    )
    runner, broker, tracker, _bus = _wire(strat, _bars(closes), mark="100")

    n = await runner.run()
    assert n == 0  # every window is in warmup -> flat -> delta 0 -> no order
    assert await broker.fills() == []
    assert tracker.position(BTC_USD) is None  # never traded -> no fill -> None


# --- idempotent re-run ----------------------------------------------------- #


async def test_idempotent_rerun_does_not_double_submit() -> None:
    """Re-running the same steps (same client-order-ids) does not double-submit.

    The runner's deterministic ``f"{name}-{step}"`` ids make a *re-run* a no-op at
    the router. We drive the run once (capturing each order the runner submitted
    via the router's ``OrderEvent``\\ s), then **replay those exact same orders**
    through the *same* router and assert the broker placed **no new fills** — the
    router deduped every id. This is the runner-half (stable ids) meeting the
    router-half (one id → one venue order) of the E4 idempotency contract.
    """
    pytest.importorskip("fynance")  # ma_crossover_signal evaluates fynance.sma
    up = [float(100 + i) for i in range(20)]
    down = [float(120 - i) for i in range(1, 21)]
    frame = _bars(up + down)
    strat = Strategy(
        name="ma",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )

    bus = EventBus()
    broker = PaperBroker(
        prices={BTC_USD: money("100")},
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={"USD": money("10000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)
    tracker = PositionTracker(event_bus=bus)

    # Capture every order the runner submits (the router emits one OrderEvent per
    # tracked order).
    from trading_bot.application import OrderEvent

    submitted_orders: list = []
    bus.subscribe(
        lambda e: (
            submitted_orders.append(e.order) if isinstance(e, OrderEvent) else None
        )
    )

    runner = StrategyRunner(strat, InMemoryFeed(frame), router, tracker)
    n1 = await runner.run()
    fills_after_1 = len(await broker.fills())
    tracked_after_1 = set(router.tracked_orders())

    assert n1 > 0  # the run traded
    assert fills_after_1 == n1  # one immediate fill per submitted order
    # Every submitted id follows the per-step scheme.
    for order in submitted_orders:
        prefix, _, idx = order.client_order_id.rpartition("-")
        assert prefix == "ma" and idx.isdigit()

    # Replay the very same orders (same client-order-ids) through the same router.
    for order in list(submitted_orders):
        again = await router.submit(order)
        # Returns the already-tracked order, never re-submits.
        assert again is order

    fills_after_2 = len(await broker.fills())
    tracked_after_2 = set(router.tracked_orders())

    # No new fills, and the tracked id set is identical — fully deduped.
    assert fills_after_2 == fills_after_1
    assert tracked_after_2 == tracked_after_1


async def test_client_order_ids_are_deterministic_and_per_step() -> None:
    """Each traded step gets ``f"{name}-{step}"``; ids follow the scheme.

    Two fresh runners over the same feed (separate stacks) produce the *same* set
    of per-step ids — determinism — and every id matches ``f"{name}-{step}"``
    with the step index aligned to the bar it traded on.
    """
    pytest.importorskip("fynance")  # ma_crossover_signal evaluates fynance.sma
    up = [float(100 + i) for i in range(12)]
    frame = _bars(up)

    def _build() -> tuple[StrategyRunner, OrderRouter]:
        strat = Strategy(
            name="ids",
            instrument=BTC_USD,
            signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
            reference_qty=money("2"),
            lookback=6,
        )
        bus = EventBus()
        tracker = PositionTracker(event_bus=bus)
        broker = PaperBroker(
            prices={BTC_USD: money("100")},
            fee_bps=money("0"),
            starting_balances={"USD": money("10000000"), "BTC": money("0")},
            event_bus=bus,
        )
        router = OrderRouter(broker, bus)
        runner = StrategyRunner(strat, InMemoryFeed(frame), router, tracker)
        return runner, router

    runner_a, router_a = _build()
    await runner_a.run()
    cids_a = set(router_a.tracked_orders())

    runner_b, router_b = _build()
    await runner_b.run()
    cids_b = set(router_b.tracked_orders())

    assert cids_a, "at least one trade expected"
    assert cids_a == cids_b  # deterministic across fresh runners
    for cid in cids_a:
        prefix, _, idx = cid.rpartition("-")
        assert prefix == "ids"
        assert idx.isdigit()


# --- causality: the signal never sees a future bar ------------------------- #


async def test_causality_signal_never_sees_future_bar() -> None:
    """A spy ``signal_fn`` records the max bar time it saw; never beyond step t.

    At step ``t`` the feed hands the runner the causal prefix ``frame[: t + 1]``,
    whose last ``time`` is bar ``t``'s. The spy captures, on each call, the max
    ``time`` in the window and the window height; we assert the window's last
    time equals the step's bar time and never a later one — no lookahead.
    """
    closes = [100.0, 101.0, 99.0, 102.0, 98.0, 103.0, 97.0, 104.0]
    frame = _bars(closes)
    all_times: list[int] = frame["time"].to_list()

    seen_max_time: list[int] = []
    seen_heights: list[int] = []

    def _spy(bars: pl.DataFrame) -> Signal:
        seen_max_time.append(int(bars["time"].max()))  # type: ignore[arg-type]
        seen_heights.append(bars.height)
        # A trivial flat signal — we only care what bars it saw.
        return Signal.exposure(BTC_USD, money("0"), ts=0)

    strat = Strategy(
        name="spy", instrument=BTC_USD, signal_fn=_spy, reference_qty=money("1")
    )
    runner, _broker, _tracker, _bus = _wire(strat, frame, mark="100")
    await runner.run()

    # One evaluation per bar, windows growing 1..N.
    assert seen_heights == list(range(1, len(closes) + 1))
    # At each step t, the max time seen is exactly bar t's time — never a later
    # bar's. (Strict no-lookahead: the window's last time == the step's time.)
    for t, max_time in enumerate(seen_max_time):
        assert max_time == all_times[t]
        # And it never exceeds the current step's bar time.
        assert max_time <= all_times[t]


# --- order shape & event trace --------------------------------------------- #


async def test_order_factory_and_log_events() -> None:
    """A custom ``order_factory`` is used (LIMIT), and a LogEvent traces a trade.

    The factory returns a LIMIT order priced off the window's close; the runner
    overrides its client-order-id with the deterministic per-step id. A LogEvent
    is emitted per submitted order on the bus.
    """
    pytest.importorskip("fynance")  # ma_crossover_signal evaluates fynance.sma
    logs: list[LogEvent] = []

    closes = [float(100 + i) for i in range(12)]
    frame = _bars(closes)

    def _limit_factory(strategy: Strategy, delta, bars: pl.DataFrame):
        from trading_bot.domain import Order

        close = money(str(bars["c"][-1]))
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        return Order(
            client_order_id="will-be-overridden",
            instrument=strategy.instrument,
            side=side,
            qty=abs(delta),
            type=OrderType.LIMIT,
            limit_price=close,
        )

    strat = Strategy(
        name="lim",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    bus = EventBus()
    bus.subscribe(lambda e: logs.append(e) if isinstance(e, LogEvent) else None)
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={"USD": money("10000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)
    runner = StrategyRunner(
        strat,
        InMemoryFeed(frame),
        router,
        tracker,
        event_bus=bus,
        order_factory=_limit_factory,
    )
    n = await runner.run()

    assert n >= 1
    # One LogEvent per submitted order.
    assert len(logs) == n
    # Every tracked order's id follows the runner's scheme, not the factory's.
    for cid in router.tracked_orders():
        assert cid.startswith("lim-")
    # The orders were LIMITs (from the factory), filled at the close.
    fills = await broker.fills()
    assert fills


# --- step_latest: single re-evaluation over the latest data (daemon hook) --- #


async def test_step_latest_evaluates_latest_window_and_is_idempotent() -> None:
    """`step_latest` steps over `feed.latest()`; a repeat on unchanged data no-ops.

    The scheduler-driven daemon hook: one re-evaluation over the freshest data
    trades to the latest target, and calling it again over the same data submits
    nothing (already on target) — idempotent under repetition.
    """
    pytest.importorskip("fynance")  # ma_crossover_signal evaluates fynance.sma
    up = [float(100 + i) for i in range(20)]
    down = [float(120 - i) for i in range(1, 21)]
    frame = _bars(up + down)
    strat = Strategy(
        name="ma",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    runner, _broker, tracker, _bus = _wire(strat, frame, mark="100")

    first = await runner.step_latest()  # from flat → trades to the latest target
    assert first is not None
    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == Decimal("-2")  # latest signal is short on the down tail

    second = await runner.step_latest()  # already on target → no order
    assert second is None
    again = tracker.position(BTC_USD)
    assert again is not None
    assert again.net_qty == Decimal("-2")  # unchanged


# --- step_latest: the idle-tick freshness gate ------------------------------ #


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


class _CountingDccdClient:
    """A fake dccd client counting **full** (``start_ns=None``) vs **tail**
    (``start_ns`` given) reads, so a test can assert the freshness gate never
    triggers a second full read when nothing new is available.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame
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
        frame = self._frame
        if start_ns is not None:
            frame = frame.filter(pl.col("TS") >= start_ns)
        if end_ns is not None:
            frame = frame.filter(pl.col("TS") <= end_ns)
        return frame


def _always_flat(bars: pl.DataFrame) -> Signal:
    return Signal.exposure(BTC_USD, money("0"), ts=0)


async def test_step_latest_gate_skips_full_read_when_no_new_bar() -> None:
    """A second tick over unchanged data skips the full read and the signal.

    The first-ever tick has no baseline, so it always runs the full path. A
    second tick where the tail probe finds nothing newer than that baseline
    must return ``None`` **without** a second full read and **without**
    invoking ``signal_fn``.
    """
    span = 86_400
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([100.0], start_ns=0, span_ns=span_ns)  # a single closed day
    client = _CountingDccdClient(raw)
    feed = DccdFeed(client, "kraken", "BTC/USD", span)

    calls: list[int] = []

    def _spy(bars: pl.DataFrame) -> Signal:
        calls.append(bars.height)
        return Signal.exposure(BTC_USD, money("1"), ts=0)

    strat = Strategy(
        name="gate", instrument=BTC_USD, signal_fn=_spy, reference_qty=money("1")
    )
    runner, _broker, _tracker, _bus = _wire_feed(strat, feed, mark="100")

    first = await runner.step_latest()
    assert first is not None  # first-ever tick: no baseline, always full
    assert client.full_reads == 1
    assert client.tail_reads == 0
    assert len(calls) == 1
    assert runner.last_asof_ms == 0  # the single day's asof (day 0)
    assert runner.last_eval_ms is not None

    second = await runner.step_latest()
    assert second is None  # nothing new -> the gate skips
    assert client.full_reads == 1  # NOT read again
    assert client.tail_reads == 1  # exactly one cheap probe
    assert len(calls) == 1  # the signal was never invoked on the skipped tick


async def test_step_latest_gate_falls_through_when_a_new_bar_appears() -> None:
    """A tail probe that finds a newer bar runs the full path exactly once more."""
    span = 86_400
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([100.0], start_ns=0, span_ns=span_ns)
    client = _CountingDccdClient(raw)
    feed = DccdFeed(client, "kraken", "BTC/USD", span)
    strat = Strategy(
        name="gate2",
        instrument=BTC_USD,
        signal_fn=_always_flat,
        reference_qty=money("1"),
    )
    runner, _broker, _tracker, _bus = _wire_feed(strat, feed, mark="100")

    await runner.step_latest()
    assert client.full_reads == 1
    assert runner.last_asof_ms == 0

    # A new day lands in the store.
    client._frame = _dccd_ohlc([100.0, 101.0], start_ns=0, span_ns=span_ns)

    await runner.step_latest()
    assert client.full_reads == 2  # the full path ran exactly once more
    assert client.tail_reads == 1  # one probe, which saw the advance
    assert runner.last_asof_ms == span_ns // 1_000_000  # day 1's asof


async def test_step_latest_gate_falls_through_on_tail_probe_error() -> None:
    """A tail-read failure is conservative: fall through to the full path."""
    span = 86_400
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([100.0], start_ns=0, span_ns=span_ns)
    client = _CountingDccdClient(raw)
    feed = DccdFeed(client, "kraken", "BTC/USD", span)
    strat = Strategy(
        name="gate3",
        instrument=BTC_USD,
        signal_fn=_always_flat,
        reference_qty=money("1"),
    )
    runner, _broker, _tracker, _bus = _wire_feed(strat, feed, mark="100")

    await runner.step_latest()
    assert client.full_reads == 1

    client.raise_on_tail = True
    result = await runner.step_latest()

    assert result is None  # nothing traded (flat signal), but no exception raised
    assert client.tail_reads == 1
    assert client.full_reads == 2  # correctness over speed: still ran the full path


async def test_step_latest_last_asof_and_last_eval_update_on_skip_and_full() -> None:
    """``last_asof_ms``/``last_eval_ms`` update correctly on both paths."""
    span = 86_400
    span_ns = span * 1_000_000_000
    raw = _dccd_ohlc([100.0], start_ns=0, span_ns=span_ns)
    client = _CountingDccdClient(raw)
    feed = DccdFeed(client, "kraken", "BTC/USD", span)
    strat = Strategy(
        name="gate4",
        instrument=BTC_USD,
        signal_fn=_always_flat,
        reference_qty=money("1"),
    )
    runner, _broker, _tracker, _bus = _wire_feed(strat, feed, mark="100")

    assert runner.last_asof_ms is None
    assert runner.last_eval_ms is None

    await runner.step_latest()
    asof_after_full = runner.last_asof_ms
    eval_after_full = runner.last_eval_ms
    assert asof_after_full == 0
    assert eval_after_full is not None

    await runner.step_latest()  # the skip path (nothing new)
    assert runner.last_asof_ms == asof_after_full  # unchanged on a skip
    assert runner.last_eval_ms is not None
    assert runner.last_eval_ms >= eval_after_full  # still updated on a skip


async def test_step_latest_offloads_the_full_read_off_the_event_loop() -> None:
    """A slow, synchronous `latest()` does not block a concurrent coroutine.

    Proves the `asyncio.to_thread` offload actually parallelises: while the
    feed's blocking read sleeps, a concurrent loop gets many chances to run —
    it would not if the read ran synchronously on the event loop.
    """

    class _SlowFeed:
        def latest(self) -> pl.DataFrame:
            time.sleep(0.15)  # a slow synchronous "dccd read"
            return _bars([100.0])

    strat = Strategy(
        name="slow",
        instrument=BTC_USD,
        signal_fn=_always_flat,
        reference_qty=money("1"),
    )
    runner, _broker, _tracker, _bus = _wire_feed(strat, _SlowFeed(), mark="100")

    ticks = 0
    stop = False

    async def _concurrent_loop() -> None:
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(_concurrent_loop())
    await runner.step_latest()
    stop = True
    ticker.cancel()

    # The concurrent loop must have gotten several chances to run while the
    # slow synchronous read was in flight on its own thread.
    assert ticks >= 5


# --- capital_provider: reference_qty = sizing_base / close ------------------ #


def _always_long(bars: pl.DataFrame) -> Signal:
    """A constant +1 (fully long) exposure signal for BTC/USD."""
    return Signal.exposure(BTC_USD, money("1"), ts=0)


def _wire_provider(
    strategy: Strategy,
    frame: pl.DataFrame,
    provider: object,
    *,
    mark: str = "100",
) -> tuple[StrategyRunner, PaperBroker, PositionTracker]:
    """A wired runner whose sizing is driven by ``provider`` (a lazy base fn)."""
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        prices={BTC_USD: money(mark)},
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={"USD": money("10000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)
    runner = StrategyRunner(
        strategy,
        InMemoryFeed(frame),
        router,
        tracker,
        event_bus=bus,
        capital_provider=provider,  # type: ignore[arg-type]
    )
    return runner, broker, tracker


async def test_capital_provider_sizes_reference_qty_from_base() -> None:
    """A step sizes ``reference_qty = base / close`` — target = exposure * that.

    With base 100 at close 100, ``reference_qty = 1`` so a +1 exposure targets
    long 1 from flat → an order for exactly 1 (money exact ``Decimal``).
    """
    strat = Strategy(name="cap", instrument=BTC_USD, signal_fn=_always_long)
    frame = _bars([100.0])
    runner, _broker, tracker = _wire_provider(strat, frame, lambda: money("100"))

    order = await runner.step(frame)
    assert order is not None
    assert order.qty == Decimal("1")
    pos = tracker.position(BTC_USD)
    assert pos is not None and pos.net_qty == Decimal("1")


async def test_capital_provider_reference_qty_tracks_close() -> None:
    """The base translates to base units at the bar's close: qty = base / close.

    Base 150 at close 30000 → ``reference_qty = 0.005`` → long 0.005, exact.
    """
    strat = Strategy(name="cap", instrument=BTC_USD, signal_fn=_always_long)
    frame = _bars([30000.0])
    runner, _broker, _tracker = _wire_provider(
        strat, frame, lambda: money("150"), mark="30000"
    )
    order = await runner.step(frame)
    assert order is not None
    assert order.qty == Decimal("0.005")  # 150 / 30000, exact


async def test_capital_provider_deposit_grows_next_step_no_rebuild() -> None:
    """A base bump (a deposit) grows the *next* step's target on the same runner.

    Step 1 (base 100) targets long 1; step 2 (base unchanged) is on target (no
    order); after lifting the base to 200, step 3 tops up to long 2 — all on the
    **same runner instance** (the lazy provider is re-read every step, no rebuild).
    """
    base = {"v": money("100")}
    strat = Strategy(name="cap", instrument=BTC_USD, signal_fn=_always_long)
    frame = _bars([100.0])
    runner, _broker, tracker = _wire_provider(strat, frame, lambda: base["v"])

    order1 = await runner.step(frame)
    assert order1 is not None and order1.qty == Decimal("1")
    assert tracker.position(BTC_USD).net_qty == Decimal("1")  # type: ignore[union-attr]

    # Same base → already on target → no order.
    assert await runner.step(frame) is None

    # A "deposit": lift the base — the very next step tops the position up.
    base["v"] = money("200")
    order3 = await runner.step(frame)
    assert order3 is not None and order3.qty == Decimal("1")  # 2 target - 1 held
    assert tracker.position(BTC_USD).net_qty == Decimal("2")  # type: ignore[union-attr]


async def test_no_provider_uses_static_reference_qty_unchanged() -> None:
    """With no provider the runner sizes on the strategy's static reference_qty.

    Byte-identical to the pre-ledger behaviour: a +1 exposure with a static
    ``reference_qty = 3`` targets long 3 regardless of the close.
    """
    strat = Strategy(
        name="static",
        instrument=BTC_USD,
        signal_fn=_always_long,
        reference_qty=money("3"),
    )
    frame = _bars([100.0])
    runner, _broker, tracker, _bus = _wire(strat, frame, mark="100")
    order = await runner.step(frame)
    assert order is not None and order.qty == Decimal("3")
    assert tracker.position(BTC_USD).net_qty == Decimal("3")  # type: ignore[union-attr]
