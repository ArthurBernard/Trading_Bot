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
* ``mode == "live"`` → live is **off by default**. The factory first checks the
  explicit opt-in :attr:`~trading_bot.application.config.AppConfig.live_enabled`:
  while it is ``False`` (the default), live raises
  :class:`~trading_bot.domain.errors.LiveTradingNotEnabled` (pointing at the
  go-live runbook, ``doc/dev/09-go-live.md``) — so flipping ``mode`` alone never
  reaches a real venue. Only when ``live_enabled`` is ``True`` does the venue's
  adapter get built, and then **only if it has credentials**. The first
  :class:`~trading_bot.application.config.BrokerConfig` selects the venue
  (``exchange``); a known live venue (``"kraken"``) without credentials, an
  unknown venue, or no broker configured at all each raise a clear
  :class:`~trading_bot.domain.errors.BrokerError` — the factory **never**
  silently falls back to paper and **never** trades live by accident.

A ``"paper"`` exchange entry is also honoured under either mode, so a config can
name the simulator explicitly.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from trading_bot.application.config import AppConfig, BrokerConfig
from trading_bot.application.events import EventBus
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.risk import RiskManager
from trading_bot.brokers.base import Broker
from trading_bot.brokers.binance import TESTNET_API_BASE, BinanceBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain.errors import BrokerError, LiveTradingNotEnabled
from trading_bot.storage.sqlite_store import SqliteStore

__all__ = ["Engine", "build_engine"]

#: Venue keys recognised as live (non-simulated) adapters.
_LIVE_VENUES = ("kraken", "binance")
#: Venue keys that offer a testnet/sandbox (paper money on the real venue).
#: Kraken has no public spot testnet, so it is deliberately absent.
_TESTNET_VENUES = ("binance",)
#: The venue key for the in-process simulator.
_PAPER_VENUE = "paper"
#: The go-live runbook the live-opt-in refusals point users at.
_RUNBOOK = "doc/dev/09-go-live.md"


@runtime_checkable
class _LiveBroker(Broker, Protocol):
    """A live venue adapter: a :class:`Broker` that also reports credentials.

    The credential gate in :func:`_build_broker` consults ``has_credentials``,
    which is *not* part of the venue-neutral :class:`Broker` port (the simulator
    has no credentials concept). Every live adapter
    (:class:`~trading_bot.brokers.kraken.KrakenBroker`,
    :class:`~trading_bot.brokers.binance.BinanceBroker`) exposes it, so this
    narrow structural extension of the port lets the factory check it (and return
    a value still assignable to :class:`Broker`) without widening the port itself.
    """

    @property
    def has_credentials(self) -> bool:
        """Whether the adapter holds both API key and secret."""
        ...


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
    LiveTradingNotEnabled
        In live mode when the explicit opt-in
        :attr:`~trading_bot.application.config.AppConfig.live_enabled` is
        ``False`` (the default) — live is off by default; the message points at
        the go-live runbook. Checked *before* credentials, so it fires
        regardless of whether keys are present.
    BrokerError
        In live mode (with ``live_enabled`` set), if the configured venue lacks
        credentials, is unknown, or no broker is configured at all. The factory
        never falls back to paper.

    """
    bus = EventBus()

    broker = _build_broker(config, bus)

    tracker = PositionTracker(event_bus=bus)
    # Seed the equity curve with the configured starting capital so the KPI
    # ratios are computed over a strictly-positive account value (the curve does
    # not sign-cross), making Sharpe/Sortino/Calmar over a real run meaningful.
    perf = PerformanceService(v0=config.starting_capital, event_bus=bus)
    # Wire the daily-loss circuit breaker to the live PnL: the risk manager reads
    # the day's *signed realised PnL* (a loss is negative) straight off the
    # performance service. Without this, ``max_daily_loss`` saw a constant zero and
    # never engaged; with it, once the day's realised loss reaches the limit the
    # gate refuses every new order (and the router escalates to the kill-switch —
    # cancelling resting orders + halting — on that breach). "Daily" here is the run
    # session (no clock); a multi-day reset wires ``reset_day`` to a scheduler.
    risk = RiskManager(
        config.risk,
        position_tracker=tracker,
        daily_pnl_provider=perf.realised_pnl,
    )
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
    :class:`~trading_bot.brokers.paper.PaperBroker`. In live mode, live is **off
    by default**: unless :attr:`~trading_bot.application.config.AppConfig.
    live_enabled` is ``True`` the live path raises
    :class:`~trading_bot.domain.errors.LiveTradingNotEnabled` (the opt-in gate,
    checked before credentials); only then is the configured venue's adapter
    built, and only when it has credentials. An explicit ``"paper"`` venue entry
    yields the simulator under either mode (no opt-in needed — it cannot trade
    real money). Refuses (raises
    :class:`~trading_bot.domain.errors.BrokerError`) rather than falling back to
    paper for a live venue that cannot trade.

    **Testnet** is a third path between paper and live: a broker with
    ``testnet: true`` (a venue that has a sandbox — Binance, not Kraken) builds an
    adapter **hard-pinned** to the venue's testnet URL. Because it structurally
    cannot reach mainnet (paper money), it is exempt from the ``live_enabled``
    opt-in — but it still requires (testnet) credentials. Checked *before* the
    ``live_enabled`` gate; ignored in paper mode (the simulator wins).
    """
    venue = _selected_venue(config)

    if config.mode == "paper" or venue == _PAPER_VENUE:
        # Paper-by-default: the simulator, wired to the bus so its fills fan out.
        return PaperBroker(event_bus=bus)

    # Testnet path: a venue's sandbox (paper money on the real testnet venue). The
    # adapter is **hard-pinned** to the testnet endpoint (it cannot reach mainnet),
    # so it does NOT need the `live_enabled` opt-in — checked *before* that gate.
    # It still requires (testnet) credentials. `testnet: true` on the selected
    # broker is what opts in; a venue with no testnet raises.
    first: BrokerConfig | None = config.brokers[0] if config.brokers else None
    if first is not None and first.testnet:
        broker = _build_testnet_venue(venue)
        if not broker.has_credentials:
            raise BrokerError(
                f"testnet for venue {venue!r} requires credentials; set the "
                "venue's (testnet) API key/secret in the environment "
                "(refusing to trade without credentials)"
            )
        return broker

    # mode == "live" with a non-paper venue. Live is OFF by default: require the
    # explicit opt-in *before* even looking at credentials, so flipping `mode`
    # alone (or a stray --live) can never reach a real venue. No order is sent
    # by constructing the adapter; this only gates whether it is built at all.
    if not config.live_enabled:
        raise LiveTradingNotEnabled(
            "live trading is not enabled (live_enabled is False): set "
            "live_enabled: true in the config (and provide credentials) after "
            f"reading the go-live runbook at {_RUNBOOK}. Paper is the default; "
            "live is off by default. No order placed."
        )

    # Opt-in is set: build the real adapter, but only if it can actually trade.
    # Never silently downgrade to paper.
    if venue in _LIVE_VENUES:
        broker = _build_live_venue(venue)
        if not broker.has_credentials:
            raise BrokerError(
                f"live mode requires credentials for venue {venue!r}; "
                "set the venue's API key/secret in the environment "
                "(refusing to trade live without credentials)"
            )
        # Final live gate: a real-money engine must carry explicit risk limits — an
        # all-None RiskConfig would trade with no size/exposure/daily-loss cap.
        _require_live_risk_limits(config)
        return broker

    raise BrokerError(
        f"live mode: unknown venue {venue!r}; "
        f"known live venues are {sorted(_LIVE_VENUES)!r}"
    )


