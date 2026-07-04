---
plan: ui-ux-overhaul/03-overview-groups
kind: leaf
status: done
complexity: low
depends: [02]
parallel: false
branch: feat/ui-overview-groups
pr: "https://github.com/ArthurBernard/Trading_Bot/pull/161"
---

# Leaf 03 — Overview: positions by strategy + summary strip

## Goal

The Overview answers "how is each strategy doing?" at a glance: positions
grouped **by strategy by default**, sticky view preferences, and a compact
summary strip on top.

## Files to change

- `trading_bot/interfaces/ui/templates/overview.html`
- `trading_bot/tests/interfaces/test_dashboard.py` (shell smoke only, if any)

## Steps

1. **"By strategy" grouping**: add a `data-group="strategy"` toggle button to
   `#pos-groups` (the server already accepts `?group_by=strategy`) and make it
   the **default** (`POS_GROUP = 'strategy'`, button initially `is-active`).
   When grouping by strategy, drop the now-redundant Strategy column from the
   rows (keep it for the other groupings/flat).
2. **Persisted preferences**: store `POS_GROUP` and `KPI_LEVEL` in
   `localStorage` (`tb.overview.posGroup` / `tb.overview.kpiLevel`); restore
   on load, tolerate absent/invalid values.
3. **Summary strip**: a row of chips/cards above the KPI card, refreshed with
   the rest:
   - running / total strategies (from `/api/strategies`);
   - open orders count (from the open-orders payload already fetched);
   - total realised PnL from `/api/kpi?level=total` — formatted via leaf 02,
     labelled with its `quote` when non-null, "mixed currencies" tooltip when
     null (**no client-side money arithmetic** — the server's exact Decimal
     total is the only source);
   - next tick countdown when `/api/health.next_tick_ts` is set (a compact
     `next check 42s`; full wiring/polish of cadence belongs to leaf 04 — here
     just render the value if present, `—` otherwise).
4. Empty states: "No open positions." / "No running strategy." copy retained;
   group headers keep their count badge.

## Tests

Existing dashboard shell tests must stay green; if a shell test asserts
Overview markup, extend it for the new toggle. No server change.

## Verification on real data

`python -m pytest` + TestClient: `GET /api/positions?group_by=strategy`
returns the grouped shape the new default consumes (bucketed `[{group, rows}]`
— already covered server-side; confirm the page consumes it, e.g. via the
template smoke test). Visual pass by the maintainer.

## Closeout

- CHANGELOG `[Unreleased]` → `### Changed`.
- Tick 00-plan checklist; frontmatter `status: done`, fill `pr:`.
