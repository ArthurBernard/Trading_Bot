"""Tests for the service factory — the engine's single wiring point.

These prove the wiring contract:

* a default (paper) :class:`AppConfig` builds a :class:`PaperBroker`, with the
  router / risk / tracker / perf all sharing the **one** bus and no store unless
  a ``db_path`` is given;
* the **paper-by-default** invariant holds — a ``live``-mode config whose venue
  lacks credentials *refuses* (raises) instead of returning a broker;
* the assembled engine is **live end to end**: an order submitted through the
  engine's router produces a fill that reaches the tracker (position moves) and
  the performance service (realised PnL / fees folded in) — the verification on
  real data, in-process against the simulator.
"""

from __future__ import annotations

import pytest

from trading_bot.application.config import AppConfig, BrokerConfig
from trading_bot.application.events import EventBus
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.risk import RiskManager
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain.errors import BrokerError
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.storage.sqlite_store import SqliteStore

_BTCUSD = Instrument(Symbol("BTC", "USD"))


def test_default_config_builds_paper_engine() -> None:
    """A default (paper) config wires a PaperBroker + every use-case on one bus."""
    engine = build_engine(AppConfig())

    assert isinstance(engine, Engine)
    assert isinstance(engine.broker, PaperBroker)
    assert isinstance(engine.bus, EventBus)
    assert isinstance(engine.router, OrderRouter)
    assert isinstance(engine.tracker, PositionTracker)
    assert isinstance(engine.perf, PerformanceService)
    assert isinstance(engine.risk, RiskManager)
    # No db_path → no store.
    assert engine.store is None


def test_paper_engine_shares_one_bus() -> None:
    """Tracker + perf subscribe to the *same* bus the broker emits fills on.

    A fill emitted on ``engine.bus`` must reach both read-side views without any
    further wiring — proof they share the single bus, not separate ones.
    """
    engine = build_engine(AppConfig())
    # Emitting a fill on the shared bus must fan out to tracker + perf.
    from trading_bot.application.events import FillEvent
    from trading_bot.domain.fill import Fill

    fill = Fill(
        fill_id="F1",
        client_order_id="cid-bus",
        instrument=_BTCUSD,
        side=OrderSide.BUY,
        qty=money("1"),
        price=money("100"),
        fee=money("0"),
        ts=1,
    )
    engine.bus.emit(FillEvent(fill))

    assert engine.tracker.position(_BTCUSD) is not None
    assert engine.tracker.position(_BTCUSD).net_qty == money("1")
    assert engine.perf.position(_BTCUSD) is not None


def test_db_path_attaches_sqlite_store(tmp_path) -> None:
    """A db_path builds and attaches a SqliteStore; events flow into it."""
    db = tmp_path / "engine.db"
    engine = build_engine(AppConfig(), db_path=db)

    assert isinstance(engine.store, SqliteStore)

    # The store is attached to the bus: an OrderEvent persists the order.
    from trading_bot.application.events import OrderEvent

    order = Order(
        client_order_id="cid-store",
        instrument=_BTCUSD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )
    engine.bus.emit(OrderEvent(order))

    assert engine.store.get_order("cid-store") is not None


def test_live_mode_without_credentials_refuses() -> None:
    """A live-mode config whose venue lacks credentials raises — no broker back.

    The paper-by-default invariant: live trading is opt-in *and* gated on
    credentials. Without them the factory must refuse, never silently fall back
    to paper and never return a live broker that cannot trade.
    """
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
    )

    # No KRAKEN_API_KEY/SECRET in the test env → KrakenBroker has no credentials.
    # Build it directly to assert the precondition the factory relies on.
    assert not KrakenBroker().has_credentials

    with pytest.raises(BrokerError):
        build_engine(cfg)


def test_live_mode_with_credentials_builds_live_broker(monkeypatch) -> None:
    """Live mode + credentials present → the live venue adapter is built."""
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")  # base64("secret")
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
    )

    engine = build_engine(cfg)
    assert isinstance(engine.broker, KrakenBroker)
    assert engine.broker.has_credentials


def test_live_mode_unknown_venue_refuses() -> None:
    """Live mode with an unknown venue raises clearly (no silent fallback)."""
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="x", exchange="not-a-venue")],
    )
    with pytest.raises(BrokerError):
        build_engine(cfg)


def test_live_mode_no_broker_configured_refuses() -> None:
    """Live mode with no configured broker raises — never trade without a venue."""
    cfg = AppConfig(mode="live")
    with pytest.raises(BrokerError):
        build_engine(cfg)


def test_paper_venue_named_explicitly_builds_paper() -> None:
    """An explicit 'paper' venue yields the simulator (even under live mode)."""
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="sim", exchange="paper")],
    )
    engine = build_engine(cfg)
    assert isinstance(engine.broker, PaperBroker)


async def test_engine_is_live_end_to_end() -> None:
    """Verification on real data: order → fill → tracker + perf, in-process.

    Submit one real order through the wired router against the PaperBroker and
    assert the broker-confirmed fill moved the tracker's position *and* folded
    into the performance service — proof the factory wired a live engine, not a
    bag of disconnected objects.
    """
    engine = build_engine(AppConfig())
    # The PaperBroker needs a mark price to fill a MARKET order.
    engine.broker.set_price(_BTCUSD, money("100"))

    order = Order(
        client_order_id="cid-e2e",
        instrument=_BTCUSD,
        side=OrderSide.BUY,
        qty=money("2"),
        type=OrderType.MARKET,
    )
    tracked = await engine.router.submit(order)

    # Router drove the lifecycle.
    assert tracked.client_order_id == "cid-e2e"
    assert tracked.venue_order_id is not None

    # The fill fanned out to the tracker: position moved to +2 BTC.
    position = engine.tracker.position(_BTCUSD)
    assert position is not None
    assert position.net_qty == money("2")

    # And to the performance service: a fee was charged (realised PnL net of it).
    assert engine.perf.fees_paid() > money("0")
    assert len(engine.perf.equity_curve()) == 1
