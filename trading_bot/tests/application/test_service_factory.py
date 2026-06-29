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

import math

import pytest

from trading_bot.application.config import AppConfig, BrokerConfig
from trading_bot.application.events import EventBus, FillEvent
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.risk import RiskManager
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.brokers.binance import TESTNET_API_BASE, BinanceBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain.errors import (
    BrokerError,
    LiveTradingNotEnabled,
    RiskLimitBreached,
)
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.storage.sqlite_store import SqliteStore

_BTCUSD = Instrument(Symbol("BTC", "USD"))


def _fill_event(
    fill_id: str,
    side: OrderSide,
    *,
    qty: str,
    price: str,
    fee: str,
    ts: int = 1,
) -> FillEvent:
    """A :class:`FillEvent` for ``_BTCUSD`` — drive the engine's read-side views."""
    return FillEvent(
        Fill(
            fill_id=fill_id,
            client_order_id="cid",
            instrument=_BTCUSD,
            side=side,
            qty=money(qty),
            price=money(price),
            fee=money(fee),
            ts=ts,
        )
    )


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


def test_starting_capital_anchors_perf_equity_curve() -> None:
    """The config's ``starting_capital`` seeds the perf service's equity curve.

    After one fill, the equity point is ``starting_capital + realised PnL`` — so
    the curve no longer starts at zero (the gap-(a) closure: a meaningful anchor
    for the KPI ratios).
    """
    cfg = AppConfig.model_validate({"mode": "paper", "starting_capital": "100000"})
    engine = build_engine(cfg)

    # A fee-free flat buy has zero realised PnL → the equity point is exactly v0.
    engine.bus.emit(
        _fill_event("F0", OrderSide.BUY, qty="1", price="100", fee="0")
    )
    curve = engine.perf.equity_curve()
    assert len(curve) == 1
    assert curve[0] == money("100000")


def test_default_starting_capital_flows_to_perf() -> None:
    """With no explicit ``starting_capital`` the perf anchor is the 100000 default."""
    engine = build_engine(AppConfig())
    engine.bus.emit(
        _fill_event("F0", OrderSide.BUY, qty="1", price="100", fee="0")
    )
    assert engine.perf.equity_curve()[0] == money("100000")


def test_winning_run_gives_finite_nonzero_sharpe() -> None:
    """Verification on real data: a winning curve → a non-zero, finite Sharpe.

    Closing gap (a): with the equity curve anchored at a positive
    ``starting_capital`` it never sign-crosses, so a profitable round-trip yields
    a *meaningful* Sharpe (non-zero and finite) — where a zero-anchored curve
    would have made the estimator degenerate / return 0.0.
    """
    pytest.importorskip("fynance")  # engine.perf.sharpe() delegates to fynance
    cfg = AppConfig.model_validate({"mode": "paper", "starting_capital": "100000"})
    engine = build_engine(cfg)
    # A profitable round-trip: buy @100, sell @110 → realised PnL +10.
    engine.bus.emit(_fill_event("F1", OrderSide.BUY, qty="1", price="100", fee="0"))
    engine.bus.emit(_fill_event("F2", OrderSide.SELL, qty="1", price="110", fee="0"))

    sharpe = engine.perf.sharpe()
    assert sharpe != 0.0
    assert math.isfinite(sharpe)


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


def test_live_mode_not_enabled_refuses_regardless_of_credentials(
    monkeypatch,
) -> None:
    """Live OFF by default: ``mode="live"`` + ``live_enabled=False`` always raises.

    The opt-in gate fires *before* the credential check, so it refuses even when
    credentials are present — flipping ``mode`` alone (without ``live_enabled``)
    can never reach a real venue. The message points at the go-live runbook.
    """
    # Even with credentials present, the opt-in gate refuses first.
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")  # base64("secret")
    cfg = AppConfig(
        mode="live",
        live_enabled=False,
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
    )

    with pytest.raises(LiveTradingNotEnabled) as exc:
        build_engine(cfg)
    # The refusal names the runbook so the user knows the deliberate next step.
    assert "doc/dev/09-go-live.md" in str(exc.value)


