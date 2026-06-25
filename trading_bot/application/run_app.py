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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from trading_bot.application.data_provider import feed_for
from trading_bot.application.orchestrator import Orchestrator
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.application.strategy import (
    SignalFn,
    Strategy,
    load_strategy,
    ma_crossover_signal,
)
from trading_bot.application.strategy_runner import StrategyRunner
from trading_bot.domain.errors import ConfigError
from trading_bot.domain.instrument import Instrument, parse_kraken_pair
from trading_bot.domain.money import Money, from_float
from trading_bot.domain.order import Order, OrderSide, OrderType

if TYPE_CHECKING:
    import polars as pl

    from trading_bot.application.config import AppConfig, StrategyConfig
    from trading_bot.application.data_feed import DataFeed
    from trading_bot.application.data_provider import DccdClient
    from trading_bot.domain.position import Position

__all__ = [
    "RunReport",
    "StrategyReport",
    "build_runners",
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
class RunReport:
    """The summary of a whole-system :func:`run_app` run.

    A small, presentation-agnostic record the CLI (or a test) reads to print a
    per-strategy summary and the aggregate PnL. Money stays exact
    :class:`~decimal.Decimal` (``realised_pnl`` / ``fees_paid``).

    Attributes
    ----------
    strategies : list of StrategyReport
        One :class:`StrategyReport` per declared strategy, in config order.
    realised_pnl : Money
        Aggregate realised PnL across all strategies (net of fees), read from the
        engine's :class:`~trading_bot.application.performance_service.
        PerformanceService` — the fill-driven source of truth.
    fees_paid : Money
        Aggregate fees paid across all strategies.

    """

    strategies: list[StrategyReport] = field(default_factory=list)
    realised_pnl: Money = field(default_factory=lambda: from_float(0.0))
    fees_paid: Money = field(default_factory=lambda: from_float(0.0))

    @property
    def total_orders(self) -> int:
        """Total orders submitted across every strategy."""
        return sum(s.orders_submitted for s in self.strategies)


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
        unknown builtin signal, or its signal cannot be built / imported.

    """
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


async def run_app(
    config: AppConfig,
    *,
    dccd_client: DccdClient | None = None,
    max_steps: int | None = None,
) -> RunReport:
    """Run the whole declared system from one config and return a summary.

    The triptych entrypoint: assemble the engine
    (:func:`~trading_bot.application.service_factory.build_engine`,
    paper-by-default — the factory enforces it), build a runner per declared
    strategy (:func:`build_runners`), and run them all **concurrently** through a
    fresh :class:`~trading_bot.application.orchestrator.Orchestrator`. After the
    run, build a :class:`RunReport` from the per-runner order counts and the
    engine's shared tracker / performance service (fills are the PnL source of
    truth).

    Parameters
    ----------
    config : AppConfig
        The validated system configuration (mode, brokers, strategies, risk,
        storage). The store is created at ``config.storage.db_path`` when set.
    dccd_client : DccdClient or None, optional
        The dccd client every strategy's feed reads through (injected for an
        offline run). ``None`` (default) lets each feed construct a real client.
    max_steps : int or None, optional
        Cap each runner at this many feed windows (bounds an otherwise
        feed-length run; useful for tests / live feeds). ``None`` (default)
        drains every feed to exhaustion.

    Returns
    -------
    RunReport
        Per-strategy order counts + final positions, and the aggregate realised
        PnL / fees from the engine's performance service.

    Raises
    ------
    ConfigError
        If any strategy is misdeclared (no signal/data, unknown builtin, ...).
    BrokerError
        In live mode if the configured venue lacks credentials (the factory
        refuses — never falling back to paper).

    """
    engine = build_engine(config, db_path=config.storage.db_path)
    runners = build_runners(
        config, engine, dccd_client=dccd_client, max_steps=max_steps
    )

    orchestrator = Orchestrator(event_bus=engine.bus)
    orchestrator.add_all(runners)
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

    return RunReport(
        strategies=strategies,
        realised_pnl=engine.perf.realised_pnl(),
        fees_paid=engine.perf.fees_paid(),
    )
