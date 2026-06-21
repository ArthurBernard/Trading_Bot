"""trading_bot application layer — the engine: use-cases + cross-cutting glue.

This package is the orchestration layer of the hexagon. It may import the inner
layers (:mod:`trading_bot.domain`, :mod:`trading_bot.transport`,
:mod:`trading_bot.brokers`) and is itself imported by the outer
``interfaces`` layer (CLI / API, not yet built). It opens with two
cross-cutting primitives:

* config — the pydantic :class:`~trading_bot.application.config.AppConfig`
  (with :class:`~trading_bot.application.config.BrokerConfig`,
  :class:`~trading_bot.application.config.StrategyConfig` and
  :class:`~trading_bot.application.config.RiskConfig`): the engine's declared,
  YAML-loadable shape. ``mode`` defaults to ``"paper"`` — a fresh config never
  trades real money by accident;
* events — the async :class:`~trading_bot.application.events.EventBus` and its
  event taxonomy (:class:`~trading_bot.application.events.OrderEvent`,
  :class:`~trading_bot.application.events.FillEvent`,
  :class:`~trading_bot.application.events.LogEvent`): the pub/sub fan-out the
  router, the position tracker and a future UI consume. Events carry domain
  objects, so money stays :class:`~decimal.Decimal` end to end.

It then layers the engine's first use-case:

* order_router — the :class:`~trading_bot.application.order_router.OrderRouter`,
  the engine's idempotent write path: it submits domain orders to a
  :class:`~trading_bot.brokers.base.Broker` (deduped by client-order-id), drives
  each through its lifecycle state machine, and emits ``OrderEvent``\\ s.
"""

from __future__ import annotations

from trading_bot.application.config import (
    AppConfig,
    BrokerConfig,
    RiskConfig,
    StrategyConfig,
)
from trading_bot.application.events import (
    Event,
    EventBus,
    FillEvent,
    LogEvent,
    OrderEvent,
)
from trading_bot.application.order_router import OrderRouter

__all__ = [
    # config
    "AppConfig",
    "BrokerConfig",
    "StrategyConfig",
    "RiskConfig",
    # events
    "EventBus",
    "Event",
    "OrderEvent",
    "FillEvent",
    "LogEvent",
    # use-cases
    "OrderRouter",
]