def test_live_mode_not_enabled_default_refuses_no_credentials() -> None:
    """``live_enabled`` defaults to False → live refuses even with no credentials."""
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
    )
    assert cfg.live_enabled is False
    with pytest.raises(LiveTradingNotEnabled):
        build_engine(cfg)


def test_live_enabled_without_credentials_refuses() -> None:
    """Opt-in set but no credentials → the credential gate still raises BrokerError.

    With ``live_enabled=True`` the opt-in gate passes; the factory then enforces
    credentials and refuses a credential-less live venue (never falls back to
    paper). The two gates are independent: opt-in *and* a key are both required.
    """
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
    )

    # No KRAKEN_API_KEY/SECRET in the test env → KrakenBroker has no credentials.
    # Build it directly to assert the precondition the factory relies on.
    assert not KrakenBroker().has_credentials

    with pytest.raises(BrokerError):
        build_engine(cfg)


def test_live_enabled_with_credentials_builds_live_broker_no_order(
    monkeypatch,
) -> None:
    """Opt-in + credentials → the live adapter is *constructed* (no order sent).

    With both gates satisfied the factory builds the live ``KrakenBroker``
    object. Construction is pure (no network, no order) — the test only asserts
    the broker's *type* and that it reports credentials; it never calls it. This
    is the whole point of the off-by-default opt-in: enabling it constructs the
    adapter without trading.
    """
    from trading_bot.application.config import RiskConfig

    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")  # base64("secret")
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
        # A real-money live config must carry explicit risk limits (see below).
        risk=RiskConfig(
            max_order=money("1"),
            max_position=money("5"),
            max_daily_loss=money("1000"),
        ),
    )

    engine = build_engine(cfg)
    # The adapter object exists and is the live venue — but is never invoked,
    # so no order / no network call was ever made.
    assert isinstance(engine.broker, KrakenBroker)
    assert engine.broker.has_credentials


def test_live_without_risk_limits_refuses(monkeypatch) -> None:
    """A real-money live config with no risk limits refuses (BrokerError).

    `RiskConfig` defaults every limit to None (unconstrained); going live with an
    all-None config would trade with no size/exposure/daily-loss cap. With opt-in
    + credentials present, `build_engine` refuses and names the missing limits —
    *after* the credential gate (so this is the real-money completeness check).
    """
    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
    )  # no risk block → all limits None
    with pytest.raises(BrokerError, match="risk limits") as exc:
        build_engine(cfg)
    msg = str(exc.value)
    assert "max_order" in msg
    assert "max_position" in msg
    assert "max_daily_loss" in msg


def test_live_with_partial_risk_limits_refuses_naming_the_gaps(monkeypatch) -> None:
    """Partial limits still refuse, naming exactly the unset ones."""
    from trading_bot.application.config import RiskConfig

    monkeypatch.setenv("KRAKEN_API_KEY", "k")
    monkeypatch.setenv("KRAKEN_API_SECRET", "c2VjcmV0")
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="kraken-main", exchange="kraken")],
        risk=RiskConfig(max_order=money("1")),  # the other two still None
    )
    with pytest.raises(BrokerError, match="risk limits") as exc:
        build_engine(cfg)
    msg = str(exc.value)
    assert "max_position" in msg
    assert "max_daily_loss" in msg
    assert "max_order" not in msg.split("unset")[0]  # max_order is set, not listed


def test_live_mode_unknown_venue_refuses() -> None:
    """Live mode (opted in) with an unknown venue raises clearly (no fallback)."""
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="x", exchange="not-a-venue")],
    )
    with pytest.raises(BrokerError):
        build_engine(cfg)


def test_live_mode_no_broker_configured_refuses() -> None:
    """Live mode with no configured broker raises — never trade without a venue."""
    cfg = AppConfig(mode="live", live_enabled=True)
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


# --- testnet path: a venue sandbox between paper and live -------------------- #


