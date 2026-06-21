"""The :class:`PaperBroker` — an in-process, deterministic fill simulator.

This is the **default** :class:`~trading_bot.brokers.base.Broker` adapter: it
implements the venue-neutral port entirely in memory so the whole engine runs
with **no venue, no API key and no network**. Where
:class:`~trading_bot.brokers.kraken.KrakenBroker` translates the port to a live
REST API, :class:`PaperBroker` *simulates* the venue — it accepts orders, fills
them against an injected mark/limit price, accrues a fee, mutates internal
balances and records :class:`~trading_bot.domain.fill.Fill`s — speaking
**domain types only**, with every amount an exact
:class:`~decimal.Decimal`.

Port purity
-----------
The broker is **port-pure**: :meth:`place_order` never mutates the caller's
:class:`~trading_bot.domain.order.Order` aggregate. Per the
:class:`~trading_bot.brokers.base.Broker` contract the *caller* (the
:class:`~trading_bot.application.order_router.OrderRouter`) owns the order's state
machine; the broker only (a) accepts an order, (b) returns a synthetic
``venue_order_id``, and (c) records/reports :class:`Fill`s via :meth:`fills`.

The simulation works purely off the order's *data* (side, qty, price, type) and
keeps its own per-venue-id bookkeeping (filled quantity + a reconstructed open
:class:`Order` snapshot). :meth:`open_orders` returns freshly reconstructed
domain orders (status, ``filled_qty``, ``avg_fill_price`` and ``venue_order_id``
populated from the simulator's view) — exactly as a real venue would, where the
caller's in-memory ``Order`` and the venue's record are distinct objects.

Optionally, if constructed with an :class:`~trading_bot.application.events.
EventBus`, the broker **emits** a
:class:`~trading_bot.application.events.FillEvent` for each simulated fill, so a
subscribed consumer (e.g. the ``PositionTracker``) sees executions without
polling :meth:`fills`. With no bus, fills are retrievable only via :meth:`fills`.

Determinism
-----------
The simulation is fully deterministic so tests assert exact values:

* **Synthetic order ids** are ``"PAPER-{n}"`` from a monotonic counter, so the
  *k*-th placed order always gets the same id within a run.
* **Fill ids** are ``"PAPER-FILL-{n}"`` from a second counter.
* **Timestamps** come from an injectable ``clock`` callable returning
  milliseconds since the Unix epoch (UTC). The default clock is *not* the wall
  clock — it returns a fixed base time and advances by one millisecond per call,
  so a run is reproducible without freezing real time.

Fill model
----------
Two fill models, selected at construction by ``fill_model``:

* ``"immediate"`` — an order is **fully** filled the moment it is placed, at its
  ``limit_price`` (LIMIT, or a priced BEST_LIMIT) or, for a MARKET order, at the
  injected mark price for its instrument. Exactly one :class:`Fill` is produced
  and ``open_orders`` is left empty.
* ``"partial"`` — the order is filled in ``partial_chunks`` equal slices (the
  last slice absorbs any rounding remainder so the slices sum **exactly** to the
  filled quantity). By default the whole quantity is consumed (the order closes
  to ``FILLED`` and ``open_orders`` is empty); set ``partial_fill_ratio`` below
  ``1`` to fill only that fraction on placement and leave the remainder *open*
  (so :meth:`cancel_order` has a live, partially-filled order to cancel).

The execution price is the same in both models (limit price, or the mark for a
MARKET order); only the *slicing* differs.

Fee model
---------
The fee for a slice of ``qty`` at ``price`` is, in quote units::

    fee = price * qty * fee_bps / 10000

i.e. ``fee_bps`` basis points of the slice notional (``10`` bps = 0.10% by
default). Fees are exact :class:`~decimal.Decimal`; they are *not* quantised
here (the simulation has no tick metadata) — callers wanting venue-tick rounding
quantise at the boundary.

Balances
--------
Balances start from ``starting_balances`` (canonical asset code -> Decimal) and
move on every fill, consistently with the fee model:

* a **BUY** debits the quote asset by ``price*qty + fee`` and credits the base
  asset by ``qty``;
* a **SELL** credits the quote asset by ``price*qty - fee`` and debits the base
  asset by ``qty``.

Balances may go negative — the paper broker does **not** enforce funding (it is a
simulator, not a risk gate); margin/funding checks live in the engine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING

from trading_bot.brokers.base import Broker, Capability
from trading_bot.domain.errors import BrokerError, MissingOrder
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderSide, OrderType

if TYPE_CHECKING:
    from trading_bot.application.events import EventBus

__all__ = ["PaperBroker"]

#: Basis-point denominator: ``fee = notional * fee_bps / _BPS_DENOMINATOR``.
_BPS_DENOMINATOR: Money = money("10000")

#: The default clock's base timestamp (ms since the Unix epoch, UTC):
#: 2024-01-01T00:00:00Z. The clock advances one millisecond per call.
_DEFAULT_CLOCK_BASE_MS = 1_704_067_200_000


def _default_clock() -> Callable[[], int]:
    """Build a deterministic clock: a fixed base time, +1ms per call."""
    ticker = count(_DEFAULT_CLOCK_BASE_MS)
    return lambda: next(ticker)


@dataclass(slots=True)
class _OpenOrder:
    """The simulator's private record of a still-live placed order.

    Holds only the order's *data* (never the caller's :class:`Order` object) plus
    the simulator's running fill state, so :meth:`PaperBroker.open_orders` can
    reconstruct a fresh domain :class:`Order` on demand — exactly as a real venue
    reports its own view, independent of the caller's in-memory order.
    """

    client_order_id: str
    instrument: Instrument
    side: OrderSide
    type: OrderType
    qty: Money
    limit_price: Money | None
    fill_tolerance: Money
    filled_qty: Money = field(default_factory=lambda: money("0"))
    avg_fill_price: Money | None = None


class PaperBroker(Broker):
    """In-process, deterministic :class:`Broker` that simulates fills.

    The default (paper-trading) broker: it serves the full :class:`Broker` port
    in memory, with no network and exact :class:`~decimal.Decimal` money. See the
    module docstring for the port-purity contract and the fill, fee and balance
    models.

    Parameters
    ----------
    prices : dict of Instrument to Decimal, optional
        Seed mark prices keyed by instrument; used to price MARKET orders and to
        answer :meth:`ticker`. Drive them at runtime with :meth:`set_price`.
        Copied defensively. Defaults to empty.
    fee_bps : Decimal, optional
        Fee in basis points of the fill notional. Defaults to ``money("10")``
        (10 bps = 0.10%). Must be non-negative.
    fill_model : {"immediate", "partial"}, optional
        How a placed order is filled. ``"immediate"`` (default) fully fills in
        one :class:`Fill`; ``"partial"`` slices into ``partial_chunks`` fills.
    starting_balances : dict of str to Decimal, optional
        Initial free balances by canonical asset code. Copied defensively.
        Defaults to empty. Balances may go negative (no funding gate).
    clock : callable, optional
        Zero-arg callable returning the current time as **milliseconds since the
        Unix epoch (UTC)**, stamped onto every :class:`Fill`. Defaults to a
        deterministic clock (fixed base, +1ms per call) so runs are reproducible.
    event_bus : EventBus, optional
        If given, the broker emits a
        :class:`~trading_bot.application.events.FillEvent` for every simulated
        fill, so subscribed consumers (e.g. the ``PositionTracker``) see
        executions without polling :meth:`fills`. Defaults to ``None`` (fills are
        retrievable only via :meth:`fills`).
    partial_chunks : int, optional
        Number of equal slices for ``fill_model="partial"``. Defaults to ``2``.
        Must be ``>= 1``.
    partial_fill_ratio : Decimal, optional
        Fraction of the order quantity consumed on placement under
        ``fill_model="partial"``. ``1`` (default) fully fills (order closes);
        a value in ``(0, 1)`` leaves the remainder *open*. Must be in ``(0, 1]``.

    Attributes
    ----------
    name : str
        The venue key, ``"paper"`` (the registry key for this adapter).

    """

    name = "paper"

    def __init__(
        self,
        *,
        prices: dict[Instrument, Money] | None = None,
        fee_bps: Money = money("10"),
        fill_model: str = "immediate",
        starting_balances: dict[str, Money] | None = None,
        clock: Callable[[], int] | None = None,
        event_bus: EventBus | None = None,
        partial_chunks: int = 2,
        partial_fill_ratio: Money = money("1"),
    ) -> None:
        if fill_model not in ("immediate", "partial"):
            raise BrokerError(
                f"unknown fill_model {fill_model!r}; "
                "expected 'immediate' or 'partial'"
            )
        if fee_bps < 0:
            raise BrokerError(f"fee_bps must be non-negative, got {fee_bps}")
        if partial_chunks < 1:
            raise BrokerError(
                f"partial_chunks must be >= 1, got {partial_chunks}"
            )
        if not (0 < partial_fill_ratio <= 1):
            raise BrokerError(
                f"partial_fill_ratio must be in (0, 1], got {partial_fill_ratio}"
            )

        self._prices: dict[Instrument, Money] = dict(prices or {})
        self._fee_bps = fee_bps
        self._fill_model = fill_model
        self._balances: dict[str, Money] = dict(starting_balances or {})
        self._clock = clock if clock is not None else _default_clock()
        self._bus = event_bus
        self._partial_chunks = partial_chunks
        self._partial_fill_ratio = partial_fill_ratio

        # Deterministic id seams.
        self._order_ids = count(1)
        self._fill_ids = count(1)
        # Live orders keyed by their synthetic venue id — the simulator's *own*
        # record (never the caller's Order); see ``_OpenOrder``.
        self._open: dict[str, _OpenOrder] = {}
        # Every fill ever produced, in execution order.
        self._fills: list[Fill] = []
        # One-shot override of the placement fill ratio (see ``arm_partial``),
        # consumed by the next ``place_order``. ``None`` means "use the model".
        self._armed_ratio: Money | None = None

    # --- capability declaration -------------------------------------------- #

    def capabilities(self) -> set[Capability]:
        """The :class:`Capability` set this adapter serves.

        All six in-process operations are implemented (place/cancel/open-orders,
        balances, fills, ticker). There is no private WebSocket feed for a
        simulator, so :data:`~trading_bot.brokers.base.Capability.PRIVATE_WS` is
        omitted.
        """
        return {
            Capability.PLACE_ORDER,
            Capability.CANCEL,
            Capability.OPEN_ORDERS,
            Capability.BALANCES,
            Capability.FILLS,
            Capability.TICKER,
        }

    # --- price hooks ------------------------------------------------------- #

    def set_price(self, instrument: Instrument, price: Money) -> None:
        """Set the mark price for ``instrument`` (drives MARKET fills & ticker).

        Parameters
        ----------
        instrument : Instrument
            The instrument to mark.
        price : Decimal
            Its mark price in quote units. Must be strictly positive.

        Raises
        ------
        BrokerError
            If ``price`` is not strictly positive.

        """
        if price <= 0:
            raise BrokerError(f"mark price must be positive, got {price}")
        self._prices[instrument] = price

    def arm_partial(self, ratio: Money) -> None:
        """Arm the **next** :meth:`place_order` to fill only ``ratio`` of qty.

        A one-shot driver seam (reset after the next placement) for simulating a
        single partial fill regardless of the broker's ``fill_model`` — the
        placed order fills ``ratio * qty`` (sliced per ``partial_chunks``) and
        keeps the remainder *open*. Lets a realistic mixed sequence (full buy ->
        partial -> sell) run against one balance-threaded broker.

        Parameters
        ----------
        ratio : Decimal
            Fraction of the next order's quantity to fill on placement. Must be
            in ``(0, 1)`` — use the normal models for a full fill.

        Raises
        ------
        BrokerError
            If ``ratio`` is not strictly inside ``(0, 1)``.

        """
        if not (0 < ratio < 1):
            raise BrokerError(
                f"arm_partial ratio must be in (0, 1), got {ratio}"
            )
        self._armed_ratio = ratio

    # --- order lifecycle --------------------------------------------------- #

    async def place_order(self, order: Order) -> str:
        """Submit ``order`` to the simulator and return its synthetic venue id.

        **Port-pure**: the caller's ``order`` object is *never mutated* — its
        status, ``filled_qty`` and ``venue_order_id`` are untouched. The simulator
        reads only the order's *data* (side, qty, price, type), allocates a fresh
        ``"PAPER-{n}"`` id, simulates fills per the configured fill model (see the
        module docstring) into its own per-venue-id record, records each
        :class:`Fill`, and moves internal balances. Any unfilled remainder is kept
        live (a reconstructed snapshot) and surfaced by :meth:`open_orders`.

        Parameters
        ----------
        order : Order
            The domain order to simulate. Read-only here: the caller owns and
            drives its lifecycle; this broker does not touch it.

        Returns
        -------
        str
            The synthetic venue order id (``"PAPER-{n}"``).

        Raises
        ------
        BrokerError
            If a MARKET (or unpriced BEST_LIMIT) order has no mark price for its
            instrument.

        """
        venue_order_id = f"PAPER-{next(self._order_ids)}"
        record = _OpenOrder(
            client_order_id=order.client_order_id,
            instrument=order.instrument,
            side=order.side,
            type=order.type,
            qty=order.qty,
            limit_price=order.limit_price,
            fill_tolerance=order.fill_tolerance,
        )

        # A one-shot armed ratio (if any) wins over the model; reset it now so
        # it only affects this placement.
        armed = self._armed_ratio
        self._armed_ratio = None

        price = self._execution_price(order)
        fill_qty = self._placement_fill_qty(order.qty, armed)
        for slice_qty in self._slice(fill_qty):
            self._execute(record, slice_qty, price)

        # Keep the order live only if a quantity remains unfilled (i.e. the
        # simulated fills did not fully consume it within tolerance).
        if not self._is_terminal(record):
            self._open[venue_order_id] = record
        return venue_order_id

    def _execution_price(self, order: Order) -> Money:
        """Resolve the price an order fills at (limit price, or the mark).

        A LIMIT (or priced BEST_LIMIT) fills at its ``limit_price``; a MARKET
        order (or an unpriced BEST_LIMIT) fills at the injected mark price.
        """
        if order.limit_price is not None:
            return order.limit_price
        # MARKET / unpriced BEST_LIMIT: take the injected mark.
        price = self._prices.get(order.instrument)
        if price is None:
            raise BrokerError(
                f"no mark price for {order.instrument} to fill "
                f"{order.type.name} order {order.client_order_id}"
            )
        return price

    def _placement_fill_qty(self, qty: Money, armed: Money | None) -> Money:
        """The quantity filled on placement (the whole qty, or a partial slice).

        A one-shot ``armed`` ratio (from :meth:`arm_partial`) wins if present;
        otherwise ``"immediate"`` and a unit ``partial_fill_ratio`` fill the
        whole ``qty`` while a sub-unit ratio under ``"partial"`` fills only that
        fraction.
        """
        if armed is not None:
            return qty * armed
        if self._fill_model == "partial" and self._partial_fill_ratio < 1:
            return qty * self._partial_fill_ratio
        return qty

    def _slice(self, qty: Money) -> list[Money]:
        """Split ``qty`` into the fill slices for the configured model.

        ``"immediate"`` yields a single slice; ``"partial"`` yields
        ``partial_chunks`` slices whose sum is exactly ``qty`` (the last slice
        absorbs any division remainder).
        """
        if self._fill_model == "immediate" or self._partial_chunks == 1:
            return [qty]
        n = self._partial_chunks
        base = qty / n
        slices = [base for _ in range(n - 1)]
        slices.append(qty - base * (n - 1))  # last absorbs the remainder
        return slices

    def _execute(self, record: _OpenOrder, qty: Money, price: Money) -> None:
        """Record one fill of ``qty`` at ``price``, moving balances & sim state.

        Updates only the simulator's *own* ``record`` (never the caller's
        ``Order``): accrues ``filled_qty`` and the quantity-weighted
        ``avg_fill_price``, appends the :class:`Fill`, moves balances and — if an
        :class:`~trading_bot.application.events.EventBus` was injected — emits a
        :class:`~trading_bot.application.events.FillEvent`.
        """
        fee = self._fee(qty, price)
        fill = Fill(
            fill_id=f"PAPER-FILL-{next(self._fill_ids)}",
            client_order_id=record.client_order_id,
            instrument=record.instrument,
            side=record.side,
            qty=qty,
            price=price,
            fee=fee,
            ts=self._clock(),
        )
        self._fills.append(fill)

        # Fold the fill into the simulator's running record (quantity-weighted
        # average across this order's fills so far).
        prior_notional = (
            record.avg_fill_price * record.filled_qty
            if record.avg_fill_price is not None
            else money("0")
        )
        new_filled = record.filled_qty + qty
        record.avg_fill_price = (prior_notional + qty * price) / new_filled
        record.filled_qty = new_filled

        self._apply_to_balances(fill)
        if self._bus is not None:
            from trading_bot.application.events import FillEvent

            self._bus.emit(FillEvent(fill))

    def _is_terminal(self, record: _OpenOrder) -> bool:
        """Whether ``record`` is fully filled within its order's tolerance.

        Mirrors :meth:`~trading_bot.domain.order.Order._is_filled_within_tolerance`
        on the simulator's own bookkeeping so :meth:`open_orders` reconstructs the
        exact status (``FILLED`` orders are dropped; the rest stay live).
        """
        if record.filled_qty >= record.qty:
            return True
        unfilled = (record.qty - record.filled_qty) / record.qty
        return unfilled < record.fill_tolerance

    def _fee(self, qty: Money, price: Money) -> Money:
        """Fee for a slice: ``price * qty * fee_bps / 10000`` in quote units."""
        return price * qty * self._fee_bps / _BPS_DENOMINATOR

    def _apply_to_balances(self, fill: Fill) -> None:
        """Move base/quote balances for ``fill`` (see the module balance model)."""
        base = fill.instrument.symbol.base
        quote = fill.instrument.symbol.quote
        notional = fill.price * fill.qty
        if fill.side is OrderSide.BUY:
            # Pay quote (notional + fee), receive base.
            self._balances[quote] = self._balance(quote) - notional - fill.fee
            self._balances[base] = self._balance(base) + fill.qty
        else:
            # Deliver base, receive quote (notional - fee).
            self._balances[base] = self._balance(base) - fill.qty
            self._balances[quote] = self._balance(quote) + notional - fill.fee

    def _balance(self, asset: str) -> Money:
        """Current balance of ``asset``, defaulting to zero if untracked."""
        return self._balances.get(asset, money("0"))

    async def cancel_order(self, venue_order_id: str) -> None:
        """Cancel the live (open / partially-filled) order ``venue_order_id``.

        Drops the simulator's own record for that id. **Port-pure**: it does not
        touch any caller :class:`Order` — the caller drives its order's
        ``CANCELLED`` transition itself.

        Parameters
        ----------
        venue_order_id : str
            The synthetic id returned by :meth:`place_order`.

        Raises
        ------
        MissingOrder
            If no live order is tracked under ``venue_order_id`` (already filled,
            already cancelled, or never placed).

        """
        record = self._open.pop(venue_order_id, None)
        if record is None:
            raise MissingOrder(venue_order_id)

    async def open_orders(self) -> list[Order]:
        """Return the still-live (open / partially-filled) orders.

        Each is a **freshly reconstructed** domain :class:`Order` (the caller's
        original object is never stored), with status, ``filled_qty``,
        ``avg_fill_price`` and ``venue_order_id`` populated from the simulator's
        view — exactly as a real venue reports its own record.

        Returns
        -------
        list of Order
            The live domain orders, in placement order.

        """
        return [
            self._reconstruct(venue_order_id, record)
            for venue_order_id, record in self._open.items()
        ]

    def _reconstruct(self, venue_order_id: str, record: _OpenOrder) -> Order:
        """Build a domain :class:`Order` mirroring the simulator's live record.

        Drives a fresh ``Order`` through ``submit -> open -> apply_fill`` so its
        status and fields match what the simulator recorded — the venue's view of
        a still-live (partially-filled) order, distinct from the caller's object.
        """
        order = Order(
            client_order_id=record.client_order_id,
            instrument=record.instrument,
            side=record.side,
            qty=record.qty,
            type=record.type,
            limit_price=record.limit_price,
            fill_tolerance=record.fill_tolerance,
        )
        order.submit()
        order.open(venue_order_id)
        if record.filled_qty > 0 and record.avg_fill_price is not None:
            # One aggregate fill at the running average reproduces the recorded
            # filled_qty / avg_fill_price exactly (a still-open order is never
            # terminal, so apply_fill leaves it PARTIALLY_FILLED).
            order.apply_fill(record.filled_qty, record.avg_fill_price)
        return order

    async def balances(self) -> dict[str, Money]:
        """Return free balances keyed by canonical asset code.

        Returns
        -------
        dict of str to Decimal
            A copy of the current per-asset balances (exact ``Decimal``).

        """
        return dict(self._balances)

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        """Return recorded fills, optionally only those at/after ``since_ms``.

        Parameters
        ----------
        since_ms : int, optional
            Lower time bound as **milliseconds since the Unix epoch (UTC)**,
            inclusive. ``None`` (default) returns every recorded fill.

        Returns
        -------
        list of Fill
            The matching fills, in execution order.

        """
        if since_ms is None:
            return list(self._fills)
        return [f for f in self._fills if f.ts >= since_ms]

    async def ticker(self, instrument: Instrument) -> Money:
        """Return the injected mark price for ``instrument``.

        Parameters
        ----------
        instrument : Instrument
            The instrument to price.

        Returns
        -------
        Decimal
            Its injected mark price, exact.

        Raises
        ------
        BrokerError
            If no price has been injected for ``instrument`` (via the constructor
            ``prices`` or :meth:`set_price`).

        """
        price = self._prices.get(instrument)
        if price is None:
            raise BrokerError(f"no ticker price for {instrument}")
        return price
