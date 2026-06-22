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

It then layers the engine's use-cases:

* order_router — the :class:`~trading_bot.application.order_router.OrderRouter`,
  the engine's idempotent write path: it submits domain orders to a
  :class:`~trading_bot.brokers.base.Broker` (deduped by client-order-id), drives
  each through its lifecycle state machine, and emits ``OrderEvent``\\ s.
* position_tracker — the
  :class:`~trading_bot.application.position_tracker.PositionTracker`, the engine's
  read-back path: it folds broker-confirmed ``Fill``\\ s (off the ``EventBus`` or
  applied explicitly) into a live net
  :class:`~trading_bot.domain.position.Position` per instrument, the owner of
  exposure and realised PnL.
* performance_service — the
  :class:`~trading_bot.application.performance_service.PerformanceService`, the
  read-side performance view: it observes the same confirmed ``Fill``\\ s and
  reports aggregate realised PnL / fees, an equity curve (``v0`` + cumulative
  realised PnL) and the fynance-backed KPI ratios (Sharpe, Sortino, max drawdown,
  Calmar). Observational only — it never places an order.
* reconcile — :func:`~trading_bot.application.reconcile.reconcile` (and its
  :class:`~trading_bot.application.reconcile.ReconResult`): the *reconcile,
  don't assume* pass that, on startup or after a disconnect, refetches the
  venue's open orders, balances and fills and converges the router's tracked
  orders and the tracker's positions to that truth — never leaving a duplicated
  or lost order.
* strategy — the :class:`~trading_bot.application.strategy.Strategy` (instrument
  + a :data:`~trading_bot.application.strategy.SignalFn` callable that maps a
  bars frame to a domain ``Signal``), the safe
  :func:`~trading_bot.application.strategy.load_strategy` loader (no
  arbitrary-file exec), and the built-in
  :func:`~trading_bot.application.strategy.ma_crossover_signal` example.
* data_feed — the :class:`~trading_bot.application.data_feed.DataFeed` protocol
  (an iterator of growing **causal** bar windows — at step ``t`` only bars
  ``≤ t``, never a future bar) with the offline
  :class:`~trading_bot.application.data_feed.InMemoryFeed` and the dccd-backed
  :class:`~trading_bot.application.data_feed.DccdFeed` (injected client; thin
  coupling), the source of the bars frames a ``signal_fn`` evaluates.
* strategy_runner — the
  :class:`~trading_bot.application.strategy_runner.StrategyRunner`, the engine's
  live loop: it pulls causal windows from a ``DataFeed``, evaluates the
  ``Strategy``'s ``Signal``, diffs it against the ``PositionTracker``'s live
  position into a target delta, and submits the resulting ``Order`` through the
  ``OrderRouter`` with a deterministic per-step ``client_order_id`` (so a re-run
  dedups). No order during warmup or when already on target; causality is
  preserved by construction.
"""

from __future__ import annotations

from trading_bot.application.config import (
    AppConfig,
    BrokerConfig,
    RiskConfig,
    StrategyConfig,
)
from trading_bot.application.data_feed import (
    BARS_SCHEMA,
    DataFeed,
    DccdFeed,
    InMemoryFeed,
)
from trading_bot.application.events import (
    Event,
    EventBus,
    FillEvent,
    LogEvent,
    OrderEvent,
)
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.reconcile import ReconResult, reconcile
from trading_bot.application.strategy import (
    SignalFn,
    Strategy,
    load_strategy,
    ma_crossover_signal,
)
from trading_bot.application.strategy_runner import OrderFactory, StrategyRunner

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
    "PositionTracker",
    "PerformanceService",
    "reconcile",
    "ReconResult",
    # data feed
    "DataFeed",
    "InMemoryFeed",
    "DccdFeed",
    "BARS_SCHEMA",
    # strategy
    "Strategy",
    "SignalFn",
    "load_strategy",
    "ma_crossover_signal",
    # strategy runner
    "StrategyRunner",
    "OrderFactory",
]