def test_testnet_binance_builds_pinned_adapter_without_live_enabled(
    monkeypatch,
) -> None:
    """``testnet: true`` builds the venue's testnet adapter — no ``live_enabled``.

    Binance has a spot testnet. A broker with ``testnet: true`` + credentials
    builds a :class:`BinanceBroker` **hard-pinned** to the testnet URL, *without*
    requiring the ``live_enabled`` opt-in (it cannot reach mainnet — paper money).
    Constructing the adapter sends no order.
    """
    monkeypatch.setenv("BINANCE_API_KEY", "tk")
    monkeypatch.setenv("BINANCE_API_SECRET", "ts")
    cfg = AppConfig(
        mode="live",
        live_enabled=False,  # NOT set — testnet does not need it
        brokers=[BrokerConfig(name="bn", exchange="binance", testnet=True)],
    )
    engine = build_engine(cfg)
    assert isinstance(engine.broker, BinanceBroker)
    assert engine.broker.is_testnet
    assert engine.broker.base_url == TESTNET_API_BASE


def test_testnet_hard_pins_url_ignoring_mainnet_env(monkeypatch) -> None:
    """``testnet: true`` forces the testnet URL even if env points at mainnet."""
    monkeypatch.setenv("BINANCE_API_KEY", "tk")
    monkeypatch.setenv("BINANCE_API_SECRET", "ts")
    # A stray env var pointing at mainnet must NOT leak through the testnet flag.
    monkeypatch.setenv("BINANCE_API_BASE", "https://api.binance.com")
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="bn", exchange="binance", testnet=True)],
    )
    engine = build_engine(cfg)
    assert isinstance(engine.broker, BinanceBroker)
    assert engine.broker.base_url == TESTNET_API_BASE  # env override ignored


def test_testnet_without_credentials_refuses(monkeypatch) -> None:
    """Testnet still needs (testnet) credentials → ``BrokerError`` without them."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="bn", exchange="binance", testnet=True)],
    )
    with pytest.raises(BrokerError, match="credentials"):
        build_engine(cfg)


def test_testnet_kraken_refuses() -> None:
    """Kraken has no public spot testnet → ``testnet: true`` raises clearly."""
    cfg = AppConfig(
        mode="live",
        brokers=[BrokerConfig(name="kr", exchange="kraken", testnet=True)],
    )
    with pytest.raises(BrokerError, match="no testnet"):
        build_engine(cfg)


def test_paper_mode_ignores_testnet() -> None:
    """Paper is the default and wins: ``testnet: true`` under paper → simulator."""
    cfg = AppConfig(
        mode="paper",
        brokers=[BrokerConfig(name="bn", exchange="binance", testnet=True)],
    )
    engine = build_engine(cfg)
    assert isinstance(engine.broker, PaperBroker)


def test_brokerconfig_testnet_defaults_false() -> None:
    """``testnet`` defaults to False (a plain broker is mainnet/live)."""
    assert BrokerConfig(name="bn", exchange="binance").testnet is False


# --- the daily-loss circuit breaker is wired to live PnL ------------------- #


def test_build_engine_wires_daily_loss_provider_to_performance_service() -> None:
    """``build_engine`` feeds the risk gate the day's realised PnL from ``perf``.

    Verification on real engine state: with ``max_daily_loss`` set, a BUY then a
    lower SELL are emitted as fills on the engine bus (realising a loss in the
    shared :class:`PerformanceService`). The risk gate — which previously saw a
    constant zero and never engaged — now reads that loss through the wired
    provider and refuses the next order with ``max_daily_loss``.
    """
    from trading_bot.application.config import RiskConfig

    config = AppConfig(risk=RiskConfig(max_daily_loss=money("5")))
    engine = build_engine(config)

    # Before any loss, the gate passes.
    probe = Order(
        client_order_id="probe-0",
        instrument=_BTCUSD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.LIMIT,
        limit_price=money("100"),
    )
    engine.risk.check(probe)  # no raise — flat day

    # Realise a loss of 10 (BUY 1 @ 100, SELL 1 @ 90) via fills on the bus, so the
    # shared performance service reports realised_pnl == -10.
    engine.bus.emit(_fill_event("F1", OrderSide.BUY, qty="1", price="100", fee="0"))
    engine.bus.emit(_fill_event("F2", OrderSide.SELL, qty="1", price="90", fee="0"))
    assert engine.perf.realised_pnl() == money("-10")

    # The gate now reads that loss through the wired provider and halts.
    with pytest.raises(RiskLimitBreached) as excinfo:
        engine.risk.check(probe)
    assert excinfo.value.limit == "max_daily_loss"
    assert excinfo.value.value == money("10")  # the loss magnitude
