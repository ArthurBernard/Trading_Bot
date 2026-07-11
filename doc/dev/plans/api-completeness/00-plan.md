---
plan: api-completeness
kind: global
status: planning
roadmap: "4. [ ] **`api-completeness` — API exposes what the app layer knows.** (UI consistency & accounting integrity, 2026-07-11)"
release_on_done: false
---

# api-completeness — the API says what the engine knows

## Goal

The 2026-07-11 audit's gap analysis: the app layer computes per-instrument
marks and unrealised PnL, knows balances, fee currencies and bar timestamps —
and the API serializes almost none of it, so the dashboard cannot show a
position's value or current price. Close the server-side gaps (UI consumption
is epic E). Must land before the road-to-1.0 API contract freeze (#5).

## Pinned decisions (maintainer, 2026-07-11 — implement, do not re-decide)

- **Mark policy v1**: mark = **the last dccd bar close** — the same data the
  strategy evaluates on. Every mark serialized WITH `mark_asof_ts` and
  `mark_source: "bar_close" | "last_fill"` (fallback = last own-fill price
  when no bar data is reachable). Live ticker is post-1.0. The UI must never
  be able to mistake a stale mark for a live price — the timestamp is
  non-optional.
- **Display currency**: global default + per-exchange override + static
  `conversion_rates` config; conversion happens **server-side** (one truth
  for every consumer): rows/aggregates gain `value_display` +
  `display_currency` fields alongside the native-quote figures (which stay
  untouched — contract).
- **API path does NO fresh I/O**: marks come from a cache the runner updates
  at its own cadence (`_latest_closes` at portfolio_runner.py:481 sees every
  frame); balances are read from the broker with the same care the existing
  snapshot endpoints use.
- **Public contract**: only ADD fields/endpoints; every leaf carries a
  contract-regression test (the accounting-guardrail leaf-03 pattern).
- All money = exact `str(Decimal)`.

## Scouted anchors

- `supervisor._mark_map(fills)` (supervisor.py:1335) — today's last-fill
  mark; `_unrealised_of` consumes it; stays as the fallback.
- `portfolio_runner._latest_closes(frames)` (:838, used at :481) — the
  per-symbol last close + the frame's asof, computed every rebalance: the
  publish point for the mark cache.
- `_position_row_dict` (app.py) — serializes 8 fields today; `PositionRow`
  (supervisor.py) is the carrier to extend.
- `Broker.balances()` — on every adapter incl. paper.
- `last_asof_ts` — already on `/api/strategies` rows.

## Decomposition

1. `01-mark-cache` — the runner publishes `(close, asof_ms)` per symbol into
   a per-engine mark cache; supervisor reads it with the last-fill fallback.
2. `02-position-row-marks` — `PositionRow` + `/api/positions` rows gain
   `mark`, `mark_asof_ts`, `mark_source`, `value` (mark × |net_qty|),
   `unrealised`, `fee_ccy`; `_unrealised_of` upgraded to prefer the cache.
3. `03-display-currency` — config (`display_currency`, per-exchange map,
   `conversion_rates`) + server-side converted `value_display`/
   `display_currency` on position rows and strategy aggregates.
4. `04-balances-and-asof` — `GET /api/balances`; `last_asof_ts` surfaced on
   the strategy-detail payloads; full contract-regression sweep.

## Leaf checklist

- [ ] 01 mark-cache — feat/mark-cache — medium
- [ ] 02 position-row-marks — feat/position-row-marks — medium
- [ ] 03 display-currency — feat/display-currency — medium
- [ ] 04 balances-and-asof — feat/api-balances — medium

## Dependencies

Serial: 01 → 02 → 03 → 04 (02 consumes 01's cache; 03 converts 02's values;
04 touches the same `app.py` as 03 — no parallel pairs).

## Done criteria

- `/api/positions` rows over the real books show a mark from the real dccd
  store with its asof, a value, an unrealised figure and a fee currency;
  `mark_source` honest (`bar_close` when the feed data exists, `last_fill`
  otherwise).
- `/api/balances` returns the paper balances for the running units.
- Cross-quote strategy aggregates carry a converted `value_display` in the
  configured display currency.
- Every pre-existing API field byte-identical (contract sweep green).
- Roadmap line removed by the last leaf.
