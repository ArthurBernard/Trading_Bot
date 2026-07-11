---
plan: dashboard-tables-ux/01-positions-tables
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/ui-positions-tables
pr: ""
---

# 01 — positions read as assets with a value (+ the shared expandable row)

## Goal

Implement the positions blueprint (see `00-plan.md`, "Table blueprints" and
"Pinned UX decisions" — authoritative) on the overview and strategy-detail
positions tables, AND build the **shared expandable-row helper** in
`base.html` that leaf 02 will reuse.

The expandable-row helper (design pinned):
- a function that, given a rendered `<tr>`'s key and a detail-HTML builder,
  wires click-to-toggle: clicking the row inserts/removes a full-width
  detail `<tr>` beneath it (one `<td colspan=N>` with a definition-list-like
  layout consistent with the existing card styles);
- an expanded-keys `Set` per table, captured before each re-render and
  re-applied after (the tables are fully rebuilt on SSE/poll refresh — read
  the existing `refreshAll` flow first);
- keyboard accessible (the row is focusable, Enter toggles) and does not
  hijack clicks on links/buttons inside the row;
- ALL server strings escaped through the existing `escapeHtml` (audit
  lesson: violation sentences and ids carry quotes).

Positions tables (both surfaces):
- visible columns: Asset | Qty | Avg entry | Price *(as-of)* | Value |
  Unrealised | Realised (net) — plus Strategy/Exchange on the overview
  variant (match the current overview grouping behaviour);
- Price cell: the mark + a small relative "as of" (`mark_asof_ts`) beside
  it; tooltip = absolute time + `mark_source`; `—` when null;
- Value/Unrealised: prefer `value_display`+`display_currency` when non-null,
  else native + the row's `fee_ccy`/quote; `—` when null; keep the existing
  `tbFmt` money/qty cell helpers (exact value on hover);
- expanded detail: native instrument pair, cumulative fees (`fees_paid`),
  realised gross vs fees breakdown (gross = `realised_pnl` + `fees_paid`,
  computed client-side, labeled), mark source + absolute asof.

## Files to change

- `trading_bot/interfaces/ui/templates/base.html` — the expandable-row
  helper (+ minimal CSS for the detail row, reusing existing card/table
  styles; check what exists before adding).
- `trading_bot/interfaces/ui/templates/overview.html` — positions table
  columns + renderer + expansion wiring.
- `trading_bot/interfaces/ui/templates/strategy_detail.html` — same.
- `trading_bot/tests/interfaces/test_dashboard.py` — template-hook tests
  following the repo's established pattern (the health-pill tests from
  PR #201 are the model): the helper exists in base.html, both pages carry
  the new column hooks and the expansion wiring.

## Steps

1. READ the existing table renderers (`rowHtml`-style functions), `tbFmt`
   helpers, the SSE `refreshAll` flow and the health-pill precedent
   (PR #201) BEFORE writing anything; match their idioms exactly.
2. Helper first, then the two tables; keep every existing behaviour
   (grouping, empty states, links) unless the blueprint changes it.
3. All three gates green.

## Tests

Template-hook tests (see Files); the suite must stay green — the existing
dashboard tests assert current template hooks, adjust any that reference the
old positions columns deliberately (state each adjustment in the report).

## Verification on real data

The leaf-02/03-of-epic-D harness: real supervisor over scratch copies of
both live books + real dccd store (read-only), one tick, then GET the
rendered pages AND `/api/positions` via TestClient. Evidence: (a) extract
the actual row-renderer + helper JS from the templates (the PR #201 Node
technique) and execute them against the real API JSON — assert the produced
HTML shows asset-first labels, the as-of mark, display values, and that the
expanded detail contains the native pair and the fees breakdown; (b) exercise
the persistence Set logic on a simulated re-render. Report generated HTML
fragments for 2 real rows per book (one with `value_display`, one null-mark
case if present — else state none existed). Originals and port 8000
untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "positions tables read asset-first with
  price (as-of), value and unrealised; click a row for the native pair, fee
  breakdown and mark provenance — the shared expandable-row helper lands
  (#XX)."
- ADR: none expected (UI consumption of decided design; the expandable-row
  persistence mechanism is worth one line in the CHANGELOG only). State so.
- Tick leaf 01 in `00-plan.md`; archive per `/finish-task`.
