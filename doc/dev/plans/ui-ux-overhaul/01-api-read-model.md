---
plan: ui-ux-overhaul/01-api-read-model
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/ui-api-read-model
pr: ""
---

# Leaf 01 — API read-model enrichment (backend only, no visual change)

## Goal

Expose the data the UX leaves need and that the API does not carry today:

- **`span` + `quote` per strategy** on `/api/strategies` rows.
- **`quote` per KPI row** on `/api/kpi`.
- **`next_tick_ts` + `tick` on `/api/health`** — the daemon's APScheduler next
  run, wired through a new `schedule_info` hook (the `dashboard` command has no
  scheduler → both stay `null`).
- **`strategy` + `ts` tags on merged SSE frames** (`/api/events` of the
  unified dashboard) so the Logs page can attribute and timestamp events.

## Files to change

- `trading_bot/application/supervisor.py`
- `trading_bot/interfaces/api/app.py`
- `trading_bot/interfaces/cli/main.py` (`_run_daemon` only)
- `trading_bot/tests/interfaces/test_dashboard.py`

## Steps

1. **`StrategyStatus`** (supervisor.py, frozen dataclass): add fields
   `span: int` and `quote: str | None`. Populate in `status()` from each
   unit's config entry:
   - single-instrument `StrategyConfig` → `entry.data.span`, quote = the part
     after `/` of `entry.symbol`;
   - `PortfolioStrategyConfig` → `entry.data.span`, quote = the common quote
     across `entry.universe` pairs, or `None` when mixed.
   Factor a small helper (e.g. `_entry_quote(entry) -> str | None`) — reuse it
   for KPI rows. Read the entry shape from how the supervisor already builds
   units; do not guess field names.
2. **`KpiRow`**: add `quote: str | None`.
   - `level="strategy"` rows: the unit's quote (helper above).
   - `level="exchange"` / `"total"` rows: the single common quote across the
     folded units, else `None` (the UI renders "mixed"). PnL/fees stay exact
     Decimal sums exactly as today.
3. **`app.py`**: emit the new fields in `_status_dict` and `_kpi_row_dict`
   (span as int; quote as str or `null`).
4. **`create_dashboard_app(...)`**: new keyword
   `schedule_info: Callable[[], dict[str, Any]] | None = None`, stored on
   `app.state`. `/api/health` gains `"next_tick_ts"` (epoch **ms**, int) and
   `"tick"` (human trigger description, str) — both `null` when the hook is
   absent or returns nothing. The hook must never break health: wrap the call
   in try/except and fall back to nulls.
5. **`_run_daemon`** (cli/main.py): after `scheduler.add_job(...)`, keep the
   job handle and pass
   `schedule_info=lambda: {"next_tick_ts": <job.next_run_time → ms or None>,
   "tick": cron or f"every {interval:g}s"}` into `create_dashboard_app`.
   (APScheduler's `job.next_run_time` is a tz-aware datetime; convert with
   `int(dt.timestamp() * 1000)`.) The `dashboard` command changes not at all.
6. **Merged SSE** (`/api/events` in `create_dashboard_app`): the queue list is
   built per running unit — keep the unit name alongside each queue, rebuild
   the per-iteration `task → name` mapping, and tag every emitted frame with
   `"strategy": <unit name>` plus `"ts": <server epoch ms int>`. The
   single-engine `create_app` SSE also gains `"ts"` (no strategy tag — one
   engine). Dedup keys unchanged.

## Tests

Extend `trading_bot/tests/interfaces/test_dashboard.py` (follow its existing
fixture style — paper supervisor + TestClient):

- `/api/strategies` rows carry `span` (int, matches the config) and `quote`.
- `/api/kpi?level=strategy` rows carry `quote`; a mixed-quote aggregate row
  yields `quote: null` (build two units with different quote currencies).
- `/api/health` has `next_tick_ts: null` + `tick: null` by default; with a
  `schedule_info` hook passed, both surface (and a raising hook degrades to
  nulls, health still 200).
- An SSE frame from the merged stream carries `strategy` and an int `ts`
  (reuse the existing SSE test pattern if one exists; otherwise read one frame
  with the TestClient's streaming API).

## Verification on real data

Run the full suite (`python -m pytest`) — the dashboard tests drive a real
PaperBroker engine under the supervisor; assert the new JSON fields come from
the actually-wired config (not hardcoded). `ruff check trading_bot/` clean.

## Closeout

- CHANGELOG `[Unreleased]` → `### Added`: one line for the new API fields.
- ADR note in `doc/dev/03-decisions.md`: `schedule_info` hook (dashboard app
  stays scheduler-agnostic; the daemon injects) + SSE frames tagged
  server-side.
- Tick the leaf in `00-plan.md`; set frontmatter `status: done`, fill `pr:`.
