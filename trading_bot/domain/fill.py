"""The :class:`Fill` value object — a broker-confirmed execution.

A :class:`Fill` is the **source of truth for PnL**. Where an
:class:`~trading_bot.domain.order.Order` is a stateful aggregate that *intends*
to trade, a fill is an immutable record that a venue *did* trade a slice of it:
``qty`` of an instrument at ``price``, costing ``fee``, on a given ``side``.
Positions, realised PnL and fees are all rebuilt by folding an ordered sequence
of fills (see :class:`~trading_bot.domain.position.Position`).

Design choices (carried into the ADR / changelog):

* **Immutable.** A fill is a fact, not a state machine — once the venue
  confirms it, it never changes. The dataclass is ``frozen=True`` so a fill is
  hashable and safe to share / store / replay.
* **All amounts are :class:`~decimal.Decimal`.** ``qty``, ``price`` and ``fee``
  never round-trip through ``float``.
* **``ts`` is an ``int`` of milliseconds since the Unix epoch (UTC).** This is
  the simplest fully-typed, timezone-free choice: it sorts and compares as a
  plain integer, needs no ``datetime``/``tzinfo`` plumbing in the pure layer,
  and matches the millisecond granularity Kraken reports. Callers that hold a
  ``datetime`` convert at the boundary (``int(dt.timestamp() * 1000)``).
* **``side`` reuses :class:`~trading_bot.domain.order.OrderSide`.** A fill is
  ``BUY`` (adds to a long / reduces a short) or ``SELL`` (the inverse); there is
  no separate fill-side enum.

The module is pure: no I/O, no async, money as :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_bot.domain.errors import OrderError
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money
from trading_bot.domain.order import OrderSide

__all__ = [
    "Fill",
]


@dataclass(frozen=True, slots=True)
class Fill:
    """A single broker-confirmed execution — immutable, the PnL source of truth.

    Parameters
    ----------
    fill_id : str
        The venue's identifier for this execution (Kraken trade id). Mandatory
        and non-empty: it is the fill's identity and lets a replayed fill be
        recognised as the same one.
    client_order_id : str
        The caller-assigned id of the order this fill belongs to. Mandatory and
        non-empty. Ties the execution back to its originating order.
    instrument : Instrument
        The instrument that was traded.
    side : OrderSide
        ``BUY`` or ``SELL`` — the direction of the execution.
    qty : Decimal
        The executed quantity (in base units). Must be strictly positive.
    price : Decimal
        The execution price (in quote units per base unit). Must be strictly
        positive.
    fee : Decimal
        The fee charged for this execution, in quote units. Must be
        non-negative (zero is allowed for rebated / fee-free fills).
    ts : int
        Execution timestamp as **milliseconds since the Unix epoch (UTC)**. Must
        be non-negative. Fills are folded in caller-supplied order; ``ts`` is
        carried for record-keeping and tie-breaking, not used to re-sort.

    Examples
    --------
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> Fill(
    ...     fill_id="T1",
    ...     client_order_id="cid-1",
    ...     instrument=Instrument(Symbol("BTC", "USD")),
    ...     side=OrderSide.BUY,
    ...     qty=money("1"),
    ...     price=money("30000"),
    ...     fee=money("12"),
    ...     ts=1_700_000_000_000,
    ... ).qty
    Decimal('1')

    """

    fill_id: str
    client_order_id: str
    instrument: Instrument
    side: OrderSide
    qty: Money
    price: Money
    fee: Money
    ts: int

    def __post_init__(self) -> None:
        """Validate construction invariants (ids non-empty, amounts in range)."""
        if not self.fill_id:
            raise OrderError(
                self.client_order_id, "fill_id is mandatory and non-empty"
            )
        if not self.client_order_id:
            raise OrderError(
                self.client_order_id, "client_order_id is mandatory and non-empty"
            )
        if self.qty <= 0:
            raise OrderError(
                self.client_order_id, f"fill qty must be positive, got {self.qty}"
            )
        if self.price <= 0:
            raise OrderError(
                self.client_order_id,
                f"fill price must be positive, got {self.price}",
            )
        if self.fee < 0:
            raise OrderError(
                self.client_order_id,
                f"fill fee must be non-negative, got {self.fee}",
            )
        if self.ts < 0:
            raise OrderError(
                self.client_order_id, f"fill ts must be non-negative, got {self.ts}"
            )

    @property
    def signed_qty(self) -> Money:
        """The execution quantity signed by side: ``+qty`` for BUY, ``-qty`` for SELL.

        This is the contribution of the fill to a position's net quantity, the
        natural unit for folding fills into a :class:`~trading_bot.domain.
        position.Position`.
        """
        return self.qty if self.side is OrderSide.BUY else -self.qty
