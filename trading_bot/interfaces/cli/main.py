"""The Typer ``trading-bot`` application — the CLI entrypoint.

This module defines :data:`app`, the :class:`typer.Typer` instance the
``trading-bot`` console script points at (target
``trading_bot.interfaces.cli.main:app``). It carries the user-facing commands:

* :func:`version` — print the installed package version;
* :func:`run` — drive a strategy over a bars source through the engine
  (paper-by-default; ``--live`` is an explicit, guarded opt-in);
* :func:`status` — show current positions + open orders;
* :func:`kpi` — show the realised-PnL / fees / KPI table.

The CLI holds **no business logic**: commands delegate to the use-cases the
:func:`~trading_bot.application.service_factory.build_engine` factory wires, and
hand the resulting state to the pure rendering helpers in
:mod:`trading_bot.interfaces.cli._render`. ``run`` builds a strategy + a
:class:`~trading_bot.application.data_feed.DataFeed`, drives the
:class:`~trading_bot.application.strategy_runner.StrategyRunner` (via
:func:`asyncio.run`), and prints a short summary.

Paper-by-default (the invariant)
--------------------------------
A ``run`` defaults to **paper** trading — no venue, no key, no network. Going
``--live`` requires *all* of: an explicit acknowledgement (``--yes-i-understand``,
or an interactive confirmation), the config's off-by-default opt-in
(``live_enabled: true``), *and* venue credentials; absent any of them, ``run``
**refuses with a non-zero exit and never places an order**, pointing the user at
the go-live runbook (``doc/dev/09-go-live.md``). The factory
(:func:`~trading_bot.application.service_factory.build_engine`) enforces the same
on its side (it raises :class:`~trading_bot.domain.errors.LiveTradingNotEnabled`
unless ``live_enabled`` is set, and never silently falls back to paper for a
credential-less live venue), so neither a missing opt-in nor a missing key can
trade real money by accident.

Offline-testable bars source
-----------------------------
``run`` always supports a fully offline path so the whole command is testable
without a network: ``--bars <path>`` loads a CSV or Parquet OHLC file into a
:class:`~trading_bot.application.data_feed.InMemoryFeed`; with no ``--bars`` a
small built-in **synthetic** trending series is used (enough bars for the
MA-crossover example to cross), so ``trading-bot run`` does something meaningful
out of the box.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import pathlib
import signal
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING

import polars as pl
import typer
from rich.console import Console

from trading_bot import __version__
from trading_bot.application.config import AppConfig, StrategyConfig
from trading_bot.application.data_feed import BARS_SCHEMA, InMemoryFeed
from trading_bot.application.performance_service import PerformanceService
from trading_bot.application.run_app import run_app
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.application.strategy import (
    Strategy,
    load_strategy,
    ma_crossover_signal,
)
from trading_bot.application.strategy_runner import StrategyRunner
from trading_bot.domain.instrument import Instrument, parse_kraken_pair
from trading_bot.domain.money import Money, from_float, money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.interfaces.cli import _render

if TYPE_CHECKING:
    from fastapi import FastAPI

app = typer.Typer(
    name="trading-bot",
    help="Execution & orchestration engine of the trading triptych.",
    no_args_is_help=True,
    add_completion=False,
)

#: The shared rich console every command prints through.
_console = Console()

#: Default synthetic-feed length (bars) when no ``--bars`` file is given — long
#: enough for the default 10/30 MA windows to warm up and cross at least once.
_SYNTHETIC_BARS = 80

#: The go-live runbook the ``--live`` refusals point the user at.
_RUNBOOK = "doc/dev/09-go-live.md"


@app.callback()
def _main() -> None:
    """``trading-bot`` — the engine's command-line interface.

    A no-op group callback so Typer treats the app as a *multi-command* group
    (without it a lone command collapses into the root callback). The real
    commands slot in alongside ``version``.
    """


@app.command()
def version() -> None:
    """Print the installed ``trading_bot`` version and exit."""
    typer.echo(__version__)


# --- run ------------------------------------------------------------------- #


def _load_bars(path: pathlib.Path) -> pl.DataFrame:
    """Load an OHLC bars file (``.csv`` or ``.parquet``) into a polars frame.

    The file must carry the :data:`~trading_bot.application.data_feed.BARS_SCHEMA`
    columns (``time, o, h, l, c, v``); :class:`InMemoryFeed` validates that on
    construction. Raises a :class:`typer.BadParameter` for a missing file or an
    unknown extension so the CLI surfaces a clean error.
    """
    if not path.exists():
        raise typer.BadParameter(f"bars file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pl.read_csv(path)
    if suffix in (".parquet", ".pq"):
        return pl.read_parquet(path)
    raise typer.BadParameter(
        f"unsupported bars file type {suffix!r}; use .csv or .parquet"
    )


def _synthetic_bars(n: int = _SYNTHETIC_BARS) -> pl.DataFrame:
    """Build a deterministic synthetic OHLC frame that trends up then down.

    A self-contained default bars source: a smooth sine-modulated uptrend that
    rises then falls, so the fast MA crosses the slow MA in both directions and
    the MA-crossover example actually trades. Deterministic (no randomness) so a
    ``run`` with no ``--bars`` is reproducible.
    """
    times = list(range(n))
    closes = [100.0 + 20.0 * math.sin(2.0 * math.pi * t / n) for t in times]
    return pl.DataFrame(
        {
            "time": times,
            "o": closes,
            "h": [c + 1.0 for c in closes],
            "l": [c - 1.0 for c in closes],
            "c": closes,
            "v": [1.0] * n,
        }
    )


def _limit_at_close_factory(
    close_col: str = "c",
) -> Callable[[Strategy, Money, pl.DataFrame], Order]:
    """Build an order factory that prices a step's order at the latest close.

    The runner submits MARKET orders by default, which the
    :class:`~trading_bot.brokers.paper.PaperBroker` can only fill if a mark price
    has been injected. Pricing each order as a LIMIT at the bar's **close** makes
    the offline run fully self-contained — the broker fills at that exact close —
    and keeps the simulated execution price equal to the price the signal saw.
    The runner overrides the ``client_order_id`` afterwards, so the factory need
    not set one meaningfully.
    """

    def _factory(strategy: Strategy, delta: Money, bars: pl.DataFrame) -> Order:
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


def _build_strategy(config: AppConfig, *, fast: int, slow: int, qty: Money) -> Strategy:
    """Load the MA-crossover example strategy for the config's first symbol.

    Uses the first configured strategy's ``symbol`` (or a ``BTC/USD`` default),
    wires the :func:`~trading_bot.application.strategy.ma_crossover_signal`
    example, and sets ``reference_qty`` (the base-unit scale a fractional
    exposure resolves against — required for the EXPOSURE-mode example) and
    ``lookback`` to the slow window (warmup) so no order fires on partial data.
    """
    strat_cfg = (
        config.strategies[0]
        if config.strategies
        else StrategyConfig(name="ma-cross", symbol="BTC/USD")
    )
    # The example signal is built *for* an instrument, so resolve the instrument
    # first (load_strategy with the built callable), then set the exposure scale
    # (reference_qty, required for the EXPOSURE-mode example) and the warmup
    # (lookback = slow window). Strategy is frozen, so use dataclasses.replace.
    instrument = Instrument(parse_kraken_pair(strat_cfg.symbol))
    signal_fn = ma_crossover_signal(instrument, fast=fast, slow=slow)
    base = load_strategy(strat_cfg, signal_fn)
    return dataclasses.replace(base, reference_qty=qty, lookback=slow)


async def _run_engine(
    engine: Engine, strategy: Strategy, feed: InMemoryFeed
) -> int:
    """Drive the strategy over the feed through the engine; return orders sent."""
    runner = StrategyRunner(
        strategy,
        feed,
        engine.router,
        engine.tracker,
        event_bus=engine.bus,
        order_factory=_limit_at_close_factory(),
    )
    return await runner.run()


@app.command()
def run(
    config_path: pathlib.Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="YAML AppConfig path. Defaults to a paper config (BTC/USD).",
    ),
    bars_path: pathlib.Path | None = typer.Option(
        None,
        "--bars",
        "-b",
        help="OHLC bars file (.csv/.parquet, schema time,o,h,l,c,v). "
        "Omit for a built-in synthetic feed.",
    ),
    db_path: pathlib.Path | None = typer.Option(
        None,
        "--db",
        help="Persist order/fill history to this SqliteStore path "
        "(read it back later with status/kpi).",
    ),
    qty: float = typer.Option(
        1.0,
        "--qty",
        help="Reference position size (base units) the exposure scales to.",
    ),
    fast: int = typer.Option(10, "--fast", help="Fast MA window (bars)."),
    slow: int = typer.Option(30, "--slow", help="Slow MA window (bars)."),
    live: bool = typer.Option(
        False,
        "--live",
        help="Trade LIVE (real money). Requires --yes-i-understand AND the "
        "config's live_enabled: true AND credentials; refuses otherwise "
        "(see doc/dev/09-go-live.md).",
    ),
    yes_i_understand: bool = typer.Option(
        False,
        "--yes-i-understand",
        help="Explicit acknowledgement required to go --live.",
    ),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Also serve the read-only live dashboard over HTTP while the run "
        "executes, so you can monitor positions / orders / PnL in real time "
        "(Ctrl-C stops both). Read-only — the dashboard never places an order.",
    ),
    serve_host: str = typer.Option(
        "127.0.0.1", "--serve-host", help="Dashboard bind interface (loopback)."
    ),
    serve_port: int = typer.Option(
        8000, "--serve-port", help="Dashboard TCP port."
    ),
) -> None:
    """Run the declared system (or a quick demo) and print a short summary.

    Two paths share the same paper-by-default engine and the same ``--live``
    guard:

    * **declared system** — when the config (``--config``) declares one or more
      strategies, the whole multi-strategy system is brought up via the triptych
      entrypoint :func:`~trading_bot.application.run_app.run_app`: the engine is
      built, a :class:`~trading_bot.application.strategy_runner.StrategyRunner` is
      created per declared strategy (its signal + dccd feed resolved from the
      config), and they all run concurrently through the
      :class:`~trading_bot.application.orchestrator.Orchestrator`. A per-strategy
      summary + the positions table is printed.
    * **quick demo** — with no config (or a config that declares no strategies),
      the built-in MA-crossover example is run over a ``--bars`` file or the
      synthetic feed, exactly as before, so ``trading-bot run`` does something
      meaningful out of the box.

    **Going live is guarded.** ``--live`` demands ``--yes-i-understand``, the
    config's off-by-default opt-in (``live_enabled: true``) *and* venue
    credentials; missing any of them, the command refuses with a non-zero exit,
    points at the go-live runbook (``doc/dev/09-go-live.md``) and **never places
    an order** (the broker is never even built down the live path until every
    check passes).
    """
    config = (
        AppConfig.from_yaml(config_path)
        if config_path is not None
        else AppConfig()
    )

    mode = _resolve_mode(config, live=live, yes_i_understand=yes_i_understand)
    config = config.model_copy(update={"mode": mode})

    # --serve: run the declared system AND serve the read-only dashboard over the
    # SAME engine, so the run can be monitored live. Handles 0+ strategies.
    if serve:
        _run_and_serve(config, host=serve_host, port=serve_port)
        return

    # A config that declares strategies (with their own data + signal) runs the
    # whole declared system via the triptych entrypoint. A bare config (no
    # strategies) keeps the quick single-strategy demo path below.
    if config.strategies:
        _run_declared_system(config)
        return

    # Build the engine. In live mode build_engine refuses (BrokerError) if the
    # configured venue lacks credentials — caught and surfaced as a clean exit,
    # with no order ever placed.
    try:
        engine = build_engine(config, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 - surface any build failure cleanly
        _console.print(f"[red]refusing to run:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    reference_qty = money(str(Decimal(str(qty))))
    try:
        strategy = _build_strategy(config, fast=fast, slow=slow, qty=reference_qty)
    except Exception as exc:  # noqa: BLE001
        _console.print(f"[red]bad strategy parameters:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    frame = (
        _load_bars(bars_path) if bars_path is not None else _synthetic_bars()
    )
    feed = InMemoryFeed(frame.select(list(BARS_SCHEMA)))

    submitted = asyncio.run(_run_engine(engine, strategy, feed))

    position = engine.tracker.position(strategy.instrument)
    net_qty = position.net_qty if position is not None else money("0")

    _console.print(
        f"[green]run complete[/green] "
        f"(mode={config.mode}, strategy={strategy.name}, "
        f"instrument={strategy.instrument})"
    )
    _console.print(f"orders submitted : {submitted}")
    _console.print(f"final net qty    : {_render.fmt_money(net_qty)}")
    _console.print(
        f"realised PnL     : {_render.fmt_money(engine.perf.realised_pnl())}"
    )
    _console.print(
        f"fees paid        : {_render.fmt_money(engine.perf.fees_paid())}"
    )
    _console.print(_render.positions_table(engine.tracker.all_positions()))


def _run_declared_system(config: AppConfig) -> None:
    """Bring up the whole declared multi-strategy system and print its summary.

    Delegates to the triptych entrypoint
    :func:`~trading_bot.application.run_app.run_app` (via :func:`asyncio.run`),
    which builds the engine (paper-by-default — the factory enforces it), a
    runner per declared strategy, and runs them concurrently through the
    :class:`~trading_bot.application.orchestrator.Orchestrator`. Then prints a
    per-strategy summary line (orders + final net qty) followed by the aggregate
    PnL / fees and the positions table. Any build/config failure (a misdeclared
    strategy, or a credential-less live venue the factory refuses) is surfaced as
    a clean non-zero exit — no order placed.
    """
    try:
        report = asyncio.run(run_app(config))
    except Exception as exc:  # noqa: BLE001 - surface any build/config failure
        _console.print(f"[red]refusing to run:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _console.print(
        f"[green]run complete[/green] "
        f"(mode={config.mode}, strategies={len(report.strategies)}, "
        f"orders={report.total_orders})"
    )
    for strat in report.strategies:
        net_qty = (
            strat.position.net_qty if strat.position is not None else money("0")
        )
        _console.print(
            f"  - {strat.name} [{strat.instrument}]: "
            f"orders={strat.orders_submitted} "
            f"net_qty={_render.fmt_money(net_qty)}"
        )
    _console.print(f"realised PnL     : {_render.fmt_money(report.realised_pnl)}")
    _console.print(f"fees paid        : {_render.fmt_money(report.fees_paid)}")

    positions = {
        s.instrument: s.position
        for s in report.strategies
        if s.position is not None
    }
    _console.print(_render.positions_table(positions))


def _run_and_serve(config: AppConfig, *, host: str, port: int) -> None:
    """Run the declared system **and** serve the live dashboard over one engine.

    Builds the system once (:func:`~trading_bot.application.run_app.prepare_system`),
    serves the read-only FastAPI dashboard
    (:func:`~trading_bot.interfaces.api.create_app`) over the **same** engine under
    uvicorn, and runs the orchestrator concurrently — so the dashboard reflects the
    live run in real time (positions / orders / PnL via the engine bus + SSE).
    uvicorn owns ``SIGINT``: Ctrl-C ends ``serve``, and the ``finally`` then drains
    the orchestrator. The dashboard is **read-only** — it can never place an order.
    A finite (paper) run completes while the dashboard keeps serving the final state
    until Ctrl-C; a live run streams until stopped. Build/config failures surface as
    a clean non-zero exit with no order placed.
    """
    import uvicorn

    from trading_bot.application.run_app import prepare_system
    from trading_bot.interfaces.api import create_app

    async def _serve() -> None:
        system = await prepare_system(config)
        api = create_app(system.engine)
        server = uvicorn.Server(
            uvicorn.Config(api, host=host, port=port, log_level="warning")
        )
        orch_task = asyncio.create_task(system.orchestrator.run())
        _console.print(
            f"[green]live dashboard[/green] (mode={config.mode}) on "
            f"http://{host}:{port}  —  Ctrl-C to stop"
        )
        try:
            await server.serve()  # blocks until SIGINT (uvicorn owns the signal)
        finally:
            system.orchestrator.stop_event.set()
            if not orch_task.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(orch_task, timeout=5.0)
            if not orch_task.done():
                orch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await orch_task

    try:
        asyncio.run(_serve())
    except Exception as exc:  # noqa: BLE001 - surface any build/config failure
        _console.print(f"[red]refusing to run:[/red] {exc}")
        raise typer.Exit(code=1) from exc


def _resolve_mode(
    config: AppConfig, *, live: bool, yes_i_understand: bool
) -> str:
    """Resolve the effective run mode, guarding the live path.

    Paper unless ``--live`` is set. ``--live`` requires *both* the explicit
    ``--yes-i-understand`` acknowledgement (or an interactive confirmation) *and*
    the config's off-by-default opt-in (``live_enabled: true``); missing either,
    the command refuses with a non-zero exit, points at the go-live runbook
    (``doc/dev/09-go-live.md``) and never proceeds to build a broker. (Credential
    presence is enforced one layer down by
    :func:`~trading_bot.application.service_factory.build_engine`, which also
    re-checks the opt-in and refuses a credential-less live venue — so the gates
    together mean a live order is impossible without an acknowledgement, the
    config opt-in, *and* a key.)
    """
    if not live:
        return "paper"

    acknowledged = yes_i_understand
    if not acknowledged:
        # No flag acknowledgement: ask interactively. A non-tty / declined
        # answer leaves it False and we refuse below.
        acknowledged = typer.confirm(
            "LIVE trading uses real money. Are you sure you want to continue?",
            default=False,
        )
    if not acknowledged:
        _console.print(
            "[red]refusing to trade live[/red] without explicit confirmation; "
            f"pass --yes-i-understand (no order was placed). See {_RUNBOOK}."
        )
        raise typer.Exit(code=1)

    # Second, off-by-default gate: the config must opt into live explicitly.
    # Mirrors the factory's LiveTradingNotEnabled — refuse here so no engine is
    # ever built and no order can be placed.
    if not config.live_enabled:
        _console.print(
            "[red]refusing to trade live[/red]: live is off by default. Set "
            f"live_enabled: true in the config and read {_RUNBOOK} first "
            "(no order was placed)."
        )
        raise typer.Exit(code=1)

    return "live"


# --- status ---------------------------------------------------------------- #


@app.command()
def status(
    db_path: pathlib.Path = typer.Option(
        ...,
        "--db",
        help="SqliteStore database path to read positions/orders from.",
    ),
) -> None:
    """Show positions + open orders read from a persisted :class:`SqliteStore`.

    The status command reads the **stored** order/fill history (written by a run
    with a store attached) rather than spinning a fresh engine: it rebuilds each
    instrument's net position from the stored fills (the PnL source of truth) and
    lists the stored orders that are still live (not terminal). This is the
    simplest genuinely-testable source — a file a previous run produced — and
    avoids re-running a strategy just to observe state.
    """
    from trading_bot.application.position_tracker import PositionTracker
    from trading_bot.domain.order import OrderStatus
    from trading_bot.storage.sqlite_store import SqliteStore

    if not db_path.exists():
        raise typer.BadParameter(f"database not found: {db_path}")

    store = SqliteStore(db_path)
    tracker = PositionTracker()
    for fill in store.fills():
        tracker.apply(fill)

    open_orders = [
        order
        for order in store.orders()
        if order.status
        not in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)
    ]

    _console.print(_render.positions_table(tracker.all_positions()))
    _console.print(_render.open_orders_table(open_orders))


# --- kpi ------------------------------------------------------------------- #


#: Built-in fallback starting capital for ``kpi`` when neither ``--capital`` nor
#: a config's ``starting_capital`` is available. Mirrors
#: :attr:`AppConfig.starting_capital`'s own default.
_KPI_DEFAULT_CAPITAL = 100_000.0


@app.command()
def kpi(
    db_path: pathlib.Path = typer.Option(
        ...,
        "--db",
        help="SqliteStore database path to compute KPIs from.",
    ),
    capital: float | None = typer.Option(
        None,
        "--capital",
        help="Starting account capital (quote units) anchoring the equity "
        "curve the KPI ratios are computed over. Overrides the config's "
        "starting_capital; defaults to 100000 when neither is given.",
    ),
    config_path: pathlib.Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="YAML AppConfig path. When given, its starting_capital anchors the "
        "equity curve unless --capital overrides it.",
    ),
) -> None:
    """Show realised PnL / fees / equity / KPI ratios from a stored fill history.

    Rebuilds a :class:`~trading_bot.application.performance_service.
    PerformanceService` from the fills persisted in a :class:`SqliteStore` (the
    fills are the PnL source of truth) and renders the KPI table — realised PnL,
    fees and the equity endpoint as exact :class:`~decimal.Decimal`, the Sharpe /
    Sortino / max-drawdown / Calmar ratios as floats.

    The KPI *ratios* are estimators over the equity curve ``capital + cumulative
    realised PnL``; the anchor must be a realistic, strictly-positive account
    value (the ratio math needs the curve not to cross zero). The realised PnL /
    fees themselves are independent of the anchor.

    Capital precedence
    ------------------
    The starting capital is resolved as **explicit ``--capital`` > config
    ``starting_capital`` (when ``--config`` is given) > built-in default
    (``100000``)**. So ``--capital`` always wins; absent it, a loaded config's
    ``starting_capital`` is used; absent both, the built-in default applies.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    if not db_path.exists():
        raise typer.BadParameter(f"database not found: {db_path}")

    resolved_capital = _resolve_kpi_capital(capital, config_path)

    store = SqliteStore(db_path)
    perf = PerformanceService(v0=resolved_capital)
    for fill in store.fills():
        perf.apply(fill)

    _console.print(_render.kpi_table(perf))


