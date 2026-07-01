---
plan: unified-dashboard/07-orders-logs-cleanup
kind: leaf
status: planned
complexity: medium
depends: [01, 02, 03, 04, 05, 06]
parallel: false
branch: feat/dashboard-orders-logs
pr: ""
---

# Unified dashboard — Orders/Fills + Logs pages, retire the split

## Goal

Finish the dashboard: an **Orders/Fills** history page **filterable by crypto /
exchange / strategy**, a **Logs** (SSE activity) page, then **retire the old split** —
`serve` / `run --serve` / `start --serve` fold onto the single `dashboard` command
(kept as thin aliases or removed), and the deploy docs + go-live runbook are updated.
Last leaf — removes the roadmap line.

## Files to change

- `trading_bot/interfaces/api/app.py` — `GET /api/fills` (order/fill history from the
  supervisor's engines' stores; `?limit=`); ensure `/api/orders` covers historical +
  open. Delete `create_control_app` (and, if fully subsumed, the read-only
  `create_app`) once nothing references them — or keep `create_app` as a thin
  `create_dashboard_app(..., read_only=True)` wrapper. Keep `__init__.py` exports
  coherent.
- `trading_bot/interfaces/ui/templates/orders.html` — orders + fills tables (open +
  recent history) with **filter controls (crypto / exchange / strategy)** driving the
  `?crypto=&exchange=&strategy=` query params; polling.
- `trading_bot/interfaces/ui/templates/logs.html` — a live activity feed over
  `/api/events` (SSE), dccd `logs.html`-style, with a small run/event history.
- `trading_bot/interfaces/cli/main.py` — make `serve` an alias that runs `dashboard
  --read-only` (or deprecate with a notice); make `start --serve` delegate to the
  dashboard app (single code path); keep `start` (headless daemon) working. Update
  help text.
- `doc/dev/10-deploy.md`, `doc/dev/09-go-live.md` — replace `serve` / `start --serve`
  guidance with the single `dashboard` command (loopback default, `--token` for
  remote, SSH-tunnel, HTTPS reverse proxy). `deploy/trading-bot.service` `ExecStart`
  if it referenced the old command.
- `trading_bot/tests/interfaces/test_dashboard.py` — extend; remove/redirect the old
  `test_control_api.py` / `test_api.py` cases that referenced the deleted factories.

## Steps

1. Read the store's order/fill read API (`SqliteStore`) and how the old `/api/orders`
   was built. Read the CLI `serve` / `start` commands to alias cleanly.
2. Add `/api/fills` + finalise `/api/orders` (open + history) over the supervisor's
   engines' stores. Build `orders.html` (tables + poll) and `logs.html` (SSE feed).
3. Retire the split: point `serve` → `dashboard --read-only`; route `start --serve`
   through `create_dashboard_app`; delete `create_control_app` (and fold `create_app`
   into a `read_only=True` wrapper or delete) with the tests updated. Keep one code
   path.
4. Update `10-deploy.md` + `09-go-live.md` + the systemd unit to the `dashboard`
   command. Update CHANGELOG/README references to `serve`/`start --serve`.
5. `pytest` + `ruff` + `mypy` green under `trading_bot_env`.

## Tests

- `/api/fills` returns history (`limit` honoured); `/api/orders` covers open + recent.
- Orders and Logs pages render; Logs subscribes to `/api/events`.
- `serve` still launches a read-only dashboard (alias); `start --serve` serves the
  unified app; no dangling imports of the deleted factories (a grep-guard test or the
  suite passing after deletion).

## Verification on real data

`trading-bot dashboard -c strategies/alloc1/binance.yaml`: after a paper rebalance,
the **Orders/Fills** page shows the alloc1 legs' orders + fills matching the engine's
store; the **Logs** page streams events live as a tick runs. Then `trading-bot serve
-c …` still brings up a **read-only** view (no controls). Ctrl-C quits cleanly. Paper
only — **no real order**.

## Closeout

- CHANGELOG (Added): "Dashboard Orders/Fills + Logs pages"; (Changed): "the
  `serve` / `start --serve` split retired onto a single `dashboard` command";
  (Removed): "`create_control_app` (folded into the unified dashboard)".
- ADR: record retiring the two-app split for one dashboard app + command.
- **Last leaf**: remove the roadmap line for this epic; set global `00-plan.md`
  `status: done`; suggest `/release`.
