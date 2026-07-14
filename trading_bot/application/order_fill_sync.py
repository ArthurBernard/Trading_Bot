"""The :class:`OrderFillSync` — venue-confirmed fills reach the tracked order.

Before this component, **nothing ever called** :meth:`~trading_bot.domain.order.
Order.apply_fill` on the :class:`~trading_bot.application.order_router.
OrderRouter`'s *tracked* instance: the broker emitted
:class:`~trading_bot.application.events.FillEvent`\\ s, the
``PositionTracker``/``PerformanceService`` folded them into positions/PnL, but
the tracked :class:`~trading_bot.domain.order.Order` (and therefore its
:class:`~trading_bot.storage.sqlite_store.SqliteStore` row, and every UI built on
it) stayed frozen at ``status='open'``, ``filled_qty=0``, ``avg_fill_price=NULL``
forever. :class:`OrderFillSync` closes that loop: it subscribes to the shared
:class:`~trading_bot.application.events.EventBus`, applies each fill to the
router's tracked order, and re-emits an
:class:`~trading_bot.application.events.OrderEvent` so the store's
``upsert_order`` persists the new state.

Why this is not a router method (carried into the ADR)
------------------------------------------------------
The router's own module docstring declares the fill boundary **out of scope**:
the router owns the *write path* (intent -> venue: submit + cancel) and exposes
no ``ingest_fill``. Applying executions is the *read-back path* — a bus consumer,
like the tracker — so fill ingestion lives in this separate component, wired next
to the router by the service factory, rather than widening the router's safety
core.

Ordering — why stash-and-drain (carried into the ADR)
-----------------------------------------------------
:meth:`~trading_bot.application.events.EventBus.emit` dispatches synchronous
handlers **inline**, and :meth:`~trading_bot.brokers.paper.PaperBroker.
place_order` fills **synchronously at placement**. So on the paper path the
``FillEvent`` fires *while the router's* ``_do_submit`` *is still awaiting*
``place_order`` — i.e. **before** ``order.submit(); order.open(venue_id)`` run
and before the order is tracked at all. At fill-arrival time there is no tracked,
fillable order to apply to. Applying eagerly is therefore impossible; instead a
fill for an unknown (or not-yet-fillable) order is **stashed by
``client_order_id``** and **drained when its** ``OrderEvent`` **arrives** with a
fillable status (``OPEN`` / ``PARTIALLY_FILLED``) — which the router emits right
after tracking the order. On the live path (fills stream in long after
submission) the order is already tracked and fillable, so the fill applies
directly at arrival; the stash is simply empty.

Restart healing
---------------
:meth:`OrderFillSync.replay` is the startup entry point: it applies the store's
*persisted* fills to the freshly-**restored** orders (see
:meth:`~trading_bot.application.order_router.OrderRouter.restore`), run
**between** ``restore`` and the startup
:func:`~trading_bot.application.reconcile.reconcile`. A genuinely-filled order is
then terminal *before* the orphan rule runs, so reconcile stops mis-cancelling
filled orders, and the healed row is persisted via the re-emitted
``OrderEvent``.

Replay's **prefix-skip contract**: the restored row's persisted ``filled_qty``
already reflects every fill that was applied before the restart, so replay must
not re-apply that prefix. Per order, in ts order, a cumulative counter is
compared against a snapshot of the persisted ``filled_qty`` (taken *before* any
apply mutates it): a fill still fully covered by the snapshot is marked seen and
skipped; only the uncovered tail is applied. This is exact for all three row
shapes — a frozen pre-fix row (``filled_qty=0``: nothing covered, everything
applies), an accurate ``PARTIALLY_FILLED`` row (every persisted fill covered,
nothing re-applies), and a crash-lagged row (the persisted state one fill behind
its fills table: the prefix is skipped, the missing tail applies).

Safety
------
* **Idempotent by ``fill_id``** — every consumed fill (applied, or deliberately
  skipped) is remembered, so a re-emitted event or a replayed history never
  double-applies. Fills are the PnL source of truth — never double-count.
* **A malformed fill never breaks the bus** — every ``apply_fill`` is wrapped:
  a domain refusal (:class:`~trading_bot.domain.errors.OrderError` /
  :class:`~trading_bot.domain.errors.OrderStatusError`) is logged as a warning
  and the component carries on, so the other bus consumers are untouched.
* **Money stays** :class:`~decimal.Decimal` end-to-end — the events carry the
  domain objects themselves; nothing is re-encoded.
"""

from __future__ import annotations

# Built-in
import logging
from typing import TYPE_CHECKING

# Local
from trading_bot.application.events import Event, EventBus, FillEvent, OrderEvent
from trading_bot.domain.errors import OrderError
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trading_bot.application.order_router import OrderRouter
    from trading_bot.domain.fill import Fill

__all__ = ["OrderFillSync"]

logger = logging.getLogger(__name__)