def _resolve_kpi_capital(
    capital: float | None, config_path: pathlib.Path | None
) -> Money:
    """Resolve the KPI starting capital by the documented precedence.

    Explicit ``--capital`` wins; absent it, a loaded config's
    ``starting_capital`` is used (when ``--config`` was given); absent both, the
    built-in default (:data:`_KPI_DEFAULT_CAPITAL`). The float ``--capital`` is
    routed through ``str`` (``from_float`` semantics) so it never carries a
    binary rounding error into the equity anchor; the config value is already an
    exact :class:`~decimal.Decimal`.
    """
    if capital is not None:
        return money(str(Decimal(str(capital))))
    if config_path is not None:
        return AppConfig.from_yaml(config_path).starting_capital
    return money(str(Decimal(str(_KPI_DEFAULT_CAPITAL))))


# --- serve ----------------------------------------------------------------- #


def _build_serve_app(config: AppConfig) -> FastAPI:
    """Build the read-only FastAPI dashboard app over a freshly-wired engine.

    The wiring seam :func:`serve` calls so the command is testable **without**
    launching uvicorn: the test patches :func:`uvicorn.run` and asserts the app
    this helper returns is what ``serve`` hands it. Builds a paper-by-default
    engine via :func:`~trading_bot.application.service_factory.build_engine`
    (persisting to ``config.storage.db_path`` when set) and wraps it in
    :func:`~trading_bot.interfaces.api.create_app`.
    """
    from trading_bot.interfaces.api import create_app

    engine = build_engine(config, db_path=config.storage.db_path)
    return create_app(engine)


