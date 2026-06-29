"""The triptych entrypoint — one :class:`AppConfig` runs the whole system.

This module is the single seam that turns a fully-declared
:class:`~trading_bot.application.config.AppConfig` into a **running
multi-strategy system**: it builds the wired engine
(:func:`~trading_bot.application.service_factory.build_engine`), loads every
declared :class:`~trading_bot.application.config.StrategyConfig` into a
:class:`~trading_bot.application.strategy.Strategy` (resolving its signal and its
bars feed), wraps each in a :class:`~trading_bot.application.strategy_runner.
StrategyRunner` over the engine's shared router/tracker/bus, and runs them all
**concurrently** through the :class:`~trading_bot.application.orchestrator.
Orchestrator`. It is the application-layer counterpart the CLI's ``run`` command
delegates to when handed a config; the CLI itself holds no orchestration logic.

Single entrypoint, single engine (carried into the ADR)
-------------------------------------------------------
The whole declared system shares **one** engine — one broker, one event bus, one
position tracker, one performance service, one risk gate — assembled once by the
factory. Every strategy's runner routes through that same engine, so fills from
all strategies fan out onto the one bus and aggregate into the one tracker/perf
view (independent *per instrument*, since the tracker keys positions by
instrument). This mirrors the factory's "single wiring point" rationale up one
level: the *system* has a single assembly+run point, so the CLI, a test or a
future daemon all bring the system up identically.

Signal resolution (carried into the ADR)
----------------------------------------
A :class:`~trading_bot.application.config.SignalRefConfig` declares a signal as a
``ref`` plus ``params``. :func:`_resolve_signal_fn` turns it into a
:data:`~trading_bot.application.strategy.SignalFn` two ways, both safe (no
arbitrary-file exec):

* a **builtin name** (``ref`` with no ``":"``) is looked up in a small, explicit
  registry (:data:`_BUILTIN_SIGNALS`) of *factory* callables. The factory is
  called with the strategy's :class:`~trading_bot.domain.instrument.Instrument`
  plus the declared ``params`` (e.g. ``ma_crossover`` + ``{"fast": 10,
  "slow": 30}``) → a bound ``SignalFn``. Only names in the registry resolve;
  anything else is a clear config error.
* a **``"module:function"`` dotted reference** is handed straight to
  :func:`~trading_bot.application.strategy.load_strategy`, which imports the
  already-importable module and ``getattr``\\ s the function — never exec'ing a
  loose file. A ``module:function`` signal is used *as the SignalFn directly*
  (params are not applied to an imported callable here; an importable signal is
  expected to be already bound / parameter-free).

Offline by construction
------------------------
:func:`build_runners` / :func:`run_app` accept an injected ``dccd_client`` that
is threaded into :func:`~trading_bot.application.data_provider.feed_for`, so the
whole entrypoint runs with **no network**: a fake client returning canned bars +
the paper broker is the engine's real data path. Paper-by-default is enforced by
the factory; this module never overrides ``config.mode``.

Order pricing for the paper broker
----------------------------------
The runner submits MARKET orders by default, which the
:class:`~trading_bot.brokers.paper.PaperBroker` can only fill against an injected
mark price. So each runner is given a small **limit-at-close** order factory:
every step's order is priced as a LIMIT at the current bar's close, making the
in-process run fully self-contained (the broker fills at the exact price the
signal saw) without seeding mark prices. A live broker ignores this and fills at
the venue; the factory only matters for the simulator.

This module lives in the application layer: it composes the sibling use-cases and
the factory, holds money as :class:`~decimal.Decimal`, and performs no I/O of its
own (the runners' router/broker and the feed's client do).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from trading_bot.application.data_provider import feed_for
from trading_bot.application.orchestrator import Orchestrator
from trading_bot.application.portfolio import (
    PortfolioStrategy,
    load_portfolio_signal,
)
from trading_bot.application.portfolio_feed import PortfolioFeed
from trading_bot.application.portfolio_runner import PortfolioRunner
from trading_bot.application.reconcile import reconcile
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.application.strategy import (
    SignalFn,
    Strategy,
    load_strategy,
    ma_crossover_signal,
)
from trading_bot.application.strategy_runner import StrategyRunner
from trading_bot.domain.errors import ConfigError
from trading_bot.domain.instrument import Instrument, Symbol, parse_kraken_pair
from trading_bot.domain.money import Money, from_float, money
from trading_bot.domain.order import Order, OrderSide, OrderType

if TYPE_CHECKING:
    import polars as pl

    from trading_bot.application.config import (
        AppConfig,
        StrategyConfig,
    )
    from trading_bot.application.data_feed import DataFeed
    from trading_bot.application.data_provider import DccdClient
    from trading_bot.domain.position import Position

__all__ = [
    "RunReport",
    "StrategyReport",
    "PortfolioReport",
    "PortfolioCoinReport",
    "build_runners",
    "build_portfolio_runners",
    "run_app",
]


#: Builtin signal registry: a config ``ref`` with no ``":"`` is looked up here.
#: Each value is a *factory* ``(instrument, **params) -> SignalFn`` — called with
#: the strategy's instrument and the declared ``params`` to bind a ``SignalFn``.
#: Kept explicit (never reflection over a module) so only these names resolve.
_BUILTIN_SIGNALS: dict[str, Callable[..., SignalFn]] = {
    "ma_crossover": ma_crossover_signal,
}


class _CappedFeed:
    """A :class:`~trading_bot.application.data_feed.DataFeed` capped to ``n`` windows.

    Wraps another feed and yields at most ``max_steps`` causal windows, so a
    :func:`run_app` ``max_steps`` bounds every runner uniformly while still
    driving them through the :class:`~trading_bot.application.orchestrator.
    Orchestrator` (whose ``run`` does not thread a per-runner cap). The wrapped
    feed's causality is preserved — capping only *shortens* the prefix sequence,
    it never reorders or peeks. ``latest`` is delegated unchanged.
    """

    def __init__(self, inner: DataFeed, max_steps: int) -> None:
        self._inner = inner
        self._max_steps = max_steps

    def __iter__(self) -> Iterator[pl.DataFrame]:
        """Yield at most ``max_steps`` causal windows from the wrapped feed."""
        return itertools.islice(iter(self._inner), self._max_steps)

    def latest(self) -> pl.DataFrame:
        """Delegate to the wrapped feed (the full known frame)."""
        return self._inner.latest()


class _CappedPortfolioFeed:
    """A portfolio feed capped to ``max_steps`` causal cross-sections.

    The multi-coin analogue of :class:`_CappedFeed`: wraps a
    :class:`~trading_bot.application.portfolio_feed.PortfolioFeed` and yields at
    most ``max_steps`` of its causal per-coin cross-sections, so :func:`run_app`'s
    ``max_steps`` bounds a :class:`PortfolioRunner` uniformly while it still runs
    through the orchestrator (which calls ``run`` without a per-runner cap). The
    wrapped feed's causality is preserved — capping only shortens the prefix
    sequence. ``asof_ms`` is delegated so the runner's timestamp resolution is
    unchanged.
    """

    def __init__(self, inner: PortfolioFeed, max_steps: int) -> None:
        self._inner = inner
        self._max_steps = max_steps

    def __iter__(self) -> Iterator[Mapping[Symbol, pl.DataFrame]]:
        """Yield at most ``max_steps`` causal cross-sections from the wrapped feed."""
        return itertools.islice(iter(self._inner), self._max_steps)

    def asof_ms(self) -> int | None:
        """Delegate to the wrapped feed (the latest common date's close, ms)."""
        return self._inner.asof_ms()


@dataclass(frozen=True, slots=True)
class StrategyReport:
    """One strategy's outcome after a :func:`run_app` run.

    Attributes
    ----------
    name : str
        The strategy's logical id (its config ``name``).
    instrument : Instrument
        The instrument the strategy traded.
    orders_submitted : int
        Number of orders the strategy's runner submitted (non-zero-delta steps).
    position : Position or None
        The strategy's final net position read from the shared tracker, or
        ``None`` if it never filled (no fill for its instrument).

    """

    name: str
    instrument: Instrument
    orders_submitted: int
    position: Position | None


@dataclass(frozen=True, slots=True)
class PortfolioCoinReport:
    """One coin's outcome within a portfolio after a :func:`run_app` run.

    Attributes
    ----------
    instrument : Instrument
        The coin's instrument (one of the portfolio's universe).
    position : Position or None
        The coin's final net position read from the **shared** tracker, or
        ``None`` if it never filled (no fill for its instrument). The portfolio
        owns this instrument exclusively (the overlap guard ensures no other
        runner touches it), so the shared per-instrument position *is* this
        portfolio coin's position.

    """

    instrument: Instrument
    position: Position | None


@dataclass(frozen=True, slots=True)
class PortfolioReport:
    """One portfolio strategy's outcome after a :func:`run_app` run.

    The multi-asset analogue of :class:`StrategyReport`: where a strategy reports
    one instrument's position, a portfolio reports a per-coin breakdown across its
    whole universe plus the count of legs its
    :class:`~trading_bot.application.portfolio_runner.PortfolioRunner` submitted.

    Attributes
    ----------
    name : str
        The portfolio's logical id (its config ``name``).
    orders_submitted : int
        Total legs the portfolio's runner submitted across every rebalance
        (summed over ticks; on-target / refused legs are not counted).
    coins : list of PortfolioCoinReport
        One :class:`PortfolioCoinReport` per coin in the portfolio's universe, in
        universe order — each coin's final position read from the shared tracker.

    """

    name: str
    orders_submitted: int
    coins: list[PortfolioCoinReport] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunReport:
    """The summary of a whole-system :func:`run_app` run.

    A small, presentation-agnostic record the CLI (or a test) reads to print a
    per-strategy / per-portfolio summary and the aggregate PnL. Money stays exact
    :class:`~decimal.Decimal` (``realised_pnl`` / ``fees_paid``).

    Attributes
    ----------
    strategies : list of StrategyReport
        One :class:`StrategyReport` per declared single-instrument strategy, in
        config order.
    portfolios : list of PortfolioReport
        One :class:`PortfolioReport` per declared portfolio strategy, in config
        order (each with its per-coin breakdown). Empty when no portfolio is
        declared.
    realised_pnl : Money
        Aggregate realised PnL across all strategies *and* portfolios (net of
        fees), read from the engine's
        :class:`~trading_bot.application.performance_service.PerformanceService` —
        the fill-driven source of truth.
    fees_paid : Money
        Aggregate fees paid across all strategies and portfolios.

    """

    strategies: list[StrategyReport] = field(default_factory=list)
    portfolios: list[PortfolioReport] = field(default_factory=list)
    realised_pnl: Money = field(default_factory=lambda: from_float(0.0))
    fees_paid: Money = field(default_factory=lambda: from_float(0.0))

    @property
    def total_orders(self) -> int:
        """Total orders submitted across every strategy **and** portfolio."""
        return sum(s.orders_submitted for s in self.strategies) + sum(
            p.orders_submitted for p in self.portfolios
        )


def _resolve_signal_fn(
    strategy_cfg: StrategyConfig, instrument: Instrument
) -> SignalFn | str:
    """Resolve a strategy's declared signal into a usable ``SignalFn`` (or ref).

    A builtin ``ref`` (no ``":"``) is bound from :data:`_BUILTIN_SIGNALS` with the
    ``instrument`` + the declared ``params``; a ``"module:function"`` ``ref`` is
    returned **as the string** so :func:`load_strategy` performs the safe import.

    Raises
    ------
    ConfigError
        If the strategy declares no ``signal``, names an unknown builtin, or a
        builtin's ``params`` are not accepted by its factory.
    """
    signal = strategy_cfg.signal
    if signal is None:
        raise ConfigError(
            f"strategy {strategy_cfg.name!r} has no signal; declare a "
            "'signal' (a builtin name like 'ma_crossover', or "
            "'module:function') with its params"
        )

    ref = signal.ref
    params: dict[str, Any] = dict(signal.params)

    if ":" in ref:
        # A dotted module:function reference — load_strategy imports it safely.
        # An imported callable is expected already bound; params do not apply.
        return ref

    factory = _BUILTIN_SIGNALS.get(ref)
    if factory is None:
        raise ConfigError(
            f"strategy {strategy_cfg.name!r}: unknown builtin signal {ref!r}; "
            f"known builtins are {sorted(_BUILTIN_SIGNALS)!r} "
            "(or use a 'module:function' reference)"
        )
    try:
        return factory(instrument, **params)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"strategy {strategy_cfg.name!r}: cannot build builtin signal "
            f"{ref!r} with params {params!r}: {exc}"
        ) from exc


