# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Claude-oriented developer brief**: [`doc/dev/`](doc/dev/) contains an
> orientation pack written specifically for Claude Code — overview, the target
> hexagonal architecture, design decisions & rationale, the per-broker capability
> matrix, testing methodology, current status, and the roadmap. Start at
> [`doc/dev/README.md`](doc/dev/README.md). `CLAUDE.md` remains authoritative for
> commands and invariants.

> **Status: rewrite in progress.** The pre-2026 implementation is parked under
> [`trading_bot/legacy/`](trading_bot/legacy/) (reference only — excluded from
> lint/type-check/tests). The new hexagonal layers are being built per
> [`doc/dev/07-roadmap.md`](doc/dev/07-roadmap.md). When this file describes a
> layer that does not exist yet, it is describing the **target** — check the
> roadmap/status for what has actually landed.

## The triptych

`trading_bot` is the **execution & orchestration** pillar of a three-repo trading
stack. It does not collect data or research signals itself — it *runs* them:

| Repo | Role | trading_bot consumes it for |
|------|------|------------------------------|
| **dccd** (`../Download_Crypto_Currencies_Data`) | market data — multi-exchange collect/store, async, Parquet | live prices (WS) + historical bars (Parquet) feeding strategies; can also *drive* dccd collection (orchestrator role) |
| **fynance** (`../Fynance`) | research — features, models, allocation, walk-forward backtest | the signal functions a live strategy evaluates; KPI/PnL math |
| **trading_bot** (this repo) | execution — strategy runner, order routing, positions, risk; **orchestrates** the other two | — |

dccd and fynance are installed **editable from their sibling repos**, not pinned
from PyPI for the integration code (see Dependencies).

## Commands

```bash
# Dev install (Python 3.11+)
pip install -e ".[dev]"

# Add the triptych deps (fynance from PyPI; dccd editable from its repo)
pip install -e ".[dev,triptych]"
pip install -e ../Download_Crypto_Currencies_Data   # dccd (not on PyPI)

# Run the full unit suite (legacy excluded; network E2E excluded by default)
pytest

# Run a single test file
pytest trading_bot/tests/test_smoke.py -v

# Real-broker / real-data end-to-end tests (hit live APIs; opt-in)
pytest -m network

# Lint
ruff check trading_bot/

# Type check (strict on domain/; mypy assumes python 3.12)
mypy trading_bot/
```

## Git Flow

**Branch model:**
```
master          ← stable releases only (tagged vX.Y.Z)
  └── develop   ← integration branch
        ├── feat/<topic>   new feature or rewrite axis
        ├── fix/<topic>    bug fix
        ├── chore/<topic>  tooling, CI, deps
        └── docs/<topic>   documentation only
```

**Rules — always follow these before committing or pushing:**
1. **Never commit directly to `master`.**
2. **Never commit directly to `develop`** — always use a feature branch + PR.
3. Branch off `develop`: `git checkout develop && git checkout -b feat/my-topic`
4. Open a PR into `develop` when done. `develop` → `master` only at release time.

**Commit style (Conventional Commits):** `feat:`, `fix:`, `chore:`, `docs:`.

Do not add `Co-Authored-By` trailers to commits — this is a personal repo (parity
with dccd/fynance).

**Before every commit:** run `pytest` and `ruff check trading_bot/`. Both must pass.

**One PR = one concern, small and disposable.** Even a large plan ships as
*several* small atomic PRs — never one fourre-tout branch. This is what makes
`/abandon-task` (kill a bad PR, keep the lesson) viable.

### Dev loop & docs of record

The iterative loop is tooled by user-level skills, with four tracked docs as the
sources of truth:

| Doc | Holds | Updated by |
|-----|-------|-----------|
| `doc/dev/07-roadmap.md` | open work — single source *index* | `/pick-task` reads · `/finish-task`, `/abandon-task` update |
| `doc/dev/plans/<epic>/` | open work *detail* — durable hierarchical plan trees | `/plan` writes · `/execute-leaf` reads · `/finish-task`/`/abandon-task` archive |
| `doc/dev/03-decisions.md` | the *why* — ADR journal | `/finish-task` (accepted), `/abandon-task` (rejected/tombstone) |
| `doc/dev/06-status.md` | where things stand | `/finish-task`, `/groom-docs` |

`CHANGELOG.md` + git log stay authoritative for *what* shipped. The loop:

