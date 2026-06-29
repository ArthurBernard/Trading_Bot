"""Tests for :class:`LiveFillStreamer` — pumping a live fill source onto the bus.

Drives the streamer with a **fake** fill source (no WebSocket, no network): it
yields a canned sequence of domain fills and can block afterwards to model a quiet
live stream. The streamer must emit one ``FillEvent`` per fill onto the bus, and a
set ``stop_event`` must end even a *blocked* stream promptly (cooperative stop via
cancellation). Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from trading_bot.application import EventBus, FillEvent
from trading_bot.application.live_fills import LiveFillStreamer
from trading_bot.domain import Fill, Instrument, OrderSide, Symbol, money

BTC_USD = Instrument(Symbol("BTC", "USD"))


def _fill(fill_id: str) -> Fill:
    return Fill(
        fill_id=fill_id,
        client_order_id="cid",
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("1"),
        price=money("30000"),
        fee=money("1"),
        ts=1,
    )


class _FakeSource:
    """A fake fill source: yields ``fills`` then optionally blocks until stopped."""

    def __init__(self, fills: list[Fill], *, block_after: bool = False) -> None:
        self._fills = fills
        self._block_after = block_after
        self._stopped = asyncio.Event()

    async def fills(self) -> AsyncIterator[Fill]:
        for fill in self._fills:
            yield fill
        if self._block_after:
            await self._stopped.wait()  # model a quiet live stream

    def stop(self) -> None:
        self._stopped.set()


def _fill_events(seen: list[object]) -> list[FillEvent]:
    return [e for e in seen if isinstance(e, FillEvent)]


async def test_emits_one_fillevent_per_fill() -> None:
    """A finite source: every fill becomes a ``FillEvent`` on the bus, in order."""
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(seen.append)

    streamer = LiveFillStreamer(
        _FakeSource([_fill("F1"), _fill("F2"), _fill("F3")]), bus
    )
    emitted = await streamer.run()

    assert emitted == 3
    assert [e.fill.fill_id for e in _fill_events(seen)] == ["F1", "F2", "F3"]


async def test_stop_event_ends_a_blocked_stream_promptly() -> None:
    """A set ``stop_event`` ends even a *blocked* (quiet) stream — no hang.

    The source yields two fills then blocks forever; the streamer must have emitted
    both, and setting the stop event must let ``run`` return promptly (the blocked
    consumer is cancelled).
    """
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(seen.append)
    stop = asyncio.Event()

    streamer = LiveFillStreamer(
        _FakeSource([_fill("F1"), _fill("F2")], block_after=True), bus
    )
    task = asyncio.create_task(streamer.run(stop_event=stop))

    # Let it emit both fills and then block on the quiet stream.
    for _ in range(200):
        if len(_fill_events(seen)) >= 2:
            break
        await asyncio.sleep(0)

    stop.set()
    emitted = await asyncio.wait_for(task, timeout=2.0)

    assert emitted == 2
    assert [e.fill.fill_id for e in _fill_events(seen)] == ["F1", "F2"]


async def test_subscribed_tracker_updates_from_streamed_fills() -> None:
    """End to end: streamed fills fan out to a subscribed tracker (live read-back)."""
    from trading_bot.application import PositionTracker

    bus = EventBus()
    tracker = PositionTracker(event_bus=bus)
    streamer = LiveFillStreamer(_FakeSource([_fill("F1"), _fill("F2")]), bus)

    await streamer.run()

    pos = tracker.position(BTC_USD)
    assert pos is not None
    assert pos.net_qty == money("2")  # two BUY 1 @ 30000 folded from the stream
