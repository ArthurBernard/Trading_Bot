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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from trading_bot.application.reconcile import reconcile
from trading_bot.application.run_app import build_portfolio_runners, build_runners
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.domain.errors import ConfigError, LiveTradingNotEnabled

if TYPE_CHECKING:
    from trading_bot.application.config import AppConfig
    from trading_bot.application.data_provider import DccdClient
    from trading_bot.application.portfolio_runner import PortfolioRunner
    from trading_bot.application.strategy_runner import StrategyRunner
    from trading_bot.domain.money import Money
    from trading_bot.domain.order import Order

__all__ = ["StrategyMode", "StrategyStatus", "StrategySupervisor"]

#: The deployment mode of a managed strategy.
StrategyMode = Literal["paper", "testnet", "live"]

_KIND = Literal["strategy", "portfolio"]


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
        """The single-unit ``AppConfig`` for ``name`` in ``mode`` on ``exchange``."""
        if kind == "strategy":
            only = [s for s in self._base.strategies if s.name == name]
            base_slice = self._base.model_copy(
                update={"strategies": only, "portfolios": []}
            )
        else:
            only_p = [p for p in self._base.portfolios if p.name == name]
            base_slice = self._base.model_copy(
                update={"strategies": [], "portfolios": only_p}
            )
        return _config_for_mode(base_slice, mode, exchange)

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
            engine.router.restore(engine.store.orders())
        await reconcile(
            engine.broker, engine.router, engine.tracker, event_bus=engine.bus
        )
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
        was_running = unit.running
        if was_running:
            await self.stop(name)
        unit.mode = mode
        unit.config = self._slice_for(name, unit.kind, mode, unit.exchange)
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
