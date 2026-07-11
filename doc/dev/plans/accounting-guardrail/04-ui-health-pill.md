---
plan: accounting-guardrail/04-ui-health-pill
kind: leaf
status: planned
complexity: medium
depends: [03]
parallel: false
branch: feat/health-pill
pr: ""
---

# 04 — the health pill in the dashboard

## Goal

Make health visible where the operator looks: a pill on the Strategies roster
and on the strategy detail header, fed by the `health`/`health_detail` fields
leaf 03 put on `/api/strategies`. Refresh rides the existing SSE + 10 s poll
cycle — no new transport.

Display rules (pinned):
- `ok` → no pill (silence is the healthy state — don't add a green badge to
  every row);
- `warn` → amber pill "warn";
- `error` → red pill "error";
- hover tooltip (`title=`) lists `health_detail` lines joined by newlines —
  the exact sentences the checker produced, no client-side rewording;
- the pill sits next to the existing run/mode status cell on the roster and
  next to the mode badge in the detail header.

## Files to change

- `trading_bot/interfaces/ui/templates/strategies.html` — pill in the Status
  cell of the roster row renderer (read the existing cell-render helpers and
  match their style — `tbFmt`/cell conventions).
- `trading_bot/interfaces/ui/templates/strategy_detail.html` — pill in the
  header, same source (the page already fetches its own strategy row).
- `trading_bot/interfaces/ui/templates/base.html` or the shared CSS block —
  one `.pill-warn` / `.pill-error` style pair IF no equivalent exists (check
  the existing badge/pill styles first — the mode badge and run pill already
  have classes; reuse before inventing).
- API-level tests already cover the fields (leaf 03); add a template smoke
  test ONLY if the repo has an existing pattern for template assertions
  (check `trading_bot/tests/` for template/UI tests first — if none exist, do
  not invent a new test category; the real-data verification below is the
  evidence).

## Steps

1. Read the two templates' row-render functions and the existing badge CSS.
2. Implement the pill + tooltip in both pages; keep the JS vanilla and
   consistent with the surrounding code (no framework, no new deps).
3. All three gates green (the suite must stay green — templates are also
   covered indirectly by API/serve tests).

## Verification on real data

Same harness as leaf 03 (local uvicorn on a scratch port over scratch book
copies, read-only, never the live daemon):

1. Post-heal copy with legacy duplicate ids → roster shows the amber "warn"
   pill on the unit; hover tooltip lists the duplicate-id sentences; detail
   header shows the same.
2. Induce the deleted-fill drift → pill turns red "error" within one poll
   cycle (≤10 s) without a page reload.
3. Clean synthetic book (fresh scratch unit) → no pill at all.
4. Screenshot or curl+DOM-grep evidence for each state; report which.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "health pill on the Strategies roster and
  strategy detail header (amber warn / red error, tooltip lists the exact
  violations) (#XX)."
- ADR: none expected (display of an existing surface); state so explicitly.
- This is the last leaf: remove the `accounting-guardrail` roadmap line, set
  the global `00-plan.md` done, archive the tree per `/finish-task`.