@app.command()
def serve(
    config_path: pathlib.Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="YAML AppConfig path. Defaults to a paper config (no strategies).",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Interface to bind. Defaults to loopback (local-only).",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        help="TCP port to listen on.",
    ),
) -> None:
    """Serve the read-only web dashboard (positions / orders / PnL) over HTTP.

    Builds a wired engine from ``--config`` (or a paper default), wraps it in the
    read-only FastAPI app (:func:`~trading_bot.interfaces.api.create_app`) and
    runs it under uvicorn. The dashboard is a **pure HTTP client** of the API and
    is **read-only** — it can observe the engine but never place an order.

    MVP scope
    ---------
    ``serve`` exposes a **freshly-built** engine: it shows whatever state that
    engine accumulates (e.g. the order/fill history persisted in the configured
    ``storage.db_path``, replayed into the tracker/performance views), not a
    separately-running live trading process. Attaching the dashboard to a
    long-running live system (one ``run`` driving strategies while ``serve`` views
    it) is future work; for now ``serve`` + a persisted store is the data path.
    """
    import uvicorn

    config = (
        AppConfig.from_yaml(config_path)
        if config_path is not None
        else AppConfig()
    )

    try:
        application = _build_serve_app(config)
    except Exception as exc:  # noqa: BLE001 - surface any build failure cleanly
        _console.print(f"[red]refusing to serve:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _console.print(
        f"[green]serving dashboard[/green] "
        f"(mode={config.mode}) on http://{host}:{port}"
    )
    uvicorn.run(application, host=host, port=port)


