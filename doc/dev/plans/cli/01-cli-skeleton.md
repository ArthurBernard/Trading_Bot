---
plan: cli/01-cli-skeleton
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: feat/cli-skeleton
pr: ""
---

# CLI skeleton — service_factory + Typer app + console script

## Goal

The single **wiring point** (`application/service_factory.py`) that assembles the
whole engine from an `AppConfig`, plus a minimal **Typer** app under
`interfaces/cli/` and the `trading-bot` console script. No business logic here —
just construction + entrypoint.

## Files to change

- `trading_bot/application/service_factory.py` — new; `build_engine(config) -> Engine` (or a small wiring dataclass).
- `trading_bot/interfaces/__init__.py`, `trading_bot/interfaces/cli/__init__.py`, `trading_bot/interfaces/cli/main.py` — new; Typer `app` with a `version` command.
- `pyproject.toml` — add `[project.scripts] trading-bot = "trading_bot.interfaces.cli.main:app"`; add `typer`/`rich` to the `daemon` extra (and `dev` if needed for tests).
- `trading_bot/tests/interfaces/__init__.py`, `trading_bot/tests/interfaces/test_cli_skeleton.py` — new.
- `trading_bot/tests/application/test_service_factory.py` — new.

## Steps

1. Read `application/config.py` (`AppConfig`, `BrokerConfig`, `StrategyConfig`,
   `RiskConfig`), `application/{events,order_router,position_tracker,performance_service}.py`,
   `application/risk.py`, `application/strategy.py`, `storage/sqlite_store.py`,
   `brokers/{base,registry,paper,kraken}.py`.
2. `service_factory.build_engine(config, *, db_path=None)`:
   - one `EventBus`;
   - a broker from `config` via a registry: **`PaperBroker` by default** (paper mode);
     `KrakenBroker` only when `config.mode == "live"` (explicit opt-in) — in this leaf,
     constructing a live broker without creds should raise/refuse clearly (no live by
     accident);
   - `PositionTracker`, `PerformanceService`, `SqliteStore` (if `db_path`) all attached
     to the bus; a `RiskManager(config.risk, position_tracker=...)`; an
     `OrderRouter(broker, bus, risk_manager=...)`.
   - return a small `Engine`/wiring object exposing these (bus, broker, router, tracker,
     perf, risk, store) so the CLI/orchestration can use them. Keep it a dataclass.
3. `interfaces/cli/main.py`: a Typer `app` with `version` (prints `trading_bot.__version__`).
   Keep commands minimal — the real commands land in leaf 02.
4. Add the console script + deps to `pyproject.toml`.

## Tests (via `.venv`)

- `build_engine(AppConfig())` (default → paper) returns an `Engine` whose broker is a
  `PaperBroker`, with router/risk/tracker/perf wired to one bus; `db_path` set → a
  `SqliteStore` attached.
- `config.mode == "live"` without credentials → `build_engine` refuses clearly (no
  accidental live broker).
- Typer `CliRunner`: `trading-bot version` exits 0 and prints the version.
- The console-script entry import path resolves (`trading_bot.interfaces.cli.main:app`).

## Verification on real data

In-process. `build_engine` a paper engine, submit one order through its `router` and
assert a fill reaches its `tracker`/`perf` (the wiring is live end-to-end). Run the
CLI `version` via `CliRunner`. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`service_factory` wiring + Typer `trading-bot` CLI skeleton + console script."
- ADR: the single-wiring-point factory + paper-default broker selection (live needs opt-in).
- Status/roadmap: deferred to leaf 04.
