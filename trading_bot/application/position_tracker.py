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

from collections.abc import Iterable

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
        # Running net position per instrument, advanced one fill at a time via
        # Position.with_fill (O(1) per fill — no full-history refold).
        self._positions: dict[Instrument, Position] = {}
        # Fill ids already folded — guards against a venue re-emitting the same
        # execution (e.g. a private-WS snapshot replay after a reconnect), which
        # would otherwise double-count the position. See :meth:`apply`.
        self._seen_fill_ids: set[str] = set()
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

        Advances the instrument's **running** :class:`Position` by exactly this
        fill via :meth:`~trading_bot.domain.position.Position.with_fill` — O(1) per
        fill, no full-history refold (the result is identical to
        :meth:`~trading_bot.domain.position.Position.from_fills` over every fill
        seen, which is how ``from_fills`` is itself defined).

        **Idempotent by ``fill_id``.** A fill whose ``fill_id`` was already folded
        is ignored (the standing position is returned unchanged) — so a venue
        re-emitting the same execution (e.g. a private-WS snapshot replay after a
        reconnect) never double-counts. A genuinely new execution must carry a new
        ``fill_id``.

        Parameters
        ----------
        fill : Fill
            A broker-confirmed execution (the PnL source of truth).

        Returns
        -------
        Position
            The instrument's net position after folding in ``fill`` (or unchanged
            if ``fill`` was a duplicate).

        """
        instrument = fill.instrument
        if fill.fill_id in self._seen_fill_ids:
            # Duplicate execution — never double-count. The instrument was already
            # seen (this id was folded into it), so its position is cached.
            return self._positions[instrument]
        self._seen_fill_ids.add(fill.fill_id)
        current = self._positions.get(instrument) or Position.flat(instrument)
        position = current.with_fill(fill)
        self._positions[instrument] = position
        return position

    def reset(self, fills: Iterable[Fill] = ()) -> None:
        """Discard all tracked fills and rebuild from ``fills`` (the new truth).

        Clears every per-instrument fill list and cached position, then folds
        ``fills`` in the order given — each fill routed to its instrument's
        bucket and that instrument's :class:`Position` recomputed via
        :meth:`apply`. The reconciliation path
        (:func:`~trading_bot.application.reconcile.reconcile`) calls this with
        the broker's confirmed :meth:`~trading_bot.brokers.base.Broker.fills`
        (the PnL source of truth) so local positions converge to **exactly**
        the venue's, with no double-counting of fills the tracker had already
        seen.

        Because the broker's fill stream is the de-duplicated source, the
        result is, by construction, ``Position.from_fills`` over the broker's
        fills per instrument — re-running with the same fills yields the same
        positions (idempotent rebuild).

        Parameters
        ----------
        fills : Iterable[Fill], optional
            The fills to rebuild from, **in execution order**. Defaults to
            empty, which clears the tracker to flat.

        """
        self._positions = {}
        self._seen_fill_ids = set()
        for fill in fills:
            self.apply(fill)

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
