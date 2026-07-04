---
plan: ui-ux-overhaul/05-tables-polish
kind: leaf
status: planned
complexity: medium
depends: [04]
parallel: false
branch: feat/ui-tables-polish
pr: ""
---

# Leaf 05 — Tables & event-feed polish

## Goal

Kill the remaining rough edges: flickering full re-renders that wipe open
controls, unlabelled order statuses, silently-capped histories, and an event
feed with client-side timestamps and no attribution/filtering.

## Files to change

- `trading_bot/interfaces/ui/templates/strategies.html`
- `trading_bot/interfaces/ui/templates/overview.html`
- `trading_bot/interfaces/ui/templates/orders.html`
- `trading_bot/interfaces/ui/templates/logs.html`
- `trading_bot/interfaces/ui/templates/pnl.html`
- `trading_bot/interfaces/ui/templates/base.html` (shared badge CSS, helper)
- `trading_bot/tests/interfaces/test_dashboard.py` (smoke only)

## Steps

1. **No-flicker refresh** (strategies + overview + orders): cache the last
   rendered payload per table (`JSON.stringify` compare) and skip the DOM
   rebuild when unchanged. Additionally, never rebuild a table while
   `document.activeElement` sits inside it (an open mode `<select>`); defer to
   the next refresh. The Strategies page polls every 3 s — this is the page
   where rebuilds visibly eat interactions.
2. **Order-status badges** (orders page + overview open-orders): render
   `status` as a badge — working/new → muted; partially_filled → amber
   (`--warn`); filled → green (`--ok`); canceled → muted (dimmed);
   rejected/expired → red (`--err`). Map by lowercase status value with a
   plain-text fallback for unknown statuses. Shared CSS in base.html.
3. **History-cap caption**: the Orders page fetches `?limit=200` (default) —
   when a table comes back at exactly the cap, show a muted caption
   "showing the most recent 200 — older rows are not displayed".
4. **Fills time**: format with seconds (`YYYY-MM-DD HH:mm:ss`); add a relative
   form in `title` (e.g. "3m ago") or vice-versa — absolute visible, relative
   on hover.
5. **Logs page**:
   - stamp lines with the **event's** `ts` from leaf 01 (fallback: client
     time);
   - show the `strategy` tag as a muted chip on each line when present;
   - filter chips above the feed: All / Orders / Fills / Logs (+ a min-level
     select for log events: info / warning / error). Filters apply to both
     incoming and already-buffered lines (keep the raw event buffer, re-render
     on filter change); keep the 500-line cap.
6. **PnL page**: stats table gains a **Return** column
   (`realised_pnl / v0`, display-only, via `tbFmt.pct`) when `v0` is a
   non-zero parseable value (else `—`); y-axis tick values via the leaf-02
   money formatter; show `v0` (starting capital) as a muted note near the
   stats table.
7. **Legacy `dashboard.html`/`app.js`**: only if not already consistent after
   leaf 02 — no new features there.

## Tests

- Template smoke tests where markup is asserted (badges/caption present in the
  page source is enough — the pages are static shells).
- Full suite + ruff green; server untouched.

## Verification on real data

`python -m pytest`. TestClient: `/api/orders?history=true&limit=2` on a seeded
multi-fill paper engine returns exactly the cap → the caption logic has a real
payload shape to key on. Maintainer does the final visual pass.

## Closeout

- CHANGELOG `[Unreleased]` → `### Changed`.
- Last leaf: mark the epic done in `00-plan.md` (`status: done`), remove the
  roadmap line added by the plan PR, update `doc/dev/06-status.md` (dashboard
  UX paragraph), and archive the tree per `doc/dev/plans/README.md`.