def _limit_at_close_factory(
    close_col: str = "c",
) -> Callable[[Strategy, Money, "pl.DataFrame"], Order]:
    """Build an order factory that prices each step's order at the latest close.

    The runner emits MARKET orders by default; the paper broker can only fill
    those against an injected mark price. Pricing each order as a LIMIT at the
    bar's close makes the in-process run self-contained — the broker fills at the
    exact close the signal saw — and is a no-op for a live broker (which fills at
    the venue). The runner overrides the ``client_order_id`` afterwards.
    """

    def _factory(strategy: Strategy, delta: Money, bars: "pl.DataFrame") -> Order:
        close = from_float(float(bars[close_col][-1]))
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        return Order(
            client_order_id="pending",  # overridden by the runner
            instrument=strategy.instrument,
            side=side,
            qty=abs(delta),
            type=OrderType.LIMIT,
            limit_price=close,
        )

    return _factory


def _reject_commingled(config: AppConfig) -> None:
    """Reject two strategies declaring the **same** instrument.

    The whole declared system shares **one** engine — one position tracker, one
    performance service — keyed *per instrument*, with no per-strategy
    attribution today (see the single-engine rationale above). So two strategies
    on the same instrument would **commingle**: their fills fold into the one
    shared per-instrument position/PnL view and can no longer be told apart (a
    BUY from one and a SELL from the other net against each other; the reported
    PnL is the blend, not either strategy's). Rather than silently produce a
    meaningless blended book, reject the config up front with a clear
    :class:`~trading_bot.domain.errors.ConfigError` naming the duplicated symbol
    and the two offending strategies.

    Symbols are compared by their **normalised**
    :class:`~trading_bot.domain.instrument.Symbol` (via
    :func:`~trading_bot.domain.instrument.parse_kraken_pair`), so two different
    spellings of the same pair (e.g. ``"BTC/USD"`` and ``"XBT/USD"``) are still
    caught as the same instrument.

    The real fix — a **per-strategy book** that attributes fills back to the
    strategy that originated them — is deferred future work; until it lands,
    one-instrument-per-strategy is the invariant this guard enforces.

    Portfolios are folded into the **same** claim map (a portfolio *owns* its N
    instruments): no instrument may be claimed by both a single-instrument
    strategy and a portfolio, nor by two portfolios — the same shared
    per-instrument tracker, the same attribution problem. See
    :func:`_claimed_symbols`.
    """
    seen: dict[Symbol, str] = {}
    for symbol, owner in _claimed_symbols(config):
        previous = seen.get(symbol)
        if previous is not None:
            raise ConfigError(
                f"{previous!r} and {owner} both claim the same instrument "
                f"{symbol!s}: the shared per-instrument tracker/performance view "
                "would commingle them (no per-strategy attribution today). Give "
                "each a distinct instrument, or run them as separate systems."
            )
        seen[symbol] = owner


