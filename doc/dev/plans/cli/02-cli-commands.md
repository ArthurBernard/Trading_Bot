---
plan: cli/02-cli-commands
kind: leaf
status: done
complexity: medium
depends: [01]
parallel: false
branch: feat/cli-commands
pr: ""
---

# CLI commands — run / status / kpi

## Goal

The user-facing Typer commands on top of the skeleton: `run` a strategy from a
config, show `status` (positions/open orders), and a `kpi` table (PnL/KPI via
**rich**). Replaces the legacy `blessed` CLI. Paper-by-default.

## Files to change

- `trading_bot/interfaces/cli/main.py` — add the commands.
- `trading_bot/interfaces/cli/_render.py` — new (optional); rich table helpers.
- `trading_bot/tests/interfaces/test_cli_commands.py` — new.
- (A tiny example config + example signal reference for `run`, if helpful — reuse the E5 `ma_crossover_signal`.)

## Steps

1. Read `interfaces/cli/main.py` + `service_factory.build_engine` (leaf 01),
   `application/strategy.py` (`load_strategy`), `application/data_feed.py`
   (`InMemoryFeed`/`DccdFeed`), `application/strategy_runner.py`,
   the legacy `legacy/cli.py` for the command set it offered (start/stop/status/KPI).
2. `run` command: load an `AppConfig` (from a YAML path), `build_engine`, load the
   strategy (`load_strategy(config, signal_ref)`), build a `DataFeed` (a `DccdFeed`
   for a real run, or an `InMemoryFeed` from a bars file/fixture for offline/backtest —
   support at least the offline path so it's testable), run the `StrategyRunner`,
   print a short summary (orders submitted, final position). **Paper by default**;
   `--live` must require explicit confirmation + credentials (refuse otherwise).
3. `status` command: build/attach to an engine and print current positions + open
   orders (rich table). For the MVP this can report a just-run in-process engine
   (a persisted-state read can come later).
4. `kpi` command: print a rich table of realised PnL / fees / equity / Sharpe /
   Sortino / max-drawdown / Calmar from a `PerformanceService` over a run (or a stored
   fill history via `SqliteStore`). Numbers formatted; money as Decimal strings.
5. Keep all rendering in helpers so commands stay thin and testable.

## Tests (via `.venv`, Typer `CliRunner`, offline)

- `trading-bot run` over an `InMemoryFeed` fixture (MA-crossover strategy, paper) →
  exits 0, prints a summary, and the position moved as expected.
- `--live` without confirmation/creds → exits non-zero with a clear refusal (no order placed).
- `status` prints positions/open orders (rich table) for a known engine state.
- `kpi` prints the KPI table with the expected realised PnL for a known fill sequence.

## Verification on real data

In-process (offline feed + PaperBroker is the engine's real data). Run
`trading-bot run` over a realistic OHLC fixture via `CliRunner`, then `kpi`, and
assert the reported PnL/position match an independent computation. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`trading-bot` CLI commands — `run`/`status`/`kpi` (rich tables)."
- ADR: the `--live` opt-in/confirmation flow, if non-trivial.
- Status/roadmap: deferred to leaf 04.