#: Statuses a fill may be applied from — the order is live on the venue.
#: Mirrors the domain state machine's own fillable set (``Order.apply_fill``
#: refuses any other source status).
_FILLABLE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
)


class OrderFillSync:
    """Apply venue-confirmed fills to the router's tracked orders, via the bus.

    Construct it with the router whose tracked orders it updates and the bus it
    listens on; it subscribes itself on construction. From then on every
    :class:`~trading_bot.application.events.FillEvent` is applied to the tracked
    order (or stashed until the order is tracked and fillable — see the module
    docstring's ordering rationale) and the updated order is re-emitted as an
    :class:`~trading_bot.application.events.OrderEvent`, so an attached store
    persists the new state. :meth:`replay` heals freshly-restored orders from
    persisted fills on startup.

    Parameters
    ----------
    router : OrderRouter
        The router whose tracked-order map holds the live
        :class:`~trading_bot.domain.order.Order` aggregates to update.
    bus : EventBus
        The shared bus to listen on and re-emit updated orders onto.
    unit_name : str or None, optional
        The managed unit this sync belongs to, threaded from
        :func:`~trading_bot.application.service_factory.build_engine` for a
        single-unit slice. When given, every applied-fill INFO line is prefixed
        ``unit=<name> ``; when ``None`` (the whole-system / multi-unit path, the
        default — keeps the historic signature working) the same line is logged
        without the prefix (starting at ``fill cid=…``).

    Examples
    --------
    >>> from trading_bot.application.events import EventBus
    >>> from trading_bot.application.order_router import OrderRouter
    >>> from trading_bot.brokers.paper import PaperBroker
    >>> bus = EventBus()
    >>> broker = PaperBroker(event_bus=bus)
    >>> router = OrderRouter(broker, bus)
    >>> sync = OrderFillSync(router, bus)  # subscribed; fills now reach orders

    """

    def __init__(
        self, router: OrderRouter, bus: EventBus, *, unit_name: str | None = None
    ) -> None:
        self._router = router
        self._bus = bus
        self._unit_name = unit_name
        # Fill-id dedup: every fill this component has consumed — applied, or
        # deliberately skipped (untracked/terminal on replay) — so a re-emitted
        # event or a replayed history never double-applies. Fills are the PnL
        # source of truth: never double-count.
        self._seen_fill_ids: set[str] = set()
        # Fills that arrived before their order was tracked/fillable, keyed by
        # client_order_id, drained when the order's OrderEvent lands (see the
        # module docstring's stash-and-drain rationale).
        self._pending: dict[str, list[Fill]] = {}
        bus.subscribe(self._on_event)

    # --- bus handler --------------------------------------------------------- #

    def _on_event(self, event: Event) -> None:
        """Route bus events: fills are applied/stashed, order events drain."""
        if isinstance(event, FillEvent):
            self._on_fill(event.fill)
        elif isinstance(event, OrderEvent):
            self._on_order(event.order)

    def _on_fill(self, fill: Fill) -> None:
        """Apply ``fill`` to its tracked order, or stash it until it can apply.

        A fill whose ``fill_id`` was already consumed is skipped (idempotency).
        A fill whose order the router does not track yet — the paper path, where
        the ``FillEvent`` fires before ``_do_submit`` tracks the order — or whose
        order is not in a fillable status is stashed under its
        ``client_order_id`` and drained by :meth:`_on_order`. A fill applied here
        re-emits one :class:`OrderEvent` so the store persists the new state.
        """
        if fill.fill_id in self._seen_fill_ids:
            return
        order = self._router.get(fill.client_order_id)
        if order is None or order.status not in _FILLABLE:
            # Unknown or not-yet-fillable order: stash-and-drain (module doc).
            self._pending.setdefault(fill.client_order_id, []).append(fill)
            return
        self._apply(order, fill)
        self._seen_fill_ids.add(fill.fill_id)
        self._bus.emit(OrderEvent(order))

    def _on_order(self, order: Order) -> None:
        """Drain any stashed fills for ``order`` now that it may be fillable.

        A terminal order can never take a fill, so its pending entry (if any) is
        dropped — this bounds the stash's memory. Otherwise, if the order is
        fillable and fills are stashed for its ``client_order_id``, they are
        applied in arrival order and **one** :class:`OrderEvent` is re-emitted
        for the whole drain. The re-emitted event finds no pending fills for
        this id (the entry was just popped), so the nested dispatch is a no-op —
        re-emission cannot recurse.
        """
        cid = order.client_order_id
        if order.is_terminal:
            # Bounded memory: fills stashed for a terminal order can never apply.
            self._pending.pop(cid, None)
            return
        if order.status not in _FILLABLE or cid not in self._pending:
            return
        for fill in self._pending.pop(cid):
            if fill.fill_id in self._seen_fill_ids:
                continue
            self._apply(order, fill)
            self._seen_fill_ids.add(fill.fill_id)
        # One event per drain, not per fill; nested dispatch no-ops (see above).
        self._bus.emit(OrderEvent(order))

    # --- restart healing ------------------------------------------------------ #

    def replay(self, fills: Iterable[Fill]) -> int:
        """Apply persisted ``fills`` to freshly-restored orders — startup healing.

        The restart entry point, run **between**
        :meth:`~trading_bot.application.order_router.OrderRouter.restore` and the
        startup :func:`~trading_bot.application.reconcile.reconcile`: each fill is
        applied to its tracked order when that order is in a fillable status
        (``OPEN`` / ``PARTIALLY_FILLED``) — exactly the live-path rule — so a
        genuinely-filled order is terminal *before* the orphan rule runs and
        reconcile no longer mis-cancels it. One :class:`OrderEvent` is re-emitted
        per **healed order** (not per fill), so an attached store persists each
        healed row once. Fills for untracked or terminal orders are recorded as
        seen and skipped (they are history, not live state); every consumed
        ``fill_id`` is remembered, so a later re-emitted event never
        double-applies.

        **Prefix-skip** (see the module docstring): the fills already reflected
        in the restored row's persisted ``filled_qty`` must not re-apply. Per
        order, a cumulative counter runs over its fills in ts order against a
        snapshot of the persisted ``filled_qty`` taken at the order's first
        fill (before any apply mutates it): while ``cumulative + fill.qty``
        stays within the snapshot the fill is covered — marked seen, counted,
        not applied. Only the uncovered tail applies, so a frozen row heals
        fully, an accurate ``PARTIALLY_FILLED`` row re-applies nothing, and a
        crash-lagged row applies exactly the missing tail — never an over-count.

        Parameters
        ----------
        fills : Iterable[Fill]
            The persisted fills to apply, **in ts order** (the caller sorts —
            partial fills must accumulate in execution order for the running
            average, and the prefix-skip, to be right).

        Returns
        -------
        int
            The number of distinct orders healed (had at least one fill applied).

        """
        healed: dict[str, Order] = {}
        # Prefix-skip state, per client_order_id: the *persisted* filled_qty
        # snapshot (captured before any apply mutates the live order) and the
        # cumulative quantity of its fills seen so far in this replay.
        covered: dict[str, Money] = {}
        cumulative: dict[str, Money] = {}
        for fill in fills:
            if fill.fill_id in self._seen_fill_ids:
                continue
            order = self._router.get(fill.client_order_id)
            if order is None or order.status not in _FILLABLE:
                # Untracked, or already terminal: history — record and skip.
                self._seen_fill_ids.add(fill.fill_id)
                continue
            cid = fill.client_order_id
            if cid not in covered:
                covered[cid] = order.filled_qty
                cumulative[cid] = money("0")
            if cumulative[cid] + fill.qty <= covered[cid]:
                # Still inside the persisted prefix: this fill is already
                # reflected in the restored row — consume it without applying.
                cumulative[cid] += fill.qty
                self._seen_fill_ids.add(fill.fill_id)
                continue
            if self._apply(order, fill):
                healed[cid] = order
            self._seen_fill_ids.add(fill.fill_id)
        for order in healed.values():
            self._bus.emit(OrderEvent(order))
        return len(healed)

    # --- guarded apply -------------------------------------------------------- #

    def _apply(self, order: Order, fill: Fill) -> bool:
        """Apply ``fill`` to ``order``, guarded — a bad fill never breaks the bus.

        Wraps :meth:`~trading_bot.domain.order.Order.apply_fill`: a domain
        refusal (:class:`~trading_bot.domain.errors.OrderError`, including its
        :class:`~trading_bot.domain.errors.OrderStatusError` subclass — e.g. a
        material over-fill, or a duplicate landing on an already-``FILLED``
        order) is logged as a warning and swallowed, so the other bus consumers
        (tracker, performance, store) are untouched. Returns whether the fill
        was actually applied.
        """
        try:
            order.apply_fill(fill.qty, fill.price)
        except OrderError as exc:
            logger.warning(
                "fill %s could not be applied to order %s (status %s): %s",
                fill.fill_id,
                order.client_order_id,
                order.status.value,
                exc,
            )
            return False
        # One INFO per applied fill — the execution the log's fill line reports
        # (signed qty, price, fee) is exactly what reached the tracked order, so a
        # reader can tie the daemon log to the store's fills table. The fee is
        # denominated in ``fee_asset`` when the venue set one, else the quote
        # currency. Prefixed ``unit=<name> `` when this sync knows its unit; the
        # same line without the prefix otherwise (see the ``unit_name`` param).
        fee_ccy = fill.fee_asset or fill.instrument.symbol.quote
        prefix = f"unit={self._unit_name} " if self._unit_name is not None else ""
        logger.info(
            "%sfill cid=%s %s @ %s fee=%s %s",
            prefix,
            fill.client_order_id,
            fill.signed_qty,
            fill.price,
            fill.fee,
            fee_ccy,
        )
        return True
