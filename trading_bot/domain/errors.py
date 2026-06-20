"""Domain error hierarchy.

Every error raised by the pure domain (and, by convention, the layers built on
top of it) descends from :class:`TradingBotError`. The hierarchy is venue- and
transport-neutral: errors carry plain data (ids, assets, amounts) and format
their own messages — they never reach for an exchange client or an I/O object.

Ported and modernised from the pre-2026 ``trading_bot/legacy/_exceptions.py``,
with the exchange coupling dropped (the legacy errors reached into live
``order`` objects to slice ``order.pair[1:4]``; here the caller passes the
already-decoded values).
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "TradingBotError",
    "OrderError",
    "OrderStatusError",
    "MissingOrder",
    "InstrumentMismatch",
    "InsufficientFunds",
    "RiskLimitBreached",
    "NoCapability",
    "SignalError",
]


class TradingBotError(Exception):
    """Root of every error raised by the trading bot domain."""


class OrderError(TradingBotError):
    """An operation on a specific order failed.

    Parameters
    ----------
    order_id : str
        Identifier of the offending order.
    msg : str, optional
        Human-readable detail. When omitted a generic message is built.

    """

    def __init__(self, order_id: str, msg: str | None = None) -> None:
        self.order_id = order_id
        detail = msg if msg is not None else "order operation failed"
        super().__init__(f"[order {order_id}] {detail}")


class OrderStatusError(OrderError):
    """An action is not allowed by the order's current status.

    Parameters
    ----------
    order_id : str
        Identifier of the offending order.
    status : str
        The current status that forbids the action.
    action : str
        The action that was attempted.

    """

    def __init__(self, order_id: str, status: str, action: str) -> None:
        self.status = status
        self.action = action
        super().__init__(order_id, f"cannot {action} order with status {status!r}")


class MissingOrder(TradingBotError):
    """A referenced order does not exist (locally or on the venue).

    Parameters
    ----------
    order_id : str
        Identifier of the order that could not be found.

    """

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"order {order_id} is missing")


class InstrumentMismatch(TradingBotError):
    """Two domain objects that must share an instrument do not.

    Raised, e.g., when folding fills into a single position and a fill names a
    different instrument than the position is built on (a position is the net
    exposure of *one* instrument).

    Parameters
    ----------
    expected : str
        The instrument the operation is bound to (its ``BASE/QUOTE`` string).
    actual : str
        The mismatching instrument that was supplied.

    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"instrument mismatch: expected {expected}, got {actual}"
        )


class InsufficientFunds(TradingBotError):
    """The available balance cannot cover the requested operation.

    Parameters
    ----------
    asset : str
        The asset that is short (canonical code, e.g. ``"USD"``).
    required : Decimal
        Amount needed to perform the operation.
    available : Decimal
        Amount actually available.

    """

    def __init__(self, asset: str, required: Decimal, available: Decimal) -> None:
        self.asset = asset
        self.required = required
        self.available = available
        super().__init__(
            f"insufficient {asset}: need {required}, only {available} available"
        )


class RiskLimitBreached(TradingBotError):
    """A risk limit (exposure, drawdown, position size, ...) was breached.

    Parameters
    ----------
    limit : str
        Name of the limit that was breached (e.g. ``"max_position"``).
    value : Decimal
        The observed value.
    threshold : Decimal
        The limit that was exceeded.

    """

    def __init__(self, limit: str, value: Decimal, threshold: Decimal) -> None:
        self.limit = limit
        self.value = value
        self.threshold = threshold
        super().__init__(
            f"risk limit {limit!r} breached: {value} exceeds {threshold}"
        )


class SignalError(TradingBotError):
    """A strategy signal is invalid (bad target, missing scale, ...).

    Raised when constructing or resolving a
    :class:`~trading_bot.domain.signal.Signal`: an out-of-range fractional
    exposure, an ambiguous/double-specified target, or a fractional signal
    resolved without the ``reference_qty`` scale it requires.

    Parameters
    ----------
    msg : str
        Human-readable detail of why the signal is invalid.

    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class NoCapability(TradingBotError):
    """A venue/adapter was asked for a capability it does not provide.

    Parameters
    ----------
    venue : str
        The venue or adapter that lacks the capability.
    capability : str
        The missing capability (e.g. ``"margin"``, ``"stream_orderbook"``).

    """

    def __init__(self, venue: str, capability: str) -> None:
        self.venue = venue
        self.capability = capability
        super().__init__(f"{venue} has no capability {capability!r}")
