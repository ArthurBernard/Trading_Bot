"""Pure domain primitives for the trading bot.

This package is the zero-dependency base of the domain: it is **pure,
synchronous and free of I/O**, and must never import from ``transport``,
``brokers``, ``storage`` or any I/O library. Everything here is exactly typed.

Public surface:

* money — exact :class:`~decimal.Decimal` price/quantity helpers
  (:data:`~trading_bot.domain.money.Money`, :func:`~trading_bot.domain.money.money`,
  :func:`~trading_bot.domain.money.from_float`,
  :func:`~trading_bot.domain.money.quantize`, ...);
* instrument — venue-neutral :class:`~trading_bot.domain.instrument.Symbol` /
  :class:`~trading_bot.domain.instrument.Instrument` with Kraken normalisation;
* order — the :class:`~trading_bot.domain.order.Order` aggregate and its
  lifecycle state machine (:class:`~trading_bot.domain.order.OrderSide`,
  :class:`~trading_bot.domain.order.OrderType`,
  :class:`~trading_bot.domain.order.OrderStatus`);
* fill — the immutable :class:`~trading_bot.domain.fill.Fill` execution record;
* position — the :class:`~trading_bot.domain.position.Position` net exposure
  rebuilt from fills;
* signal — the venue-neutral :class:`~trading_bot.domain.signal.Signal` strategy
  target (:class:`~trading_bot.domain.signal.SignalMode`) and its delta to a
  position;
* errors — the :class:`~trading_bot.domain.errors.TradingBotError` hierarchy.
"""

from __future__ import annotations

from trading_bot.domain.errors import (
    BrokerError,
    InstrumentMismatch,
    InsufficientFunds,
    MissingOrder,
    NoCapability,
    OrderError,
    OrderStatusError,
    RiskLimitBreached,
    SignalError,
    TradingBotError,
)
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import (
    Instrument,
    Symbol,
    normalise,
    parse_kraken_pair,
)
from trading_bot.domain.money import (
    Money,
    add,
    from_float,
    money,
    mul,
    quantize,
    sub,
)
from trading_bot.domain.order import (
    DEFAULT_FILL_TOLERANCE,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trading_bot.domain.performance import (
    PerformanceDependencyError,
    calmar,
    cum_pnl,
    equity_array,
    equity_curve,
    exchanged_volume,
    fee_series,
    max_drawdown,
    pnl,
    position_series,
    returns,
    sharpe,
    sortino,
)
from trading_bot.domain.position import Position
from trading_bot.domain.signal import Signal, SignalMode

__all__ = [
    # money
    "Money",
    "money",
    "from_float",
    "quantize",
    "add",
    "sub",
    "mul",
    # instrument
    "Symbol",
    "Instrument",
    "normalise",
    "parse_kraken_pair",
    # order
    "Order",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "DEFAULT_FILL_TOLERANCE",
    # fill
    "Fill",
    # position
    "Position",
    # signal
    "Signal",
    "SignalMode",
    # performance
    "returns",
    "exchanged_volume",
    "position_series",
    "fee_series",
    "pnl",
    "cum_pnl",
    "equity_curve",
    "equity_array",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "PerformanceDependencyError",
    # errors
    "TradingBotError",
    "OrderError",
    "OrderStatusError",
    "MissingOrder",
    "InstrumentMismatch",
    "InsufficientFunds",
    "RiskLimitBreached",
    "NoCapability",
    "BrokerError",
    "SignalError",
]
