---
plan: accounting-guardrail/03-health-surface
kind: leaf
status: planned
complexity: medium
depends: [02]
parallel: false
branch: feat/health-surface
pr: ""
---

# 03 — per-strategy health, supervisor → API

## Goal

Expose the accounting report — and the until-now invisible kill-switch — as a
per-strategy health field, end-to-end server-side. The 2026-07-11 audit
confirmed `RiskManager.tripped`/`trip_reason` have **zero readers** anywhere
in `interfaces/` or `supervisor.py`: a tripped kill-switch is currently
invisible. This leaf gives both signals one shared surface (and thereby
delivers the kill-switch *status* half of road-to-1.0 #3; the trip/reset
*endpoint* stays in that roadmap item).

Health mapping (pinned): any `error`-severity violation **or**
`engine.risk.tripped` → `"error"`; else any `warn` violation → `"warn"`;
else `"ok"`. A stopped unit (no engine) → `"ok"` with empty detail unless its
last stored report says otherwise.

## Files to change

- `trading_bot/application/supervisor.py`:
  - `StrategyStatus` gains `health: str` and `health_detail: tuple[str, ...]`
    (the violations' `detail` sentences, kill-switch reason first when
    tripped — keep it a flat tuple of strings, the UI renders them verbatim);
  - `_status_of` computes them from `_accounting_of(unit)` (leaf 02) +
    `unit.engine.risk.tripped` / `.trip_reason`.
- `trading_bot/interfaces/api/app.py`:
  - `_status_dict` passes `health` + `health_detail` through on every
    `/api/strategies` row (and therefore on the POST/start/stop/mode response
    envelopes that reuse it);
  - `/api/health` gains `worst: "ok"|"warn"|"error"` (worst across running
    units) and `unhealthy: int` (count of non-ok units) — keep the existing
    fields untouched (public contract).
- Existing supervisor/API test modules — tests below.

## Steps

1. Read `StrategyStatus`, `_status_of`, `_status_dict`, `/api/health`
   handler; mirror field-addition conventions from the strategy-capital epic
   (which added `allocation`/`unrealised`/… the same way).
2. Implement; keep money/Decimal out of it (health is strings only).
3. Tests; all three gates green.

## Tests

- `test_status_health_ok_warn_error`: three units seeded with clean / warn /
  error reports → mapped statuses.
- `test_kill_switch_folds_to_error`: trip the risk manager on a unit with a
  clean book → `health == "error"`, `trip_reason` first in `health_detail`.
- `test_api_strategies_carries_health`: `/api/strategies` row has both fields
  (string + list of strings, JSON-safe).
- `test_api_health_worst_and_count`: worst-of aggregation + unhealthy count;
  existing `/api/health` fields unchanged (contract regression).

## Verification on real data

Serve the API locally (uvicorn, scratch port, read-only) over a scratch copy
of alloc1-binance (post-heal, with the deleted-fill drift induced as in leaf
02): `curl /api/strategies` shows `health:"error"` with the drift sentence in
`health_detail`; restore the fill copy → `health:"warn"` (legacy duplicate
ids only); `curl /api/health` shows `worst`/`unhealthy` consistent. NEVER
against the live daemon's port or books. Report the actual JSON fragments.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "per-strategy `health`/`health_detail` on
  `/api/strategies` (+ `worst`/`unhealthy` on `/api/health`) — accounting
  violations and the kill-switch finally visible (#XX)."
- ADR: the health mapping + kill-switch fold (delivers the status half of
  road-to-1.0 #3).
- Tick leaf 03 in `00-plan.md`; archive per `/finish-task`.
