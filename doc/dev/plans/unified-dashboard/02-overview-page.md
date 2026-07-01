---
plan: unified-dashboard/02-overview-kpi
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/dashboard-overview
pr: ""
---

# Unified dashboard — Overview + KPI at 3 levels, groupable positions/orders

## Goal

The Overview page: aggregate **positions**, **open orders** and **KPI** across the
supervisor's per-strategy engines, live in the leaf-01 shell (SSE + polling). KPI is
shown at **three levels — per strategy, per exchange, total** (realised PnL, fees,
and — per strategy — Sharpe/Sortino/Calmar/maxDD). Positions and open orders are
**groupable by crypto and/or exchange**. This restores everything the old read-only
`create_app` showed, sourced from the **supervisor** so one dashboard covers the
whole system, plus the multi-level KPI the maintainer asked for.

## Files to change

- `trading_bot/application/supervisor.py` — read accessors aggregating over the units'
  engines:
  - `positions()` → per (strategy, exchange, instrument) rows (net qty + venue tag).
  - `open_orders()` → across engines, tagged with strategy + exchange.
  - `kpi(level)` → realised PnL + fees at `level in {strategy, exchange, total}`; the
    ratio KPIs (`sharpe/sortino/calmar/max_drawdown`) per strategy from each engine's
    `PerformanceService` (aggregate ratios are handled in leaf 05 on the combined
    curve — here they are `null` at exchange/total).
  Pure reads (in-memory trackers/perf + stores already built); money exact `Decimal`.
- `trading_bot/interfaces/api/app.py` — in `create_dashboard_app`: `GET /api/positions`
  (with `?group_by=crypto|exchange|strategy`), `GET /api/orders` (open; same grouping),
  `GET /api/kpi?level=strategy|exchange|total`, and `GET /api/events` — a merged SSE
  stream fanning every unit's engine bus onto one feed (dedup by event id, close on
  disconnect).
- `trading_bot/interfaces/ui/templates/overview.html` — the real page: a KPI strip with
  a **level toggle** (strategy / exchange / total) showing PnL / fees / ratios; a
  positions table with a **group-by** control (crypto / exchange); an open-orders
  table. Inline `{% block scripts %}` fetches the endpoints, paints the DOM, subscribes
  to `/api/events` (polling fallback). Reuse `base.html` helpers (`api()`, `fmtMoney`,
  `connect()`).
- `trading_bot/tests/interfaces/test_dashboard.py` — extend.
- `trading_bot/tests/application/test_supervisor.py` — extend for the accessors.

## Steps

1. Read the old `create_app` `/api/positions|orders|kpi` + `/api/events`, and
   `PositionTracker.all_positions()`, `PerformanceService` (realised_pnl / fees_paid /
   sharpe / sortino / calmar / max_drawdown), the `EventBus`, and the supervisor's
   `_Unit`/engine + `_exchange_of`.
2. Add the supervisor accessors. For `kpi(level)`: `strategy` = one row per unit;
   `exchange` = fold units sharing a venue (sum PnL/fees); `total` = fold all. Keep the
   per-strategy ratios; leave aggregate ratios `null` (leaf 05 fills them on the
   combined curve). Positions/orders carry `strategy` + `exchange` tags so the API can
   group.
3. Wire the read endpoints (+ `group_by` / `level` query params) and the merged SSE
   (subscribe each engine bus; multiplex; dedup; close cleanly).
4. Build `overview.html`: KPI strip with the level toggle; positions table with the
   group-by control; open-orders table; live via SSE, polling fallback.
5. `pytest` + `ruff` + `mypy` green under `trading_bot_env`.

## Tests

- Supervisor: two seeded paper units on different venues → `kpi("strategy")` has both
  rows; `kpi("exchange")` folds per venue; `kpi("total")` sums all; `positions()` /
  `open_orders()` carry strategy + exchange tags; empty supervisor → empty.
- Dashboard: `/api/kpi?level=` returns each level; `/api/positions?group_by=` groups;
  Overview page contains the KPI strip + tables; `/api/events` yields an event on a fill.

## Verification on real data

Run `trading-bot dashboard -c strategies/alloc1/binance.yaml`, drive a paper rebalance
through the supervised alloc1 unit (14 legs on the book). Confirm the Overview (or
`curl /api/positions?group_by=exchange` + `/api/kpi?level=total`) shows the **14 alloc1
positions** and the realised PnL/fees **matching what the engine reports** — the
dashboard reflects broker/engine truth, not local optimism. Grouping by exchange puts
them under `binance`. Paper only; **no real order**.

## Closeout

- CHANGELOG (Added): "Dashboard Overview — positions / open orders / KPI aggregated
  across the supervisor's engines, **KPI at per-strategy / per-exchange / total**,
  positions & orders groupable by crypto/exchange, live via merged SSE."
- ADR if the aggregation model (per-strategy ownership, exchange folding) is a
  non-trivial choice.
- Do NOT remove the roadmap line (deferred to leaf 06).
