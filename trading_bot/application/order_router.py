"""The :class:`OrderRouter` — the engine's idempotent write path.

The router is the **safety core of execution**: it is the single use-case that
turns a domain :class:`~trading_bot.domain.order.Order` into a live venue order.
It does three things and no more:

* **submits orders idempotently** — keyed by the order's ``client_order_id``, so
  a retry (or a concurrent double-submit) of the same id produces **exactly one**
  broker order, never a duplicate venue order;
* **drives the order's state machine** from the broker's response
  (``NEW -> SUBMITTED -> OPEN``, or ``-> REJECTED`` on a broker/order failure);
* **cancels** a tracked order on the venue and transitions it.

It speaks **domain types only** and never touches money as ``float`` (orders and
events carry :class:`~decimal.Decimal` throughout). Every broker operation is
gated through :func:`~trading_bot.brokers.base.require` against the broker's
declared :class:`~trading_bot.brokers.base.Capability` set, so a venue is never
asked for an operation it has not declared.

Idempotency mechanism (carried into the ADR)
---------------------------------------------
Idempotency here is **purely engine-side**: a dedup map ``client_order_id ->
Order`` records every order the router has *tracked*. A second :meth:`submit`
with the same id returns the already-tracked order and does **not** call the
broker again. This is the only line of defence the router owns; venue-level
idempotency (an exchange-side dedup token on ``AddOrder``) is a deferred go-live
item (see ``doc/dev/06-status.md``) and is **out of scope** here.

A rejected submission is *also* recorded (the dedup map keeps the terminal,
``REJECTED`` order), so a retry of a poisoned id surfaces the original rejection
without a second broker call — the router never double-submits, even on failure.

Concurrency guard (carried into the ADR)
----------------------------------------
The dedup map alone is not enough under concurrency: two ``await``\\ ed submits of
the same id interleaving at the first ``await broker.place_order(...)`` could each
see an empty map and each call the broker. The guard is a **per-id in-flight
future map** (``dict[str, asyncio.Future[Order]]``): the first submit of an id
installs a future and does the real work; any concurrent submit of the same id
finds the future and simply ``await``\\ s its result. The future resolves to the
tracked order on success, or *raises* (propagating the rejection) on failure, and
is removed once settled. Because asyncio is single-threaded and the
install-or-find check runs synchronously (no ``await`` between the lookup and the
install), exactly one coroutine ever reaches the broker per id.

Fill ingestion — the boundary (carried into the ADR)
----------------------------------------------------
The router owns **submit and cancel only**. Fill ingestion does **not** live
here: applying a :class:`~trading_bot.domain.fill.Fill` to an order, recomputing
the average price and folding it into a position is the job of the
``PositionTracker`` (leaf 04), which subscribes to the broker's fill stream and
owns PnL. Keeping the router to the write path (intent -> venue) and the tracker
to the read-back path (executions -> position) is the simplest correct boundary:
the router never needs to know about PnL, and the tracker never needs to know how
an order was submitted. The router therefore exposes no ``ingest_fill`` method.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from trading_bot.application.events import EventBus, OrderEvent
from trading_bot.brokers.base import Broker, Capability, require
from trading_bot.domain.errors import BrokerError, MissingOrder, OrderError
from trading_bot.domain.order import Order, OrderStatus

if TYPE_CHECKING:
    from trading_bot.application.risk import RiskManager

__all__ = ["OrderRouter"]

logger = logging.getLogger(__name__)


class OrderRouter:
    """Idempotent order submission + lifecycle driving over a :class:`Broker`.

    Construct it with the broker to route to and the :class:`EventBus` to emit
    lifecycle events on, then drive orders with :meth:`submit` and :meth:`cancel`.
    The broker must declare :attr:`~trading_bot.brokers.base.Capability.PLACE_ORDER`
    and :attr:`~trading_bot.brokers.base.Capability.CANCEL`; this is checked up
    front so a mis-capable broker fails loudly at construction, not at first use.

    Parameters
    ----------
    broker : Broker
        The venue adapter to route orders to. Must declare ``PLACE_ORDER`` and
        ``CANCEL`` capabilities.
    event_bus : EventBus
        The bus every order lifecycle change is emitted on, as an
        :class:`~trading_bot.application.events.OrderEvent` carrying the live
        :class:`Order`.
    risk_manager : RiskManager, optional
        The pre-trade gate. When given, every :meth:`submit` calls
        :meth:`~trading_bot.application.risk.RiskManager.check` on the order
        **before** the broker is touched; a
        :class:`~trading_bot.domain.errors.RiskLimitBreached` propagates and the
        order is **never placed** and **never tracked** (no broker call, no
        half-tracked submission — so the idempotency map stays a record of
        *accepted* submissions only, and a later retry is free to re-attempt).
        ``None`` (the default) runs the router with no risk gate.

    Raises
    ------
    NoCapability
        If ``broker`` does not declare both ``PLACE_ORDER`` and ``CANCEL``.

    """

    def __init__(
        self,
        broker: Broker,
        event_bus: EventBus,
        *,
        risk_manager: RiskManager | None = None,
    ) -> None:
        # Gate up front: a broker that cannot place *and* cancel is not a valid
        # write-path target, so fail at construction rather than at first call.
        require(broker, Capability.PLACE_ORDER)
        require(broker, Capability.CANCEL)
        self._broker = broker
        self._bus = event_bus
        self._risk = risk_manager
        # Dedup map: every order the router has tracked, keyed by its identity.
        self._orders: dict[str, Order] = {}
        # Per-id in-flight submissions, the concurrency guard (see module doc).
        self._inflight: dict[str, asyncio.Future[Order]] = {}

    async def submit(self, order: Order) -> Order:
        """Submit ``order`` to the broker idempotently and drive its lifecycle.

        Idempotent on ``order.client_order_id``: if that id was already submitted
        (successfully or rejected), the already-tracked order is returned and the
        broker is **not** called again. A concurrent second submit of the same id
        awaits the in-flight submission rather than starting a second one (see the
        module docstring's concurrency guard).

        On a fresh id the router drives ``NEW -> SUBMITTED -> OPEN``:
        :meth:`Order.submit`, then ``venue_id = await broker.place_order(order)``,
        then :meth:`Order.open` with the venue id, tracks the order, and emits one
        :class:`~trading_bot.application.events.OrderEvent`.

        Parameters
        ----------
        order : Order
            The order to submit. Its ``client_order_id`` is the idempotency key.

        Returns
        -------
        Order
            The tracked order. On a duplicate id this is the *original* tracked
            order, not ``order``.

        Raises
        ------
        RiskLimitBreached
            If a ``risk_manager`` is wired and its
            :meth:`~trading_bot.application.risk.RiskManager.check` refuses the
            order (a breached limit or a tripped kill-switch). The broker is
            **not** called and the order is **not** tracked — a refused order is
            not a submission, so it leaves no record and a later retry is a fresh
            attempt.
        BrokerError or OrderError
            If the broker (or the order's own state machine) fails the
            submission. The order is driven to ``REJECTED``, a reject
            :class:`OrderEvent` is emitted, the attempt is recorded (so a retry of
            the same id does not re-call the broker), and the error is re-raised.

        """
        cid = order.client_order_id

        # Already tracked (succeeded earlier, or rejected earlier): return the
        # tracked order, never re-call the broker. This is the steady-state
        # idempotency check for a *sequential* retry.
        existing = self._orders.get(cid)
        if existing is not None:
            return existing

        # An in-flight submission of this id is running: await its result instead
        # of starting a second one. This is the concurrency guard.
        inflight = self._inflight.get(cid)
        if inflight is not None:
            return await inflight

        # We are the first submitter of this id. Install the in-flight future
        # *synchronously* (no await before this point since the lookup), so a
        # concurrently-scheduled submit of the same id finds it above.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Order] = loop.create_future()
        self._inflight[cid] = future
        try:
            result = await self._do_submit(order)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            # The future has done its job (relayed the result/exception to any
            # waiters); drop it so the dedup map is the single source of truth.
            self._inflight.pop(cid, None)

    async def _do_submit(self, order: Order) -> Order:
        """Drive one fresh submission ``NEW -> SUBMITTED -> OPEN`` (or reject).

        Per the :class:`Broker` port, the *caller* drives the lifecycle and the
        broker only transmits the order and reports its venue id back — it never
        touches the caller's ``Order``. So the router calls ``place_order`` with
        the order still ``NEW``, then drives ``NEW -> SUBMITTED -> OPEN`` itself
        with the returned venue id.

        The **risk gate runs first**, before any broker call or state change:
        :meth:`~trading_bot.application.risk.RiskManager.check` is the last safety
        block before a venue sees the order. A
        :class:`~trading_bot.domain.errors.RiskLimitBreached` propagates *out of*
        this method without calling the broker, without driving any transition,
        and without tracking the order — a refused order is **not** a submission,
        so it leaves no ``REJECTED`` record and the dedup map is unchanged (a
        subsequent submit of the same id is a fresh attempt, not a deduped
        replay).
        """
        if self._risk is not None:
            # Pre-trade gate: raises RiskLimitBreached before the broker is ever
            # touched. Left to propagate untracked (see the docstring).
            self._risk.check(order)
        try:
            venue_id = await self._broker.place_order(order)
            order.submit()
            order.open(venue_id)
        except (BrokerError, OrderError) as exc:
            # Record the (rejected) attempt *before* surfacing, so a retry of the
            # same id is deduped and never double-submits to the venue.
            self._reject(order, str(exc))
            raise
        # Track the (now live or terminal-by-fill) order so the dedup map owns it.
        self._orders[order.client_order_id] = order
        self._bus.emit(OrderEvent(order))
        return order

    def _reject(self, order: Order, reason: str) -> None:
        """Drive ``order`` to ``REJECTED``, track the attempt, and emit an event.

        Rejection is only legal from ``SUBMITTED`` in the state machine, but a
        ``place_order`` may fail while the order is still ``NEW`` (a port-pure
        broker that never advanced it). To always land on the terminal,
        deduped-and-tracked ``REJECTED`` state, the router first nudges a ``NEW``
        order to ``SUBMITTED`` (this never reaches the venue) and then rejects it.
        Tolerant if even that is forbidden: the id is still tracked so a retry is
        deduped, and a reject event is still emitted.
        """
        try:
            if order.status is OrderStatus.NEW:
                order.submit()
            order.reject(reason)
        except OrderError:
            # The state machine forbade the transition (e.g. the order is already
            # terminal). The id must still be recorded so a retry is deduped;
            # carry on to track + emit whatever state the order is in.
            logger.debug(
                "order %s could not transition to REJECTED from %s",
                order.client_order_id,
                order.status.value,
            )
        self._orders[order.client_order_id] = order
        self._bus.emit(OrderEvent(order))

    async def cancel(self, order_or_id: Order | str) -> Order:
        """Cancel a tracked order on the broker and transition it to ``CANCELLED``.

        Resolves ``order_or_id`` to the tracked order, cancels it on the venue via
        the order's ``venue_order_id``, drives :meth:`Order.cancel`, and emits an
        :class:`~trading_bot.application.events.OrderEvent`.

        Parameters
        ----------
        order_or_id : Order or str
            Either a tracked :class:`Order` or its ``client_order_id``.

        Returns
        -------
        Order
            The now-cancelled tracked order.

        Raises
        ------
        MissingOrder
            If no order is tracked under that id (it was never submitted here).
        BrokerError
            If the broker fails the cancellation. The order's local state is left
            untouched (the cancel is driven only after the broker confirms).

        """
        order = self._resolve(order_or_id)
        if order.venue_order_id is None:
            raise MissingOrder(order.client_order_id)
        await self._broker.cancel_order(order.venue_order_id)
        # The broker only cancels its own venue record (it never touches our
        # Order); the router drives the local CANCELLED transition.
        order.cancel()
        self._bus.emit(OrderEvent(order))
        return order

    def _resolve(self, order_or_id: Order | str) -> Order:
        """Resolve an :class:`Order` or a client-order-id to the tracked order."""
        cid = order_or_id if isinstance(order_or_id, str) else order_or_id.client_order_id
        order = self._orders.get(cid)
        if order is None:
            raise MissingOrder(cid)
        return order

    def get(self, client_order_id: str) -> Order | None:
        """Return the tracked order for ``client_order_id``, or ``None``.

        A read-only view of the dedup map, for callers (tests, a UI) that need to
        inspect what the router has tracked without driving a transition.
        """
        return self._orders.get(client_order_id)

    def tracked_orders(self) -> dict[str, Order]:
        """Return a snapshot of every tracked order, keyed by client-order-id.

        A read-only copy of the dedup map (the mapping is fresh; the
        :class:`Order` values are shared). For callers — notably
        :func:`~trading_bot.application.reconcile.reconcile` — that need to
        diff the router's view against the venue's truth without driving a
        transition.
        """
        return dict(self._orders)

    def ingest(self, order: Order) -> Order:
        """Adopt an already-live venue ``order`` into the tracked map, no submit.

        The reconciliation path (:func:`~trading_bot.application.reconcile.
        reconcile`) uses this to adopt an order the **venue** reports as open
        but the router never tracked locally (e.g. submitted before a restart,
        or in a window the engine missed). Unlike :meth:`submit`, this performs
        **no broker call** and drives **no lifecycle transition** — ``order`` is
        the venue's already-reconstructed view (its ``status`` and
        ``venue_order_id`` are populated by the broker's ``open_orders``), and
        the router simply records it under its ``client_order_id`` so subsequent
        :meth:`get` / :meth:`cancel` resolve it.

        Idempotent on ``client_order_id``: ingesting an id the router already
        tracks is a no-op that returns the **existing** tracked order (the
        engine's own object is authoritative for an id it already owns; the
        venue snapshot is not allowed to clobber it). This keeps reconciliation
        from ever duplicating an order.

        Parameters
        ----------
        order : Order
            The venue's reconstructed open order to adopt. Its
            ``client_order_id`` is the tracking key.

        Returns
        -------
        Order
            The tracked order: the freshly-ingested ``order`` on a new id, or
            the pre-existing tracked order on an id already owned.

        """
        existing = self._orders.get(order.client_order_id)
        if existing is not None:
            return existing
        self._orders[order.client_order_id] = order
        self._bus.emit(OrderEvent(order))
        return order

    def forget(self, client_order_id: str) -> Order | None:
        """Drop ``client_order_id`` from the tracked map, returning the order.

        The reconciliation path uses this to evict an **orphan**: a locally
        tracked order the venue reports neither as open nor via any fill, after
        reconcile has driven it terminal. Returns the removed order, or ``None``
        if the id was not tracked (a no-op, so a second reconcile is clean).
        """
        return self._orders.pop(client_order_id, None)
