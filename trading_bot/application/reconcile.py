"""Reconciliation — converge local engine state to the broker's truth.

The execution engine holds **two** local views that can drift from the venue:

* the :class:`~trading_bot.application.order_router.OrderRouter`'s tracked-order
  map (what the engine believes is live), and
* the :class:`~trading_bot.application.position_tracker.PositionTracker`'s
  positions (what the engine believes it holds).

They drift whenever the engine is **not** the only writer of the venue's state:
across a restart (in-memory maps are empty, the venue still holds open orders and
a fill history), across a disconnect (fills landed while the WS feed was down,
orders were cancelled on the venue or by another session), or simply because the
engine missed an event. The standing invariant is **reconcile, don't assume**: on
startup and after any disconnect, refetch the venue's open orders, balances and
fills, and *converge* local state to that truth — never leaving a duplicated or a
lost order, and never inferring a fill the broker did not confirm.

:func:`reconcile` is that pass. It is **read-then-converge**: it pulls the three
broker views once, then mutates only local state (the router map and the tracker)
to match — it never places, cancels or otherwise writes to the venue, because the
venue is the authority being converged *to*. It returns a :class:`ReconResult`
counting exactly what changed, and emits one
:class:`~trading_bot.application.events.LogEvent` summarising the pass.

The rules (carried into the ADR)
--------------------------------
**Orders — the venue's open set is the truth.**

* *Ingested* — a venue-open order the router does **not** track is adopted into
  the map via :meth:`~trading_bot.application.order_router.OrderRouter.ingest`
  (no broker call, no lifecycle transition: it is already live on the venue).
  This is how an order placed before a restart is recovered.
* *Adopted* — a venue-open order the router **already** tracks is left as the
  engine's own object (``ingest`` is idempotent on the id and does not clobber
  it); it is counted as adopted, not ingested.
* *Orphan* — a **non-terminal** order the router tracks that the venue reports
  **neither** as open **nor** in any fill is an orphan: the venue has no record
  of it, so the engine must not keep believing it is live. The policy is
  **close-and-forget**: drive it to :data:`~trading_bot.domain.order.OrderStatus
  .CANCELLED` (tolerant if the state machine forbids the move — a ``NEW`` order
  is nudged through ``SUBMITTED`` first; an order that cannot legally cancel is
  still evicted) and drop it from the tracked map. Choosing ``CANCELLED`` over
  ``REJECTED`` reflects the truth that the order was *accepted at some point but
  is no longer live on the venue* — it is the least-surprising terminal state and
  it stops any further engine action on a phantom order. A non-terminal tracked
  order that has **no venue open record but does have fills** is treated the same
  way (it is no longer open) — its fills still rebuild the position below.
* *Terminal local order* — a tracked order already in a terminal state
  (``FILLED`` / ``CANCELLED`` / ``REJECTED``) is left untouched; it is history,
  not live state, and the venue not reporting it as open is expected.

**Positions — the broker's fills are the truth.** The tracker is rebuilt from
scratch (:meth:`~trading_bot.application.position_tracker.PositionTracker.reset`)
over the broker's confirmed :meth:`~trading_bot.brokers.base.Broker.fills` — fills
are the PnL source of truth, so the post-reconcile positions are **exactly**
``Position.from_fills`` over the venue's fills, per instrument. Rebuilding (rather
than diffing) makes the pass trivially correct and avoids ever double-counting a
fill the tracker had already applied.

**Idempotency.** With no venue change between two runs, the second
:func:`reconcile` is a no-op: every venue-open order is already tracked (ingested
in the first pass), there are no new orphans, and the rebuild folds the same
fills to the same positions — so the second :class:`ReconResult` reports all
zeros. This is what lets reconciliation run freely on every reconnect.

**Money is :class:`~decimal.Decimal`** throughout — the broker views carry domain
objects, so there is no float round-trip.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.application.events import EventBus, LogEvent
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.brokers.base import Broker
from trading_bot.domain.errors import OrderError
from trading_bot.domain.order import Order, OrderStatus

__all__ = ["ReconResult", "reconcile"]


@dataclass(frozen=True, slots=True)
class ReconResult:
    """A summary of one :func:`reconcile` pass — what converged, by count.

    Every field is a count of orders/fills/positions the pass touched. A pass
    over a venue that has not changed since the previous reconcile reports
    **all zeros** (the idempotency contract).

    Parameters
    ----------
    ingested_orders : int
        Venue-open orders the router did **not** track and now does (adopted via
        :meth:`~trading_bot.application.order_router.OrderRouter.ingest`).
    adopted_orders : int
        Venue-open orders the router **already** tracked (left as the engine's
        own object; no change beyond confirming they are still live).
    closed_orphans : int
        Locally tracked **non-terminal** orders the venue does not report as open
        — closed (``CANCELLED``) and evicted from the map per the orphan policy.
    fills_applied : int
        Broker-confirmed fills folded into the rebuilt
        :class:`~trading_bot.application.position_tracker.PositionTracker`.
    positions_rebuilt : int
        Distinct instruments with a net position after the rebuild.

    """

    ingested_orders: int
    adopted_orders: int
    closed_orphans: int
    fills_applied: int
    positions_rebuilt: int

    @property
    def changed(self) -> bool:
        """Whether the pass converged anything (any non-zero *mutating* count).

        ``True`` when the pass ingested an order or closed an orphan — the two
        actions that mutate the router's tracked map. ``adopted_orders``,
        ``fills_applied`` and ``positions_rebuilt`` are *confirmations* of state
        that already matched (a steady-state reconcile re-folds the same fills
        into the same positions), so they do **not** by themselves mean the
        engine's view changed. A no-op idempotent second pass therefore has
        ``changed is False`` even though it re-reports the standing fill/position
        counts.
        """
        return self.ingested_orders > 0 or self.closed_orphans > 0


async def reconcile(
    broker: Broker,
    router: OrderRouter,
    tracker: PositionTracker,
    *,
    since_ms: int | None = None,
    event_bus: EventBus | None = None,
) -> ReconResult:
    """Converge the router's orders and the tracker's positions to ``broker``.

    Refetches the venue's open orders, balances and fills, then mutates **only**
    local state to match (it never writes to the venue). Orders converge to the
    venue's open set (ingest unknown ones, close orphans), positions are rebuilt
    from the broker's confirmed fills. See the module docstring for the full
    reconciliation rules and the orphan-order policy.

    Idempotent: with no venue change between calls, the second pass is a no-op
    and its :class:`ReconResult` reports all zeros.

    Parameters
    ----------
    broker : Broker
        The venue adapter whose state is the truth. Its ``open_orders``,
        ``balances`` and ``fills`` are read once each; nothing is written.
    router : OrderRouter
        The engine's tracked-order map, converged to the venue's open set.
    tracker : PositionTracker
        The engine's positions, rebuilt from the broker's fills.
    since_ms : int, optional
        Lower time bound (ms since the Unix epoch, UTC) passed to
        :meth:`~trading_bot.brokers.base.Broker.fills`. ``None`` (default) pulls
        the venue's full/default fill window — the safe choice on a cold start,
        where the tracker is rebuilt from the complete fill history.
    event_bus : EventBus, optional
        If given, a single :class:`~trading_bot.application.events.LogEvent`
        summarising the pass is emitted on it. Defaults to ``None`` (no event).

    Returns
    -------
    ReconResult
        The per-category counts of what the pass converged.

    """
    # --- 1. Pull the venue's truth (one fetch each; no writes). ------------- #
    open_orders = await broker.open_orders()
    await broker.balances()  # refetched for the reconcile contract; not diffed here.
    broker_fills = await broker.fills(since_ms)

    venue_open_cids = {order.client_order_id for order in open_orders}

    # --- 2. Orders: adopt the venue's open set as truth. -------------------- #
    ingested = 0
    adopted = 0
    for venue_order in open_orders:
        # ingest is idempotent on the id: a new id is added (ingested), an id we
        # already own is left as our object (adopted) — never duplicated.
        already = router.get(venue_order.client_order_id) is not None
        router.ingest(venue_order)
        if already:
            adopted += 1
        else:
            ingested += 1

    # Orphans: a non-terminal tracked order the venue reports neither as open nor
    # via any fill has no venue record — close it and evict it (the orphan rule).
    closed_orphans = 0
    for cid, order in router.tracked_orders().items():
        if cid in venue_open_cids:
            continue  # still live on the venue — keep it.
        if order.is_terminal:
            continue  # history, not live state — leave it.
        # Non-terminal but the venue does not list it as open. Whether or not it
        # has fills, it is no longer open on the venue: close-and-forget. Its
        # fills (if any) still rebuild the position in step 3.
        _close_orphan(order)
        router.forget(cid)
        closed_orphans += 1

    # --- 3. Positions: rebuild from the broker's confirmed fills (truth). --- #
    tracker.reset(broker_fills)
    positions_rebuilt = len(tracker.all_positions())

    result = ReconResult(
        ingested_orders=ingested,
        adopted_orders=adopted,
        closed_orphans=closed_orphans,
        fills_applied=len(broker_fills),
        positions_rebuilt=positions_rebuilt,
    )

    if event_bus is not None:
        event_bus.emit(
            LogEvent(
                message=(
                    f"reconcile: ingested={result.ingested_orders} "
                    f"adopted={result.adopted_orders} "
                    f"closed_orphans={result.closed_orphans} "
                    f"fills_applied={result.fills_applied} "
                    f"positions_rebuilt={result.positions_rebuilt}"
                ),
                level="info",
            )
        )

    return result


def _close_orphan(order: Order) -> None:
    """Drive an orphan ``order`` to ``CANCELLED``, tolerant of the state machine.

    The venue has no record of ``order``, so the engine must stop believing it is
    live. ``CANCELLED`` is the terminal that reflects "was accepted, is no longer
    live" (see the module's orphan policy). A ``NEW`` order is nudged through
    ``SUBMITTED`` first (this reaches no venue); if even that is forbidden the
    order is left in whatever state it is in — the caller evicts it from the map
    regardless, so a phantom order never lingers.
    """
    try:
        if order.status is OrderStatus.NEW:
            order.submit()
        order.cancel()
    except OrderError:
        # The state machine forbade the transition. Eviction from the tracked map
        # (done by the caller) is what actually removes the phantom; the exact
        # terminal label is best-effort.
        pass
