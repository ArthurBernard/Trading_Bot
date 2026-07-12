---
plan: dashboard-tables-ux
kind: global
status: planning
roadmap: "5. [ ] **`dashboard-tables-ux` — tables redesign.** (UI consistency & accounting integrity, 2026-07-11)"
release_on_done: false
---

# dashboard-tables-ux — the tables finally say what the operator needs

## Goal

The 2026-07-11 audit's UX findings, decided with the maintainer that morning:
positions must read as *assets with a value*, orders must show what actually
executed, fills belong under their order, and bar timing must be honest.
Epic D (`api-completeness`, PRs #209–#213) put everything needed on the API —
this epic is pure UI consumption (templates + vanilla JS; no server change).

## Pinned UX decisions (maintainer, 2026-07-11 — implement, do not re-decide)

- **Expandable rows** are the detail pattern (NOT hover popins — can't copy,
  no touch, no side-by-side compare; NOT per-order pages — too heavy). Click
  a row → a detail panel opens beneath it. **Expanded state must survive the
  SSE re-render** (key by `client_order_id` / instrument; re-apply after
  each table rebuild).
- **Silence is the healthy/normal state** — no filler badges, no columns of
  nulls (a missing mark renders as `—` with the native fields intact).
- Tooltips keep their existing role (exact Decimals, absolute timestamps) —
  they carry metadata, never primary data.
- **Positions display by ASSET** (`base`), not pair: the primary label is
  `BTC`, the native pair (`BTC/USDT`) moves to the expanded detail. Rows are
  still one-per-instrument (never merge quotes — they are distinct
  positions).
- Every mark/value shown with its freshness: the `mark_asof_ts` renders as a
  relative "as of" (tooltip absolute) next to the price — a stale mark must
  never look live (`mark_source` in the tooltip).

## Table blueprints

**Positions** (overview + strategy detail): visible — Asset | Qty |
Avg entry | Price *(as-of)* | Value | Unrealised | Realised (net) [| Strategy
| Exchange on the overview]. Values prefer `value_display`/`display_currency`
when non-null, else native + quote. Expanded — native instrument, cumulative
fees, realised gross vs fees breakdown (realised + fees = gross), mark
source/asof absolute, fill count when cheaply available client-side.

**Orders** (strategy detail "Recent orders", Orders page, overview open
orders): visible — Time | Instrument | Side | Type | Qty | Filled % |
Avg fill | Value | Status. `Filled %` = filled_qty/qty (the store rows are
truthful since v0.12.0); `Value` = filled_qty × avg_fill_price when filled,
else qty × limit_price, else `—`. Expanded — that order's fills (Time, Qty,
Price, Fee — client-joined from `/api/fills` by `client_order_id`, already
in the payload), client/venue ids, limit/stop, Σ fees, reject_reason.

**Fills**: the standalone "Recent fills" table DISAPPEARS from the strategy
detail page (it becomes the orders' expanded content). The Orders page keeps
a flat fills view for audit, gaining Order (client_order_id, truncated with
full id in tooltip) and Value (qty × price) columns.

**Bar timing**: a "last bar X ago → next bar in Y" chip on the strategies
roster and the detail header — last = `last_asof_ts` (server truth), next =
`last_asof_ts + span × 1000` (the honest derivation; retire the epoch-aligned
`nextBarCloseMs` guess). Tooltip: absolute times + "data through".

**Timezone**: one affordance — the rendered-timezone label (e.g. "times in
Europe/Paris") shown once per page footer/header area, absolute UTC already
in tooltips. No toggle in this epic.

## Decomposition

1. `01-positions-tables` — positions blueprint on overview + detail
   (+ the shared expandable-row helper in base.html, built here, reused by
   02; expanded-state persistence mechanism included).
2. `02-orders-fills-tables` — orders blueprint on all three surfaces, fills
   demoted to expanded detail, Orders-page flat fills gains Order/Value.
3. `03-bar-timing-chip` — the last→next chip (roster + detail header),
   retire the epoch guess.
4. `04-timezone-and-empty-states` — the timezone label; empty states
   distinguish "no data" from "filtered to nothing" (Orders page filters).

## Leaf checklist

- [x] 01 positions-tables — feat/ui-positions-tables — medium (#215)
- [ ] 02 orders-fills-tables — feat/ui-orders-fills — high
- [ ] 03 bar-timing-chip — feat/ui-bar-timing — medium
- [ ] 04 timezone-and-empty-states — feat/ui-tz-empty-states — low

## Dependencies

Serial: 01 → 02 → 03 → 04 (01 builds the shared expandable-row helper 02
reuses; all touch the same templates/base.html).

## Done criteria

- On the real dashboard over real books: a position reads
  `BTC · qty · price (as of) · value · unrealised` at a glance; clicking an
  order shows its fills; the detail page has no standalone fills table; the
  roster shows honest last→next bar timing; expanded rows survive an SSE
  refresh.
- No server change anywhere in the epic.
- Roadmap line removed by the last leaf.
