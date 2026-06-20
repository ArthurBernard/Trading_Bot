"""The :class:`Position` value object — net exposure rebuilt from fills.

A :class:`Position` is the net result of folding an **ordered** sequence of
:class:`~trading_bot.domain.fill.Fill` records for a *single* instrument. It
tracks four things, all exact :class:`~decimal.Decimal`:

* ``net_qty`` — signed net exposure: positive = long, negative = short, zero =
  flat;
* ``avg_entry_price`` — the quantity-weighted average entry price of the
  currently-open exposure (``None`` when flat);
* ``realised_pnl`` — cumulative profit/loss locked in by closing exposure,
  **net of fees**;
* ``fees_paid`` — cumulative fees across every folded fill.

PnL sign convention
-------------------
PnL is realised only when exposure is *reduced* (a fill in the opposite
direction of the open position) — opening or increasing exposure realises
nothing. For the ``closed_qty`` (always a positive magnitude) that a reducing
fill closes against an entry at ``avg_entry_price`` and an exit at the fill's
``price``:

* **long** position being reduced (a SELL)::

      gross_pnl = (exit_price - avg_entry_price) * closed_qty

* **short** position being reduced (a BUY)::

      gross_pnl = (avg_entry_price - exit_price) * closed_qty

i.e. a short's PnL is the long formula with the sign flipped. Fees are then
subtracted: ``realised_pnl += gross_pnl`` and, separately, every fill's ``fee``
is subtracted from ``realised_pnl`` and accrued into ``fees_paid`` — so fees
always reduce realised PnL, on opening fills as well as closing ones.

Flip handling
-------------
A **flip** is a fill that reverses the position's sign: a reducing fill whose
``qty`` exceeds the open ``net_qty`` magnitude. It is handled in two stages:

1. **close** the entire existing exposure — realise PnL on that closed part
   against the old ``avg_entry_price`` and the flipping fill's ``price``;
2. **open** the remainder (``fill.qty - |old net_qty|``) in the new direction at
   the flipping fill's ``price`` — so the new ``avg_entry_price`` is exactly the
   flipping fill's price.

Increases in the same direction take the quantity-weighted average of the old
and the added exposure.

One instrument per position
---------------------------
A position is the exposure of exactly one instrument. :meth:`Position.from_fills`
rejects a sequence whose fills name more than one instrument with
:class:`~trading_bot.domain.errors.InstrumentMismatch`.

The module is pure: no I/O, no async, money as :class:`~decimal.Decimal`,
deterministic in fill order.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from trading_bot.domain.errors import InstrumentMismatch
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import OrderSide

__all__ = [
    "Position",
]

_ZERO: Money = money("0")


@dataclass(frozen=True, slots=True)
class Position:
    """The net exposure of one instrument, folded from its fills.

    Immutable: :meth:`from_fills` computes a final snapshot in one pass. ``long``
    / ``short`` / ``is_flat`` are convenience views over the sign of
    :attr:`net_qty`.

    Parameters
    ----------
    instrument : Instrument
        The single instrument this position is exposed to.
    net_qty : Decimal
        Signed net quantity: ``> 0`` long, ``< 0`` short, ``0`` flat.
    avg_entry_price : Decimal or None
        Quantity-weighted average entry price of the open exposure, or ``None``
        when flat.
    realised_pnl : Decimal
        Cumulative realised PnL, net of fees (see the module-level sign
        convention).
    fees_paid : Decimal
        Cumulative fees paid across all folded fills.

    Examples
    --------
    >>> from trading_bot.domain.fill import Fill
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> from trading_bot.domain.order import OrderSide
    >>> inst = Instrument(Symbol("BTC", "USD"))
    >>> f = Fill("T1", "cid-1", inst, OrderSide.BUY, money("2"), money("30000"),
    ...          money("0"), 1)
    >>> pos = Position.from_fills([f])
    >>> pos.net_qty, pos.avg_entry_price
    (Decimal('2'), Decimal('30000'))

    """

    instrument: Instrument
    net_qty: Money
    avg_entry_price: Money | None
    realised_pnl: Money
    fees_paid: Money

    @property
    def is_flat(self) -> bool:
        """Whether the position holds no exposure (``net_qty == 0``)."""
        return self.net_qty == 0

    @property
    def is_long(self) -> bool:
        """Whether the position is net long (``net_qty > 0``)."""
        return self.net_qty > 0

    @property
    def is_short(self) -> bool:
        """Whether the position is net short (``net_qty < 0``)."""
        return self.net_qty < 0

    @classmethod
    def from_fills(cls, fills: Iterable[Fill]) -> Position:
        """Fold an **ordered** sequence of fills into a net :class:`Position`.

        Handles increases (quantity-weighted average entry), partial closes and
        full closes (realise PnL on the closed part), flips (close then re-open
        at the flipping fill's price), and fee accrual. See the module docstring
        for the PnL sign convention and flip handling.

        Parameters
        ----------
        fills : Iterable[Fill]
            The fills to fold, **in execution order**. Must be non-empty and all
            name the same instrument.

        Returns
        -------
        Position
            The net position after all fills.

        Raises
        ------
        ValueError
            If ``fills`` is empty (an instrument cannot be inferred).
        InstrumentMismatch
            If the fills name more than one instrument.

        """
        ordered: Sequence[Fill] = tuple(fills)
        if not ordered:
            raise ValueError("from_fills requires at least one fill")

        instrument: Instrument = ordered[0].instrument

        net_qty: Money = _ZERO
        # Average entry price of the *currently open* exposure. Only meaningful
        # while net_qty != 0; kept as a plain Decimal (defaults to zero when
        # flat) and exposed as None when flat in the final snapshot.
        avg_entry: Money = _ZERO
        realised_pnl: Money = _ZERO
        fees_paid: Money = _ZERO

        for fill in ordered:
            if fill.instrument != instrument:
                raise InstrumentMismatch(str(instrument), str(fill.instrument))

            # Fees always accrue and always reduce realised PnL.
            fees_paid += fill.fee
            realised_pnl -= fill.fee

            signed = fill.signed_qty  # +qty for BUY, -qty for SELL
            price = fill.price

            if net_qty == 0:
                # Opening from flat: the fill becomes the whole exposure.
                net_qty = signed
                avg_entry = price
                continue

            same_direction = (net_qty > 0) == (fill.side is OrderSide.BUY)

            if same_direction:
                # Increasing exposure: quantity-weighted average of old + added.
                # Work in magnitudes; both legs share the position's sign.
                old_mag = abs(net_qty)
                add_mag = fill.qty
                total_mag = old_mag + add_mag
                avg_entry = (avg_entry * old_mag + price * add_mag) / total_mag
                net_qty += signed
                continue

            # Opposite direction: this fill reduces (and maybe flips) exposure.
            open_mag = abs(net_qty)
            closed_mag = min(open_mag, fill.qty)
            realised_pnl += _close_pnl(
                was_long=net_qty > 0,
                entry=avg_entry,
                exit_price=price,
                closed_qty=closed_mag,
            )

            new_net = net_qty + signed
            if new_net == 0:
                # Exact close back to flat.
                net_qty = _ZERO
                avg_entry = _ZERO
            elif (new_net > 0) == (net_qty > 0):
                # Partial close: same sign, entry unchanged, qty reduced.
                net_qty = new_net
            else:
                # Flip: old side fully closed above; remainder opens at the
                # flipping fill's price, which becomes the new average entry.
                net_qty = new_net
                avg_entry = price

        return cls(
            instrument=instrument,
            net_qty=net_qty,
            avg_entry_price=None if net_qty == 0 else avg_entry,
            realised_pnl=realised_pnl,
            fees_paid=fees_paid,
        )


def _close_pnl(
    *, was_long: bool, entry: Money, exit_price: Money, closed_qty: Money
) -> Money:
    """Gross PnL from closing ``closed_qty`` of exposure (sign convention above).

    Parameters
    ----------
    was_long : bool
        Whether the exposure being closed was long.
    entry : Decimal
        The average entry price of the closed exposure.
    exit_price : Decimal
        The price at which the exposure is closed.
    closed_qty : Decimal
        The (positive) magnitude of exposure closed.

    Returns
    -------
    Decimal
        ``(exit - entry) * closed_qty`` for a long, the negation for a short.

    """
    if was_long:
        return (exit_price - entry) * closed_qty
    return (entry - exit_price) * closed_qty
