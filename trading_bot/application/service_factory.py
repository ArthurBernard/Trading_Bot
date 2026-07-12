"""The service factory — the engine's **single wiring point**.

:func:`build_engine` is the one place the whole engine is assembled from a
validated :class:`~trading_bot.application.config.AppConfig`. It constructs every
use-case (the :class:`~trading_bot.application.order_router.OrderRouter`, the
:class:`~trading_bot.application.order_fill_sync.OrderFillSync`, the
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

import os
import pathlib
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from trading_bot.application.config import AppConfig, BrokerConfig
from trading_bot.application.events import EventBus
from trading_bot.application.instrument_specs import InstrumentSpecResolver
from trading_bot.application.mark_cache import MarkCache
from trading_bot.application.order_fill_sync import OrderFillSync
from trading_bot.application.order_router import OrderRouter
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.position_tracker import PositionTracker
from trading_bot.application.risk import RiskManager
from trading_bot.brokers.base import Broker
from trading_bot.brokers.binance import TESTNET_API_BASE, BinanceBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain.errors import BrokerError, LiveTradingNotEnabled
from trading_bot.domain.money import Money
from trading_bot.storage.sqlite_store import SqliteStore

__all__ = ["Engine", "build_engine", "genesis_v0"]

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
    fill_sync : OrderFillSync
        The fill→order synchroniser: applies every venue-confirmed fill to the
        router's *tracked* :class:`~trading_bot.domain.order.Order` and re-emits
        the updated :class:`~trading_bot.application.events.OrderEvent` so the
        store row follows the order to its terminal state; its ``replay`` heals
        restored orders from persisted fills on startup.
    tracker : PositionTracker
        The live net-position view, subscribed to the bus's fills.
    perf : PerformanceService
        The read-side PnL/KPI view, subscribed to the bus's fills.
    risk : RiskManager
        The pre-trade gate + kill-switch the router consults before every order.
    store : SqliteStore or None
        The append-only order/fill history, attached to the bus. ``None`` when
        no ``db_path`` was given to :func:`build_engine`.
    spec_resolver : InstrumentSpecResolver
        The engine's venue instrument-spec resolver — one per engine (so, under
        the supervisor, one per unit), its ``(exchange, symbol)`` cache living
        exactly as long as the engine does. Threaded into the portfolio runners
        (:func:`~trading_bot.application.run_app.build_portfolio_runners`) so
        every rebalance leg is prepared against the venue's real minimums.
    mark_cache : MarkCache
        The engine's per-symbol mark cache — one per engine (so, under the
        supervisor, one per unit), living exactly as long as the engine does.
        The portfolio runner publishes every rebalance's per-symbol closes here
        (:func:`~trading_bot.application.run_app.build_portfolio_runners`); the
        API layer will read it (leaf 02) without any I/O of its own — see
        :mod:`~trading_bot.application.mark_cache`.

    """

    config: AppConfig
    bus: EventBus
    broker: Broker
    router: OrderRouter
    fill_sync: OrderFillSync
    tracker: PositionTracker
    perf: PerformanceService
    risk: RiskManager
    store: SqliteStore | None
    # default_factory (rather than a required field) keeps direct Engine(...)
    # constructions — tests wire engines by hand — working unchanged.
    spec_resolver: InstrumentSpecResolver = field(
        default_factory=InstrumentSpecResolver
    )
    mark_cache: MarkCache = field(default_factory=MarkCache)


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
    # Anchor the equity curve at the unit's genesis capital (its ``allocation``,
    # or a portfolio's ``capital``) when the config is a single-unit slice —
    # falling back to ``starting_capital`` for a multi-unit / legacy config that
    # declares no per-unit money (see ``genesis_v0``). A strictly-positive anchor
    # keeps the curve from sign-crossing, so the KPI ratios (Sharpe/Sortino/
    # Calmar) over a real run stay meaningful. KPI ratios stay on this fill-only
    # curve — a deposit is a capital movement, never a return.
    perf = PerformanceService(v0=genesis_v0(config), event_bus=bus)
    # Wire the daily-loss circuit breaker to the live PnL: the risk manager reads
    # the *current UTC day's* signed realised PnL (a loss is negative) off the
    # performance service via ``realised_pnl_since(day_start_ms)``. "Daily" is a
    # real UTC calendar day — the manager derives today's midnight from its clock
    # and asks the service for the realised PnL since then, so the window resets
    # automatically at the boundary (yesterday's loss no longer latches the book).
    # Once the day's realised loss reaches the limit the gate refuses every new
    # order (and the router escalates to the kill-switch — cancelling resting
    # orders + halting — on that breach) for the rest of that UTC day.
    risk = RiskManager(
        config.risk,
        position_tracker=tracker,
        daily_pnl_provider=perf.realised_pnl_since,
    )
    router = OrderRouter(broker, bus, risk_manager=risk)
    # Close the fill loop: without this, no component ever applies a venue-
    # confirmed fill to the router's *tracked* Order, so every filled order
    # freezes at OPEN/0 in the store and UI. Constructed right after the router
    # (it subscribes itself to the bus on construction), before the store
    # attaches, so its re-emitted OrderEvent is what the store persists.
    fill_sync = OrderFillSync(router, bus)

    store: SqliteStore | None = None
    if db_path is not None:
        store = SqliteStore(db_path)
        store.attach(bus)

    return Engine(
        config=config,
        bus=bus,
        broker=broker,
        router=router,
        fill_sync=fill_sync,
        tracker=tracker,
        perf=perf,
        risk=risk,
        store=store,
        # One venue-spec resolver per engine (= per supervised unit): its
        # (exchange, symbol) cache — and its keyless public adapters — live and
        # die with the engine. Constructed here, the single wiring point, and
        # threaded to the portfolio runners by build_portfolio_runners.
        spec_resolver=InstrumentSpecResolver(),
        # One mark cache per engine (= per supervised unit), same lifetime
        # discipline as spec_resolver above: constructed here and threaded to
        # the portfolio runners by build_portfolio_runners, which publish every
        # rebalance's per-symbol closes into it.
        mark_cache=MarkCache(),
    )