# --- start (daemon) -------------------------------------------------------- #


async def _run_daemon(
    config: AppConfig,
    *,
    interval: float,
    cron: str | None,
    serve: bool = False,
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_token: str | None = None,
    dccd_client: object | None = None,
) -> None:
    """Supervise the declared strategies and step them on a schedule until stopped.

    Builds a :class:`~trading_bot.application.supervisor.StrategySupervisor`, starts
    every unit (each in its configured mode — paper by default), then steps the
    running units on an **interval** (or **cron**) via an ``apscheduler``
    ``AsyncIOScheduler``. When ``serve`` is set, the control dashboard
    (:func:`~trading_bot.interfaces.api.create_control_app`) is served over the same
    supervisor on ``host:port`` (loopback by default — it can change what trades),
    and **uvicorn owns the signal** (Ctrl-C ends serve, then the daemon tears down);
    headless, the daemon installs its own ``SIGINT``/``SIGTERM`` handlers. Each step
    is idempotent over unchanged data, so a tick that finds nothing to do trades
    nothing.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from trading_bot.application.supervisor import StrategySupervisor

    supervisor = StrategySupervisor(config, dccd_client=dccd_client)  # type: ignore[arg-type]
    await supervisor.start_all()

    async def _tick() -> None:
        try:
            stepped = await supervisor.step_all()
            if stepped:
                _console.print(f"[dim]daemon tick: stepped {stepped} strategy(ies)[/dim]")
        except Exception as exc:  # noqa: BLE001 - never let a tick kill the daemon
            _console.print(f"[red]daemon tick error:[/red] {exc}")

    trigger = (
        CronTrigger.from_crontab(cron)
        if cron is not None
        else IntervalTrigger(seconds=interval)
    )
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_tick, trigger)
    scheduler.start()
    _console.print(
        f"[green]daemon started[/green] (mode={config.mode}): "
        f"{len(supervisor.names())} strateg(ies), "
        f"tick={cron or f'every {interval:g}s'}"
    )
    try:
        if serve:
            import uvicorn

            from trading_bot.interfaces.api import create_control_app

            if host not in ("127.0.0.1", "localhost", "::1") and not auth_token:
                _console.print(
                    "[red]refusing to bind a non-loopback control dashboard with no "
                    "auth token[/red] — set --serve-token / TRADING_BOT_UI_TOKEN, or "
                    "bind 127.0.0.1 and tunnel (the control plane can trade)."
                )
                raise typer.Exit(code=1)
            api = create_control_app(supervisor, auth_token=auth_token)
            if auth_token:
                _console.print("[dim]control dashboard auth: token login enabled[/dim]")
            server = uvicorn.Server(
                uvicorn.Config(api, host=host, port=port, log_level="warning")
            )
            _console.print(
                f"[green]control dashboard[/green] on http://{host}:{port}"
                "  —  Ctrl-C to stop"
            )
            await server.serve()  # uvicorn owns SIGINT; blocks until Ctrl-C
        else:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, stop.set)
            _console.print("[dim]Ctrl-C / SIGTERM to stop[/dim]")
            await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await supervisor.shutdown()
        _console.print("[green]daemon stopped[/green] (all strategies shut down)")


@app.command()
def start(
    config_path: pathlib.Path | None = typer.Option(
        None, "--config", "-c", help="YAML AppConfig path. Defaults to a paper config."
    ),
    interval: float = typer.Option(
        60.0,
        "--interval",
        help="Seconds between re-evaluations (idempotent ticks). Ignored if --cron.",
    ),
    cron: str | None = typer.Option(
        None,
        "--cron",
        help="Crontab expression for re-evaluation (e.g. '5 0 * * *' = 00:05 daily).",
    ),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Also serve the control dashboard (start/stop strategies, switch mode) "
        "over HTTP — loopback by default, since it can change what trades.",
    ),
    serve_host: str = typer.Option(
        "127.0.0.1", "--serve-host", help="Control dashboard bind interface (loopback)."
    ),
    serve_port: int = typer.Option(
        8000, "--serve-port", help="Control dashboard TCP port."
    ),
    serve_token: str | None = typer.Option(
        None,
        "--serve-token",
        envvar="TRADING_BOT_UI_TOKEN",
        help="Require this token to log in to the control dashboard (enables auth). "
        "Mandatory to bind a non-loopback --serve-host. Reads TRADING_BOT_UI_TOKEN.",
    ),
) -> None:
    """Run the trading **daemon**: supervise the declared strategies, step on a schedule.

    The long-running process (systemd's ``ExecStart``): it builds a per-strategy
    supervisor, starts every declared strategy (in its configured mode — **paper by
    default**), and re-evaluates them on an interval or cron until stopped. Each
    strategy runs in its **own** engine, so they can be switched between paper /
    testnet / live independently from the **control dashboard** (``--serve``). Going
    live still requires the explicit gates — the daemon never trades real money by
    merely starting, and the dashboard requires a typed confirmation to go live.
    """
    config = (
        AppConfig.from_yaml(config_path)
        if config_path is not None
        else AppConfig()
    )
    try:
        asyncio.run(
            _run_daemon(
                config,
                interval=interval,
                cron=cron,
                serve=serve,
                host=serve_host,
                port=serve_port,
                auth_token=serve_token,
            )
        )
    except Exception as exc:  # noqa: BLE001 - surface any build/config failure cleanly
        _console.print(f"[red]refusing to start daemon:[/red] {exc}")
        raise typer.Exit(code=1) from exc


# --- dashboard (unified UI) ------------------------------------------------ #


#: The default manifest the dashboard reads/rewrites when no ``--config`` is given
#: — one persistent control plane common to every strategy it declares. Under the
#: gitignored ``configs/`` tree (deployment content, LOCAL-only).
_DEFAULT_MANIFEST = pathlib.Path("configs/dashboard.yaml")


def _load_or_create_manifest(path: pathlib.Path) -> AppConfig:
    """Load the manifest at ``path``, creating a fresh empty-paper one if absent.

    The dashboard is a **persistent control plane**: it reads a manifest on
    startup and rewrites it on every membership change. With no ``--config`` this
    is the default ``configs/dashboard.yaml``; an explicit ``-c`` names its own
    file. A missing manifest is created as a fresh empty-paper
    :class:`~trading_bot.application.config.AppConfig` (written to ``path``), so a
    first launch has a file to persist deployments into.
    """
    if path.exists():
        return AppConfig.from_yaml(path)
    config = AppConfig()  # paper by default, no strategies
    config.to_yaml(path)
    return config


async def _start_dashboard_units(supervisor: object) -> None:
    """Start every declared unit before the dashboard serves — tolerant of failures.

    Wraps :meth:`~trading_bot.application.supervisor.StrategySupervisor.start_all`
    per unit so the declared strategies come up **restored** (a paper unit's
    persisted book replayed into its fresh engine) and immediately controllable.
    A unit that fails to start (e.g. a live/testnet unit lacking credentials, or a
    misconfigured feed) is logged as a warning and **skipped** — one bad unit never
    stops the dashboard from serving the rest.
    """
    from trading_bot.application.supervisor import StrategySupervisor

    assert isinstance(supervisor, StrategySupervisor)
    for name in supervisor.names():
        try:
            await supervisor.start(name)
        except Exception as exc:  # noqa: BLE001 - one bad unit must not crash serve
            _console.print(
                f"[yellow]skipping strategy {name!r}[/yellow] "
                f"(failed to start: {exc})"
            )


@app.command()
def dashboard(
    config_path: pathlib.Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="YAML AppConfig path. Defaults to a paper config (no strategies).",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Interface to bind. Defaults to loopback (local-only).",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        help="TCP port to listen on.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        envvar="TRADING_BOT_UI_TOKEN",
        help="Require this token to log in to the dashboard (enables auth). "
        "Mandatory to bind a non-loopback --host. Reads TRADING_BOT_UI_TOKEN.",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Advertise a read-only stance (later leaves hide/disable controls).",
    ),
) -> None:
    """Serve the **unified dashboard** (Overview / Strategies / Orders / PnL / Logs).

    Builds a :class:`~trading_bot.application.supervisor.StrategySupervisor` from
    ``--config`` (or a paper default), **starts every declared strategy** (so each
    comes online restored — a paper unit's persisted book replayed into its engine
    — and immediately controllable) and serves the single-shell dashboard
    (:func:`~trading_bot.interfaces.api.create_dashboard_app`) over uvicorn. A unit
    that fails to start (e.g. a live unit lacking credentials) is logged and
    skipped — the others still serve.

    Binds **loopback** by default. Binding a non-loopback ``--host`` requires a
    ``--token`` (``TRADING_BOT_UI_TOKEN``) — the same guard as ``start`` — since
    the dashboard is the control surface; otherwise the command refuses.

    Clean shutdown
    --------------
    ``uvicorn.run`` **owns SIGINT**: Ctrl-C makes it return promptly, and the
    ``finally`` then shuts the supervisor down. There is no scheduler here (the
    daemon's ``start`` steps strategies on a tick; the dashboard just serves the
    restored + controllable units), so a plain ``uvicorn.run`` inside ``try/finally``
    is the whole loop — we deliberately do **not** also register a competing
    ``loop.add_signal_handler(SIGINT, …)`` (that override is what makes
    ``start --serve`` feel unquittable).
    """
    import uvicorn

    from trading_bot.application.supervisor import StrategySupervisor
    from trading_bot.interfaces.api import create_dashboard_app

    # The manifest the dashboard reads on startup and rewrites on every change:
    # an explicit --config, or the default configs/dashboard.yaml (created fresh
    # empty-paper if absent) so one dashboard is common to all strategies it
    # declares and persists across restarts.
    manifest_path = config_path if config_path is not None else _DEFAULT_MANIFEST
    config = _load_or_create_manifest(manifest_path)

    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        _console.print(
            "[red]refusing to bind a non-loopback dashboard with no auth "
            "token[/red] — set --token / TRADING_BOT_UI_TOKEN, or bind 127.0.0.1 "
            "and tunnel (the dashboard is the control surface)."
        )
        raise typer.Exit(code=1)

    try:
        supervisor = StrategySupervisor(config)
    except Exception as exc:  # noqa: BLE001 - surface any build failure cleanly
        _console.print(f"[red]refusing to serve dashboard:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    # Bring the declared strategies up before serving so they come online
    # **restored** (a paper unit's persisted book replayed into its engine — see
    # StrategySupervisor.start) and immediately controllable. start_all() is async;
    # the restored state lives in the in-memory engines, which persist across this
    # asyncio.run and the later uvicorn.run. A unit that fails to start (e.g. a live
    # unit lacking credentials) is logged and skipped — one bad unit never crashes
    # the dashboard; the others still serve.
    asyncio.run(_start_dashboard_units(supervisor))

    # Persist the manifest back to its path after any membership change (the
    # dashboard owns the manifest). `manifest()` reconstructs the AppConfig from
    # the live units; `to_yaml` round-trips it (money as exact Decimal strings).
    def _persist_manifest() -> None:
        supervisor.manifest().to_yaml(manifest_path)

    application = create_dashboard_app(
        supervisor,
        auth_token=token,
        read_only=read_only,
        on_change=None if read_only else _persist_manifest,
    )
    if token:
        _console.print("[dim]dashboard auth: token login enabled[/dim]")
    _console.print(
        f"[green]serving dashboard[/green] (mode={config.mode}"
        f"{', read-only' if read_only else ''}) on http://{host}:{port}"
        "  —  Ctrl-C to stop"
    )
    try:
        # uvicorn owns SIGINT: Ctrl-C returns from run() cleanly the first time.
        uvicorn.run(application, host=host, port=port)
    finally:
        # Tear the supervisor down whether serve returned normally or on Ctrl-C.
        asyncio.run(supervisor.shutdown())
        _console.print("[green]dashboard stopped[/green]")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
