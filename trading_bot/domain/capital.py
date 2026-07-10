"""The :class:`CapitalEvent` value object — a strategy's money movements.

Where a :class:`~trading_bot.domain.fill.Fill` is the source of truth for a
strategy's *trading* PnL, a :class:`CapitalEvent` is its analogue for *capital*
movements: money the operator funded a strategy with, deposited into it, or
withdrew from it. Folding the two streams together — ``events ⊕ fills`` — is
what lets a strategy's total account value be reconstructed without ever
conflating "money I put in" with "money I made":

    value(t) = contributed_capital(events ≤ t) + realised_pnl(fills ≤ t)

Design choices (carried into the ADR / changelog):

* **Immutable, mirroring Fill's discipline** (see ``domain/fill.py``). A
  capital event is a fact, not a state machine — once recorded, it never
  changes. The dataclass is ``frozen=True`` so it is hashable and safe to
  share / store / replay.
* **The sign lives in the type, not the amount.** Exactly how
  :attr:`~trading_bot.domain.fill.Fill.side` and ``qty`` factor a fill's
  signed contribution, :class:`CapitalEvent` keeps ``amount`` **strictly
  positive** and lets :class:`CapitalEventType` carry the direction:
  ``FUNDING`` and ``DEPOSIT`` add capital, ``WITHDRAWAL`` removes it. This
  keeps a negative amount from ever silently double-negating a withdrawal.
* **All amounts are :class:`~decimal.Decimal`.** ``amount`` never round-trips
  through ``float`` — routed through :func:`~trading_bot.domain.money.money`
  exactly like every other domain money field.
* **``ts`` is an ``int`` of milliseconds since the Unix epoch (UTC)** — the
  same unit as :attr:`~trading_bot.domain.fill.Fill.ts`, so events and fills
  sort and interleave on a shared timeline with no conversion.
* **Business-rule validators raise ``ValueError``.** Unlike ``Fill`` (whose
  range guards raise :class:`~trading_bot.domain.errors.OrderError`, keyed to
  an order id), a capital event has no order to key an error to, so its
  emptiness/range guards raise the plain built-in ``ValueError``. The
  float/non-finite guard is unchanged: it is enforced by
  :func:`~trading_bot.domain.money.money` itself (``TypeError`` for a raw
  ``float``, :class:`~trading_bot.domain.errors.MoneyError` for a non-finite
  ``Decimal``), the same gate every other domain money field goes through.

The module is pure: no I/O, no async, money as :class:`~decimal.Decimal`. It
never imports from ``application`` — the realised-PnL fold that
:func:`value_series` reuses is the domain
:class:`~trading_bot.domain.position.Position` fold itself (the same one
``application/pnl_series.equity_series`` is built on), not the application
helper, so domain purity holds while the two still reconcile to the cent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money
from trading_bot.domain.position import Position

__all__ = [
    "CapitalEventType",
    "CapitalEvent",
    "ValuePoint",
    "contributed_capital",
    "value_series",
]

_ZERO: Money = money("0")

#: Event kinds that add capital; every other kind (``WITHDRAWAL``) removes it.
_INFLOWS = frozenset({"funding", "deposit"})


class CapitalEventType(Enum):
    """Which kind of capital movement a :class:`CapitalEvent` records."""

    FUNDING = "funding"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


@dataclass(frozen=True, slots=True)
class CapitalEvent:
    """A single capital movement for a strategy — immutable, sign in the type.

    Parameters
    ----------
    event_id : str
        Identifier for this event (e.g. an operator-assigned or generated id).
        Mandatory and non-empty: it is the event's identity, letting a replayed
        event be recognised as the same one.
    strategy : str
        The strategy this capital movement applies to. Mandatory and
        non-empty.
    event_type : CapitalEventType
        ``FUNDING`` / ``DEPOSIT`` (capital added) or ``WITHDRAWAL`` (capital
        removed). The direction of the movement; see :func:`contributed_capital`.
    amount : Decimal
        The magnitude of the movement, in quote units. Must be **strictly
        positive** — the direction is carried by ``event_type``, never by the
        sign of ``amount``.
    ts : int
        Timestamp as **milliseconds since the Unix epoch (UTC)**, the same
        unit as :attr:`~trading_bot.domain.fill.Fill.ts`. Must be non-negative.
    note : str, optional
        Free-form human-readable annotation (e.g. "initial allocation").
        Defaults to ``""``.

    Examples
    --------
    >>> from trading_bot.domain.money import money
    >>> CapitalEvent(
    ...     event_id="E1",
    ...     strategy="alloc1",
    ...     event_type=CapitalEventType.FUNDING,
    ...     amount=money("1000"),
    ...     ts=1_700_000_000_000,
    ... ).amount
    Decimal('1000')

    """

    event_id: str
    strategy: str
    event_type: CapitalEventType
    amount: Money
    ts: int
    note: str = ""

    def __post_init__(self) -> None:
        """Validate construction invariants (ids non-empty, amount in range).

        ``amount`` is routed through :func:`~trading_bot.domain.money.money`
        first: it **rejects a raw ``float``** (``TypeError``) and any
        non-finite ``Decimal`` (``MoneyError``) before the range guard below
        — the same fail-fast discipline as :class:`~trading_bot.domain.fill.
        Fill`. The dataclass is frozen, so the guarded value is written back
        via ``object.__setattr__``.
        """
        if not self.event_id:
            raise ValueError("event_id is mandatory and non-empty")
        if not self.strategy:
            raise ValueError("strategy is mandatory and non-empty")
        # Guard the money field through money(): reject float / non-finite.
        object.__setattr__(self, "amount", money(self.amount))
        if self.amount <= 0:
            raise ValueError(
                f"capital event amount must be strictly positive, got {self.amount}"
            )
        if self.ts < 0:
            raise ValueError(f"capital event ts must be non-negative, got {self.ts}")


@dataclass(frozen=True, slots=True)
class ValuePoint:
    """One point of a strategy's value curve — ``(ts_ms, contributed, realised_pnl, value)``.

    The value object :func:`value_series` yields per event or fill it folds:
    the running contributed capital, the running realised PnL, and their sum
    (the strategy's total value) as of that point on the merged timeline.
    Money is exact :class:`~decimal.Decimal`.

    Attributes
    ----------
    ts_ms : int
        The folded event's or fill's timestamp, milliseconds since the Unix
        epoch (UTC).
    contributed : Money
        Cumulative contributed capital through this point (see
        :func:`contributed_capital`).
    realised_pnl : Money
        Cumulative realised PnL (net of fees) through this point, across every
        instrument in the folded fill stream.
    value : Money
        The strategy's total value at this point: ``contributed + realised_pnl``.

    """

    ts_ms: int
    contributed: Money
    realised_pnl: Money
    value: Money


def contributed_capital(events: Iterable[CapitalEvent]) -> Money:
    """Fold ``events`` into the net capital contributed to a strategy.

    ``FUNDING`` and ``DEPOSIT`` events add their ``amount``; ``WITHDRAWAL``
    events subtract theirs — the direction lives in :class:`CapitalEventType`,
    never in the sign of ``amount`` (every event's ``amount`` is strictly
    positive by construction).

    Parameters
    ----------
    events : Iterable[CapitalEvent]
        The capital events to fold, in any order (the fold is commutative:
        only the totals per type matter, not their sequence).

    Returns
    -------
    Money
        ``Σ amount [FUNDING|DEPOSIT] − Σ amount [WITHDRAWAL]``. ``0`` for no
        events.

    Examples
    --------
    >>> from trading_bot.domain.money import money
    >>> events = [
    ...     CapitalEvent("E1", "alloc1", CapitalEventType.FUNDING, money("1000"), 0),
    ...     CapitalEvent("E2", "alloc1", CapitalEventType.WITHDRAWAL, money("100"), 1),
    ... ]
    >>> contributed_capital(events)
    Decimal('900')

    """
    total: Money = _ZERO
    for event in events:
        if event.event_type.value in _INFLOWS:
            total += event.amount
        else:
            total -= event.amount
    return total


def value_series(
    fills: Iterable[Fill], events: Iterable[CapitalEvent]
) -> list[ValuePoint]:
    """Fold ``events`` and ``fills`` (merged by ``ts``) into a strategy value curve.

    Interleaves the two streams on a shared timeline: at each point, an event
    updates the running **contributed capital** (see :func:`contributed_capital`)
    while a fill updates the running **realised PnL** — folded via a per-
    instrument :class:`~trading_bot.domain.position.Position`
    (:meth:`~trading_bot.domain.position.Position.with_fill`), exactly the fold
    ``application/pnl_series.equity_series`` uses, so the two reconcile to the
    cent (never a re-derivation). ``value`` at each point is
    ``contributed + realised_pnl``.

    Ties are broken **event before fill**: when an event and a fill share the
    same ``ts``, the event is folded first, so e.g. a genesis funding event and
    the first fill at the same timestamp yield the funded capital already in
    place for that fill's point. Within the same kind and ``ts``, input order
    is preserved (a stable sort on ``(ts, kind)``).

    Parameters
    ----------
    fills : Iterable[Fill]
        The confirmed fills to fold (any instrument mix). Need not be
        pre-sorted.
    events : Iterable[CapitalEvent]
        The capital events to fold. Need not be pre-sorted.

    Returns
    -------
    list of ValuePoint
        One point per event or fill, in ascending ``ts`` order (event before
        fill on a tie). Empty if both streams are empty.

    Examples
    --------
    >>> from trading_bot.domain.instrument import Instrument, Symbol
    >>> from trading_bot.domain.money import money
    >>> from trading_bot.domain.order import OrderSide
    >>> inst = Instrument(Symbol("BTC", "USD"))
    >>> genesis = CapitalEvent("E1", "alloc1", CapitalEventType.FUNDING, money("1000"), 0)
    >>> buy = Fill("T1", "c1", inst, OrderSide.BUY, money("1"), money("100"), money("0"), 1)
    >>> sell = Fill("T2", "c2", inst, OrderSide.SELL, money("1"), money("110"), money("0"), 2)
    >>> points = value_series([buy, sell], [genesis])
    >>> [p.value for p in points]
    [Decimal('1000'), Decimal('1000'), Decimal('1010')]

    """
    # Sort each stream independently first (Python's sort is stable, so
    # same-ts items within a stream keep their input order); the two-pointer
    # merge below then interleaves the streams by ts, taking the event on a
    # cross-stream tie (the ``<=`` below).
    ordered_events = sorted(events, key=lambda event: event.ts)
    ordered_fills = sorted(fills, key=lambda fill: fill.ts)

    positions: dict[Instrument, Position] = {}
    contributed: Money = _ZERO
    realised: Money = _ZERO
    points: list[ValuePoint] = []

    ei, fi = 0, 0
    while ei < len(ordered_events) or fi < len(ordered_fills):
        take_event = fi >= len(ordered_fills) or (
            ei < len(ordered_events) and ordered_events[ei].ts <= ordered_fills[fi].ts
        )
        if take_event:
            event = ordered_events[ei]
            ei += 1
            if event.event_type.value in _INFLOWS:
                contributed += event.amount
            else:
                contributed -= event.amount
            ts = event.ts
        else:
            fill = ordered_fills[fi]
            fi += 1
            instrument = fill.instrument
            prev = positions.get(instrument) or Position.flat(instrument)
            now = prev.with_fill(fill)
            positions[instrument] = now
            # The fill's contribution to the aggregate is the delta of its
            # instrument's realised PnL (fees included — with_fill nets them).
            realised += now.realised_pnl - prev.realised_pnl
            ts = fill.ts
        points.append(
            ValuePoint(
                ts_ms=ts,
                contributed=contributed,
                realised_pnl=realised,
                value=contributed + realised,
            )
        )
    return points
