"""The :class:`StrategySupervisor` — each declared strategy as an independent unit.

Where :func:`~trading_bot.application.run_app.run_app` builds **one** shared engine
for the whole config, the supervisor splits the config into per-strategy /
per-portfolio **units**, each running in its **own**
:func:`~trading_bot.application.service_factory.build_engine` (its own broker,
mode, tracker, PnL). That is what lets a strategy be **started, stopped and
switched between paper / testnet / live independently** — the control plane behind
the daemon and the dashboard. Because each unit owns its engine, there is no
cross-unit commingling (the single-engine path's
:func:`~trading_bot.application.run_app._reject_commingled` concern does not arise
here).

Modes (carried into the ADR)
----------------------------
A unit's :data:`StrategyMode` maps to an :class:`~trading_bot.application.config.
AppConfig` slice:

* ``"paper"`` — ``mode: paper`` (the simulator; no venue, no key).
* ``"testnet"`` — ``mode: live`` + every broker ``testnet: true`` (paper money on
  the real sandbox; mainnet-incapable, so no ``live_enabled`` needed).
* ``"live"`` — ``mode: live`` + ``live_enabled: true`` + brokers ``testnet: false``
  (**real money**).

**Real money is gated.** :meth:`set_mode` to ``"live"`` raises unless an explicit
``confirm_live=True`` is passed — the deliberate acknowledgement the control API /
UI obtains (a typed confirmation), never a casual flip. Paper ↔ testnet need no
confirmation. The usual factory gates (credentials, the risk-limit requirement)
still apply when the live engine is actually built on :meth:`start`.

This module is part of the application layer: it composes the factory and the
runners, holds money as :class:`~decimal.Decimal`, and performs venue I/O only
through the engines it builds (reconcile on start; the runners' router/broker).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from trading_bot.application.pnl_series import by_mode, equity_series
from trading_bot.application.reconcile import reconcile
from trading_bot.application.run_app import build_portfolio_runners, build_runners
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.domain.errors import ConfigError, LiveTradingNotEnabled
from trading_bot.domain.money import money
from trading_bot.domain.performance import (
    PerformanceDependencyError,
    calmar,
    max_drawdown,
    sharpe,
    sortino,
)
from trading_bot.storage.sqlite_store import SqliteStore

if TYPE_CHECKING:
    from trading_bot.application.config import (
        AppConfig,
        PortfolioStrategyConfig,
        StrategyConfig,
    )
    from trading_bot.application.data_provider import DccdClient
    from trading_bot.application.portfolio_runner import PortfolioRunner
    from trading_bot.application.strategy_runner import StrategyRunner
    from trading_bot.domain.fill import Fill
    from trading_bot.domain.money import Money
    from trading_bot.domain.order import Order
    from trading_bot.storage.sqlite_store import StoredFill

__all__ = [
    "FillRow",
    "KpiLevel",
    "KpiRow",
    "OrderRow",
    "PositionRow",
    "StrategyMode",
    "StrategyStatus",
    "StrategySupervisor",
]

#: The deployment mode of a managed strategy.
StrategyMode = Literal["paper", "testnet", "live"]

#: The aggregation level a :meth:`StrategySupervisor.kpi` view folds to.
KpiLevel = Literal["strategy", "exchange", "total"]

_KIND = Literal["strategy", "portfolio"]

_ZERO: Money = money("0")


@dataclass(frozen=True, slots=True)
class StrategyStatus:
    """A read-only snapshot of one managed strategy's state (for the API / UI).

    Attributes
    ----------
    name : str
        The strategy/portfolio's logical id.
    kind : {"strategy", "portfolio"}
        Whether it is a single-instrument strategy or a multi-asset portfolio.
    exchange : str
        The venue this strategy is for (a single-instrument strategy's
        ``data.exchange``; a portfolio's ``venue``) — the key the dashboard groups
        by, and the broker the unit uses on testnet/live.
    mode : StrategyMode
        Its current deployment mode (``paper`` / ``testnet`` / ``live``).
    running : bool
        Whether it is started (its engine is built and steppable).
    realised_pnl : Money or None
        The unit engine's realised PnL when running, else ``None``.
    open_orders : int
        The number of orders the unit's router currently tracks as non-terminal
        (``0`` when stopped).

    """

    name: str
    kind: _KIND
    exchange: str
    mode: StrategyMode
    running: bool
    realised_pnl: Money | None
    open_orders: int


@dataclass(frozen=True, slots=True)
class PositionRow:
    """A net position of one running unit, tagged with its strategy + venue.

    The supervisor-level view of a per-engine
    :class:`~trading_bot.domain.position.Position`: the same money-exact exposure
    the unit's :class:`~trading_bot.application.position_tracker.PositionTracker`
    holds, plus the ``strategy`` and ``exchange`` tags the dashboard groups by
    (by crypto — the instrument's base asset — and/or by exchange).

    Attributes
    ----------
    strategy : str
        The managed unit this exposure belongs to.
    exchange : str
        The venue the unit runs on (its group-by-exchange key).
    instrument : str
        The canonical instrument (``BASE/QUOTE``).
    base : str
        The instrument's base asset (its group-by-crypto key, e.g. ``BTC``).
    net_qty : Money
        Signed net quantity (exact :class:`~decimal.Decimal`): ``>0`` long,
        ``<0`` short.
    avg_entry_price : Money or None
        Quantity-weighted average entry price of the open exposure (``None`` when
        flat).
    realised_pnl : Money
        The position's realised PnL, net of fees (exact).
    fees_paid : Money
        The position's cumulative fees (exact).

    """

    strategy: str
    exchange: str
    instrument: str
    base: str
    net_qty: Money
    avg_entry_price: Money | None
    realised_pnl: Money
    fees_paid: Money


@dataclass(frozen=True, slots=True)
class OrderRow:
    """An open order of one running unit, tagged with its strategy + venue.

    The supervisor-level view of a per-engine
    :class:`~trading_bot.domain.order.Order` that is not yet terminal (still
    working / partially filled), plus the ``strategy`` and ``exchange`` tags the
    dashboard groups by.

    Attributes
    ----------
    strategy : str
        The managed unit that placed the order.
    exchange : str
        The venue the unit runs on.
    order : Order
        The live order aggregate (money fields intact as
        :class:`~decimal.Decimal`); the API renders it with money as strings.

    """

    strategy: str
    exchange: str
    order: Order


@dataclass(frozen=True, slots=True)
class FillRow:
    """A confirmed fill of one unit, tagged with its strategy + venue + base crypto.

    The supervisor-level view of a persisted :class:`~trading_bot.domain.fill.Fill`
    (the PnL source of truth) read back from a unit's store, plus the ``strategy``,
    ``exchange`` and ``base`` crypto tags the dashboard's Orders/Fills page filters
    on. Unlike :meth:`positions` / :meth:`open_orders` (running units only), fills
    are **history** — they are read from every unit's store (running or stopped), so
    a stopped unit's past executions still surface.

    Attributes
    ----------
    strategy : str
        The managed unit that recorded the fill.
    exchange : str
        The venue the unit ran on when it executed (the store's ``venue`` tag when
        set, else the unit's declared exchange).
    base : str
        The fill instrument's base asset (its group-by/filter-by-crypto key, e.g.
        ``BTC``).
    fill : Fill
        The immutable execution record (money intact as :class:`~decimal.Decimal`);
        the API renders it with money as strings.

    """

    strategy: str
    exchange: str
    base: str
    fill: Fill


@dataclass(frozen=True, slots=True)
class KpiRow:
    """A realised-PnL / fees / ratios view at one aggregation level.

    Produced by :meth:`StrategySupervisor.kpi`. At ``level="strategy"`` there is
    one row per running unit and the ratios are that unit's
    :class:`~trading_bot.application.performance_service.PerformanceService`
    estimates. At ``level="exchange"`` the units sharing a venue are folded (PnL
    and fees summed) and the ratios are computed on the **combined equity curve**
    of that venue's units (:meth:`~StrategySupervisor.combined_equity_series`). At
    ``level="total"`` every unit is folded (ratios on the combined curve of every
    unit likewise). An aggregate ratio degrades to ``None`` when fynance is absent
    or the combined curve is too short to estimate it.

    Attributes
    ----------
    level : KpiLevel
        The aggregation level this row belongs to.
    key : str
        The row's identity: the strategy name (``"strategy"``), the exchange
        (``"exchange"``), or ``"total"`` (``"total"``).
    strategy : str or None
        The strategy name at ``level="strategy"``; ``None`` otherwise.
    exchange : str or None
        The venue at ``level`` in ``{"strategy", "exchange"}``; ``None`` for the
        total row.
    realised_pnl : Money
        Realised PnL, net of fees, summed over the folded units (exact).
    fees_paid : Money
        Fees paid, summed over the folded units (exact).
    sharpe, sortino, calmar, max_drawdown : float or None
        The risk ratios — the unit's own at ``level="strategy"``, the group's
        combined-curve ratios at ``level="exchange"`` / ``"total"``. ``None`` when
        fynance is absent or the curve is too short to estimate the ratio.

    """

    level: KpiLevel
    key: str
    strategy: str | None
    exchange: str | None
    realised_pnl: Money
    fees_paid: Money
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    max_drawdown: float | None


@dataclass
class _Unit:
    """One managed strategy: its config slice, mode, and (when running) engine."""

    name: str
    kind: _KIND
    exchange: str
    mode: StrategyMode
    config: AppConfig
    engine: Engine | None = None
    runner: StrategyRunner | PortfolioRunner | None = None
    running: bool = False


class StrategySupervisor:
    """Manage each declared strategy as an independently-deployable unit.

    Splits ``base_config`` into one unit per declared strategy and portfolio, each
    with its own engine and mode. Drive them with :meth:`start` / :meth:`stop` /
    :meth:`set_mode` / :meth:`step`, and read state with :meth:`status`.

    Parameters
    ----------
    base_config : AppConfig
        The declared system. Its mode seeds every unit's initial mode; each unit
        can then be switched independently.
    dccd_client : DccdClient or None, optional
        The dccd client every unit's feed reads through (injected for an offline
        run/test). ``None`` lets each feed construct a real client.

    """

    def __init__(
        self, base_config: AppConfig, *, dccd_client: DccdClient | None = None
    ) -> None:
        self._base = base_config
        self._dccd_client = dccd_client
        self._units: dict[str, _Unit] = {}
        seed = _mode_of(base_config)
        for strategy in base_config.strategies:
            self._add_unit(strategy.name, "strategy", seed)
        for portfolio in base_config.portfolios:
            self._add_unit(portfolio.name, "portfolio", seed)

    # --- registry ---------------------------------------------------------- #

    def _add_unit(self, name: str, kind: _KIND, mode: StrategyMode) -> None:
        if name in self._units:
            raise ConfigError(
                f"duplicate strategy name {name!r}: each managed unit needs a "
                "unique name across strategies and portfolios"
            )
        exchange = self._exchange_of(name, kind)
        config = self._slice_for(name, kind, mode, exchange)
        self._units[name] = _Unit(
            name=name, kind=kind, exchange=exchange, mode=mode, config=config
        )

    def add_unit(self, entry: StrategyConfig | PortfolioStrategyConfig) -> str:
        """Deploy a new **stopped** unit from a single config entry (paper-safe).

        The dynamic-membership counterpart of ``__init__``'s config split: adds
        ``entry`` (a single-instrument :class:`~trading_bot.application.config.
        StrategyConfig` or a multi-asset :class:`~trading_bot.application.config.
        PortfolioStrategyConfig`) to the base config and builds a **stopped**
        :class:`_Unit` for it, exactly the way ``__init__`` builds the declared
        units (same ``_exchange_of`` / ``_slice_for``, same seed mode). The unit
        is **never auto-started** — deploying is paper-safe; a later ``start``
        brings it online.

        Validation is up front and atomic: the entry is folded into a new
        :class:`~trading_bot.application.config.AppConfig` via
        :meth:`~trading_bot.application.config.AppConfig.add_strategy` /
        :meth:`~trading_bot.application.config.AppConfig.add_portfolio` (rejecting
        a duplicate name / an invalid slice), and the unit is sliced for its mode
        (so a testnet/live seed with no matching broker is rejected) **before**
        anything is mutated — a bad entry adds nothing.

        Parameters
        ----------
        entry : StrategyConfig or PortfolioStrategyConfig
            The deployment to add — a signal ref + venue/mode/capital already
            declared. The engine never authors the signal code; it only wires an
            existing, importable signal.

        Returns
        -------
        str
            The name of the newly-added unit.

        Raises
        ------
        ConfigError
            If the name is already managed, or the entry cannot form a runnable
            unit in the seed mode (e.g. no matching broker for a non-paper seed).

        """
        # Import here (not at module top) to avoid a circular import: config is a
        # TYPE_CHECKING-only import above, and the concrete classes are needed at
        # runtime only for this dispatch.
        from trading_bot.application.config import (
            PortfolioStrategyConfig,
            StrategyConfig,
        )

        name = entry.name
        if name in self._units:
            raise ConfigError(
                f"duplicate strategy name {name!r}: each managed unit needs a "
                "unique name across strategies and portfolios"
            )
        # Fold the entry into a fresh, fully-validated base config first (a bad
        # slice / duplicate name raises here, before any mutation).
        try:
            if isinstance(entry, StrategyConfig):
                new_base = self._base.add_strategy(entry)
                kind: _KIND = "strategy"
            elif isinstance(entry, PortfolioStrategyConfig):
                new_base = self._base.add_portfolio(entry)
                kind = "portfolio"
            else:  # pragma: no cover - guarded by the type signature
                raise ConfigError(
                    f"cannot add unit from {type(entry).__name__}; expected a "
                    "StrategyConfig or PortfolioStrategyConfig"
                )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        # Swap in the extended base so `_exchange_of` / `_slice_for` see the new
        # entry, then build the stopped unit exactly as `__init__` does. Restore
        # the old base and re-raise if the mode slice is not runnable — nothing
        # is left half-added.
        old_base = self._base
        self._base = new_base
        try:
            self._add_unit(name, kind, _mode_of(new_base))
        except Exception:
            self._base = old_base
            raise
        return name

    def remove_unit(self, name: str) -> None:
        """Stop (if running) and drop a managed unit, and forget its config.

        The inverse of :meth:`add_unit`: tears the unit's engine down if it is
        running (via :meth:`stop`), removes it from the registry, and drops its
        entry from the base config so :meth:`manifest` no longer reports it.
        Idempotent lookup — an unknown name raises a clear error.

        Raises
        ------
        ConfigError
            If ``name`` is not a managed unit.

        """
        unit = self._unit(name)
        if unit.running:
            # `stop` is async but only clears in-memory state (no awaited I/O);
            # inline its effect so `remove_unit` stays a sync accessor mirroring
            # the shape of the other registry helpers.
            unit.running = False
            unit.runner = None
            unit.engine = None
        del self._units[name]
        self._base = self._base.remove_entry(name)

    def manifest(self) -> AppConfig:
        """Reconstruct the current :class:`AppConfig` from the live units.

        The persistence source: the base config as it now stands after any
        :meth:`add_unit` / :meth:`remove_unit`, so the dashboard can write it back
        to disk and reload the same deployment on the next launch. Returns the
        base config directly (an immutable pydantic model); its ``strategies`` /
        ``portfolios`` are exactly the managed units.
        """
        return self._base

    def names(self) -> list[str]:
        """The managed strategy names, in registration order."""
        return list(self._units)

    @property
    def mode(self) -> str:
        """The base config's deployment mode (``"paper"`` / ``"live"``).

        The system-level mode the dashboard's health chip reports — the seed each
        unit's own mode starts from (units can then be switched independently).
        """
        return self._base.mode

    def _unit(self, name: str) -> _Unit:
        try:
            return self._units[name]
        except KeyError:
            raise ConfigError(f"unknown strategy {name!r}") from None

    # --- config slicing + mode mapping ------------------------------------- #

    def _exchange_of(self, name: str, kind: _KIND) -> str:
        """The venue a unit is for — a strategy's ``data.exchange``, a portfolio's ``venue``.

        Falls back to the first configured broker's exchange (then ``"paper"``) for
        a single-instrument strategy that declares no data source.
        """
        if kind == "strategy":
            cfg = next(s for s in self._base.strategies if s.name == name)
            if cfg.data is not None:
                return cfg.data.exchange
            return self._base.brokers[0].exchange if self._base.brokers else "paper"
        pf = next(p for p in self._base.portfolios if p.name == name)
        return pf.venue

    def _slice_for(
        self, name: str, kind: _KIND, mode: StrategyMode, exchange: str
    ) -> AppConfig:
        """The single-unit ``AppConfig`` for ``name`` in ``mode`` on ``exchange``.

        Resolves the unit's store path last: if the strategy/portfolio entry
        declares its own ``db_path``, the sliced config's ``storage.db_path`` is
        overridden with it, so this unit's :func:`~trading_bot.application.
        service_factory.build_engine` (and every store-backed read —
        :meth:`_stored_fills_of`, :meth:`pnl_series`, :meth:`_replay_paper_book`,
        :meth:`order_history`) reads an **isolated** store. Absent → the global
        ``storage.db_path`` is kept (fully backward-compatible).
        """
        if kind == "strategy":
            only = [s for s in self._base.strategies if s.name == name]
            base_slice = self._base.model_copy(
                update={"strategies": only, "portfolios": []}
            )
            entry_db_path = only[0].db_path if only else None
        else:
            only_p = [p for p in self._base.portfolios if p.name == name]
            base_slice = self._base.model_copy(
                update={"strategies": [], "portfolios": only_p}
            )
            entry_db_path = only_p[0].db_path if only_p else None
        sliced = _config_for_mode(base_slice, mode, exchange)
        if entry_db_path is not None:
            # A per-strategy store path isolates this unit's book/PnL from the
            # global store (so two strategies in one manifest never commingle their
            # fills). Override the sliced config's storage.db_path with it.
            sliced = sliced.model_copy(
                update={
                    "storage": sliced.storage.model_copy(
                        update={"db_path": entry_db_path}
                    )
                }
            )
        return sliced

    # --- lifecycle --------------------------------------------------------- #

    async def start(self, name: str) -> None:
        """Build the unit's engine (in its mode) and make it steppable.

        Builds a fresh :class:`~trading_bot.application.service_factory.Engine` from
        the unit's config, **restores** the router's dedup map from the store (if
        any) and **reconciles** to the broker (a no-op on paper; the safety
        backstop on a live/testnet venue), then builds the unit's runner. Idempotent
        — starting an already-running unit is a no-op.

        Raises
        ------
        LiveTradingNotEnabled or BrokerError
            From the factory if a live/testnet unit lacks the opt-in/credentials/
            risk limits (the usual go-live gates apply on the real build).

        """
        unit = self._unit(name)
        if unit.running:
            return
        engine = build_engine(unit.config, db_path=unit.config.storage.db_path)
        if engine.store is not None:
            # Tag every fill this unit records with its deployment mode + venue so
            # a per-mode PnL curve keeps live and testnet (fake money) as separate
            # series. The tag is a storage/deployment concern (it never touches the
            # pure domain Fill); set it before any fill flows onto the bus.
            engine.store.set_context(mode=unit.mode, venue=unit.exchange)
            engine.router.restore(engine.store.orders())
        await reconcile(
            engine.broker, engine.router, engine.tracker, event_bus=engine.bus
        )
        if unit.mode == "paper":
            # Paper has no venue to reconcile against, so `reconcile` above just
            # reset the tracker to the paper broker's (empty) fill set. Replay the
            # store's persisted fills into the tracker + performance service so a
            # paper unit's book survives a restart (positions + realised PnL). On
            # live/testnet the venue's `reconcile` is the source of truth for
            # positions, so we deliberately do NOT replay here — that would
            # double-count against the broker-reported fills. Both `apply`s are
            # idempotent by `fill_id`, so a start-twice (guarded by `unit.running`
            # anyway) never double-applies.
            self._replay_paper_book(engine)
        if unit.kind == "strategy":
            runners = build_runners(
                unit.config, engine, dccd_client=self._dccd_client
            )
            unit.runner = runners[0]
        else:
            pruns = build_portfolio_runners(
                unit.config, engine, dccd_client=self._dccd_client
            )
            unit.runner = pruns[0]
        unit.engine = engine
        unit.running = True

    @staticmethod
    def _replay_paper_book(engine: Engine) -> None:
        """Fold the store's persisted fills into a paper engine's tracker + perf.

        The paper simulator holds no venue state, so the startup ``reconcile``
        leaves the freshly-built engine's tracker / performance service empty even
        when the store holds a book. This replays the store's confirmed **paper**
        fills — the PnL source of truth — into both. Idempotent: both
        :meth:`~trading_bot.application.position_tracker.PositionTracker.apply` and
        :meth:`~trading_bot.application.performance_service.PerformanceService.apply`
        dedup by ``fill_id``, so a re-run never double-counts. Money stays exact
        :class:`~decimal.Decimal`. A no-op when the engine has no store.

        **Only the paper-tagged fills are replayed.** A store can hold fills from
        several deployment modes (a strategy that ran testnet or live, then switched
        back to paper), and testnet/live are *different money* — folding them into
        the paper book would commingle fake / real money into the simulator's PnL
        (and, when a testnet round trip lands on the same instrument as a large open
        paper position, realise a spurious close against the wrong entry price).
        Filtering on the storage ``mode`` tag keeps the paper book pure.

        **Paper only** (the caller gates on ``unit.mode == "paper"``): on
        live/testnet the venue's :func:`~trading_bot.application.reconcile.reconcile`
        already rebuilt the positions from the *broker's* fills, so replaying the
        store's fills too would double-count.
        """
        if engine.store is None:
            return
        for record in engine.store.stored_fills():
            if record.mode != "paper":
                continue
            engine.tracker.apply(record.fill)
            engine.perf.apply(record.fill)

    async def stop(self, name: str) -> None:
        """Tear down the unit's engine — it is no longer stepped. Idempotent."""
        unit = self._unit(name)
        unit.running = False
        unit.runner = None
        unit.engine = None

    async def set_mode(
        self, name: str, mode: StrategyMode, *, confirm_live: bool = False
    ) -> None:
        """Switch a unit's mode (paper / testnet / live), restarting if running.

        Real money is gated: ``mode="live"`` requires ``confirm_live=True`` — the
        deliberate acknowledgement the control API / UI obtains (a typed
        confirmation). Paper ↔ testnet need none. If the unit was running it is
        stopped, re-sliced for the new mode, and started again (so the new engine /
        broker takes effect immediately).

        Raises
        ------
        LiveTradingNotEnabled
            If ``mode == "live"`` without ``confirm_live`` — real money is never
            engaged by a casual flip.

        """
        if mode == "live" and not confirm_live:
            raise LiveTradingNotEnabled(
                f"refusing to switch {name!r} to live (real money) without an "
                "explicit confirmation; the control plane requires a deliberate "
                "acknowledgement (see doc/dev/09-go-live.md). No order placed."
            )
        unit = self._unit(name)
        # Validate the new mode (slice it) BEFORE mutating the unit, so a mode that
        # cannot run (e.g. testnet/live on a venue with no matching broker → a
        # ConfigError) leaves the unit untouched — "nothing changes" on a refused
        # switch, matching the live-confirm gate above.
        new_config = self._slice_for(name, unit.kind, mode, unit.exchange)
        was_running = unit.running
        if was_running:
            await self.stop(name)
        unit.mode = mode
        unit.config = new_config
        if was_running:
            await self.start(name)

    async def step(self, name: str) -> Order | object | None:
        """Run **one** re-evaluation of the unit over the latest data.

        Calls :meth:`~trading_bot.application.strategy_runner.StrategyRunner
        .step_latest` (or
        :meth:`~trading_bot.application.portfolio_runner.PortfolioRunner
        .rebalance_latest`). A no-op (returns ``None``) when the unit is stopped.
        This is what the daemon's scheduler calls per tick.
        """
        unit = self._unit(name)
        if not unit.running or unit.runner is None:
            return None
        if unit.kind == "strategy":
            from trading_bot.application.strategy_runner import StrategyRunner

            assert isinstance(unit.runner, StrategyRunner)
            return await unit.runner.step_latest()
        from trading_bot.application.portfolio_runner import PortfolioRunner

        assert isinstance(unit.runner, PortfolioRunner)
        return await unit.runner.rebalance_latest()

    async def start_all(self) -> None:
        """Start every managed unit (the daemon's boot — each in its config mode)."""
        for name in list(self._units):
            await self.start(name)

    async def step_all(self) -> int:
        """Step every **running** unit once — the daemon's per-tick action.

        Returns the number of units stepped (running units; stopped units are
        skipped). Each unit's :meth:`step` is idempotent over unchanged data, so a
        tick that finds nothing to do trades nothing.
        """
        stepped = 0
        for name in list(self._units):
            if self._units[name].running:
                await self.step(name)
                stepped += 1
        return stepped

    async def shutdown(self) -> None:
        """Stop every running unit (the daemon's graceful teardown)."""
        for name in list(self._units):
            await self.stop(name)

    # --- read side --------------------------------------------------------- #

    def status(self, name: str | None = None) -> list[StrategyStatus]:
        """A snapshot of every unit's state (or just ``name`` if given)."""
        names = [name] if name is not None else list(self._units)
        return [self._status_of(self._unit(n)) for n in names]

    @staticmethod
    def _status_of(unit: _Unit) -> StrategyStatus:
        realised: Money | None = None
        open_orders = 0
        if unit.running and unit.engine is not None:
            realised = unit.engine.perf.realised_pnl()
            open_orders = sum(
                1
                for order in unit.engine.router.tracked_orders().values()
                if not order.is_terminal
            )
        return StrategyStatus(
            name=unit.name,
            kind=unit.kind,
            exchange=unit.exchange,
            mode=unit.mode,
            running=unit.running,
            realised_pnl=realised,
            open_orders=open_orders,
        )

    # --- aggregate read accessors (for the dashboard Overview) ------------- #

    def _running_units(self) -> list[_Unit]:
        """Every running unit with a built engine (the aggregate reads' source)."""
        return [
            unit
            for unit in self._units.values()
            if unit.running and unit.engine is not None
        ]

    def positions(self) -> list[PositionRow]:
        """Every unit's net positions, tagged with strategy + exchange.

        Folds each **running** unit's
        :class:`~trading_bot.application.position_tracker.PositionTracker`
        (``all_positions()``) into one flat list of :class:`PositionRow`, each
        carrying the owning ``strategy`` and its ``exchange`` so the dashboard can
        group by crypto (the instrument's base asset) and/or by exchange. A pure
        in-memory read (money exact :class:`~decimal.Decimal`); stopped units and
        flat books contribute nothing. Empty when no unit is running.

        Returns
        -------
        list of PositionRow
            One row per (strategy, instrument) with live exposure, in unit then
            instrument order.

        """
        rows: list[PositionRow] = []
        for unit in self._running_units():
            assert unit.engine is not None
            positions = unit.engine.tracker.all_positions()
            for position in positions.values():
                rows.append(
                    PositionRow(
                        strategy=unit.name,
                        exchange=unit.exchange,
                        instrument=str(position.instrument),
                        base=position.instrument.symbol.base,
                        net_qty=position.net_qty,
                        avg_entry_price=position.avg_entry_price,
                        realised_pnl=position.realised_pnl,
                        fees_paid=position.fees_paid,
                    )
                )
        return rows

    def open_orders(self) -> list[OrderRow]:
        """Every unit's **open** (non-terminal) orders, tagged strategy + exchange.

        Across every **running** unit's
        :class:`~trading_bot.application.order_router.OrderRouter`, collects the
        orders that are not terminal (still working / partially filled) into one
        list of :class:`OrderRow`, each tagged with the owning ``strategy`` and its
        ``exchange``. A pure in-memory read; empty when nothing is open.

        Returns
        -------
        list of OrderRow
            One row per open order, in unit then tracking order.

        """
        rows: list[OrderRow] = []
        for unit in self._running_units():
            assert unit.engine is not None
            for order in unit.engine.router.tracked_orders().values():
                if not order.is_terminal:
                    rows.append(
                        OrderRow(
                            strategy=unit.name,
                            exchange=unit.exchange,
                            order=order,
                        )
                    )
        return rows

    def order_history(self) -> list[OrderRow]:
        """Every unit's orders — **open and recent history** — tagged strategy + venue.

        The Orders page's full order view: unlike :meth:`open_orders` (non-terminal
        only, running units only), this returns **all** orders — working, partially
        filled, filled, cancelled and rejected — across every unit. The **store** is
        the source of truth for the history (its append-only ``orders`` table keeps
        every order the unit ever recorded, whereas a running unit's live router map
        only holds *currently-tracked* orders — the startup reconcile evicts a
        restored historical order as an "orphan" the venue no longer reports). A
        **running** unit's live router orders are **unioned** in on top (keyed by
        ``client_order_id``, the live object winning) so a freshly-submitted,
        not-yet-persisted order still surfaces. Money stays exact
        :class:`~decimal.Decimal`.

        Returns
        -------
        list of OrderRow
            One row per order (any status), in unit then insertion order (stored
            history first, then any live-only orders).

        """
        rows: list[OrderRow] = []
        for unit in self._units.values():
            by_cid: dict[str, Order] = {
                order.client_order_id: order for order in self._stored_orders_of(unit)
            }
            if unit.running and unit.engine is not None:
                # Union the live router's orders on top (a live object wins its cid,
                # catching a just-submitted order not yet flushed to the store).
                for order in unit.engine.router.tracked_orders().values():
                    by_cid[order.client_order_id] = order
            for order in by_cid.values():
                rows.append(
                    OrderRow(strategy=unit.name, exchange=unit.exchange, order=order)
                )
        return rows

    def fills(self) -> list[FillRow]:
        """Every unit's confirmed fills — the **fill history** — tagged for filtering.

        The Orders/Fills page's data: folds every unit's persisted fills (running or
        stopped — read from the live ``engine.store`` when running, else a store
        opened at the unit's configured ``db_path``) into one flat list of
        :class:`FillRow`, each carrying the owning ``strategy``, the ``exchange`` it
        executed on (the store's ``venue`` tag when set, else the unit's declared
        exchange) and the instrument's ``base`` crypto — the three dimensions the
        page filters by. Fills are the PnL source of truth; money stays exact
        :class:`~decimal.Decimal`. Empty when no unit has recorded a fill.

        Returns
        -------
        list of FillRow
            One row per confirmed fill, in unit then execution order.

        """
        rows: list[FillRow] = []
        for unit in self._units.values():
            for record in self._stored_fills_of(unit):
                fill = record.fill
                rows.append(
                    FillRow(
                        strategy=unit.name,
                        exchange=record.venue or unit.exchange,
                        base=fill.instrument.symbol.base,
                        fill=fill,
                    )
                )
        return rows

    def _stored_orders_of(self, unit: _Unit) -> list[Order]:
        """A stopped unit's persisted orders — from a store at its ``db_path``.

        The order-history counterpart of :meth:`_stored_fills_of`: a stopped unit
        has no live router, so its recent orders are read back from a store opened
        at its configured ``db_path``. With no ``db_path`` there is nowhere to read
        from, so the orders are empty.
        """
        db_path = unit.config.storage.db_path
        if db_path is None:
            return []
        return SqliteStore(db_path).orders()

    def kpi(self, level: KpiLevel = "strategy") -> list[KpiRow]:
        """Realised PnL + fees (+ per-strategy ratios) at ``level``.

        Three aggregation levels over the **running** units' per-engine
        :class:`~trading_bot.application.performance_service.PerformanceService`:

        * ``"strategy"`` — one :class:`KpiRow` per running unit, carrying that
          unit's realised PnL, fees and its Sharpe / Sortino / Calmar /
          max-drawdown ratios (read straight off the unit's performance service).
        * ``"exchange"`` — the units sharing a venue folded into one row per
          exchange (PnL and fees **summed**, exact :class:`~decimal.Decimal`); the
          ratios are computed on the venue units' **combined equity curve**
          (:meth:`combined_equity_series`, each unit's own current mode).
        * ``"total"`` — every running unit folded into a single row, ratios on the
          combined equity curve of every unit.

        Aggregate ratios degrade to ``None`` when fynance is absent (never raise)
        or the combined curve is too short / degenerate to estimate the ratio.
        Money is kept exact end to end (never float); only the ratios are floats.
        Empty when no unit is running.

        Parameters
        ----------
        level : {"strategy", "exchange", "total"}, optional
            The aggregation level. Defaults to ``"strategy"``.

        Returns
        -------
        list of KpiRow
            The rows for ``level`` (see above).

        Raises
        ------
        ValueError
            If ``level`` is not one of the three recognised levels.

        """
        if level not in ("strategy", "exchange", "total"):
            raise ValueError(
                f"unknown KPI level {level!r}; expected one of "
                "'strategy', 'exchange', 'total'"
            )
        units = self._running_units()
        if level == "strategy":
            return [self._strategy_kpi(unit) for unit in units]
        if level == "exchange":
            return self._exchange_kpi(units)
        return self._total_kpi(units)

    def _combined_ratios(
        self, units: list[_Unit]
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """The Sharpe / Sortino / Calmar / maxDD of ``units``' combined equity curve.

        Folds every unit in the group onto **one** combined equity curve — each
        unit contributes the fills of **its own current mode** (so live and testnet
        fake money are never mixed, and a paper unit contributes its paper book),
        anchored at the sum of the units' ``v0`` — and estimates the four risk
        ratios on it. Reuses :meth:`combined_equity_series` per mode (its ``mode``
        slices exactly the live-vs-testnet separation the PnL series enforces) and
        merges the resulting curves by timestamp.

        Degrades to ``(None, None, None, None)`` when fynance is absent (the
        estimators raise :class:`~trading_bot.domain.performance.
        PerformanceDependencyError`) or the combined curve is too short to estimate
        (each ratio degrades independently). Never raises — the dashboard stays
        functional without the research dependency.
        """
        # Group the units by their current mode so live and testnet (fake money)
        # never share a curve, then combine per mode and merge by timestamp. Each
        # unit's own mode is what its book was built for.
        names_by_mode: dict[StrategyMode, list[str]] = {}
        for unit in units:
            names_by_mode.setdefault(unit.mode, []).append(unit.name)
        merged: list[list[object]] = []
        for mode, names in names_by_mode.items():
            merged.extend(self.combined_equity_series(names, mode=mode))
        # Sort the merged points by timestamp (stable) so a mixed-mode group's
        # equity path is chronological before the ratio estimators consume it.
        merged.sort(key=lambda point: cast("int", point[0]))
        # combined_equity_series' points are [ts_ms, realised_pnl, equity]; the
        # equity column is Money (Decimal), the sequence the ratio estimators take.
        equity: list[Money] = [cast("Money", point[2]) for point in merged]

        # An empty curve has no ratio to estimate — fynance indexes ``X[0]`` and
        # raises on it. Short-circuit to None (the "undefined estimator" outcome)
        # before touching the research dependency at all.
        if not equity:
            return (None, None, None, None)

        def _ratio(fn: Callable[[Sequence[Money]], float]) -> float | None:
            try:
                return fn(equity)
            except PerformanceDependencyError:
                return None
            except (ValueError, ZeroDivisionError, ArithmeticError, IndexError):
                # A degenerate / too-short combined curve — the ratio is undefined
                # on it (fynance raises); surface it as None, like the per-strategy
                # path's "undefined estimator → None" convention.
                return None

        return (
            _ratio(sharpe),
            _ratio(sortino),
            _ratio(calmar),
            _ratio(max_drawdown),
        )

    @staticmethod
    def _strategy_kpi(unit: _Unit) -> KpiRow:
        """One :class:`KpiRow` for a running unit — PnL/fees + its own ratios.

        The ratio KPIs (Sharpe/Sortino/Calmar/maxDD) are computed via ``fynance``;
        if the optional dependency is absent they degrade to ``None`` (the dashboard
        still reports PnL/fees) rather than raising.
        """
        assert unit.engine is not None
        perf = unit.engine.perf

        def _ratio(fn: Callable[[], float]) -> float | None:
            try:
                return fn()
            except PerformanceDependencyError:
                return None

        return KpiRow(
            level="strategy",
            key=unit.name,
            strategy=unit.name,
            exchange=unit.exchange,
            realised_pnl=perf.realised_pnl(),
            fees_paid=perf.fees_paid(),
            sharpe=_ratio(perf.sharpe),
            sortino=_ratio(perf.sortino),
            calmar=_ratio(perf.calmar),
            max_drawdown=_ratio(perf.max_drawdown),
        )

    def _exchange_kpi(self, units: list[_Unit]) -> list[KpiRow]:
        """Fold the units per venue: PnL/fees summed, ratios on the combined curve.

        Preserves first-seen exchange order so the view is deterministic. The
        ratios come from each venue's units' combined equity curve (degrading to
        ``None`` without fynance / on a too-short curve).
        """
        pnl: dict[str, Money] = {}
        fees: dict[str, Money] = {}
        by_venue: dict[str, list[_Unit]] = {}
        order: list[str] = []
        for unit in units:
            assert unit.engine is not None
            venue = unit.exchange
            if venue not in pnl:
                pnl[venue] = _ZERO
                fees[venue] = _ZERO
                by_venue[venue] = []
                order.append(venue)
            pnl[venue] += unit.engine.perf.realised_pnl()
            fees[venue] += unit.engine.perf.fees_paid()
            by_venue[venue].append(unit)
        rows: list[KpiRow] = []
        for venue in order:
            sh, so, ca, mdd = self._combined_ratios(by_venue[venue])
            rows.append(
                KpiRow(
                    level="exchange",
                    key=venue,
                    strategy=None,
                    exchange=venue,
                    realised_pnl=pnl[venue],
                    fees_paid=fees[venue],
                    sharpe=sh,
                    sortino=so,
                    calmar=ca,
                    max_drawdown=mdd,
                )
            )
        return rows

    def _total_kpi(self, units: list[_Unit]) -> list[KpiRow]:
        """Fold every unit into a single total row (ratios on the combined curve).

        Always one row (a zero-PnL total even when no unit is running), so the
        dashboard's total strip has a value to paint. The ratios come from every
        unit's combined equity curve (``None`` without fynance / on a short curve).
        """
        total_pnl: Money = _ZERO
        total_fees: Money = _ZERO
        for unit in units:
            assert unit.engine is not None
            total_pnl += unit.engine.perf.realised_pnl()
            total_fees += unit.engine.perf.fees_paid()
        sh, so, ca, mdd = self._combined_ratios(units)
        return [
            KpiRow(
                level="total",
                key="total",
                strategy=None,
                exchange=None,
                realised_pnl=total_pnl,
                fees_paid=total_fees,
                sharpe=sh,
                sortino=so,
                calmar=ca,
                max_drawdown=mdd,
            )
        ]

    # --- PnL series (per-mode realised-PnL / equity curve over time) -------- #

    def pnl_series(self, name: str) -> dict[str, object]:
        """The per-mode realised-PnL / equity curve for one strategy, over time.

        The data foundation for the dashboard's PnL chart. Reads the strategy's
        confirmed fills — tagged with the mode + venue they executed under — and
        **derives** an equity curve per mode by folding them in timestamp order
        (``equity(t) = v0 + Σ realised_pnl(fills ≤ t)`` via the pure
        :func:`~trading_bot.application.pnl_series.equity_series`). **Live and
        testnet are kept as separate series** — testnet is fake money and is never
        combined with real-money live. Plus a current mark-to-market end point per
        mode (the running engine's open positions × the last-known fill price),
        left ``None`` when no mark is available (continuous MTM history is out of
        scope for v1).

        Fills are read from the **running** unit's ``engine.store``; if the unit
        is stopped they are read from a store opened at its configured
        ``db_path`` (``None`` when the unit persists nothing — then the series are
        empty). The curve reconciles to the running engine's
        :meth:`~trading_bot.application.performance_service.PerformanceService.
        realised_pnl` to the cent (same fold, same ``v0``).

        Parameters
        ----------
        name : str
            The managed strategy to read.

        Returns
        -------
        dict
            ``{"strategy", "v0", "series": {mode: [[ts_ms, pnl, equity], ...]},
            "current": {mode: {"equity", "unrealised"}}}`` — money as exact
            :class:`~decimal.Decimal` (the API stringifies it), timestamps integer
            ms.

        Raises
        ------
        ConfigError
            If ``name`` is not a managed unit.

        """
        unit = self._unit(name)
        v0 = self._v0_of(unit)
        stored = self._stored_fills_of(unit)
        buckets = by_mode(stored)

        series: dict[str, list[list[object]]] = {}
        current: dict[str, dict[str, Money | None]] = {}
        for mode, fills in buckets.items():
            points = equity_series(fills, v0=v0)
            series[mode] = [
                [p.ts_ms, p.realised_pnl, p.equity] for p in points
            ]
            end_equity = points[-1].equity if points else v0
            current[mode] = {
                "equity": end_equity,
                "unrealised": self._unrealised_of(unit, mode, fills),
            }
        return {
            "strategy": unit.name,
            "v0": v0,
            "series": series,
            "current": current,
        }

    def combined_equity_series(
        self, names: list[str] | None = None, *, mode: StrategyMode = "paper"
    ) -> list[list[object]]:
        """Combine several strategies' equity series for ``mode``, aligned by ts.

        The seam the aggregate ratio KPIs (a later leaf) fold over: it takes each
        named strategy's per-mode fills, merges them into **one** timestamp-ordered
        stream, and folds that stream into a single combined equity curve anchored
        at the **sum** of the strategies' ``v0`` (each contributes its own
        starting capital). Only the ``mode`` slice is combined — live and testnet
        (fake money) are never mixed. A pure fold over the confirmed fills; money
        stays exact.

        Parameters
        ----------
        names : list of str or None, optional
            The strategies to combine. ``None`` (default) combines every managed
            unit.
        mode : StrategyMode, optional
            The deployment mode slice to combine (default ``"paper"``).

        Returns
        -------
        list of list
            ``[[ts_ms, realised_pnl, equity], ...]`` — the combined curve, one
            point per fill, ascending ts. Empty when no fill matches.

        """
        wanted = names if names is not None else list(self._units)
        combined_fills: list[Fill] = []
        combined_v0: Money = _ZERO
        for name in wanted:
            unit = self._unit(name)
            combined_v0 += self._v0_of(unit)
            buckets = by_mode(self._stored_fills_of(unit))
            combined_fills.extend(buckets.get(mode, []))
        points = equity_series(combined_fills, v0=combined_v0)
        return [[p.ts_ms, p.realised_pnl, p.equity] for p in points]

    # --- PnL series helpers ------------------------------------------------- #

    @staticmethod
    def _v0_of(unit: _Unit) -> Money:
        """The unit's equity-curve anchor — its config ``starting_capital``.

        The same ``v0`` :func:`~trading_bot.application.service_factory.build_engine`
        seeds the unit's performance service with, so a derived
        :meth:`pnl_series` curve reconciles to the running engine's
        ``perf.realised_pnl()`` exactly (``final equity == v0 + realised PnL``).
        """
        return unit.config.starting_capital

    def _stored_fills_of(self, unit: _Unit) -> list[StoredFill]:
        """The unit's tagged fills — from the running engine's store, else its db.

        A running unit reads straight off its live ``engine.store``. A stopped
        unit (no live engine) reads a store opened at its configured ``db_path``;
        with no ``db_path`` there is nowhere to read from, so the fills are empty.
        """
        if unit.running and unit.engine is not None and unit.engine.store is not None:
            return unit.engine.store.stored_fills()
        db_path = unit.config.storage.db_path
        if db_path is None:
            return []
        return SqliteStore(db_path).stored_fills()

    @staticmethod
    def _unrealised_of(
        unit: _Unit, mode: str, fills: list[Fill]
    ) -> Money | None:
        """Best-effort mark-to-market of the running unit's open book, in ``mode``.

        The running engine's tracker holds the net open positions; each is marked
        against the **last-known fill price** for its instrument in ``mode``'s
        stream (``(mark - avg_entry) * net_qty``) — a fully in-memory,
        non-network last-known mark (continuous MTM history is out of scope). The
        book is meaningful only for the unit's **current** mode (the tracker was
        built for it), so a non-current mode marks to ``None``. ``None`` too when
        the unit is stopped, flat, or has no priced instrument.
        """
        if (
            not unit.running
            or unit.engine is None
            or mode != unit.mode
            or not fills
        ):
            return None
        last_price: dict[object, Money] = {}
        for fill in fills:
            last_price[fill.instrument] = fill.price
        unrealised: Money = _ZERO
        marked = False
        for position in unit.engine.tracker.all_positions().values():
            if position.is_flat or position.avg_entry_price is None:
                continue
            mark = last_price.get(position.instrument)
            if mark is None:
                continue
            unrealised += (mark - position.avg_entry_price) * position.net_qty
            marked = True
        return unrealised if marked else None


def _mode_of(config: AppConfig) -> StrategyMode:
    """Infer a :data:`StrategyMode` from an :class:`AppConfig`'s mode/brokers."""
    if config.mode == "paper":
        return "paper"
    if any(broker.testnet for broker in config.brokers):
        return "testnet"
    return "live"


def _config_for_mode(
    base_slice: AppConfig, mode: StrategyMode, exchange: str
) -> AppConfig:
    """Rewrite a single-unit config for ``mode`` (paper / testnet / live) + ``exchange``.

    ``testnet`` and ``live`` select **only the broker whose exchange matches** the
    unit's venue — so a strategy on Kraken uses the Kraken broker and one on Binance
    the Binance broker (each unit has its own engine). A unit with no matching broker
    can only run ``paper``.
    """
    if mode == "paper":
        return base_slice.model_copy(
            update={"mode": "paper", "live_enabled": False}
        )
    matching = [
        b for b in base_slice.brokers if b.exchange.lower() == exchange.lower()
    ]
    if not matching:
        raise ConfigError(
            f"cannot run mode {mode!r} on exchange {exchange!r}: no matching broker "
            f"configured (have {[b.exchange for b in base_slice.brokers]!r}); add a "
            "'brokers' entry for that venue, or keep the strategy on paper"
        )
    testnet = mode == "testnet"
    brokers = [b.model_copy(update={"testnet": testnet}) for b in matching]
    return base_slice.model_copy(
        update={
            "mode": "live",
            "live_enabled": mode == "live",
            "brokers": brokers,
        }
    )