def _claimed_symbols(config: AppConfig) -> Iterator[tuple[Symbol, str]]:
    """Yield ``(Symbol, owner-label)`` for every instrument the config claims.

    Walks the single-instrument strategies (one symbol each) **and** the
    portfolios (their whole universe), in config order, normalising each pair to
    its canonical :class:`~trading_bot.domain.instrument.Symbol` (so two spellings
    of the same pair collide). The owner label names the claimant for a clear
    error — ``"strategy 'btc-ma'"`` or ``"portfolio 'ls1' (coin 'BTC/USDT')"`` —
    so :func:`_reject_commingled` can report exactly who overlaps whom across the
    whole system (strategy↔strategy, strategy↔portfolio, portfolio↔portfolio).
    """
    for strategy_cfg in config.strategies:
        yield parse_kraken_pair(strategy_cfg.symbol), f"strategy {strategy_cfg.name!r}"
    for portfolio_cfg in config.portfolios:
        for raw in portfolio_cfg.universe:
            yield (
                parse_kraken_pair(raw),
                f"portfolio {portfolio_cfg.name!r} (coin {raw!r})",
            )


def build_runners(
    config: AppConfig,
    engine: Engine,
    *,
    dccd_client: DccdClient | None = None,
    max_steps: int | None = None,
) -> list[StrategyRunner]:
    """Build one :class:`StrategyRunner` per declared strategy, over ``engine``.

    For each :class:`~trading_bot.application.config.StrategyConfig` in
    ``config.strategies`` this resolves the signal (a builtin name + ``params``,
    or a ``"module:function"`` import), loads a
    :class:`~trading_bot.application.strategy.Strategy` (carrying the config's
    ``reference_qty`` / ``lookback``), builds its bars
    :class:`~trading_bot.application.data_feed.DataFeed` via
    :func:`~trading_bot.application.data_provider.feed_for` (the ``dccd_client``
    is threaded through so the build is offline-testable), and wraps it in a
    runner over the engine's **shared** router / tracker / event bus.

    Each runner is given a limit-at-close order factory so the paper broker can
    fill its orders without seeded mark prices (a no-op for a live broker).

    Parameters
    ----------
    config : AppConfig
        The validated system configuration; its ``strategies`` drive the build.
    engine : Engine
        The wired engine whose ``router`` / ``tracker`` / ``bus`` every runner
        shares (from :func:`~trading_bot.application.service_factory.build_engine`).
    dccd_client : DccdClient or None, optional
        The dccd client each :func:`feed_for` reads through. ``None`` (default)
        lets ``feed_for`` lazily construct a real client; injecting a fake keeps
        the whole build offline.
    max_steps : int or None, optional
        Cap each runner's feed at this many causal windows (each feed is wrapped
        in a :class:`_CappedFeed`). ``None`` (default) leaves every feed
        uncapped (drained to exhaustion).

    Returns
    -------
    list of StrategyRunner
        One runner per declared strategy, in config order.

    Raises
    ------
    ConfigError
        If a strategy declares no ``signal`` or no ``data`` source, names an
        unknown builtin signal, its signal cannot be built / imported, or two
        strategies declare the **same** instrument (see :func:`_reject_commingled`).

    """
    _reject_commingled(config)

    runners: list[StrategyRunner] = []
    for strategy_cfg in config.strategies:
        instrument = Instrument(parse_kraken_pair(strategy_cfg.symbol))
        signal_fn = _resolve_signal_fn(strategy_cfg, instrument)

        try:
            base = load_strategy(strategy_cfg, signal_fn)
        except Exception as exc:  # SignalError (bad import) → a config error
            raise ConfigError(
                f"strategy {strategy_cfg.name!r}: cannot load signal: {exc}"
            ) from exc

        # Carry the declared sizing/warmup onto the (frozen) strategy.
        strategy = Strategy(
            name=base.name,
            instrument=base.instrument,
            signal_fn=base.signal_fn,
            reference_qty=strategy_cfg.reference_qty,
            lookback=strategy_cfg.lookback,
        )

        if strategy_cfg.data is None:
            raise ConfigError(
                f"strategy {strategy_cfg.name!r} has no data source; declare a "
                "'data' (dccd exchange/span/...) so its bars can be fed"
            )
        feed: DataFeed = feed_for(
            strategy_cfg,
            client=dccd_client,
            data_path=config.storage.data_path,
        )
        if max_steps is not None:
            feed = _CappedFeed(feed, max_steps)

        runner = StrategyRunner(
            strategy,
            feed,
            engine.router,
            engine.tracker,
            event_bus=engine.bus,
            order_factory=_limit_at_close_factory(),
        )
        runners.append(runner)
    return runners


