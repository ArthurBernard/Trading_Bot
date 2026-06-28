"""Event bus — the engine's pub/sub fan-out for order, fill and log events.

The :class:`EventBus` is the cross-cutting nervous system of the application
layer: use-cases (the router, the position tracker, future UI) *emit* events
without knowing who consumes them, and consumers *subscribe* (sync handlers) or
*drain a queue* (async consumers) without knowing who produced them. It mirrors
dccd's ``application/events.py`` — synchronous handler dispatch plus an
async fan-out over a *set* of :class:`asyncio.Queue` so several consumers each
receive every event.

Design choices (carried into the ADR):

* **Event taxonomy.** Three event types, each a frozen value object carrying
  domain objects / ids (never a re-encoded copy):

  - :class:`OrderEvent` — an order's lifecycle moved (carries the
    :class:`~trading_bot.domain.order.Order` aggregate);
  - :class:`FillEvent` — a venue-confirmed execution landed (carries the
    immutable :class:`~trading_bot.domain.fill.Fill`, the PnL source of truth);
  - :class:`LogEvent` — a human-readable line (level + message).

  All money stays :class:`~decimal.Decimal`, because the events carry the domain
  objects themselves — there is no float round-trip.

* **A *set* of queues, not one.** Each async consumer registers its own queue
  via :meth:`EventBus.add_queue`; :meth:`EventBus.emit` puts the event on every
  registered queue. A single shared queue would let the last consumer steal
  events from the others.

* **emit never blocks and never raises.** Sync handler exceptions are logged and
  swallowed (one bad subscriber must not break the others). Queue puts are
  non-blocking (:meth:`asyncio.Queue.put_nowait`); a full queue drops the event
  (a slow consumer must not stall the producer) — see :meth:`EventBus.emit`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trading_bot.domain.fill import Fill
from trading_bot.domain.order import Order

__all__ = [
    "Event",
    "OrderEvent",
    "FillEvent",
    "LogEvent",
    "EventBus",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """An order's lifecycle changed — carries the live :class:`Order` aggregate.

    Parameters
    ----------
    order : Order
        The order whose status just moved. Carried by reference: money fields
        (``qty``, ``avg_fill_price``, ...) stay :class:`~decimal.Decimal`.

    """

    order: Order


@dataclass(frozen=True, slots=True)
class FillEvent:
    """A venue-confirmed execution landed — carries the immutable :class:`Fill`.

    Parameters
    ----------
    fill : Fill
        The execution record (the PnL source of truth). Its ``qty``, ``price``
        and ``fee`` are :class:`~decimal.Decimal`, intact.

    """

    fill: Fill


@dataclass(frozen=True, slots=True)
class LogEvent:
    """A human-readable log line emitted by a use-case.

    Parameters
    ----------
    message : str
        The log message.
    level : str, optional
        Severity, lower-case (``"info"`` by default, e.g. ``"warning"``,
        ``"error"``).

    """

    message: str
    level: str = "info"


#: The union of every event the bus carries.
Event = OrderEvent | FillEvent | LogEvent

#: A synchronous subscriber: called with each emitted event.
Handler = Callable[[Event], Any]


class EventBus:
    """Pub/sub bus fanning engine events to handlers and async queues.

    Use-cases :meth:`emit` :class:`OrderEvent` / :class:`FillEvent` /
    :class:`LogEvent`. Consumers either register a synchronous :meth:`subscribe`
    handler or, for async consumption, :meth:`add_queue` to get their own
    :class:`asyncio.Queue` to drain. Every registered handler and queue receives
    every event.

    Examples
    --------
    >>> bus = EventBus()
    >>> seen = []
    >>> bus.subscribe(seen.append)
    >>> bus.emit(LogEvent(message="started"))
    >>> seen[0].message
    'started'

    """

    def __init__(self) -> None:
        """Start with no handlers and no queues."""
        self._handlers: list[Handler] = []
        # A *set* of queues so several async consumers (e.g. the position
        # tracker, a live UI) each receive every event; a single shared queue
        # would let one consumer steal events from the others.
        self._queues: set[asyncio.Queue[Event]] = set()

    def subscribe(self, handler: Handler) -> None:
        """Register a handler called synchronously for every emitted event."""
        self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        """Remove a previously registered handler (no-op if not registered)."""
        self._handlers = [h for h in self._handlers if h != handler]

    def emit(self, event: Event) -> None:
        """Publish *event* to every handler and every registered queue.

        Never blocks and never propagates: a handler that raises is logged and
        skipped (one bad subscriber must not break the rest), and a put onto a
        full queue is dropped (a slow consumer must not stall the producer).
        """
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("EventBus handler error")
        for queue in self._queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("EventBus queue full; dropping event %r", event)

    def add_queue(self, maxsize: int = 1000) -> asyncio.Queue[Event]:
        """Register and return a fresh queue that receives every event.

        Parameters
        ----------
        maxsize : int, optional
            Bound on the queue. When full, :meth:`emit` drops new events for
            this queue rather than blocking. ``0`` means unbounded.

        Returns
        -------
        asyncio.Queue
            The newly registered queue. Pass it to :meth:`remove_queue` when the
            consumer disconnects.

        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.add(queue)
        return queue

    def remove_queue(self, queue: asyncio.Queue[Event]) -> None:
        """Unregister a queue (no-op if it was not registered)."""
        self._queues.discard(queue)
