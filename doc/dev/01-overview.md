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

- **Rewrite in progress.** The pre-2026 implementation (a `multiprocessing`
  server/clients design over an authenticated socket, REST Kraken/Bitfinex, a
  `blessed` CLI) is **parked under `trading_bot/legacy/`** — kept as reference and
  spec, excluded from all tooling.
- The new hexagonal, async-first layers are being built from scratch, harmonised
  with dccd. See [`02-architecture.md`](02-architecture.md) for the target and
  [`07-roadmap.md`](07-roadmap.md) for what is being built next.
- **Decided direction:** full rewrite (not incremental); execution **and**
  orchestration; multi-exchange-ready with **Kraken first**; **paper-first**;
  name kept as `trading_bot` for now (final name deferred). See
  [`03-decisions.md`](03-decisions.md).

## Repo map

```
trading_bot/         # the package
  legacy/            # pre-2026 implementation — reference only (excluded from tooling)
  tests/             # new test suite (smoke for now)
  __init__.py        # exposes __version__
doc/dev/             # this developer brief + plan trees
strategies/          # example strategy folders (legacy shape — will evolve)
data_base/           # example data fixtures (legacy)
execution_scripts/   # legacy shell launchers
CLAUDE.md            # authoritative working rules
pyproject.toml       # packaging + tooling config
```

## Legacy concepts worth preserving (as spec, not code)

The legacy tree encodes real domain knowledge to mine when rebuilding:

| Legacy | Becomes |
|--------|---------|
| `StrategyBot` (`legacy/strategy_manager.py`) | `application/StrategyRunner` |
| `_BasisOrder` / `OrderSL` / `OrderBestLimit` (`legacy/orders.py`) | `domain/order.py` + state machine |
| `OrdersManager` (funds check, call-counter) (`legacy/orders_manager.py`) | `application/OrderRouter` + `RiskManager` |
| `_PnLI` (`legacy/performance.py`) | `application/PerformanceService` (KPI via fynance) |
| `blessed` CLI (`legacy/cli.py`) | `interfaces/cli` (Typer) |
| `exchanges/API_kraken.py`, `API_bfx.py` | `brokers/kraken.py` (+ port, registry) |
