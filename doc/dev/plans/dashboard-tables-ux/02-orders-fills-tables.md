---
plan: dashboard-tables-ux/02-orders-fills-tables
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/ui-orders-fills
pr: ""
---

# 02 — orders show what executed; fills live under their order

## Goal

Implement the orders blueprint (see `00-plan.md` — authoritative) on all
three order surfaces, demote the strategy-detail fills table into the
orders' expanded detail, and enrich the Orders page's flat fills view.
Reuses leaf 01's expandable-row helper.

Orders tables (strategy detail "Recent orders", Orders page, overview
open-orders):
- visible: Time | Instrument | Side | Type | Qty | Filled % | Avg fill |
  Value | Status (overview keeps its Strategy/Exchange columns);
- `Filled %`: `filled_qty / qty` rendered as a percentage (exact fraction in
  the tooltip); the store rows are truthful since v0.12.0;
- `Value`: `filled_qty × avg_fill_price` when any fill, else
  `qty × limit_price` when priced, else `—`; label which via tooltip;
- expanded detail (the helper): **that order's fills** — Time | Qty | Price
  | Fee rows client-joined from the fills payload by `client_order_id`
  (both `/api/fills` row fields are already served; the detail page already
  fetches fills — reuse that fetch, do NOT add a new endpoint or request
  per expansion), plus client/venue order ids, limit/stop prices, Σ fees,
  `reject_reason` when set.

Fills:
- strategy detail: REMOVE the standalone "Recent fills" table (its data now
  lives in the expansions). Keep the underlying fills fetch (it feeds the
  join).
- Orders page: keep the flat fills table (audit view), ADD Order
  (client_order_id, truncated ~12 chars with the full id in the tooltip)
  and Value (`qty × price`) columns.

## Files to change

- `trading_bot/interfaces/ui/templates/strategy_detail.html` — orders
  renderer + expansion + fills-table removal.
- `trading_bot/interfaces/ui/templates/orders.html` — orders renderer +
  expansion; fills renderer Order/Value columns.
- `trading_bot/interfaces/ui/templates/overview.html` — open-orders columns
  (expansion optional here — include it if the shared helper makes it free,
  else state why not).
- `trading_bot/tests/interfaces/test_dashboard.py` — template-hook tests
  (new columns present, expansion wiring present, detail-page standalone
  fills table GONE, orders-page fills has Order/Value); adjust existing
  hook tests that reference removed markup (state each).

## Steps

1. READ leaf 01's helper as merged, the three current order renderers and
   the fills renderers; map every existing behaviour (history vs open,
   200-cap caption, filters) — none may regress.
2. Implement surface by surface; keep the SSE/poll refresh + expanded-state
   persistence working on all three.
3. All three gates green.

## Tests

Template-hook tests as above; full suite green.

## Verification on real data

Same harness (real books' copies + real dccd store + one tick, TestClient):
extract and execute the real renderer JS against the real
`/api/orders?history=true` + `/api/fills` payloads. Evidence per book:
(a) an order row's generated HTML — Filled % = 100 % on a healed
v0.12.0-filled order, Value = filled × avg price; (b) its expanded detail
HTML joining exactly its fills (assert the join keys match, count == that
order's fills in the store); (c) a cancelled wave-2 order shows status
`cancelled`, Value from limit price, empty fills detail; (d) the
orders-page flat fills rows carry Order + Value. Confirm the detail page
markup no longer contains the standalone fills table hook. Originals and
port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added` (+ a `Removed` line for the standalone
  detail-page fills table): "orders tables show Filled % / Avg fill / Value
  with click-to-expand fills detail; the Orders page's audit fills view
  gains Order and Value (#XX)."
- ADR: none expected (decided design); state so.
- Tick leaf 02 in `00-plan.md`; archive per `/finish-task`.