def build_portfolio_runners(
    config: AppConfig,
    engine: Engine,
    *,
    dccd_client: DccdClient | None = None,
    max_steps: int | None = None,
) -> list[PortfolioRunner]:
    """Build one :class:`PortfolioRunner` per declared portfolio, over ``engine``.

    For each :class:`~trading_bot.application.config.PortfolioStrategyConfig` in
    ``config.portfolios`` this resolves the weight-vector signal
    (:func:`~trading_bot.application.portfolio.load_portfolio_signal` — a
    ``"module:function"`` ref), builds a
    :class:`~trading_bot.application.portfolio_feed.PortfolioFeed` over the
    declared ``universe`` from the portfolio's ``data`` source, assembles a
    :class:`~trading_bot.application.portfolio.PortfolioStrategy` (carrying the
    config's ``capital`` / ``gross_cap``), and wraps it in a runner over the
    engine's **shared** router / tracker / event bus.

    The dccd ``client`` is threaded into every :class:`PortfolioFeed` so the build
    is offline-testable. A daily portfolio reading a 1-minute store should inject
    a :class:`~trading_bot.application.data_provider.ResamplingDccdClient` here
    (the live daily-bars seam — dccd's store serves 1m, not daily); the offline
    tests inject a fake *daily* client directly and need no resampling.

    Parameters
    ----------
    config : AppConfig
        The validated system configuration; its ``portfolios`` drive the build.
    engine : Engine
        The wired engine whose ``router`` / ``tracker`` / ``bus`` every runner
        shares.
    dccd_client : DccdClient or None, optional
        The dccd client each :class:`PortfolioFeed` reads through. ``None``
        (default) lets the feed lazily construct a real client; injecting a fake
        (or a :class:`ResamplingDccdClient` wrapping one) keeps the build offline /
        serves daily bars off a 1m store.
    max_steps : int or None, optional
        Cap each runner at this many rebalance ticks (passed to
        :meth:`PortfolioRunner.run` by the orchestrating caller — recorded here so
        the signature mirrors :func:`build_runners`; the cap is applied at run
        time, not by wrapping the feed). ``None`` (default) drains every feed.

    Returns
    -------
    list of PortfolioRunner
        One runner per declared portfolio, in config order.

    Raises
    ------
    ConfigError
        If a portfolio's signal cannot be resolved (bad ``"module:function"``
        ref), or — via :func:`_reject_commingled`, called by the system entrypoint
        — if any instrument is claimed by two runners.

    """
    runners: list[PortfolioRunner] = []
    for portfolio_cfg in config.portfolios:
        signal_fn = load_portfolio_signal(portfolio_cfg.signal.ref)

        universe = tuple(parse_kraken_pair(raw) for raw in portfolio_cfg.universe)
        gross_cap = (
            None if portfolio_cfg.gross_cap is None else money(portfolio_cfg.gross_cap)
        )
        strategy = PortfolioStrategy(
            name=portfolio_cfg.name,
            universe=universe,
            signal_fn=signal_fn,
            capital=money(portfolio_cfg.capital),
            gross_cap=gross_cap,
        )

        data = portfolio_cfg.data
        feed = PortfolioFeed(
            universe,
            exchange=data.exchange,
            client=dccd_client,
            span=data.span,
            start_ns=_portfolio_start_ns(data.start),
            data_type=data.data_type,
            data_path=config.storage.data_path,
        )
        capped: object = (
            feed if max_steps is None else _CappedPortfolioFeed(feed, max_steps)
        )

        runner = PortfolioRunner(
            strategy,
            capped,
            engine.router,
            engine.tracker,
            event_bus=engine.bus,
        )
        runners.append(runner)
    return runners


