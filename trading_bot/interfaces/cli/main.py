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
``--live`` requires *both* an explicit acknowledgement (``--yes-i-understand``,
or an interactive confirmation) *and* venue credentials; absent either, ``run``
**refuses with a non-zero exit and never places an order**. The factory
(:func:`~trading_bot.application.service_factory.build_engine`) enforces the same
on its side (it never silently falls back to paper for a credential-less live
venue), so a missing key cannot trade real money by accident.

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
import dataclasses
import math
import pathlib
from collections.abc import Callable
from decimal import Decimal

import polars as pl
import typer
from rich.console import Console

from trading_bot import __version__
from trading_bot.application.config import AppConfig, StrategyConfig
from trading_bot.application.data_feed import BARS_SCHEMA, InMemoryFeed
from trading_bot.application.performance_service import PerformanceService
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
        help="Trade LIVE (real money). Requires --yes-i-understand AND "
        "credentials; refuses otherwise.",
    ),
    yes_i_understand: bool = typer.Option(
        False,
        "--yes-i-understand",
        help="Explicit acknowledgement required to go --live.",
    ),
) -> None:
    """Run a strategy over a bars source and print a short summary.

    Builds the engine (paper-by-default), loads the MA-crossover example strategy
    for the config's symbol, replays the bars source (a ``--bars`` file or the
    built-in synthetic feed) through the
    :class:`~trading_bot.application.strategy_runner.StrategyRunner`, then prints
    orders submitted, the final net position and the realised PnL.

    **Going live is guarded.** ``--live`` demands both ``--yes-i-understand`` and
    venue credentials; missing either, the command refuses with a non-zero exit
    and **never places an order** (the broker is never even built down the live
    path until both checks pass).
    """
    config = (
        AppConfig.from_yaml(config_path)
        if config_path is not None
        else AppConfig()
    )

    mode = _resolve_mode(config, live=live, yes_i_understand=yes_i_understand)
    config = config.model_copy(update={"mode": mode})

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


def _resolve_mode(
    config: AppConfig, *, live: bool, yes_i_understand: bool
) -> str:
    """Resolve the effective run mode, guarding the live path.

    Paper unless ``--live`` is set. ``--live`` requires the explicit
    ``--yes-i-understand`` acknowledgement (or an interactive confirmation);
    without it the command refuses with a non-zero exit and never proceeds to
    build a broker. (Credential presence is enforced one layer down by
    :func:`~trading_bot.application.service_factory.build_engine`, which refuses a
    credential-less live venue — so the two gates together mean a live order is
    impossible without *both* an acknowledgement and a key.)
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
            "pass --yes-i-understand (no order was placed)."
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


@app.command()
def kpi(
    db_path: pathlib.Path = typer.Option(
        ...,
        "--db",
        help="SqliteStore database path to compute KPIs from.",
    ),
    capital: float = typer.Option(
        100_000.0,
        "--capital",
        help="Starting account capital (quote units) anchoring the equity "
        "curve the KPI ratios are computed over.",
    ),
) -> None:
    """Show realised PnL / fees / equity / KPI ratios from a stored fill history.

    Rebuilds a :class:`~trading_bot.application.performance_service.
    PerformanceService` from the fills persisted in a :class:`SqliteStore` (the
    fills are the PnL source of truth) and renders the KPI table — realised PnL,
    fees and the equity endpoint as exact :class:`~decimal.Decimal`, the Sharpe /
    Sortino / max-drawdown / Calmar ratios as floats.

    The KPI *ratios* are estimators over the equity curve ``capital + cumulative
    realised PnL``; ``--capital`` anchors it to a realistic, strictly-positive
    account value (the ratio math needs the curve not to cross zero). The
    realised PnL / fees themselves are independent of ``--capital``.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    if not db_path.exists():
        raise typer.BadParameter(f"database not found: {db_path}")

    store = SqliteStore(db_path)
    perf = PerformanceService(v0=money(str(Decimal(str(capital)))))
    for fill in store.fills():
        perf.apply(fill)

    _console.print(_render.kpi_table(perf))


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
