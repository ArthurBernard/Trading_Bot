# 01 — Overview

## What trading_bot is

The **execution & orchestration** pillar of a three-repo trading stack. It runs
trading strategies live: evaluates signals on incoming market data, turns target
positions into orders, routes and manages those orders on exchanges, and tracks
positions, PnL and risk. As the *orchestrator* of the triptych, it also wires the
other two repos together into one end-to-end app.

## North star

> A trader defines a strategy (a fynance-backed signal + a config). trading_bot
> feeds it live and historical data from dccd, computes target positions, and
> safely executes the resulting orders across one or more exchanges — paper or
> live — while continuously reconciling state with the venue and enforcing risk
> limits. One config, one entrypoint, the three repos behind it.

This is the direction, not a promise of scope at any given moment. The roadmap
deepens it epic by epic.

## Place in the triptych

| Repo | Role | Consumed here for |
|------|------|-------------------|
| **dccd** | market data (collect/store, async, Parquet) | live WS prices + historical bars feeding strategies; orchestrated collection |
| **fynance** | research (features, models, allocation, backtest) | the signal a live strategy evaluates; KPI/PnL math |
| **trading_bot** | execution & orchestration | — |

## Current state (2026-06)

- **Rewrite complete through the MVP.** The new hexagonal, async-first layers
  exist natively — domain, transport, brokers (Kraken + paper), storage,
  application (router/risk/tracker/perf/reconcile/strategy/datafeed/runner/
  orchestrator) and a Typer CLI. The pre-2026 implementation (a
  `multiprocessing` server/clients design over an authenticated socket, REST
  Kraken/Bitfinex, a `blessed` CLI) has been retired; it lives in git history
  only (no in-tree legacy package).
- See [`02-architecture.md`](02-architecture.md) for the design and
  [`07-roadmap.md`](07-roadmap.md) for what comes next.
- **Decided direction:** full rewrite (not incremental); execution **and**
  orchestration; multi-exchange-ready with **Kraken first**; **paper-first**;
  name **decided** — kept as `trading_bot` (with `dccd` / `fynance`); no rename. See
  [`03-decisions.md`](03-decisions.md).

## Repo map

```
trading_bot/         # the package
  domain/            # pure core — orders, positions, fills, money, KPI
  transport/         # async HTTP/WS, rate-limit, retry
  brokers/           # Kraken + paper broker behind the Broker port
  storage/           # append-only order/fill history + engine state
  application/       # runner, router, risk, tracker, perf, reconcile, orchestrator
  interfaces/        # Typer CLI
  tests/             # the test suite
  __init__.py        # exposes __version__
doc/dev/             # this developer brief + plan trees
strategies/          # example strategy folders (pre-2026 shape — will evolve)
data_base/           # example data fixtures (pre-2026)
execution_scripts/   # pre-2026 shell launchers
CLAUDE.md            # authoritative working rules
pyproject.toml       # packaging + tooling config
```

## History

The pre-2026 implementation has been fully superseded and removed from the tree
(it remains in git history). The mapping from the old design onto the rewrite:

| Pre-2026 | Became |
|----------|--------|
| `StrategyBot` (`strategy_manager.py`) | `application/StrategyRunner` |
| `_BasisOrder` / `OrderSL` / `OrderBestLimit` (`orders.py`) | `domain/order.py` + state machine |
| `OrdersManager` (funds check, call-counter) (`orders_manager.py`) | `application/OrderRouter` + `RiskManager` |
| `_PnLI` (`performance.py`) | `application/PerformanceService` (KPI via fynance) |
| `blessed` CLI (`cli.py`) | `interfaces/cli` (Typer) |
| `exchanges/API_kraken.py`, `API_bfx.py` | `brokers/kraken.py` (+ port, registry) |
