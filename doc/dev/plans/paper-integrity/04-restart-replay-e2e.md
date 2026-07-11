---
plan: paper-integrity/04-restart-replay-e2e
kind: leaf
status: planned
complexity: medium
depends: [01, 02, 03]
parallel: false
branch: chore/paper-restart-e2e
pr: ""
---

# 04 — restart/replay end-to-end regression + real-data verification

## Goal

The scenario every existing test missed: **two engine lifetimes over one
store**. Encode it as a permanent E2E test so the three fixes can never
regress independently, then run the epic's final verification on copies of
the real dashboard books.

## Files to change

- `trading_bot/tests/application/test_restart_replay_e2e.py` — **new**. Uses
  the real seams only: `build_engine` (scratch config, paper mode, scratch
  SQLite path), `OrderRouter.submit`, `SqliteStore`, and the supervisor's
  restore→replay→reconcile sequence (either through `StrategySupervisor`
  start/stop of a minimal unit, or by reproducing `_start_locked`'s exact
  sequence if wiring a full unit is disproportionate — decide by reading
  `supervisor.py`; prefer the supervisor path if a unit can run without a
  data feed).
- (If the harness needs a tiny fixture helper, put it beside the test — no
  production code changes in this leaf.)

## Steps

1. Lifetime 1: build engine on a fresh scratch db, submit 2 orders (one
   fills immediately — default model; one resting: use
   `fill_model="partial"` with `arm_partial`/ratio or a non-marketable
   limit — read `paper.py` for the cleanest way to keep an order open).
   Assert store: 2 order rows, ≥1 fill, filled row terminal (leaf 02).
2. "Restart": drop the engine, build a second engine on the SAME db path,
   run restore → `fill_sync.replay` → `reconcile` (leaf 02/03 ordering).
3. Lifetime 2: submit 1 new order that fills.
4. Assertions (the epic's invariants — Epic B will make these permanent
   checks):
   - all venue ids and all fill ids in the store are pairwise distinct
     (leaf 01);
   - tracker position per instrument == signed sum of that instrument's
     store fills (no fill swallowed);
   - every filled order row is `FILLED` with `filled_qty == qty`; the
     lifetime-1 resting order row is `CANCELLED` (persisted orphan close,
     leaf 03); no row left `open`/`submitted`;
   - `PerformanceService.realised_pnl()` over lifetime 2 equals the same
     computation over `store.stored_fills()` (fills stay the sole PnL
     source).
5. `python -m pytest && ruff check trading_bot/` green.

## Tests

The E2E above IS the test. Keep it in the default (non-network) suite —
everything is local/sync; if runtime forces a compromise, mark the real-data
part below as the manual step, never the two-lifetime scenario.

## Verification on real data

On scratch copies of BOTH `var/dashboard/alloc1-binance.sqlite` and
`alloc1-kraken.sqlite` (never the originals — the daemon is running):

1. Run the lifetime-2 sequence (restore → replay → reconcile) against each
   copy with a `build_engine` paper engine.
2. Report, per book: wave-1 rows healed `open → filled` (14 / 13), wave-2
   resting rows persisted `open → cancelled` (14 / 13), positions unchanged
   by healing, all ids distinct.
3. Submit one scripted fresh order per book (any instrument, small qty) and
   show its fill lands in store + tracker + order row (nothing swallowed) —
   the forward-looking guarantee the running paper soak needs.
4. Paste the per-book summary in the PR description.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "Restart/replay E2E regression test —
  two engine lifetimes over one store (unique ids, no swallowed fills,
  truthful order terminals)."
- `doc/dev/06-status.md`: note the paper soak's books were verified healed
  (date + counts); note the pre-fix soak KPIs may include swallowed-fill
  drift (unrecoverable, self-corrected at the next rebalance).
- This is the last leaf: remove the `paper-integrity` roadmap line, mark the
  global `00-plan.md` done, archive the tree per `/finish-task`.
