---
plan: api-completeness/02-position-row-marks
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/position-row-marks
pr: ""
---

# 02 — position rows carry mark, value, unrealised and fee currency

## Goal

`/api/positions` rows finally answer "what is this position worth, at what
price, as of when": extend `PositionRow` (supervisor.py) and
`_position_row_dict` (app.py) with — all ADDITIVE, existing fields untouched:

- `mark`, `mark_asof_ts`, `mark_source`: from the engine's `MarkCache`
  (leaf 01) via `Symbol`; fallback when the cache has no entry: the
  existing last-own-fill mark (`_mark_map`) with `mark_source="last_fill"`
  and the fill's ts as `mark_asof_ts`; if neither exists → all three `null`.
- `value`: `mark × |net_qty|` (exact Decimal string; `null` when no mark).
- `unrealised`: `(mark − avg_entry_price) × net_qty` (sign-correct for
  shorts — mirror `_unrealised_of`'s arithmetic; `null` when no mark or no
  entry price).
- `fee_ccy`: the instrument's quote (`symbol.quote`).

Also upgrade the strategy-aggregate unrealised (`_unrealised_of`,
supervisor.py:~1930) to prefer the mark cache over the last-fill map (same
fallback order as the rows) so the roster's `total_value`/`unrealised` and
the per-position figures agree — one mark policy everywhere.

## Files to change

- `trading_bot/application/supervisor.py` — `PositionRow` fields +
  `positions()` fill-in + `_unrealised_of` cache preference.
- `trading_bot/interfaces/api/app.py` — `_position_row_dict` pass-through.
- Supervisor + dashboard test modules — tests below.

## Steps

1. Read `PositionRow`/`positions()`/`_unrealised_of` and the
   field-addition convention from the strategy-capital epic; implement.
2. All three gates green.

## Tests

- `test_position_row_mark_from_cache` (bar_close mark + value + unrealised,
  Decimal-exact, short position sign checked).
- `test_position_row_falls_back_to_last_fill` (no cache entry → last-fill
  mark, `mark_source="last_fill"`).
- `test_position_row_no_mark_is_null` (flat/unknown → nulls, no crash).
- `test_fee_ccy_is_quote`.
- `test_aggregate_and_row_agree` (strategy `unrealised` == Σ row unrealised
  under one mark policy).
- Contract regression: every pre-existing `/api/positions` field unchanged.

## Verification on real data

Real supervisor start over scratch copies of BOTH live books (the daemon's
real manifest sources, read-only; dccd store under `~/data/arthurserver`),
TestClient GET `/api/positions`: report 3 real rows per book with
mark/asof/source/value/unrealised — marks must be `bar_close` with a recent
asof when the dccd data covers the symbol, and the strategy aggregate must
equal the row sum. Originals and port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "`/api/positions` rows carry `mark` /
  `mark_asof_ts` / `mark_source` / `value` / `unrealised` / `fee_ccy`; the
  strategy aggregate now uses the same bar-close mark policy (#XX)."
- ADR: the mark policy (bar_close v1 + tagged fallback, timestamp
  non-optional; live ticker post-1.0).
- Tick leaf 02 in `00-plan.md`; archive per `/finish-task`.
