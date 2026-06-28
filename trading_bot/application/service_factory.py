"""The service factory — the engine's **single wiring point**.

:func:`build_engine` is the one place the whole engine is assembled from a
validated :class:`~trading_bot.application.config.AppConfig`. It constructs every
use-case (the :class:`~trading_bot.application.order_router.OrderRouter`, the
:class:`~trading_bot.application.position_tracker.PositionTracker`, the
:class:`~trading_bot.application.performance_service.PerformanceService`, the
:class:`~trading_bot.application.risk.RiskManager`), the
:class:`~trading_bot.brokers.base.Broker` adapter, the shared
:class:`~trading_bot.application.events.EventBus` and an optional
:class:`~trading_bot.storage.sqlite_store.SqliteStore`, wires them onto **one**
bus, and returns them packaged in a frozen :class:`Engine`.

Why a single wiring point (carried into the ADR)
------------------------------------------------
Construction order matters — the tracker must subscribe to the bus before fills
flow, the router must hold the risk gate, the store must attach to the same bus
the broker emits on. Concentrating that ordering in one factory keeps every
caller (the CLI, tests, a future daemon) building an identically-wired engine,
and gives the codebase a single seam to evolve when a new collaborator is added.
Mirrors dccd's ``application`` wiring: the interfaces layer never news-up a
use-case itself; it asks the factory.

Broker selection — paper by default, live only on explicit opt-in
-----------------------------------------------------------------
The **paper-by-default** invariant lives here. The broker is chosen by
``config.mode`` and the configured venue:

* ``mode == "paper"`` (the :class:`AppConfig` default) → always a
  :class:`~trading_bot.brokers.paper.PaperBroker`. No venue, no key, no network
  — a fresh config can never trade real money.
* ``mode == "live"`` → the configured venue's adapter is built **only if it has
  credentials**. The first :class:`~trading_bot.application.config.BrokerConfig`
  selects the venue (``exchange``); a known live venue (``"kraken"``) without
  credentials, an unknown venue, or no broker configured at all each raise a
  clear :class:`~trading_bot.domain.errors.BrokerError` — the factory **never**
  silently falls back to paper and **never** trades live by accident.

A ``"paper"`` exchange entry is also honoured under either mode, so a config can
name the simulator explicitly.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from trading_bot.application.config import AppConfig, BrokerConfig
from trading_bot.application.events import EventBus
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.risk import RiskManager
from trading_bot.brokers.base import Broker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain.errors import BrokerError
from trading_bot.storage.sqlite_store import SqliteStore

__all__ = ["Engine", "build_engine"]

#: Venue keys recognised as live (non-simulated) adapters.
_LIVE_VENUES = ("kraken",)
#: The venue key for the in-process simulator.
_PAPER_VENUE = "paper"


@dataclass(frozen=True, slots=True)
class Engine:
    """The assembled engine — every wired collaborator, ready to run.

    A frozen bundle returned by :func:`build_engine`: the single object the
    interfaces layer (CLI, future daemon) holds to drive the engine. Every field
    shares the one :class:`~trading_bot.application.events.EventBus`, so an
    :class:`~trading_bot.application.events.OrderEvent` /
    :class:`~trading_bot.application.events.FillEvent` emitted by the broker or
    the router reaches the tracker, the performance service and (when present)
    the store automatically.

    Attributes
    ----------
    config : AppConfig
        The validated configuration the engine was built from.
    bus : EventBus
        The shared pub/sub bus every collaborator emits on / subscribes to.
    broker : Broker
        The selected venue adapter — a
        :class:`~trading_bot.brokers.paper.PaperBroker` in paper mode, the live
        venue adapter (e.g. :class:`~trading_bot.brokers.kraken.KrakenBroker`)
        in live mode.
    router : OrderRouter
        The idempotent write path, gated by ``risk`` and routing to ``broker``.
    tracker : PositionTracker
        The live net-position view, subscribed to the bus's fills.
    perf : PerformanceService
        The read-side PnL/KPI view, subscribed to the bus's fills.
    risk : RiskManager
        The pre-trade gate + kill-switch the router consults before every order.
    store : SqliteStore or None
        The append-only order/fill history, attached to the bus. ``None`` when
        no ``db_path`` was given to :func:`build_engine`.

    """

    config: AppConfig
    bus: EventBus
    broker: Broker
    router: OrderRouter
    tracker: PositionTracker
    perf: PerformanceService
    risk: RiskManager
    store: SqliteStore | None


def build_engine(
    config: AppConfig, *, db_path: str | pathlib.Path | None = None
) -> Engine:
    """Assemble a fully-wired :class:`Engine` from ``config``.

    The single wiring point: builds one :class:`~trading_bot.application.events.
    EventBus`, selects the broker per ``config.mode`` (paper by default; live
    only with credentials — see the module docstring), constructs the tracker,
    performance service, risk manager and router onto that bus, optionally
    attaches a :class:`~trading_bot.storage.sqlite_store.SqliteStore`, and
    returns them in a frozen :class:`Engine`.

    Parameters
    ----------
    config : AppConfig
        The validated engine configuration (mode, brokers, risk limits).
    db_path : str or pathlib.Path, optional
        Where to persist order/fill history. When given, a
        :class:`~trading_bot.storage.sqlite_store.SqliteStore` is created and
        attached to the bus (so it fills itself from the event stream); when
        ``None`` (default) the engine runs with no store
        (:attr:`Engine.store` is ``None``).

    Returns
    -------
    Engine
        The wired engine — every collaborator sharing one bus.

    Raises
    ------
    BrokerError
        In live mode, if the configured venue lacks credentials, is unknown, or
        no broker is configured at all. The factory never falls back to paper.

    """
    bus = EventBus()

    broker = _build_broker(config, bus)

    tracker = PositionTracker(event_bus=bus)
    # Seed the equity curve with the configured starting capital so the KPI
    # ratios are computed over a strictly-positive account value (the curve does
    # not sign-cross), making Sharpe/Sortino/Calmar over a real run meaningful.
    perf = PerformanceService(v0=config.starting_capital, event_bus=bus)
    risk = RiskManager(config.risk, position_tracker=tracker)
    router = OrderRouter(broker, bus, risk_manager=risk)

    store: SqliteStore | None = None
    if db_path is not None:
        store = SqliteStore(db_path)
        store.attach(bus)

    return Engine(
        config=config,
        bus=bus,
        broker=broker,
        router=router,
        tracker=tracker,
        perf=perf,
        risk=risk,
        store=store,
    )


def _build_broker(config: AppConfig, bus: EventBus) -> Broker:
    """Select and construct the broker for ``config`` — paper-by-default.

    In paper mode (the default), always a bus-wired
    :class:`~trading_bot.brokers.paper.PaperBroker`. In live mode, the configured
    venue's adapter, built only when it has credentials; an explicit ``"paper"``
    venue entry yields the simulator under either mode. Refuses (raises
    :class:`~trading_bot.domain.errors.BrokerError`) rather than falling back to
    paper for a live venue that cannot trade.
    """
    venue = _selected_venue(config)

    if config.mode == "paper" or venue == _PAPER_VENUE:
        # Paper-by-default: the simulator, wired to the bus so its fills fan out.
        return PaperBroker(event_bus=bus)

    # mode == "live" with a non-paper venue: build the real adapter, but only if
    # it can actually trade. Never silently downgrade to paper.
    if venue in _LIVE_VENUES:
        broker = _build_live_venue(venue)
        if not broker.has_credentials:
            raise BrokerError(
                f"live mode requires credentials for venue {venue!r}; "
                "set the venue's API key/secret in the environment "
                "(refusing to trade live without credentials)"
            )
        return broker

    raise BrokerError(
        f"live mode: unknown venue {venue!r}; "
        f"known live venues are {sorted(_LIVE_VENUES)!r}"
    )


def _selected_venue(config: AppConfig) -> str:
    """The venue key the engine should use — the first configured broker's.

    Paper mode needs no configured broker (it always uses the simulator), so a
    missing broker defaults to ``"paper"`` there. Live mode with no broker
    configured is an error surfaced by :func:`_build_broker`.
    """
    first: BrokerConfig | None = config.brokers[0] if config.brokers else None
    if first is None:
        if config.mode == "live":
            raise BrokerError(
                "live mode requires a configured broker, but none was given "
                "(refusing to trade live without an explicit venue)"
            )
        return _PAPER_VENUE
    return first.exchange.lower()


def _build_live_venue(venue: str) -> KrakenBroker:
    """Construct the live adapter for ``venue`` (reads credentials from env)."""
    if venue == "kraken":
        # KrakenBroker reads KRAKEN_API_KEY / KRAKEN_API_SECRET from the
        # environment; ``has_credentials`` reports whether both are present.
        return KrakenBroker()
    # Unreachable: callers gate on ``_LIVE_VENUES`` first. Defensive only.
    raise BrokerError(f"no live adapter for venue {venue!r}")
