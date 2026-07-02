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
    "MoneyError",
    "OrderError",
    "OrderStatusError",
    "MissingOrder",
    "InstrumentMismatch",
    "RiskLimitBreached",
    "NoCapability",
    "BrokerError",
    "OrderTooSmall",
    "InsufficientBalance",
    "InvalidInstrument",
    "InvalidNonce",
    "RateLimited",
    "ServiceUnavailable",
    "SignalError",
    "ConfigError",
    "LiveTradingNotEnabled",
]


class TradingBotError(Exception):
    """Root of every error raised by the trading bot domain."""


class MoneyError(TradingBotError):
    """A monetary value is not a valid, finite decimal amount.

    Raised by :func:`~trading_bot.domain.money.money` when a value cannot serve
    as money: a ``float`` (whose binary rounding error is baked in — pass a
    ``str``/``int``/``Decimal`` instead), a ``bool``, an unsupported type, an
    unparsable string, or a **non-finite** ``Decimal`` (``NaN``, ``sNaN``,
    ``±Inf``). A non-finite amount would silently corrupt the PnL source of
    truth (``NaN`` poisons every comparison and every fold it touches), so it is
    rejected at construction with this typed domain error rather than a bare
    :class:`decimal.InvalidOperation` leaking out of arithmetic later.

    Parameters
    ----------
    msg : str
        Human-readable detail of why the value is not valid money.

    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class BrokerError(TradingBotError):
    """A broker/venue adapter operation failed.

    The venue-neutral error every :class:`~trading_bot.brokers.base.Broker`
    adapter raises for a generic failure that is not better described by a more
    specific member of this hierarchy (e.g. a venue returning an error body, an
    unknown venue requested of the service factory, an adapter that cannot
    satisfy a request). It carries plain data only — adapters decode the venue payload and
    pass a human-readable message; the error never reaches for a client or an
    I/O object.

    Parameters
    ----------
    msg : str
        Human-readable detail of the failure.

    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class InsufficientBalance(BrokerError):
    """A venue rejected an order because the account balance is too low.

    The venue-neutral mapping of an *insufficient funds* rejection reported by a
    venue's order endpoint (Kraken ``EOrder:Insufficient funds``; Binance
    ``-2010`` naming an insufficient balance). A venue rejection string carries
    no reliable asset/required/available breakdown, so this subclass of
    :class:`BrokerError` carries only the venue detail and, being a
    ``BrokerError``, stays catchable by the order router's ``except BrokerError``
    reject path.

    Parameters
    ----------
    msg : str, optional
        Human-readable venue detail. When omitted a generic message is built.

    """

    def __init__(self, msg: str | None = None) -> None:
        detail = msg if msg is not None else "insufficient balance"
        super().__init__(f"insufficient balance at venue: {detail}")


class InvalidInstrument(BrokerError):
    """A venue rejected an order because the pair/instrument is not tradeable.

    The venue-neutral mapping of an *invalid pair / unknown symbol* rejection
    (Kraken ``EQuery:Unknown asset pair`` / ``EOrder:Unknown pair``; Binance
    ``-1121 Invalid symbol``). A subclass of :class:`BrokerError` so existing
    ``except BrokerError`` sites keep catching it, while a caller that wants to
    distinguish an unfixable-by-retry instrument error can match on the type.

    Parameters
    ----------
    symbol : str
        The venue symbol/pair that was rejected.
    msg : str, optional
        Human-readable venue detail. When omitted a generic message is built.

    """

    def __init__(self, symbol: str, msg: str | None = None) -> None:
        self.symbol = symbol
        detail = msg if msg is not None else "invalid or unknown instrument"
        super().__init__(f"invalid instrument {symbol!r}: {detail}")


class InvalidNonce(BrokerError):
    """A venue rejected a signed request because its nonce was invalid.

    Maps Kraken ``EAPI:Invalid nonce``. Almost always a *client-side* nonce bug
    (non-monotonic counter, a clock step-back, or two callers sharing a nonce
    source) rather than a transient venue condition, so it is **not** retriable:
    a blind retry with a fresh nonce could double-submit a non-idempotent
    ``AddOrder``. Surfaced distinctly so the operator sees the real cause.

    Parameters
    ----------
    msg : str, optional
        Human-readable venue detail. When omitted a generic message is built.

    """

    def __init__(self, msg: str | None = None) -> None:
        detail = msg if msg is not None else "invalid nonce"
        super().__init__(f"nonce rejected by venue: {detail}")