def genesis_v0(config: AppConfig) -> Money:
    """The equity-curve anchor for ``config`` — the unit's genesis capital.

    The KPI anchor ``v0`` :func:`build_engine` seeds the performance service
    with. When ``config`` is a **single-unit slice** (exactly one strategy and no
    portfolio, or exactly one portfolio and no strategy — the shape the
    :class:`~trading_bot.application.supervisor.StrategySupervisor` builds every
    unit's engine from), the anchor is that unit's **genesis capital**: a
    strategy's declared ``allocation`` (or a portfolio's ``allocation``, else its
    required ``capital``). A unit that declares no money base — a single-instrument
    strategy with no ``allocation`` — falls back to ``config.starting_capital``, as
    does any multi-unit / empty config (the whole-system
    :func:`~trading_bot.application.run_app.run_app` path, and every legacy
    manifest), so their anchoring is unchanged.

    Keeping this identical to the supervisor's own per-unit ``_v0_of`` is what
    makes a derived
    :meth:`~trading_bot.application.supervisor.StrategySupervisor.pnl_series` curve
    reconcile to the running engine's ``perf.realised_pnl()`` to the cent (same
    ``v0``, same fill fold).

    Parameters
    ----------
    config : AppConfig
        The engine configuration (typically a single-unit slice under the
        supervisor; the whole system under ``run_app``).

    Returns
    -------
    Money
        The genesis capital of the single declared unit, else
        ``config.starting_capital``.

    """
    strategies = config.strategies
    portfolios = config.portfolios
    if len(strategies) == 1 and not portfolios:
        allocation = strategies[0].allocation
        return allocation if allocation is not None else config.starting_capital
    if len(portfolios) == 1 and not strategies:
        portfolio = portfolios[0]
        return (
            portfolio.allocation
            if portfolio.allocation is not None
            else portfolio.capital
        )
    return config.starting_capital