`/pick-task` (smallest coherent slice; **no branch yet**) →
`/plan` (decompose into a `doc/dev/plans/<epic>/` tree — single leaf for a trivial
task, a global `00-plan.md` + leaves otherwise — and open the **plan PR** onto
`develop`) →
`/execute-leaf <epic> next` (cut the leaf branch, **spawn an agent at the model
derived from the leaf's `complexity`**, which implements + tests + **verifies on
real data**) →
`/finish-task` (tests, ADR, CHANGELOG, leaf PR, archive the leaf, tick the global
checklist) → … per leaf … → last leaf removes the roadmap line → `/release`.

The full plan-tree format lives in [`doc/dev/plans/README.md`](doc/dev/plans/README.md).

**Model per task** (advisory — set via `/model`, or a plan leaf's `complexity`
derives it: `low→haiku`, `medium→sonnet`, `high→opus`):

| Model | For |
|-------|-----|
| `opus` | judgement, design, decisions, planning, review |
| `sonnet` | implementation — code, tests, docstrings |
| `haiku` | mechanical fan-out (doc scans, checklists) — spawn explicitly as a subagent |

## Architecture (target — hexagonal, mirrors dccd)

The rewrite mirrors dccd's hexagonal layering under the same `trading_bot/`
package. Layers land incrementally (see roadmap); the legacy tree is replaced
module by module, never extended.

```
trading_bot/
  domain/        # pure, sync, zero I/O — Order(+state machine), Position, Fill,
                 # Signal, Instrument, Money(Decimal), PnL/KPI, errors
  transport/     # async — AsyncHTTPClient (httpx), WebSocketBase, RateLimiter, retry/backoff
  brokers/       # exchange/broker adapters (≈ dccd sources/): Broker port + registry;
                 # KrakenBroker (REST+WS) first; PaperBroker (simulation); others declared not-implemented
  storage/       # order/fill history + engine state (SQLite, append-only) — reconciliation source
  application/   # StrategyRunner, OrderRouter (idempotent submit + reconciliation),
                 # PositionTracker, PerformanceService, RiskManager (limits + kill-switch),
                 # Scheduler, EventBus, Config (pydantic), service_factory (single wiring point)
  interfaces/
    cli/         # Typer CLI (start/stop strategies, status, KPI table)
    api/ + ui/   # FastAPI + Jinja2 dashboard (later)
  legacy/        # pre-2026 implementation — reference only, excluded from tooling
  tests/
```

**Domain stays pure** (no I/O; never imports transport/brokers/storage).
**All money is `Decimal`** (prices, sizes, fees) — never float.
**Adding an exchange**: add the adapter under `brokers/`, register it in
`application/service_factory.py`. Multi-exchange is designed for from day one;
only **Kraken** is implemented at MVP (others declare their capabilities but raise
early if unimplemented).

## Invariants — do not regress (live trading; real money)

- **Paper-trading is the default.** Going live requires explicit opt-in +
  credentials + confirmation. `PaperBroker` and the live broker sit behind the
  same `Broker` port.
- **Order submission is idempotent**: every order carries a client-order-id so a
  retry never creates a duplicate order.
- **Reconcile, never assume**: on startup and after any disconnect, refetch open
  orders + balances + fills from the broker and reconcile local state.
- **Fills are the source of truth for PnL** — never double-count; never infer a
  fill that the broker did not confirm.
- **Risk limits + kill-switch** gate every order (max position, max order size,
  max daily loss). The kill-switch cancels open orders and halts new ones.
- **Rate-limit per exchange** (token-bucket; Kraken call-counter) — never exceed
  the venue's budget.
- **Secrets never logged, never committed** (`.env`, gitignored). Redact keys in
  any log line.

## Testing conventions

Tests live in `trading_bot/tests/`. The legacy tree is excluded from collection
(`--ignore=trading_bot/legacy`). Coverage is measured on every run
(`--cov=trading_bot`). CI matrix: Python 3.11–3.13.

**Test the chain on real data, not just the pieces.** A green unit suite is not
enough for an execution engine: for any order path, run the real operation
against the **PaperBroker** (and, opt-in, a real sandbox), read what the broker
reports back, and compare it to what was requested. Reconciliation and PnL must
be checked against broker-reported fills, not against local optimism. Full
methodology: [`doc/dev/05-testing.md`](doc/dev/05-testing.md).

## Dependencies

Core (Python 3.11+): `httpx`, `websockets`, `pydantic>=2`, `numpy`.
Triptych extra (`[triptych]`): `fynance` (PyPI); **dccd** installed editable from
`../Download_Crypto_Currencies_Data` (not on PyPI).
Daemon extra: `typer`, `uvicorn`, `fastapi`, `jinja2`, `pyyaml`, `apscheduler`.
Dev extra: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `interrogate`.
