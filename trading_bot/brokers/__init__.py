"""trading_bot brokers layer — venue adapters behind a :class:`Broker` port.

This is the **execution layer**: the venue-neutral
:class:`~trading_bot.brokers.base.Broker` contract that every exchange adapter
implements and a :class:`~trading_bot.brokers.base.Capability` model declaring
what a given adapter actually serves. The port speaks :mod:`trading_bot.domain`
types only and its concrete adapters use the :mod:`trading_bot.transport`
plumbing; the domain never imports a broker. Venue selection lives in
:func:`~trading_bot.application.service_factory.build_engine` (an explicit
per-venue dispatch), not a registry.

The :class:`~trading_bot.brokers.paper.PaperBroker` is the in-process default
(paper-trading) adapter; :class:`~trading_bot.brokers.kraken.KrakenBroker` is the
first live venue adapter behind this port.

Public surface:

* :class:`~trading_bot.brokers.base.Broker` — the async, runtime-checkable
  :class:`~typing.Protocol` every venue adapter satisfies;
* :class:`~trading_bot.brokers.base.Capability` — the operations an adapter may
  declare it supports;
* :func:`~trading_bot.brokers.base.require` — the gate that raises
  :class:`~trading_bot.domain.errors.NoCapability` when an adapter is asked for
  an operation it has not declared;
* :class:`~trading_bot.domain.errors.BrokerError` — the venue-neutral broker
  failure (re-exported for convenience);
* :class:`~trading_bot.brokers.kraken.KrakenBroker` — the concrete Kraken REST
  adapter (signed orders/balances/fills + public market data);
* :class:`~trading_bot.brokers.binance.BinanceBroker` — the concrete Binance spot
  REST adapter (HMAC-SHA256-signed orders/balances/fills + public market data,
  testnet-capable);
* :class:`~trading_bot.brokers.kraken_ws.KrakenPrivateWS` — the Kraken v2 private
  WebSocket adapter streaming ``executions`` (own trades / order updates) into
  domain :class:`~trading_bot.domain.fill.Fill`s (auth-token flow; live private
  connection gated on credentials, parse path mock-verified);
* :class:`~trading_bot.brokers.paper.PaperBroker` — the in-process, deterministic
  fill simulator and **default** broker (no venue, no key, no network).
"""

from __future__ import annotations

from trading_bot.brokers.base import Broker, BrokerError, Capability, require
from trading_bot.brokers.binance import BinanceBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.kraken_ws import KrakenPrivateWS
from trading_bot.brokers.paper import PaperBroker

__all__ = [
    "Broker",
    "Capability",
    "require",
    "BrokerError",
    "BinanceBroker",
    "KrakenBroker",
    "KrakenPrivateWS",
    "PaperBroker",
]
