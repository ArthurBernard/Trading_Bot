---
plan: dashboard-tables-ux/04-timezone-and-empty-states
kind: leaf
status: planned
complexity: low
depends: [03]
parallel: false
branch: feat/ui-tz-empty-states
pr: ""
---

# 04 — timezone label + honest empty states

## Goal

Two audit leftovers, closing the epic:

1. **Timezone affordance**: every timestamp renders in the browser's local
   timezone with no on-screen cue (audit Q6). Add ONE small label per page
   (footer or below the nav — match the existing layout's least intrusive
   slot): "times in <IANA zone>" using
   `Intl.DateTimeFormat().resolvedOptions().timeZone`, rendered once by a
   shared base.html helper. No toggle (pinned) — absolute values stay in
   tooltips as today.
2. **Empty states distinguish "no data" from "filtered out"** (audit Q6:
   the Orders page filters can silently produce 0 rows indistinguishable
   from an empty book): when a table renders 0 rows WITH active filters
   (crypto/exchange/strategy selects not at their "all" default), the empty
   message becomes "No orders match the filters." (mirror wording per
   table); with no active filter, keep the existing messages. Orders page
   (orders + fills tables) is the mandatory target; apply the same pattern
   to any other filtered table found (grep the templates for filter
   selects; state what was found).

## Files to change

- `trading_bot/interfaces/ui/templates/base.html` — the timezone label
  helper (+ its single CSS line if needed).
- `trading_bot/interfaces/ui/templates/orders.html` — filtered-empty-state
  logic (+ the label placement).
- Other templates: the label placement (overview, strategies,
  strategy_detail, logs — wherever the shared layout slot lands, ideally
  once in base.html's shell).
- `trading_bot/tests/interfaces/test_dashboard.py` — hook tests (tz-label
  helper present in the shell; orders.html carries the filtered-empty
  wording hook).

## Steps

1. READ the shared page shell in base.html (where one element lands on
   every page) and the Orders page's filter state handling.
2. Implement; all three gates green.

## Tests

Hook tests; suite green.

## Verification on real data

Same harness, TestClient on the rendered pages: the shell carries the tz
helper; execute the orders-page empty-state logic against the real payload
with a filter that matches nothing (e.g. crypto=ZZZ) → "No orders match the
filters."; with no filter and a non-empty book → the table renders rows.
Report the fragments. Originals and port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "timezone label on every page; filtered
  tables say 'no match' instead of masquerading as empty books (#XX)."
- `doc/dev/06-status.md`: epic shipped note (the dashboard now renders the
  full 2026-07-11 UX design).
- Last leaf: remove the `dashboard-tables-ux` roadmap line, set the global
  `00-plan.md` done, archive the tree per `/finish-task`.
