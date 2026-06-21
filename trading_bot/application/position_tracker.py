"""The :class:`PositionTracker` — live net positions from confirmed fills.

The tracker is the **read-back path** of execution and the engine's owner of
live exposure: where the :class:`~trading_bot.application.order_router.OrderRouter`
turns intent into venue orders (the write path), the tracker folds the venue's
**confirmed fills** — the PnL source of truth — into a net
:class:`~trading_bot.domain.position.Position` *per instrument*. It is the single
place that answers "what do we hold, and what PnL have we realised?".

Fill ingestion — the boundary (carried into the ADR)
----------------------------------------------------
Fills reach the tracker one of two ways, and both land in the same
:meth:`apply`:

* **Subscribed** — constructed with an
  :class:`~trading_bot.application.events.EventBus`, the tracker subscribes to
  :class:`~trading_bot.application.events.FillEvent` and ``apply``\\ s each one
  automatically. The :class:`~trading_bot.brokers.paper.PaperBroker` (and, later,
  a live broker's private fill stream) **emits** ``FillEvent``\\ s, so the order ->
  fill -> position flow is wired end to end with no polling: the router submits,
  the broker confirms fills onto the bus, the tracker updates. This is the clean
  boundary — the router never needs to know about PnL, and the tracker never
  needs to know how an order was submitted.
* **Explicit** — a caller that drains :meth:`~trading_bot.brokers.base.Broker.fills`
  itself (e.g. a startup reconciliation pass) feeds each fill to :meth:`apply`
  directly. No bus required.

Computation — delegates to :meth:`Position.from_fills`
------------------------------------------------------
The tracker keeps an **ordered fill list per instrument** and recomputes that
instrument's :class:`Position` via
:meth:`~trading_bot.domain.position.Position.from_fills` on every fill. Position
math (increases, partial closes, flips, fee accrual, the PnL sign convention)
lives entirely in the pure :class:`Position`; the tracker adds no money logic of
its own — it only routes fills to the right per-instrument bucket and caches the
folded snapshot. This keeps the single source of PnL truth in one tested place
and makes the tracker trivially correct: ``position(inst)`` is *by construction*
``Position.from_fills`` over exactly the fills seen for ``inst``, in arrival
order.

The module is part of the application layer: it imports the pure domain and the
event bus, holds money as :class:`~decimal.Decimal` end to end, and is
deterministic in fill order.
"""

from __future__ import annotations

from trading_bot.application.events import Event, EventBus, FillEvent
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.position import Position

__all__ = ["PositionTracker"]


class PositionTracker:
    """Live net :class:`Position` per instrument, folded from confirmed fills.

    Construct it bare (and call :meth:`apply` for each fill) or with an
    :class:`~trading_bot.application.events.EventBus` (then it subscribes to
    :class:`~trading_bot.application.events.FillEvent` and applies fills
    automatically). Read the live exposure with :meth:`position` /
    :meth:`all_positions`.

    Parameters
    ----------
    event_bus : EventBus, optional
        If given, the tracker subscribes to it and applies every
        :class:`~trading_bot.application.events.FillEvent` as it is emitted. With
        ``None`` (the default) the tracker is driven only by explicit
        :meth:`apply` calls.

    Examples
    --------
    >>> from trading_bot.domain.fill import Fill
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> from trading_bot.domain.order import OrderSide
    >>> inst = Instrument(Symbol("BTC", "USD"))
    >>> tracker = PositionTracker()
    >>> tracker.apply(
    ...     Fill("T1", "cid-1", inst, OrderSide.BUY, money("2"), money("30000"),
    ...          money("0"), 1)
    ... )
    >>> tracker.position(inst).net_qty
    Decimal('2')

    """

    def __init__(self, event_bus: EventBus | None = None) -> None:
        # Ordered fills per instrument (arrival order == fold order) and the
        # cached snapshot recomputed on each new fill.
        self._fills: dict[Instrument, list[Fill]] = {}
        self._positions: dict[Instrument, Position] = {}
        self._bus = event_bus
        if event_bus is not None:
            event_bus.subscribe(self._on_event)

    def _on_event(self, event: Event) -> None:
        """Bus handler: apply the fill of a :class:`FillEvent`, ignore the rest.

        Subscribed to the :class:`~trading_bot.application.events.EventBus`, which
        fans out every event type; the tracker only cares about
        :class:`~trading_bot.application.events.FillEvent`.
        """
        if isinstance(event, FillEvent):
            self.apply(event.fill)

    def apply(self, fill: Fill) -> Position:
        """Fold ``fill`` into its instrument's running net position.

        Appends the fill to that instrument's ordered list and recomputes the
        instrument's :class:`Position` via
        :meth:`~trading_bot.domain.position.Position.from_fills` over the full
        sequence seen so far (arrival order). Idempotent only in the trivial
        sense that re-applying a *new* ``Fill`` object always reflects it — the
        tracker does **not** dedup by ``fill_id``; the broker's fill stream is the
        de-duplicated source.

        Parameters
        ----------
        fill : Fill
            A broker-confirmed execution (the PnL source of truth).

        Returns
        -------
        Position
            The instrument's net position after folding in ``fill``.

        """
        instrument = fill.instrument
        fills = self._fills.setdefault(instrument, [])
        fills.append(fill)
        position = Position.from_fills(fills)
        self._positions[instrument] = position
        return position

    def position(self, instrument: Instrument) -> Position | None:
        """Return the live net :class:`Position` for ``instrument``, or ``None``.

        Parameters
        ----------
        instrument : Instrument
            The instrument to read.

        Returns
        -------
        Position or None
            The folded position, or ``None`` if no fill for ``instrument`` has
            been applied yet.

        """
        return self._positions.get(instrument)

    def all_positions(self) -> dict[Instrument, Position]:
        """Return a snapshot of every tracked instrument's net position.

        Returns
        -------
        dict of Instrument to Position
            A copy of the per-instrument position map (the mapping is fresh; the
            :class:`Position` values are immutable and shared).

        """
        return dict(self._positions)
