---
plan: triptych-orchestration/03-entrypoint
kind: leaf
status: done
complexity: high
depends: [01, 02]
parallel: false
branch: feat/triptych-entrypoint
pr: ""
---

# Triptych entrypoint — one config runs the whole system

## Goal

One entrypoint that turns a full `AppConfig` into a **running multi-strategy
system**: build the engine, load every declared strategy (signal + feed), wrap each
in a `StrategyRunner`, and run them all via the `Orchestrator`. Wire it into the CLI
so `trading-bot run <config.yaml>` brings up the whole declared (paper) system. The
last E8 leaf — closes the E8 roadmap line.

## Files to change

- `trading_bot/application/run_app.py` — new; `build_runners(config, engine) -> list[StrategyRunner]` + `async run_app(config) -> ...`.
- `trading_bot/application/__init__.py` — export them.
- `trading_bot/interfaces/cli/main.py` — `run` accepts a config path and runs the whole declared system (multi-strategy) via the entrypoint; keep the single-strategy/synthetic path for quick demos.
- `trading_bot/tests/application/test_run_app.py` — new.
- `trading_bot/tests/interfaces/test_cli_run_config.py` — new (CLI over a config).

## Steps

1. Read `service_factory.build_engine`, `application/strategy.py` (`load_strategy`),
   `application/data_provider.py` (`feed_for`, leaf 02), `application/strategy_runner.py`,
   `application/orchestrator.py` (`Orchestrator.add`/`run`), the extended `AppConfig`
   (leaf 01), and the CLI `run` (leaf E7-02).
2. `build_runners(config, engine, *, dccd_client=None) -> list[StrategyRunner]`:
   for each `StrategyConfig`: `strategy = load_strategy(strategy_cfg, strategy_cfg.signal.ref + params)`
   (resolve a builtin name like `ma_crossover` to the factory, or import a `module:function`);
   `feed = feed_for(strategy_cfg, client=dccd_client)`; build a `StrategyRunner(strategy,
   feed, engine.router, engine.tracker, event_bus=engine.bus)`. Return the list.
3. `async run_app(config, *, db_path=None, dccd_client=None, max_steps=None)`:
   `engine = build_engine(config, db_path=config.storage.db_path or db_path)`;
   `runners = build_runners(config, engine, dccd_client=...)`; `orch = Orchestrator(event_bus=engine.bus)`;
   `orch.add_all(runners)`; `await orch.run()`; return a summary (per-strategy orders,
   final positions, realised PnL from `engine.perf`). Paper-by-default (engine enforces it).
4. CLI `run`: when given a **config path**, load `AppConfig.from_yaml`, call `run_app`,
   and print a per-strategy summary + the positions/KPI table. Keep the existing
   synthetic/single-strategy quick path (no config) working. `--live` guard unchanged.
5. Offline-testable: pass a **fake dccd client** (or strategies using `InMemoryFeed`
   via a test seam) + `PaperBroker` so the whole entrypoint runs with no network.

## Tests (via `.venv`)

- `run_app` over a 2-strategy `AppConfig` (fake dccd client returning canned bars,
   paper) → both strategies run via the `Orchestrator`, positions update independently,
   the summary reports each strategy's orders/PnL.
- CLI `trading-bot run <config.yaml>` (a real example config + an injected/fake feed
   path) → exit 0, prints a multi-strategy summary.
- Backward-compat: `trading-bot run` with no config (synthetic) still works.
- `--live` config without ack/creds → refused, nothing placed.

## Verification on real data

In-process (fake dccd client + PaperBroker is the engine's real data). Run `run_app`
over a realistic 2-strategy config end-to-end and assert each strategy's position/PnL
match an independent computation from its fills; run the same via the CLI. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.run_app` + CLI — run a whole declared multi-strategy system from one config (the triptych entrypoint)."
- ADR: the single-entrypoint orchestration (config → engine → per-strategy runners → Orchestrator); note E8 fulfils the execution+orchestration scope.
- Status/roadmap: **remove the E8 line** from `07-roadmap.md`; mark E8 done in `06-status.md`.
