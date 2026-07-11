---
plan: api-completeness/04-balances-and-asof
kind: leaf
status: planned
complexity: medium
depends: [03]
parallel: false
branch: feat/api-balances
pr: ""
---

# 04 — /api/balances, last_asof surfacing, contract sweep

## Goal

Close the epic's remaining gaps:

1. **`GET /api/balances`** (auth-gated like the other read endpoints;
   optional `?strategy=` filter mirroring `/api/positions`): per RUNNING
   unit, the broker's `balances()` serialized as
   `{strategy, exchange, mode, balances: {asset: "exact-decimal"}}` rows
   (grouped shape mirroring the other collection endpoints). `balances()`
   is async on the port — check how other handlers await broker/supervisor
   async calls and mirror it; a stopped unit contributes nothing; a broker
   error degrades to an empty row with an `error` string field, never a 500
   (the endpoint must be safe to poll).
   Note in the docstring: this is the prerequisite seam for the
   positions↔balances cross-check (accounting-guardrail extension) and the
   canary-roundtrip live oracle (roadmap #6).
2. **`last_asof_ts` surfacing**: verify it is present on every payload the
   detail page consumes (`/api/strategies` has it; check the capital/pnl
   payloads need nothing extra) and add it where the audit found it missing
   — the goal is that epic E's "last bar → next bar" chip needs NO further
   server change. Document in the endpoint docstrings what the field means
   (as-of of the last completed evaluation).
3. **Contract-regression sweep**: one test module pass asserting the exact
   pre-epic shapes of `/api/strategies`, `/api/positions`, `/api/fills`,
   `/api/orders`, `/api/kpi`, `/api/health` still hold field-for-field
   (additive-only proof for the whole epic, before the API freeze).

## Files to change

- `trading_bot/interfaces/api/app.py` — the endpoint + docstrings.
- `trading_bot/application/supervisor.py` — a `balances()` snapshot method
  if the handler shouldn't reach into units directly (mirror how
  `positions()` wraps unit access).
- Dashboard/control-api test modules — tests below.

## Steps

1. Read the collection-endpoint patterns (`/api/positions` grouping, auth,
   read_only) and the supervisor snapshot wrappers; implement.
2. All three gates green.

## Tests

- `/api/balances`: running paper unit → its simulator balances (exact
  Decimal strings); stopped unit absent; `?strategy=` filter; broker error
  → error row, HTTP 200; auth + read-only behaviour identical to peers.
- `last_asof_ts` presence assertions on the consuming payloads.
- The epic-wide contract sweep (exact pre-epic shapes, additive-only).

## Verification on real data

Real supervisor over the two book copies, TestClient:
`GET /api/balances` → report the actual paper balances rows per unit (the
simulator's seeded quote balances net of the books' fills); confirm
`last_asof_ts` on `/api/strategies` matches the dccd store's last bar for
each unit. Originals and port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "`GET /api/balances` (per running unit,
  poll-safe) + `last_asof_ts` surfaced for the bar-timing chip; epic-wide
  additive-only contract sweep (#XX)."
- `doc/dev/06-status.md`: epic shipped note.
- Last leaf: remove the `api-completeness` roadmap line, set the global
  `00-plan.md` done, archive the tree per `/finish-task`.
