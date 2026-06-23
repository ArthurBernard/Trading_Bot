"""Tests for the :class:`~trading_bot.application.orchestrator.Orchestrator`.

These prove the orchestrator's contract — run many ``StrategyRunner`` loops
concurrently and stop them all *cleanly* — fully offline against the real
:class:`~trading_bot.brokers.paper.PaperBroker`:

* **concurrent run**: two runners over two finite ``InMemoryFeed``\\ s run
  concurrently via :meth:`Orchestrator.run`; both complete and their
  positions/orders are independent;
* **graceful shutdown**: a long/looping run + :meth:`Orchestrator.shutdown`
  (setting the shared ``stop_event``) ends promptly with no exception, stopping
  *between* steps so no order is left half-submitted and state stays consistent;
* **per-runner failure**: a runner that raises does not leave its siblings hung —
  the sibling still completes and the error is surfaced (re-raised);
  multiple failures aggregate into a :class:`RunnerGroupError`;
* **signal handling**: calling :meth:`Orchestrator.install_signal_handlers` with a
  fake loop registers a handler that triggers :meth:`shutdown` — no real SIGINT.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Iterator
from decimal import Decimal

import polars as pl
import pytest

from trading_bot.application import (
    EventBus,
    InMemoryFeed,
    LogEvent,
    Orchestrator,
    OrderRouter,
    PositionTracker,
    RunnerGroupError,
    Strategy,
    StrategyRunner,
    ma_crossover_signal,
)
from trading_bot.brokers import PaperBroker
from trading_bot.domain import Instrument, OrderSide, Signal, Symbol, money

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


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


def _wire(
    strategy: Strategy,
    feed: object,
    instrument: Instrument,
    *,
    mark: str = "100",
) -> tuple[StrategyRunner, PaperBroker, PositionTracker]:
    """Build a fully wired runner over its own broker/tracker/router stack.

    Each runner gets its **own** bus/broker/tracker so the two strategies are
    fully independent (one strategy's fills never touch the other's position).
    """
    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    broker = PaperBroker(
        prices={instrument: money(mark)},
        fee_bps=money("0"),
        fill_model="immediate",
        starting_balances={"USD": money("10000000"), "BTC": money("0"),
                           "ETH": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)
    runner = StrategyRunner(strategy, feed, router, tracker, event_bus=bus)  # type: ignore[arg-type]
    return runner, broker, tracker


class _LoopingFeed:
    """An *infinite* causal feed that re-yields its frame, yielding control.

    Models a never-ending live feed for the shutdown test: each iteration yields
    the same constant window (and the strategy always targets long, so the first
    step trades and every later step is already on target → no order). It awaits
    a no-op ``asyncio.sleep(0)`` between windows so the event loop can run the
    shutdown coroutine — i.e. it *yields control between steps*, the boundary the
    cooperative stop relies on.

    It also counts how many windows it produced (``produced``), so a test can
    assert the run stopped promptly (a bounded number of steps), not run forever.
    """

    def __init__(self, frame: pl.DataFrame) -> None:
        self._frame = frame
        self.produced = 0

    def __iter__(self) -> Iterator[pl.DataFrame]:
        while True:
            self.produced += 1
            yield self._frame

    def latest(self) -> pl.DataFrame:
        return self._frame


def _always_long(instrument: Instrument) -> object:
    """A signal_fn that always targets a full long exposure on ``instrument``."""

    def _fn(bars: pl.DataFrame) -> Signal:
        return Signal.exposure(instrument, money("1"), ts=0)

    return _fn


# --- concurrent run -------------------------------------------------------- #


async def test_two_runners_run_concurrently_and_independently() -> None:
    """Two runners over two finite feeds both complete; state is independent.

    A BTC strategy (trends up → long) and an ETH strategy (trends down → short)
    run concurrently via :meth:`Orchestrator.run`. Both reach completion, the
    result maps each runner to its order count, and each tracker reflects only
    its own strategy's fills (independent books).
    """
    up = [float(100 + i) for i in range(20)]
    down = [float(120 - i) for i in range(1, 21)]

    strat_btc = Strategy(
        name="btc",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    strat_eth = Strategy(
        name="eth",
        instrument=ETH_USD,
        signal_fn=ma_crossover_signal(ETH_USD, fast=3, slow=6),
        reference_qty=money("3"),
        lookback=6,
    )
    runner_btc, broker_btc, tracker_btc = _wire(
        strat_btc, InMemoryFeed(_bars(up)), BTC_USD
    )
    runner_eth, broker_eth, tracker_eth = _wire(
        strat_eth, InMemoryFeed(_bars(down)), ETH_USD
    )

    orch = Orchestrator()
    orch.add(runner_btc)
    orch.add(runner_eth)

    results = await orch.run()

    # Both runners completed and are in the result map with their order counts.
    assert set(results) == {runner_btc, runner_eth}
    assert results[runner_btc] > 0
    assert results[runner_eth] > 0

    # BTC trended up -> long 2; ETH trended down -> short 3. Independent books.
    pos_btc = tracker_btc.position(BTC_USD)
    pos_eth = tracker_eth.position(ETH_USD)
    assert pos_btc is not None and pos_btc.net_qty == Decimal("2")
    assert pos_eth is not None and pos_eth.net_qty == Decimal("-3")

    # Each broker only saw its own instrument's fills (no cross-contamination).
    for f in await broker_btc.fills():
        assert f.instrument == BTC_USD
    for f in await broker_eth.fills():
        assert f.instrument == ETH_USD
    # And neither tracker knows the other's instrument.
    assert tracker_btc.position(ETH_USD) is None
    assert tracker_eth.position(BTC_USD) is None


async def test_run_with_no_runners_returns_empty() -> None:
    """:meth:`run` with no registered runners is a no-op returning ``{}``."""
    orch = Orchestrator()
    assert await orch.run() == {}


# --- graceful shutdown ----------------------------------------------------- #


async def test_shutdown_stops_a_looping_run_cleanly() -> None:
    """A looping run ends promptly on :meth:`shutdown`, between steps, no error.

    A runner over an infinite ``_LoopingFeed`` always targets long: it trades
    once (step 0) then is on target forever (no further orders). We launch the
    orchestrator, let it spin a few steps, then call :meth:`shutdown`; the run
    must return without raising, the stop must land *between* steps (so the
    single fill is intact — no half-submitted order), and the loop must not run
    unboundedly.
    """
    feed = _LoopingFeed(_bars([100.0]))
    strat = Strategy(
        name="loop",
        instrument=BTC_USD,
        signal_fn=_always_long(BTC_USD),  # type: ignore[arg-type]
        reference_qty=money("1"),
    )
    runner, broker, tracker = _wire(strat, feed, BTC_USD)

    orch = Orchestrator()
    orch.add(runner)

    run_task = asyncio.create_task(orch.run())
    # Let the loop spin through several windows (it yields control each step).
    for _ in range(20):
        await asyncio.sleep(0)
    assert not run_task.done(), "the looping run should still be going"

    await orch.shutdown()
    results = await asyncio.wait_for(run_task, timeout=2.0)  # ends promptly

    # No exception; the runner is in the results with exactly one order (it
    # bought to long 1 on step 0 and was on target every step after).
    assert results[runner] == 1
    # The stop landed between steps: exactly one fill, position consistent.
    fills = await broker.fills()
    assert len(fills) == 1
    assert fills[0].side is OrderSide.BUY
    pos = tracker.position(BTC_USD)
    assert pos is not None and pos.net_qty == Decimal("1")
    # The loop did not run forever — it stopped after a bounded number of steps.
    assert feed.produced > 0


async def test_shutdown_before_run_makes_run_a_quick_drain() -> None:
    """Setting the stop event before :meth:`run` stops each runner immediately.

    The runner checks the stop event at the top of its first iteration, so an
    already-set event means it submits nothing and returns 0 at once — proving
    the stop is observed *before* a step, never mid-submit.
    """
    feed = _LoopingFeed(_bars([100.0]))
    strat = Strategy(
        name="pre",
        instrument=BTC_USD,
        signal_fn=_always_long(BTC_USD),  # type: ignore[arg-type]
        reference_qty=money("1"),
    )
    runner, broker, _tracker = _wire(strat, feed, BTC_USD)

    orch = Orchestrator()
    orch.add(runner)
    await orch.shutdown()  # request stop before running

    results = await asyncio.wait_for(orch.run(), timeout=2.0)
    assert results[runner] == 0
    assert await broker.fills() == []  # nothing submitted


# --- per-runner failure ---------------------------------------------------- #


async def test_one_runner_raising_does_not_hang_siblings() -> None:
    """A runner that raises is surfaced; its sibling still completes.

    One runner's feed raises mid-iteration (a feed/broker fault); the other is a
    normal finite feed. With gather(return_exceptions=True) the healthy sibling
    runs to completion, and the orchestrator re-raises the lone failure (not a
    group error). The sibling is never left hung.
    """

    class _BoomFeed:
        def __iter__(self) -> Iterator[pl.DataFrame]:
            yield _bars([100.0])
            raise RuntimeError("feed exploded")

        def latest(self) -> pl.DataFrame:
            return _bars([100.0])

    up = [float(100 + i) for i in range(20)]
    good_strat = Strategy(
        name="good",
        instrument=ETH_USD,
        signal_fn=ma_crossover_signal(ETH_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    bad_strat = Strategy(
        name="bad",
        instrument=BTC_USD,
        signal_fn=_always_long(BTC_USD),  # type: ignore[arg-type]
        reference_qty=money("1"),
    )
    good_runner, _gb, good_tracker = _wire(
        good_strat, InMemoryFeed(_bars(up)), ETH_USD
    )
    bad_runner, _bb, _bt = _wire(bad_strat, _BoomFeed(), BTC_USD)

    orch = Orchestrator()
    orch.add(good_runner)
    orch.add(bad_runner)

    with pytest.raises(RuntimeError, match="feed exploded"):
        await orch.run()

    # The healthy sibling was *not* hung/cancelled: it completed and took its
    # position (the orchestrator let it finish before re-raising).
    pos = good_tracker.position(ETH_USD)
    assert pos is not None and pos.net_qty == Decimal("2")


async def test_multiple_runners_failing_aggregate_into_group_error() -> None:
    """Two failing runners aggregate into a :class:`RunnerGroupError`.

    When more than one runner raises, the orchestrator wraps them in a
    :class:`RunnerGroupError` carrying the per-runner exception map (so the
    caller sees every failure, not just the first).
    """

    class _BoomFeed:
        def __init__(self, msg: str) -> None:
            self._msg = msg

        def __iter__(self) -> Iterator[pl.DataFrame]:
            raise RuntimeError(self._msg)
            yield  # pragma: no cover - makes this a generator

        def latest(self) -> pl.DataFrame:
            return _bars([100.0])

    strat1 = Strategy(name="b1", instrument=BTC_USD,
                      signal_fn=_always_long(BTC_USD),  # type: ignore[arg-type]
                      reference_qty=money("1"))
    strat2 = Strategy(name="b2", instrument=ETH_USD,
                      signal_fn=_always_long(ETH_USD),  # type: ignore[arg-type]
                      reference_qty=money("1"))
    r1, _b1, _t1 = _wire(strat1, _BoomFeed("boom-1"), BTC_USD)
    r2, _b2, _t2 = _wire(strat2, _BoomFeed("boom-2"), ETH_USD)

    orch = Orchestrator()
    orch.add_all([r1, r2])

    with pytest.raises(RunnerGroupError) as exc_info:
        await orch.run()

    err = exc_info.value
    assert set(err.errors) == {r1, r2}
    msgs = {str(e) for e in err.errors.values()}
    assert msgs == {"boom-1", "boom-2"}


# --- signal handling (injected; no real signal) ---------------------------- #


async def test_install_signal_handlers_registers_handler_triggering_shutdown(
) -> None:
    """The injected hook registers a handler that triggers :meth:`shutdown`.

    A *fake* loop captures the ``add_signal_handler`` registrations; we then call
    the registered SIGINT handler and assert it scheduled :meth:`shutdown` (the
    shared ``stop_event`` ends up set) — exercising the signal path without a
    real SIGINT.
    """
    registered: dict[int, object] = {}
    scheduled: list[object] = []

    class _FakeLoop:
        def add_signal_handler(self, sig: int, cb: object) -> None:
            registered[sig] = cb

        def create_task(self, coro: object) -> object:
            scheduled.append(coro)
            return coro

    orch = Orchestrator()
    fake_loop = _FakeLoop()
    orch.install_signal_handlers(fake_loop)  # type: ignore[arg-type]

    # Both default signals were registered on the loop.
    assert set(registered) == {signal.SIGINT, signal.SIGTERM}

    assert not orch.stop_event.is_set()
    # Fire the SIGINT handler as the loop would; it schedules shutdown().
    registered[signal.SIGINT]()  # type: ignore[operator]
    assert len(scheduled) == 1
    # The scheduled coroutine is shutdown(); awaiting it sets the stop event.
    await scheduled[0]  # type: ignore[arg-type]
    assert orch.stop_event.is_set()


async def test_install_signal_handlers_falls_back_when_unsupported() -> None:
    """When the loop lacks ``add_signal_handler``, fall back to ``signal.signal``.

    Some platforms/loops raise ``NotImplementedError`` from
    ``add_signal_handler``; the orchestrator must fall back to the stdlib
    ``signal.signal`` rather than crash. We assert the handlers it installs are
    restored afterwards so the test leaves no global state behind.
    """
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}

    class _NoSignalLoop:
        def add_signal_handler(self, sig: int, cb: object) -> None:
            raise NotImplementedError

        def call_soon_threadsafe(self, cb: object) -> None:  # pragma: no cover
            cb()

        def create_task(self, coro: object) -> object:  # pragma: no cover
            return coro

    orch = Orchestrator()
    try:
        orch.install_signal_handlers(_NoSignalLoop())  # type: ignore[arg-type]
        # A stdlib handler is now installed for each signal (not the default).
        for s in (signal.SIGINT, signal.SIGTERM):
            assert callable(signal.getsignal(s))
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


# --- lifecycle trace ------------------------------------------------------- #


async def test_orchestrator_emits_lifecycle_log_events() -> None:
    """With an event bus, the orchestrator emits start/finish LogEvents."""
    logs: list[LogEvent] = []
    bus = EventBus()
    bus.subscribe(lambda e: logs.append(e) if isinstance(e, LogEvent) else None)

    up = [float(100 + i) for i in range(12)]
    strat = Strategy(
        name="trace",
        instrument=BTC_USD,
        signal_fn=ma_crossover_signal(BTC_USD, fast=3, slow=6),
        reference_qty=money("2"),
        lookback=6,
    )
    runner, _b, _t = _wire(strat, InMemoryFeed(_bars(up)), BTC_USD)
    orch = Orchestrator(event_bus=bus)
    orch.add(runner)
    await orch.run()

    messages = " | ".join(e.message for e in logs)
    assert "starting 1 runner" in messages
    assert "finished" in messages
