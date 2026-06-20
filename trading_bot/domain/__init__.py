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
* errors — the :class:`~trading_bot.domain.errors.TradingBotError` hierarchy.
"""

from __future__ import annotations

from trading_bot.domain.errors import (
    InsufficientFunds,
    MissingOrder,
    NoCapability,
    OrderError,
    OrderStatusError,
    RiskLimitBreached,
    TradingBotError,
)
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
    # errors
    "TradingBotError",
    "OrderError",
    "OrderStatusError",
    "MissingOrder",
    "InsufficientFunds",
    "RiskLimitBreached",
    "NoCapability",
]
