---
plan: accounting-guardrail/02-engine-wiring
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/accounting-wiring
pr: ""
---

# 02 — wire the checker into the unit lifecycle

## Goal

Run `check_book` at the moments that matter and make its findings visible on
the bus. Three behaviours:

1. **Startup check**: in `supervisor._start_locked`, right after
   `_replay_paper_book(engine)` (the book is now fully rebuilt), compute the
   report and store it on the unit.
2. **TTL recompute**: a `StrategySupervisor._accounting_of(unit)` helper
   returns the cached report, recomputing when older than 60 s — the
   dashboard polls every 10 s, so the check runs at most once a minute per
   unit, and only while someone is looking. No new background task, no
   scheduler change.
3. **Alerts on NEW violations only**: diff the fresh report against the
   previous one (key: `(kind, subject)`); each newly-appeared violation emits
   one `LogEvent` on `engine.bus` (`level="error"` for error severity,
   `"warning"` for warn) — the SSE stream and Logs page display these with
   zero new plumbing. Disappeared violations emit one `LogEvent(level="info",
   "accounting: resolved …")`. A stable violation set stays silent (the
   legacy duplicate-id warns must not spam every recompute).

## Files to change

- `trading_bot/application/supervisor.py`:
  - a small holder on `_Unit` (e.g. `accounting: AccountingState | None`)
    carrying the last `list[Violation]` + computed-at ms;
  - `_accounting_of(unit)` — compute/cached-return per the TTL; inputs:
    `engine.tracker.all_positions()`, the store's fills **filtered to
    `unit.mode`** and orders (read `store.stored_fills()` / `store.orders()`
    — same pattern the existing read paths use), `check_book` from leaf 01;
    stopped units (no engine) → last known report or empty;
  - the startup call in `_start_locked` after `_replay_paper_book`;
  - the diff-and-emit logic (one helper, called from both the startup check
    and the TTL recompute).
- `trading_bot/tests/application/test_supervisor.py` (or the existing
  supervisor test module — locate it) — tests below.

Do NOT touch `StrategyStatus`, the API or templates — that is leaf 03/04.

## Steps

1. Read `supervisor.py`'s `_Unit`, `_start_locked`, `_status_of` and the
   existing store-read patterns first; mirror their conventions (lock
   discipline included — decide whether `_accounting_of` needs the unit lock
   by reading how `positions()`/`order_history()` handle it, and follow suit).
2. Implement holder + helper + startup call + diff-emit.
3. Tests; all three gates green.

## Tests

- `test_startup_check_runs_and_stores_report`: unit started over a store with
  a seeded inconsistency → `unit.accounting` populated, one `LogEvent`
  captured with the right level.
- `test_ttl_recompute`: two calls within the TTL → one computation (count via
  monkeypatched `check_book`); advance the clock beyond the TTL → recompute.
- `test_alerts_only_on_new_violations`: stable violation set across
  recomputes → no new LogEvents; add a violation → exactly one event; resolve
  it → one info event.
- `test_mode_filtering`: fills from another mode in the same store do not
  enter the unit's check.

## Verification on real data

On scratch copies of both live books (originals untouched): start a real
engine sequence over the copy (the leaf-04-of-paper-integrity harness pattern:
`build_engine` + restore→replay→reconcile + `_replay_paper_book` equivalent),
then call the new helper directly:

- Post-heal book → report contains exactly the legacy `duplicate_venue_ids`
  warns (14 Binance / 13 Kraken), nothing else; a second call within the TTL
  does not recompute; the bus captured one warning LogEvent per legacy warn on
  the FIRST computation and none on the second.
- Delete one fill row from the Binance copy (SQL on the copy) and recompute
  past the TTL → `position_drift` error appears + one `LogEvent(level="error")`.
- Report the exact counts and the captured event messages.

## Closeout

- CHANGELOG `[Unreleased] > Added` (extend the epic's entry or add one):
  "the checker runs at unit startup and on a 60 s TTL behind the dashboard
  reads; new violations alert once on the SSE stream (#XX)."
- ADR only if a non-obvious choice emerged beyond the pinned design (TTL
  value, lock discipline) — otherwise fold into leaf 01's entry.
- Tick leaf 02 in `00-plan.md`; archive per `/finish-task`.