class RateLimited(BrokerError):
    """A venue rejected a request because a rate limit was exceeded.

    Maps Kraken ``EAPI:Rate limit exceeded`` / ``EGeneral:Too many requests``
    (Kraken returns these inside an HTTP-200 body) and Binance ``-1003``. A
    **retriable** condition: backing off and retrying the *idempotent* calls is
    the correct response. A subclass of :class:`BrokerError`.

    Parameters
    ----------
    msg : str, optional
        Human-readable venue detail. When omitted a generic message is built.

    """

    def __init__(self, msg: str | None = None) -> None:
        detail = msg if msg is not None else "rate limit exceeded"
        super().__init__(f"rate limited by venue: {detail}")


class ServiceUnavailable(BrokerError):
    """A venue is temporarily unavailable / in maintenance.

    Maps Kraken ``EService:Unavailable`` / ``EService:Busy`` (returned inside an
    HTTP-200 body, which is why the transport's 5xx retry never sees them) and
    Binance ``-1001``. A **retriable** condition for idempotent calls. A subclass
    of :class:`BrokerError`.

    Parameters
    ----------
    msg : str, optional
        Human-readable venue detail. When omitted a generic message is built.

    """

    def __init__(self, msg: str | None = None) -> None:
        detail = msg if msg is not None else "service unavailable"
        super().__init__(f"venue temporarily unavailable: {detail}")


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


class OrderTooSmall(BrokerError):
    """An order is below the venue's minimum tradeable size / notional.

    Raised **client-side, before submission**, when quantizing an order to the
    venue's lot/tick step leaves a quantity of zero (sub-lot dust), or the
    quantity is below the venue's ``min_qty``, or the order's notional
    (``qty * price``) is below the venue's ``min_notional``. Rejecting here
    turns a doomed request (the venue would reject it anyway, costing a round
    trip and a rate-limit token) into a clear, immediate error. A subclass of
    :class:`BrokerError` so the order router's ``except BrokerError`` path drives
    the order to ``REJECTED`` (a too-small order was never live on the venue).

    Parameters
    ----------
    instrument : str
        The instrument the order is for (its ``BASE/QUOTE`` string).
    detail : str
        Human-readable reason (which minimum was violated, and by how much).

    """

    def __init__(self, instrument: str, detail: str) -> None:
        self.instrument = instrument
        super().__init__(f"order on {instrument} is too small: {detail}")


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


class ConfigError(TradingBotError):
    """A declared configuration cannot be resolved into a runnable system.

    Raised while turning a validated
    :class:`~trading_bot.application.config.AppConfig` into running collaborators
    (e.g. by :func:`~trading_bot.application.run_app.build_runners`): a strategy
    that declares no signal or no data source, names an unknown builtin signal,
    or whose signal reference cannot be built/imported. Distinct from a pydantic
    ``ValidationError`` (a *shape* violation caught at parse time) — this is a
    *resolution* failure for a config that is individually well-formed.

    Parameters
    ----------
    msg : str
        Human-readable detail of why the configuration cannot be resolved.

    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)


class LiveTradingNotEnabled(TradingBotError):
    """Live trading was requested but the explicit opt-in is not set.

    The off-by-default guard for going live. The engine refuses to wire a live
    venue adapter for ``mode == "live"`` unless ``AppConfig.live_enabled`` is
    explicitly ``True`` — so a config that merely flips ``mode`` to ``"live"``
    (or a stray ``--live`` flag) can never reach a real venue by accident. Going
    live is a deliberate, documented choice: read the runbook
    (``doc/dev/09-go-live.md``), provide credentials and set ``live_enabled:
    true``. Distinct from :class:`BrokerError` (which gates on *credentials*
    once live is enabled): this gates on the *opt-in itself*.

    Parameters
    ----------
    msg : str, optional
        Human-readable detail. When omitted a default message naming the opt-in
        flag (``live_enabled``) and the runbook (``doc/dev/09-go-live.md``) is
        built.

    """

    #: The default refusal — names the opt-in flag and the runbook to read.
    _DEFAULT = (
        "live trading is not enabled: set live_enabled: true in the config "
        "(and provide credentials) after reading the go-live runbook at "
        "doc/dev/09-go-live.md. Paper trading is the default; live is "
        "off by default and must be opted into deliberately. No order placed."
    )

    def __init__(self, msg: str | None = None) -> None:
        super().__init__(msg if msg is not None else self._DEFAULT)


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