def _portfolio_start_ns(start: str | int | None) -> int | None:
    """Map a portfolio data source ``start`` to an epoch-ns bound.

    Reuses :func:`~trading_bot.application.data_provider._resolve_start_ns` (the
    single, audited ``start`` → ``start_ns`` parser) so a portfolio's history
    start is interpreted identically to a single-instrument strategy's.
    """
    from trading_bot.application.data_provider import _resolve_start_ns

    return _resolve_start_ns(start)


async def run_app(
    config: AppConfig,
    *,
    dccd_client: DccdClient | None = None,
    max_steps: int | None = None,
    reconcile_on_start: bool = True,
) -> RunReport:
    """Run the whole declared system from one config and return a summary.

    The triptych entrypoint: assemble the engine
    (:func:`~trading_bot.application.service_factory.build_engine`,
    paper-by-default — the factory enforces it), **reconcile** the freshly-built
    engine's local state to the broker's truth before any order is placed (the
    *reconcile, don't assume* invariant — see below), build a runner per declared
    single-instrument strategy (:func:`build_runners`) **and** per declared
    portfolio (:func:`build_portfolio_runners`), reject any instrument claimed by
    two runners (:func:`_reject_commingled`, spanning strategies *and*
    portfolios), and run them all **concurrently** through a fresh
    :class:`~trading_bot.application.orchestrator.Orchestrator`. After the run,
    build a :class:`RunReport` from the per-runner order counts and the engine's
    shared tracker / performance service (fills are the PnL source of truth).

    Reconcile-on-startup (carried into the ADR)
    -------------------------------------------
    A freshly-built :class:`~trading_bot.application.service_factory.Engine` has
    **empty** local maps (the router tracks no orders, the tracker holds no
    positions), but the venue may already hold open orders and a fill history
    from a previous session/restart. Running blind would risk re-submitting an
    order the venue already has or trading against a stale (zero) position. So
    before the first order, :func:`~trading_bot.application.reconcile.reconcile`
    pulls the broker's open orders / balances / fills **once** and converges the
    router + tracker to them (ingest unknown live orders, close orphans, rebuild
    positions from confirmed fills). On a fresh
    :class:`~trading_bot.brokers.paper.PaperBroker` this is a harmless no-op (no
    open orders, no fills); on a live/testnet venue it is the safety backstop that
    recovers state after a restart. The pass emits one
    :class:`~trading_bot.application.events.LogEvent` on the engine bus.

    Reconciling **after a disconnect** (the WS-reconnect half of the invariant)
    attaches to the private fill stream, which is not yet wired into this run
    loop; that lands with live fill streaming (tracked in the roadmap).

    Parameters
    ----------
    config : AppConfig
        The validated system configuration (mode, brokers, strategies,
        portfolios, risk, storage). The store is created at
        ``config.storage.db_path`` when set.
    dccd_client : DccdClient or None, optional
        The dccd client every strategy's / portfolio's feed reads through
        (injected for an offline run). ``None`` (default) lets each feed construct
        a real client. A daily portfolio reading a 1-minute store should inject a
        :class:`~trading_bot.application.data_provider.ResamplingDccdClient`.
    max_steps : int or None, optional
        Cap each runner at this many feed windows / rebalance ticks (bounds an
        otherwise feed-length run; useful for tests / live feeds). ``None``
        (default) drains every feed to exhaustion.
    reconcile_on_start : bool, optional
        Whether to reconcile the engine to the broker before running (default
        ``True``, enforcing the startup invariant). Pass ``False`` only to skip
        the pass in a test/offline context where the broker reads are irrelevant.

    Returns
    -------
    RunReport
        Per-strategy order counts + final positions, per-portfolio per-coin
        breakdowns, and the aggregate realised PnL / fees from the engine's
        performance service.

    Raises
    ------
    ConfigError
        If any strategy/portfolio is misdeclared (no signal/data, unknown
        builtin, unresolvable portfolio signal), or any instrument is claimed by
        two runners (strategy↔strategy, strategy↔portfolio, portfolio↔portfolio).
    BrokerError
        In live mode if the configured venue lacks credentials (the factory
        refuses — never falling back to paper).

    """
    engine = build_engine(config, db_path=config.storage.db_path)
    # Reconcile, don't assume: converge the fresh engine's empty maps to the
    # broker's truth (open orders + fills) before the first order is placed, so a
    # restart never re-submits a venue-held order or trades a stale position. A
    # no-op on a fresh PaperBroker; the safety backstop on a live/testnet venue.
    if reconcile_on_start:
        await reconcile(
            engine.broker, engine.router, engine.tracker, event_bus=engine.bus
        )
    # Reject any instrument claimed by two runners up front — across both the
    # single-instrument strategies and the portfolios (the shared per-instrument
    # tracker has no attribution). build_runners also calls this for the
    # strategy-only path; calling it here covers the cross-portfolio overlaps too.
    _reject_commingled(config)
    runners = build_runners(
        config, engine, dccd_client=dccd_client, max_steps=max_steps
    )
    portfolio_runners = build_portfolio_runners(
        config, engine, dccd_client=dccd_client, max_steps=max_steps
    )

    orchestrator = Orchestrator(event_bus=engine.bus)
    orchestrator.add_all(runners)
    orchestrator.add_all(portfolio_runners)  # type: ignore[arg-type]
    results = await orchestrator.run()

    strategies: list[StrategyReport] = []
    for runner in runners:
        strat = runner.strategy
        strategies.append(
            StrategyReport(
                name=strat.name,
                instrument=strat.instrument,
                orders_submitted=results.get(runner, 0),
                position=engine.tracker.position(strat.instrument),
            )
        )

    # The orchestrator keys its results by the runner objects it ran (both the
    # StrategyRunners and the PortfolioRunners added via add_all); its return type
    # is annotated for the single-instrument runner, so view it as a plain object
    # map to look up the portfolio runners.
    results_by_runner = cast("dict[object, int]", results)
    portfolios: list[PortfolioReport] = []
    for prunner in portfolio_runners:
        pstrat = prunner.strategy
        coins = [
            PortfolioCoinReport(
                instrument=Instrument(symbol),
                position=engine.tracker.position(Instrument(symbol)),
            )
            for symbol in pstrat.universe
        ]
        portfolios.append(
            PortfolioReport(
                name=pstrat.name,
                orders_submitted=results_by_runner.get(prunner, 0),
                coins=coins,
            )
        )

    return RunReport(
        strategies=strategies,
        portfolios=portfolios,
        realised_pnl=engine.perf.realised_pnl(),
        fees_paid=engine.perf.fees_paid(),
    )
