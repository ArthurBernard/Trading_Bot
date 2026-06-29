"""The :class:`LiveFillStreamer` — pump a venue's live fills onto the engine bus.

During a **live** run a venue's private WebSocket (e.g.
:class:`~trading_bot.brokers.kraken_ws.KrakenPrivateWS`) streams the venue's own
executions as domain :class:`~trading_bot.domain.fill.Fill`s. This component
consumes that stream and emits one
:class:`~trading_bot.application.events.FillEvent` per fill onto the shared
:class:`~trading_bot.application.events.EventBus`, so the
:class:`~trading_bot.application.position_tracker.PositionTracker`, the
:class:`~trading_bot.application.performance_service.PerformanceService` and (when
present) the store update from the venue's **confirmed** fills in real time —
rather than only from the simulator. Fill-id dedup in those views guards against a
re-emitted snapshot after a reconnect, so the snapshot Kraken replays on every
resubscribe never double-counts.

It exposes the same cooperative ``run(stop_event=...) -> int`` contract as a
:class:`~trading_bot.application.strategy_runner.StrategyRunner`, so the
:class:`~trading_bot.application.orchestrator.Orchestrator` hosts it as **just
another concurrent task** sharing the one stop event. Consumption is raced against
the stop event and **cancelled promptly** when it fires (a blocked read on a quiet
stream does not delay shutdown).

This module lives in the application layer: it bridges a transport-level fill
source to the event bus, holds no money logic, and performs no I/O of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Protocol

from trading_bot.application.events import EventBus, FillEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trading_bot.domain.fill import Fill

__all__ = ["FillSource", "LiveFillStreamer"]

logger = logging.getLogger(__name__)


class FillSource(Protocol):
    """A live source of domain :class:`~trading_bot.domain.fill.Fill`s.

    The structural contract :class:`LiveFillStreamer` consumes —
    :class:`~trading_bot.brokers.kraken_ws.KrakenPrivateWS` satisfies it (its
    ``fills()`` async-iterates executed trades, reconnecting internally; ``stop()``
    is inherited from :class:`~trading_bot.transport.ws.WebSocketBase`). A test
    injects a fake.
    """

    def fills(self) -> AsyncIterator[Fill]:
        """Yield confirmed fills as they arrive (the source reconnects internally)."""
        ...

    def stop(self) -> None:
        """Request the underlying stream to end (cooperative shutdown)."""
        ...


class LiveFillStreamer:
    """Emit a :class:`FillEvent` on the bus for every fill from a live source.

    Parameters
    ----------
    source : FillSource
        The live fill stream (e.g. a
        :class:`~trading_bot.brokers.kraken_ws.KrakenPrivateWS`).
    event_bus : EventBus
        The shared bus to emit each fill onto (the tracker / performance service /
        store are subscribed to it).

    """

    def __init__(self, source: FillSource, event_bus: EventBus) -> None:
        self._source = source
        self._bus = event_bus
        self._emitted = 0

    async def run(self, *, stop_event: asyncio.Event | None = None) -> int:
        """Stream fills onto the bus until the stream ends or ``stop_event`` fires.

        Mirrors the :class:`~trading_bot.application.strategy_runner.StrategyRunner`
        contract so the :class:`~trading_bot.application.orchestrator.Orchestrator`
        can host it. Consumption is raced against ``stop_event``; when the event is
        set the source is stopped and the (possibly blocked) consumption is
        **cancelled**, so a quiet stream never delays shutdown.

        Parameters
        ----------
        stop_event : asyncio.Event, optional
            The shared cooperative-stop flag. ``None`` runs until the source's
            stream naturally ends (a finite/test source).

        Returns
        -------
        int
            The number of fills emitted onto the bus.

        """
        consume = asyncio.create_task(self._consume())
        if stop_event is None:
            await consume
            return self._emitted

        stop_wait = asyncio.create_task(stop_event.wait())
        await asyncio.wait({consume, stop_wait}, return_when=asyncio.FIRST_COMPLETED)

        if not consume.done():
            # Stop fired first: end the stream and cancel the (blocked) consumer.
            self._source.stop()
            consume.cancel()
        for task in (consume, stop_wait):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self._emitted

    async def _consume(self) -> None:
        """Emit a :class:`FillEvent` for each fill the source yields."""
        async for fill in self._source.fills():
            self._bus.emit(FillEvent(fill))
            self._emitted += 1