def _build_broker(config: AppConfig, bus: EventBus) -> Broker:
    """Select and construct the broker for ``config`` — paper-by-default.

    In paper mode (the default), always a bus-wired
    :class:`~trading_bot.brokers.paper.PaperBroker`, built with
    ``strict=config.paper_strict`` (default ``True`` — the simulator enforces
    venue lot/precision quantization and minimum-order rejection exactly like a
    real venue; set ``paper_strict: false`` to restore the historical
    permissive model). In live mode, live is **off
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
        # The engine's simulator gets the REAL wall clock (epoch ms), not the
        # PaperBroker default (a deterministic 2024-01-01 base advancing +1ms per
        # fill, kept for reproducible tests): a live-test daemon's paper fills are
        # read by everything that trusts fill `ts` — the equity-curve x axis, the
        # Fills table, and the `max_daily_loss` breaker's `realised_pnl_since`
        # midnight window, which silently never matched fills stamped in 2024.
        # `strict` is threaded from `config.paper_strict` (default True): the
        # factory-built simulator predicts the venue's lot/precision quantization
        # and min_qty/min_notional rejection; the constructor's own default stays
        # permissive (`strict=False`) for direct/test construction.
        return PaperBroker(
            event_bus=bus,
            clock=lambda: int(time.time() * 1000),
            strict=config.paper_strict,
            # Explicit funding seam (paper simulators start unfunded): thread
            # `paper_starting_balances` so a factory-built paper engine — e.g.
            # the canary's — can be funded declaratively. Empty by default.
            starting_balances=config.paper_starting_balances,
        )

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


def _binance_testnet_credentials() -> tuple[str, str]:
    """The Binance **testnet** key/secret, from the environment.

    Testnet and mainnet are distinct credentials (a mainnet key is rejected by
    ``testnet.binance.vision`` with ``-2015``), so the testnet adapter reads the
    **testnet-specific** ``BINANCE_TESTNET_API_KEY`` / ``BINANCE_TESTNET_API_SECRET``
    first, falling back to the generic ``BINANCE_API_KEY`` / ``BINANCE_API_SECRET``
    for the older single-key setup where the only Binance key *was* the testnet one.
    Returns ``("", "")`` when absent (the caller's ``has_credentials`` gate refuses).
    """
    key = os.environ.get("BINANCE_TESTNET_API_KEY") or os.environ.get(
        "BINANCE_API_KEY", ""
    )
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or os.environ.get(
        "BINANCE_API_SECRET", ""
    )
    return key, secret


def _build_testnet_venue(venue: str) -> _LiveBroker:
    """Construct a venue's **testnet** adapter, hard-pinned to its sandbox URL.

    Only venues in :data:`_TESTNET_VENUES` have a testnet. The base URL is forced
    to the venue's testnet endpoint (passed explicitly, so any ``BINANCE_API_BASE``
    env value is overridden) — the adapter can therefore never reach mainnet, which
    is why the caller skips the ``live_enabled`` opt-in for it. Credentials are the
    venue's **testnet** keys (``BINANCE_TESTNET_*``, falling back to ``BINANCE_*``);
    see :func:`_binance_testnet_credentials`. A venue with no testnet (e.g. Kraken,
    which has no public spot sandbox) raises.
    """
    if venue == "binance":
        # Hard-pin the testnet base URL (explicit arg overrides the env default),
        # so this adapter is structurally incapable of hitting api.binance.com, and
        # feed it the *testnet* credentials (not the mainnet key, which testnet
        # rejects with -2015).
        key, secret = _binance_testnet_credentials()
        return BinanceBroker(api_key=key, api_secret=secret, base_url=TESTNET_API_BASE)
    raise BrokerError(
        f"venue {venue!r} has no testnet/sandbox; testnet is available for "
        f"{sorted(_TESTNET_VENUES)!r} only (Kraken has no public spot testnet — "
        "use paper, or real-money live behind the go-live runbook)"
    )