def _require_live_risk_limits(config: AppConfig) -> None:
    """Refuse a real-money live engine whose risk limits are not all set.

    A :class:`~trading_bot.application.config.RiskConfig` leaves every limit
    ``None`` (unconstrained) by default, so a live config with no ``risk:`` block
    would place orders with **no** size, exposure or daily-loss cap — unacceptable
    for real money. This gate requires ``max_order``, ``max_position`` **and**
    ``max_daily_loss`` to be set before the live adapter is returned, naming any
    that are missing. Reached **only** on the opted-in real-money path (``mode:
    live`` + ``live_enabled`` + credentials); paper mode and testnet (paper money)
    never get here, so they are exempt.
    """
    risk = config.risk
    missing = [
        name
        for name, value in (
            ("max_order", risk.max_order),
            ("max_position", risk.max_position),
            ("max_daily_loss", risk.max_daily_loss),
        )
        if value is None
    ]
    if missing:
        raise BrokerError(
            "live trading requires explicit risk limits, but "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} unset "
            "in RiskConfig (each None = unconstrained). Set max_order, "
            "max_position and max_daily_loss before going live (see "
            f"{_RUNBOOK}). No order placed."
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


def _build_live_venue(venue: str) -> _LiveBroker:
    """Construct the live adapter for ``venue`` (reads credentials from env)."""
    if venue == "kraken":
        # KrakenBroker reads KRAKEN_API_KEY / KRAKEN_API_SECRET from the
        # environment; ``has_credentials`` reports whether both are present.
        return KrakenBroker()
    if venue == "binance":
        # BinanceBroker reads BINANCE_API_KEY / BINANCE_API_SECRET (and the
        # optional BINANCE_API_BASE testnet toggle) from the environment;
        # ``has_credentials`` reports whether both key + secret are present.
        return BinanceBroker()
    # Unreachable: callers gate on ``_LIVE_VENUES`` first. Defensive only.
    raise BrokerError(f"no live adapter for venue {venue!r}")


def _build_testnet_venue(venue: str) -> _LiveBroker:
    """Construct a venue's **testnet** adapter, hard-pinned to its sandbox URL.

    Only venues in :data:`_TESTNET_VENUES` have a testnet. The base URL is forced
    to the venue's testnet endpoint (passed explicitly, so any ``BINANCE_API_BASE``
    env value is overridden) — the adapter can therefore never reach mainnet, which
    is why the caller skips the ``live_enabled`` opt-in for it. Credentials are
    still read from the environment (testnet keys). A venue with no testnet (e.g.
    Kraken, which has no public spot sandbox) raises.
    """
    if venue == "binance":
        # Hard-pin the testnet base URL (explicit arg overrides the env default),
        # so this adapter is structurally incapable of hitting api.binance.com.
        return BinanceBroker(base_url=TESTNET_API_BASE)
    raise BrokerError(
        f"venue {venue!r} has no testnet/sandbox; testnet is available for "
        f"{sorted(_TESTNET_VENUES)!r} only (Kraken has no public spot testnet — "
        "use paper, or real-money live behind the go-live runbook)"
    )
