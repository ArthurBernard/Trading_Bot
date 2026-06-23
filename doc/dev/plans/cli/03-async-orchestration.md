---
plan: cli/03-async-orchestration
kind: leaf
status: done
complexity: high
depends: [01]
parallel: false
branch: feat/async-orchestration
pr: ""
---

# Async orchestration — concurrent strategy loops + graceful shutdown

## Goal

The async engine lifecycle that runs **one or more** `StrategyRunner` loops
concurrently with **graceful shutdown** (signal handling), replacing the legacy
multiprocessing server/clients (`legacy/bot_manager.py` + `legacy/_server.py`).
This is what `trading-bot run` drives for a live/continuous run.

## Files to change

- `trading_bot/application/orchestrator.py` — new; `Orchestrator` (or `run_engine`).
- `trading_bot/application/__init__.py` — export it.
- `trading_bot/tests/application/test_orchestrator.py` — new.

## Steps

1. Read `application/strategy_runner.py` (`StrategyRunner.run`), `service_factory`
   (leaf 01), `application/events.py`, and the legacy `bot_manager.py`/`_server.py`
   for the concept it replaces (multiple strategy clients under one manager).
2. `Orchestrator(engine)`:
   - `add(runner)` / accept a list of `StrategyRunner`s (one per strategy);
   - `async run()` — run all runners **concurrently** (`asyncio.gather` / task group),
     each over its own `DataFeed`; aggregate their completion;
   - `async shutdown()` / a `stop_event` — request all runners to stop and await a
     clean drain (no half-submitted state); install **SIGINT/SIGTERM** handlers (only
     when run as the process entrypoint) that trigger `shutdown` so Ctrl-C is graceful.
   - Surface a runner exception without silently killing the others (log + cancel
     siblings cleanly, or use a task group's structured semantics — document).
3. Keep signal handling optional/injectable so tests don't depend on real signals.

## Tests (via `.venv`)

- Two `StrategyRunner`s over two `InMemoryFeed`s run concurrently via the
  `Orchestrator` and both reach completion; their positions update independently.
- A `stop_event`/`shutdown()` ends a long/looping run promptly with no exception and
  no partial submission left mid-flight.
- A runner that raises does not leave siblings hung (documented behaviour: siblings
  cancelled/drained, error surfaced).
- Signal handling is exercised via the injected hook (no real SIGINT needed).

## Verification on real data

In-process. Orchestrate two paper strategies over realistic OHLC fixtures
concurrently; assert both produce the expected positions and that a `shutdown()`
mid-run stops them cleanly (final state consistent, fills all accounted). Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.Orchestrator` — concurrent strategy loops with graceful shutdown (replaces the legacy multiprocessing server)."
- ADR: the concurrency model (gather/taskgroup) + shutdown/signal handling + per-runner failure policy.
- Status/roadmap: deferred to leaf 04.
