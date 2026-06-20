"""Exact, ``Decimal``-backed money and quantity helpers.

Money in this domain is *always* a :class:`decimal.Decimal`. Binary ``float``
cannot represent decimal fractions exactly (``0.1 + 0.2 != 0.3``), so prices,
sizes and fees must never round-trip through ``float``. The helpers here make
that invariant enforceable:

* :func:`money` constructs a ``Decimal`` from ``str`` / ``int`` / ``Decimal``
  only and **rejects ``float``** outright — the binary error would already be
  baked into the bits before we ever saw the value. A caller that genuinely
  starts from a ``float`` (e.g. a number off the wire) must opt in explicitly
  via :func:`from_float`, which routes through ``str`` so the *shortest*
  round-tripping decimal is taken.
* :func:`quantize` snaps a value to a venue tick / lot size, defaulting to
  banker's-unsafe-free ``ROUND_DOWN`` (never hand a venue more size/price than
  intended).

The public API takes and returns ``Decimal`` exclusively — no ``float`` leaks.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

__all__ = [
    "Money",
    "money",
    "from_float",
    "quantize",
    "add",
    "sub",
    "mul",
]

#: Public alias for the money type. All monetary values are exact decimals.
Money = Decimal

# Accepted inputs for exact construction. ``bool`` is an ``int`` subclass but
# is meaningless as money, so it is rejected explicitly below.
_Exact = str | int | Decimal


def money(value: _Exact) -> Money:
    """Build an exact :class:`~decimal.Decimal` from ``str``/``int``/``Decimal``.

    Parameters
    ----------
    value : str or int or Decimal
        The monetary value. Strings are parsed exactly (``money("0.1")`` is
        exactly one tenth). ``float`` is rejected — use :func:`from_float`.

    Returns
    -------
    Decimal
        The exact value.

    Raises
    ------
    TypeError
        If ``value`` is a ``float`` (or ``bool``), or any other unsupported
        type. Floats are refused because the binary rounding error is already
        present in the bits.
    decimal.InvalidOperation
        If a ``str`` does not parse as a number.

    Examples
    --------
    >>> money("0.1") + money("0.2") == money("0.3")
    True
    >>> money(5)
    Decimal('5')

    """
    if isinstance(value, bool):
        raise TypeError("money() does not accept bool")
    if isinstance(value, float):
        raise TypeError(
            "money() refuses float to avoid binary rounding error; "
            "pass a str/int/Decimal, or use from_float() to opt in explicitly"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (str, int)):
        return Decimal(value)
    raise TypeError(f"cannot build Money from {type(value).__name__}")


def from_float(value: float) -> Money:
    """Explicitly convert a ``float`` to :class:`Money` via its ``str`` form.

    This is the *only* sanctioned float entry point. Going through ``str``
    yields the shortest decimal that round-trips the float (e.g.
    ``from_float(0.1) == Decimal("0.1")``), which is almost always the value a
    human meant — far better than ``Decimal(0.1)``'s 55-digit tail.

    Use this only at boundaries where the value genuinely arrived as a float;
    prefer keeping money as :class:`Decimal` end to end.

    Parameters
    ----------
    value : float
        The float to convert.

    Returns
    -------
    Decimal
        The shortest decimal that round-trips ``value``.

    Examples
    --------
    >>> from_float(0.1)
    Decimal('0.1')

    """
    if not isinstance(value, float):
        raise TypeError("from_float() expects a float")
    return Decimal(str(value))


def quantize(value: Money, tick: Money, *, rounding: str = ROUND_DOWN) -> Money:
    """Snap ``value`` to a multiple of ``tick`` (a venue tick or lot size).

    Parameters
    ----------
    value : Decimal
        The value to quantise.
    tick : Decimal
        The tick / lot size, e.g. ``Decimal("0.00001")`` for a Kraken price
        tick or ``Decimal("0.00000001")`` for a satoshi lot. Must be positive.
    rounding : str, optional
        A :mod:`decimal` rounding mode. Defaults to ``ROUND_DOWN`` so an order
        never overshoots the intended price/size.

    Returns
    -------
    Decimal
        ``value`` rounded to the nearest multiple of ``tick`` (toward zero by
        default), carrying the same exponent as ``tick``.

    Raises
    ------
    ValueError
        If ``tick`` is not strictly positive.

    Examples
    --------
    >>> quantize(money("27123.456789"), money("0.1"))
    Decimal('27123.4')
    >>> quantize(money("0.123456789"), money("0.00000001"))
    Decimal('0.12345678')

    """
    if tick <= 0:
        raise ValueError(f"tick must be positive, got {tick}")
    snapped = (value / tick).quantize(Decimal(1), rounding=rounding) * tick
    # Re-quantize to the tick's exponent so the result's scale matches the tick
    # exactly (e.g. 0.1 -> "0.1", not "0.10000...").
    return snapped.quantize(tick, rounding=ROUND_HALF_EVEN)


def add(a: Money, b: Money) -> Money:
    """Exact addition of two :class:`Money` values."""
    return a + b


def sub(a: Money, b: Money) -> Money:
    """Exact subtraction ``a - b`` of two :class:`Money` values."""
    return a - b


def mul(a: Money, b: Money) -> Money:
    """Exact multiplication of two :class:`Money` values."""
    return a * b
