"""The :class:`Signal` value object — a strategy's venue-neutral target.

A :class:`Signal` is what a strategy *wants* an instrument's exposure to be, said
once, venue-neutrally, before any broker or order type is involved. A future
``StrategyRunner`` turns a signal plus the current
:class:`~trading_bot.domain.position.Position` into an order; this module owns
the arithmetic of "where I am vs. where I want to be".

Two target modes
----------------
A strategy can express its target in one of **two** mutually-exclusive ways, and
a signal is exactly one of them (constructing both, or neither, is rejected):

* **fractional exposure** — a normalised number in ``[-1, 1]`` where ``-1`` is
  fully short, ``0`` is flat and ``+1`` is fully long. This is the natural
  output of most strategies (a sign / a confidence in ``[-1, 1]``). It is a
  *fraction of a reference size*, so it cannot be turned into a quantity on its
  own: resolving it needs a ``reference_qty`` (the max position size, in base
  units) supplied at :meth:`Signal.delta_to` time.

* **explicit target quantity** — a signed :class:`~decimal.Decimal` (``+`` long,
  ``-`` short, ``0`` flat) that *is* the desired net position in base units. It
  carries its own scale, so :meth:`Signal.delta_to` needs no ``reference_qty``.

The two are built with named constructors :meth:`Signal.exposure` and
:meth:`Signal.target_qty`; the private ``__init__`` is not meant to be called
directly. :attr:`Signal.mode` (a :class:`SignalMode`) tags which one a given
signal is.

The delta to a position
------------------------
:meth:`Signal.delta_to` returns the signed :class:`~decimal.Decimal` change the
router must apply to reach the target from a current position::

    delta = target_net_qty - position.net_qty

For an explicit-qty signal ``target_net_qty`` is the target directly. For a
fractional signal ``target_net_qty = exposure * reference_qty`` — hence the
required ``reference_qty``. A positive delta means *buy that much base*, a
negative delta means *sell*, zero means *already on target*. A flat target
(``0``) against an open position yields ``-position.net_qty`` — a full close.

Legacy mapping
--------------
This unifies the vocabulary of the legacy ``legacy/performance.py`` PnL columns
(referenced, never imported):

* legacy ``signal`` (``cumsum(delta_signal) + pos_init``) is the **target net
  position** — i.e. an explicit-qty :class:`Signal`'s target;
* legacy ``delta_signal`` (the per-step ``+1``/``-1`` position change) is the
  output of :meth:`Signal.delta_to` — the change to apply to the current
  position.

So PnL (leaf 05) and the runner share one type: ``signal`` is the target, the
``delta_to`` a :class:`Position` is the ``delta_signal``.

The module is pure: no I/O, no async, money/quantities as
:class:`~decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from trading_bot.domain.errors import SignalError
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money
from trading_bot.domain.position import Position

__all__ = [
    "SignalMode",
    "Signal",
]

_ONE: Money = money("1")
_NEG_ONE: Money = money("-1")


class SignalMode(Enum):
    """How a :class:`Signal`'s ``target`` should be interpreted."""

    #: ``target`` is a normalised exposure in ``[-1, 1]`` (short..long); it is a
    #: fraction of a reference size and needs a ``reference_qty`` to resolve.
    EXPOSURE = "exposure"
    #: ``target`` is an explicit signed net position quantity in base units.
    TARGET_QTY = "target_qty"


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's venue-neutral target exposure for one instrument.

    Immutable. Build one with :meth:`Signal.exposure` (a normalised ``[-1, 1]``
    exposure) or :meth:`Signal.target_qty` (an explicit signed net quantity) —
    never via the raw constructor, which does not validate the mode/target
    pairing on its own intent. :meth:`delta_to` then computes the signed change
    from a current :class:`~trading_bot.domain.position.Position`.

    Parameters
    ----------
    instrument : Instrument
        The instrument the target applies to.
    mode : SignalMode
        Whether :attr:`target` is a fractional exposure or an explicit quantity.
    target : Decimal
        In :data:`SignalMode.EXPOSURE` mode, a normalised exposure in
        ``[-1, 1]``. In :data:`SignalMode.TARGET_QTY` mode, the signed desired
        net position in base units.
    ts : int
        Timestamp the signal was produced, as **milliseconds since the Unix
        epoch (UTC)** — the same unit as :attr:`~trading_bot.domain.fill.Fill.ts`.
        Must be non-negative.
    strength : Decimal or None, optional
        Optional strategy confidence / conviction in ``[0, 1]`` (``None`` if the
        strategy does not express one). Advisory metadata only: it does **not**
        scale :meth:`delta_to`. Must be in ``[0, 1]`` when given.

    Examples
    --------
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> inst = Instrument(Symbol("BTC", "USD"))
    >>> Signal.exposure(inst, money("1"), ts=1).mode
    <SignalMode.EXPOSURE: 'exposure'>
    >>> Signal.target_qty(inst, money("2.5"), ts=1).target
    Decimal('2.5')

    """

    instrument: Instrument
    mode: SignalMode
    target: Money
    ts: int
    strength: Money | None = None

    def __post_init__(self) -> None:
        """Validate the target against its mode, ``ts`` and ``strength``."""
        if self.ts < 0:
            raise SignalError(f"signal ts must be non-negative, got {self.ts}")

        if self.mode is SignalMode.EXPOSURE:
            if not (_NEG_ONE <= self.target <= _ONE):
                raise SignalError(
                    "exposure target must be in [-1, 1], got "
                    f"{self.target}"
                )
        # TARGET_QTY accepts any signed Decimal (incl. 0 = flat); no bound.

        if self.strength is not None and not (0 <= self.strength <= 1):
            raise SignalError(
                f"strength must be in [0, 1], got {self.strength}"
            )

    # --- named constructors ------------------------------------------------ #

    @classmethod
    def exposure(
        cls,
        instrument: Instrument,
        target: Money,
        *,
        ts: int,
        strength: Money | None = None,
    ) -> Signal:
        """Build a fractional-exposure signal (``target`` in ``[-1, 1]``).

        Parameters
        ----------
        instrument : Instrument
            The instrument the target applies to.
        target : Decimal
            Normalised exposure in ``[-1, 1]`` (``-1`` short, ``0`` flat, ``+1``
            long).
        ts : int
            Timestamp in milliseconds since the Unix epoch (UTC).
        strength : Decimal or None, optional
            Optional confidence in ``[0, 1]`` (advisory only).

        Returns
        -------
        Signal
            A signal in :data:`SignalMode.EXPOSURE` mode.

        Raises
        ------
        SignalError
            If ``target`` is outside ``[-1, 1]`` (or ``strength``/``ts`` invalid).

        """
        return cls(
            instrument=instrument,
            mode=SignalMode.EXPOSURE,
            target=target,
            ts=ts,
            strength=strength,
        )

    @classmethod
    def target_qty(
        cls,
        instrument: Instrument,
        target: Money,
        *,
        ts: int,
        strength: Money | None = None,
    ) -> Signal:
        """Build an explicit target-quantity signal (signed net position).

        Parameters
        ----------
        instrument : Instrument
            The instrument the target applies to.
        target : Decimal
            The desired signed net position in base units (``+`` long, ``-``
            short, ``0`` flat). Any magnitude is allowed.
        ts : int
            Timestamp in milliseconds since the Unix epoch (UTC).
        strength : Decimal or None, optional
            Optional confidence in ``[0, 1]`` (advisory only).

        Returns
        -------
        Signal
            A signal in :data:`SignalMode.TARGET_QTY` mode.

        Raises
        ------
        SignalError
            If ``strength``/``ts`` are invalid.

        """
        return cls(
            instrument=instrument,
            mode=SignalMode.TARGET_QTY,
            target=target,
            ts=ts,
            strength=strength,
        )

    # --- views ------------------------------------------------------------- #

    def target_net_qty(self, reference_qty: Money | None = None) -> Money:
        """The desired *absolute* net position in base units.

        For an explicit-qty signal this is :attr:`target` directly. For a
        fractional signal it is ``target * reference_qty``, so ``reference_qty``
        (the max position size in base units) is required.

        Parameters
        ----------
        reference_qty : Decimal or None, optional
            The reference size (max position, in base units) a fractional
            exposure is a fraction of. Required for
            :data:`SignalMode.EXPOSURE` signals; ignored for
            :data:`SignalMode.TARGET_QTY` signals.

        Returns
        -------
        Decimal
            The signed target net quantity in base units.

        Raises
        ------
        SignalError
            If this is a fractional signal and ``reference_qty`` is ``None`` or
            not strictly positive.

        """
        if self.mode is SignalMode.TARGET_QTY:
            return self.target
        # EXPOSURE: needs a positive scale to become a quantity.
        if reference_qty is None:
            raise SignalError(
                "a fractional-exposure signal needs a reference_qty to resolve "
                "into a target quantity"
            )
        if reference_qty <= 0:
            raise SignalError(
                f"reference_qty must be positive, got {reference_qty}"
            )
        return self.target * reference_qty

    def delta_to(
        self, position: Position, reference_qty: Money | None = None
    ) -> Money:
        """The signed position change to reach this target from ``position``.

        ``delta = target_net_qty - position.net_qty``. A positive result means
        *buy* that many base units, a negative result means *sell*, and ``0``
        means the position is already on target. A flat target (``0``) against
        an open position returns ``-position.net_qty`` — a full close.

        Parameters
        ----------
        position : Position
            The current net exposure for the instrument.
        reference_qty : Decimal or None, optional
            The reference size a fractional exposure is a fraction of (in base
            units). **Required** for :data:`SignalMode.EXPOSURE` signals;
            ignored for :data:`SignalMode.TARGET_QTY` signals.

        Returns
        -------
        Decimal
            The signed quantity change to apply (the router's order size/side).

        Raises
        ------
        SignalError
            If this is a fractional signal and ``reference_qty`` is missing or
            not positive.

        Notes
        -----
        This is the modern equivalent of the legacy ``delta_signal`` column,
        while :meth:`target_net_qty` is the legacy ``signal`` (target position).

        """
        return self.target_net_qty(reference_qty) - position.net_qty
