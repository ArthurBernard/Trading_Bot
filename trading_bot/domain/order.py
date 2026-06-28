"""The :class:`Order` aggregate and its explicit lifecycle state machine.

This module replaces the legacy ad-hoc status ``{None, 'open', 'canceled',
'closed'}`` (see ``trading_bot/legacy/orders.py``) with a typed, validated
state machine. An :class:`Order` is a **stateful aggregate**: it has identity
(its mandatory ``client_order_id``) and mutates through explicit, guarded
transitions — :meth:`Order.submit`, :meth:`Order.open`,
:meth:`Order.apply_fill`, :meth:`Order.cancel`, :meth:`Order.reject`. Any
transition the machine forbids raises :class:`~trading_bot.domain.errors.
OrderStatusError`.

Design choices (carried into the ADR):

* **Mutable, not immutable-with-copy.** An order has a stable identity and a
  long, fill-by-fill life; threading a fresh copy through every partial fill
  would add noise without buying safety. State only ever changes via the five
  guarded methods, never by reaching into the fields, so the machine stays the
  single source of truth.
* **Tolerance rule (ported from legacy ``check_vol_exec``).** The default
  tolerance is ``0.1%`` (legacy ``tol=0.001``). After a fill, if the *unfilled*
  fraction ``(qty - filled_qty) / qty`` is strictly below ``tol``, the order is
  treated as fully :data:`OrderStatus.FILLED` even though a dust amount is
  technically outstanding — venues routinely leave sub-tick remainders. An
  exact fill (``filled_qty == qty``) always closes to ``FILLED``. Over-filling
  (``filled_qty > qty``) is rejected with
  :class:`~trading_bot.domain.errors.OrderError`.
* **Order-type price invariants.** ``MARKET`` forbids both prices. ``LIMIT``
  requires ``limit_price`` and forbids ``stop_price``. ``STOP_LOSS`` requires
  ``stop_price`` and forbids ``limit_price`` (a stop *limit* is out of scope
  here). ``BEST_LIMIT`` is a limit whose price is discovered at runtime, so
  ``limit_price`` is *optional* at construction; ``stop_price`` is forbidden.

The module is pure: no I/O, no async, money as :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from trading_bot.domain.errors import OrderError, OrderStatusError
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money

__all__ = [
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "Order",
    "DEFAULT_FILL_TOLERANCE",
]

#: Default unfilled-fraction tolerance below which an order is treated as fully
#: filled. Ported from the legacy ``_BasisOrder.tol`` default of ``0.001``
#: (0.1%). See :meth:`Order.apply_fill`.
DEFAULT_FILL_TOLERANCE: Money = money("0.001")


class OrderSide(Enum):
    """Which way the order trades."""

    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """How the order is priced / routed."""

    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    BEST_LIMIT = "best_limit"


class OrderStatus(Enum):
    """The order's position in its lifecycle.

    The legal flow is::

        NEW -> SUBMITTED -> OPEN -> PARTIALLY_FILLED -> FILLED
                  |          |            |
                  v          v            v
               REJECTED   CANCELLED    CANCELLED

    ``FILLED``, ``CANCELLED`` and ``REJECTED`` are terminal.
    """

    NEW = "new"
    SUBMITTED = "submitted"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# Allowed status transitions. A status missing from a value set forbids that
# move; ``apply_fill`` resolves its own PARTIALLY_FILLED-vs-FILLED target and is
# only gated on the *source* status here.
_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.SUBMITTED}),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.OPEN, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

# Statuses from which a fill may be applied (the order is live on the venue).
_FILLABLE: frozenset[OrderStatus] = frozenset(
    {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
)


@dataclass(slots=True)
class Order:
    """A trading order with an explicit, guarded lifecycle.

    The order is a *stateful aggregate*: create it, then drive it through its
    lifecycle with :meth:`submit`, :meth:`open`, :meth:`apply_fill`,
    :meth:`cancel` and :meth:`reject`. Status, fills and the average fill price
    only ever change through those methods.

    Parameters
    ----------
    client_order_id : str
        Caller-assigned idempotency key. Mandatory: it is the order's identity
        and lets a retry be recognised as the same order. Must be non-empty.
    instrument : Instrument
        The instrument being traded.
    side : OrderSide
        ``BUY`` or ``SELL``.
    qty : Decimal
        The order's total quantity. Must be strictly positive.
    type : OrderType
        Pricing / routing type (see :class:`OrderType`).
    limit_price : Decimal, optional
        Required for ``LIMIT``; optional for ``BEST_LIMIT`` (discovered at
        runtime); forbidden for ``MARKET`` and ``STOP_LOSS``.
    stop_price : Decimal, optional
        Required for ``STOP_LOSS``; forbidden otherwise.
    fill_tolerance : Decimal, optional
        Unfilled-fraction threshold below which the order closes to ``FILLED``.
        Defaults to :data:`DEFAULT_FILL_TOLERANCE` (0.1%).

    Attributes
    ----------
    filled_qty : Decimal
        Cumulative executed quantity. Starts at ``0``.
    avg_fill_price : Decimal or None
        Quantity-weighted average price across all fills, exact in
        :class:`~decimal.Decimal`. ``None`` until the first fill.
    status : OrderStatus
        Current lifecycle status. Starts at :data:`OrderStatus.NEW`.
    venue_order_id : str or None
        The venue's order id, set by :meth:`open`. ``None`` until then.
    reject_reason : str or None
        Why the order was rejected, set by :meth:`reject`. ``None`` otherwise.

    Examples
    --------
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> o = Order(
    ...     client_order_id="cid-1",
    ...     instrument=Instrument(Symbol("BTC", "USD")),
    ...     side=OrderSide.BUY,
    ...     qty=money("2"),
    ...     type=OrderType.LIMIT,
    ...     limit_price=money("30000"),
    ... )
    >>> o.submit(); o.open("VID-1")
    >>> o.apply_fill(money("1"), money("30000"))
    >>> o.status
    <OrderStatus.PARTIALLY_FILLED: 'partially_filled'>
    >>> o.apply_fill(money("1"), money("30100"))
    >>> o.status, o.avg_fill_price
    (<OrderStatus.FILLED: 'filled'>, Decimal('30050'))

    """

    client_order_id: str
    instrument: Instrument
    side: OrderSide
    qty: Money
    type: OrderType
    limit_price: Money | None = None
    stop_price: Money | None = None
    fill_tolerance: Money = DEFAULT_FILL_TOLERANCE

    filled_qty: Money = field(default_factory=lambda: money("0"))
    avg_fill_price: Money | None = None
    status: OrderStatus = OrderStatus.NEW
    venue_order_id: str | None = None
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate construction invariants (identity, qty, price/type rules)."""
        if not self.client_order_id:
            raise OrderError(
                self.client_order_id, "client_order_id is mandatory and non-empty"
            )
        if self.qty <= 0:
            raise OrderError(
                self.client_order_id, f"qty must be positive, got {self.qty}"
            )
        if self.fill_tolerance < 0:
            raise OrderError(
                self.client_order_id,
                f"fill_tolerance must be non-negative, got {self.fill_tolerance}",
            )
        self._validate_prices()

    def _validate_prices(self) -> None:
        """Enforce the per-:class:`OrderType` price invariants."""
        otype = self.type
        if otype is OrderType.MARKET:
            if self.limit_price is not None or self.stop_price is not None:
                raise OrderError(
                    self.client_order_id,
                    "MARKET order forbids limit_price and stop_price",
                )
        elif otype is OrderType.LIMIT:
            if self.limit_price is None:
                raise OrderError(
                    self.client_order_id, "LIMIT order requires limit_price"
                )
            if self.stop_price is not None:
                raise OrderError(
                    self.client_order_id, "LIMIT order forbids stop_price"
                )
        elif otype is OrderType.STOP_LOSS:
            if self.stop_price is None:
                raise OrderError(
                    self.client_order_id, "STOP_LOSS order requires stop_price"
                )
            if self.limit_price is not None:
                raise OrderError(
                    self.client_order_id, "STOP_LOSS order forbids limit_price"
                )
        else:  # OrderType.BEST_LIMIT
            # BEST_LIMIT discovers its price at runtime, so limit_price is
            # optional; a stop price is meaningless for it.
            if self.stop_price is not None:
                raise OrderError(
                    self.client_order_id, "BEST_LIMIT order forbids stop_price"
                )

    # --- lifecycle transitions --------------------------------------------- #

    def _require_transition(self, target: OrderStatus, action: str) -> None:
        """Raise :class:`OrderStatusError` if ``target`` is not reachable now."""
        if target not in _TRANSITIONS[self.status]:
            raise OrderStatusError(self.client_order_id, self.status.value, action)

    def submit(self) -> None:
        """Mark the order as sent to the venue (``NEW -> SUBMITTED``)."""
        self._require_transition(OrderStatus.SUBMITTED, "submit")
        self.status = OrderStatus.SUBMITTED

    def open(self, venue_order_id: str) -> None:
        """Acknowledge the venue accepted the order (``SUBMITTED -> OPEN``).

        Parameters
        ----------
        venue_order_id : str
            The venue's identifier for the live order. Must be non-empty.

        """
        self._require_transition(OrderStatus.OPEN, "open")
        if not venue_order_id:
            raise OrderError(
                self.client_order_id, "venue_order_id must be non-empty to open"
            )
        self.venue_order_id = venue_order_id
        self.status = OrderStatus.OPEN

    def apply_fill(self, qty: Money, price: Money) -> None:
        """Apply an execution of ``qty`` at ``price``, updating the average.

        Accumulates :attr:`filled_qty` and recomputes :attr:`avg_fill_price` as
        the exact quantity-weighted average across all fills so far. The status
        moves to :data:`OrderStatus.PARTIALLY_FILLED`, or to
        :data:`OrderStatus.FILLED` once the order is filled within tolerance
        (see :data:`DEFAULT_FILL_TOLERANCE`).

        Parameters
        ----------
        qty : Decimal
            The executed quantity for this fill. Must be strictly positive.
        price : Decimal
            The execution price for this fill. Must be strictly positive.

        Raises
        ------
        OrderStatusError
            If the order is not live (must be ``OPEN`` or ``PARTIALLY_FILLED``).
        OrderError
            If ``qty``/``price`` are not positive, or the fill would push
            :attr:`filled_qty` beyond :attr:`qty` (over-fill).

        """
        if self.status not in _FILLABLE:
            raise OrderStatusError(
                self.client_order_id, self.status.value, "apply_fill"
            )
        if qty <= 0:
            raise OrderError(
                self.client_order_id, f"fill qty must be positive, got {qty}"
            )
        if price <= 0:
            raise OrderError(
                self.client_order_id, f"fill price must be positive, got {price}"
            )

        new_filled = self.filled_qty + qty
        if new_filled > self.qty:
            raise OrderError(
                self.client_order_id,
                f"over-fill: {new_filled} exceeds order qty {self.qty}",
            )

        # Exact quantity-weighted average: (sum of qty*price) / sum of qty.
        prior_notional = (
            self.avg_fill_price * self.filled_qty
            if self.avg_fill_price is not None
            else money("0")
        )
        notional = prior_notional + qty * price
        self.filled_qty = new_filled
        self.avg_fill_price = notional / new_filled

        if self._is_filled_within_tolerance():
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED

    def _is_filled_within_tolerance(self) -> bool:
        """Whether the unfilled fraction is within :attr:`fill_tolerance`.

        Ported from legacy ``check_vol_exec``: an exact fill always counts; a
        residual unfilled fraction strictly below ``fill_tolerance`` is treated
        as a full fill (venues leave sub-tick dust).
        """
        if self.filled_qty >= self.qty:
            return True
        unfilled_fraction = (self.qty - self.filled_qty) / self.qty
        return unfilled_fraction < self.fill_tolerance

    def cancel(self) -> None:
        """Cancel a live order (from ``SUBMITTED``, ``OPEN`` or partially filled).

        Raises
        ------
        OrderStatusError
            If the current status forbids cancellation (terminal or ``NEW``).

        """
        self._require_transition(OrderStatus.CANCELLED, "cancel")
        self.status = OrderStatus.CANCELLED

    def reject(self, reason: str) -> None:
        """Reject the order (``SUBMITTED -> REJECTED``), recording ``reason``.

        Parameters
        ----------
        reason : str
            Human-readable rejection reason (e.g. the venue error). Stored on
            :attr:`reject_reason`.

        Raises
        ------
        OrderStatusError
            If the current status forbids rejection.

        """
        self._require_transition(OrderStatus.REJECTED, "reject")
        self.reject_reason = reason
        self.status = OrderStatus.REJECTED

    # --- derived views ----------------------------------------------------- #

    @property
    def remaining_qty(self) -> Money:
        """Quantity not yet filled (``qty - filled_qty``, never negative)."""
        remaining = self.qty - self.filled_qty
        return remaining if remaining > 0 else money("0")

    @property
    def is_terminal(self) -> bool:
        """Whether the order has reached a terminal status (no transitions left)."""
        return not _TRANSITIONS[self.status]
