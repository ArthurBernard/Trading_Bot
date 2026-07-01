---
plan: unified-dashboard/05-pnl-data-model
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/dashboard-pnl-data
pr: ""
---

# Unified dashboard — PnL time-series data model (mode-tagged, live vs testnet)

## Goal

The data foundation for the PnL chart: persist enough to draw a **per-strategy
realised-PnL / equity curve over time**, with **live and testnet as separate series**
(testnet is fake money — never combined). The chosen model (decision in `00-plan.md`):
**tag each fill with the mode + venue** it executed under, then **derive** the curve by
folding a strategy's fills in timestamp order — `equity(t) = starting_capital + Σ
realised_pnl(fills ≤ t)` — split by mode. Plus the current **unrealised** (mark-to-
market) point from live prices. Continuous mark-to-market history is out of scope (v1).

## Files to change

- `trading_bot/storage/sqlite_store.py` — add a `mode` (and `venue`) column to the
  persisted **fills** (append-only), with a lightweight migration (ALTER TABLE / add
  column; existing rows default to `"paper"` / their venue). A fill write records the
  mode+venue active for that unit's engine.
- `trading_bot/application/performance_service.py` **or** a new
  `application/pnl_series.py` — a pure helper `equity_series(fills, *, v0)` →
  ordered `[(ts_ms, realised_pnl, equity)]`, and a `by_mode` split producing one series
  per mode. Keep exact `Decimal`.
- `trading_bot/application/supervisor.py` — `pnl_series(name)` → `{mode: [(ts, pnl,
  equity)]}` for a strategy (folding its store's mode-tagged fills), plus the current
  unrealised end point (from the tracker's open positions × latest prices, when
  available). A `combined` helper for aggregate ratio KPIs (used by leaf 06).
- `trading_bot/interfaces/api/app.py` — `GET /api/pnl?strategy=<name>&mode=<live|testnet|paper|all>`
  returning the series (one array per mode) + metadata (v0, current equity, unrealised).
- `trading_bot/tests/storage/test_sqlite_store.py`, `.../application/test_supervisor.py`,
  a new `.../application/test_pnl_series.py` — cover the schema/migration, the fold, the
  per-mode split, and the endpoint.

## Steps

1. Read `SqliteStore` fill persistence (schema, append, read-back) and
   `PerformanceService` (realised PnL fold, `equity_curve`). Read how a unit's engine
   knows its mode/venue (supervisor `_Unit` + `_config_for_mode`).
2. Add the `mode`/`venue` fill columns + migration; thread the unit's mode+venue into
   the fill write path (the store write, or the FillEvent carries it). Default existing
   rows to `paper`.
3. Write the pure `equity_series` fold + `by_mode` split; add `supervisor.pnl_series`
   (per-mode arrays + current unrealised point).
4. Add `GET /api/pnl` (per strategy, filterable by mode). Money exact; timestamps ms.
5. `pytest` + `ruff` + `mypy` green under `trading_bot_env`.

## Tests

- Store: fills persist with `mode`/`venue`; migration adds the column to an old DB
  without data loss; read-back carries the tag.
- `equity_series`: a known fill sequence yields the expected ordered `(ts, pnl,
  equity)`; `by_mode` splits a mixed-mode sequence into separate series that each start
  from `v0`.
- `/api/pnl?strategy=…`: returns one array per mode; `&mode=live` filters; empty
  history → empty series (not an error).

## Verification on real data

Drive a paper alloc1 rebalance (fills land in `./var/alloc1.sqlite`), then switch the
unit's mode and drive another tick (paper→testnet is free) so fills exist under **two**
modes. `curl "http://127.0.0.1:8000/api/pnl?strategy=alloc1"` returns **separate
series per mode**, and each series' final `equity` equals `starting_capital +` the
engine's reported realised PnL for that mode — i.e. the curve matches broker/engine
truth. Paper/testnet only; **no real order**.

## Closeout

- CHANGELOG (Added): "PnL time-series — fills tagged by mode+venue; a per-strategy,
  per-mode realised-PnL/equity curve (`GET /api/pnl`), live vs testnet separate."
- ADR: record the **derive-from-mode-tagged-fills** model (vs periodic equity
  snapshots vs full continuous mark-to-market) and why testnet/live stay separate.
- Do NOT remove the roadmap line (deferred to leaf 07).
