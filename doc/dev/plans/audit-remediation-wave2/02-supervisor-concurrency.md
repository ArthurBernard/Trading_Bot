---
plan: audit-remediation-wave2/02-supervisor-concurrency
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/supervisor-concurrency
pr: ""
---

# Leaf 02 — supervisor-concurrency

## Goal
Serialise per-unit lifecycle vs stepping so they can't race. Fixes `A-3`, `A-10`.

## Files
`trading_bot/application/supervisor.py`, tests under
`trading_bot/tests/application/`.

## Steps
1. `A-3` (Medium): `step` vs `set_mode`/`stop`/`remove_unit` are un-locked
   coroutines over shared `_Unit` state — a scheduler `step` can race a control-plane
   teardown and hit `None.step_latest()` or a half-built engine
   (`supervisor.py:665-685, 620-663`). Add a **per-unit `asyncio.Lock`** guarding the
   critical sections of `start`/`stop`/`step`/`set_mode`/`remove_unit` so a unit is
   never stepped while being torn down or re-built. Keep it per-unit (not one global
   lock) so independent strategies still run concurrently. Avoid deadlocks (consistent
   lock acquisition; don't hold the lock across long feed drains if avoidable).
2. `A-10` (Low): `remove_unit` is sync and hand-inlines `stop`'s async teardown —
   reconcile the two so they can't diverge (e.g. route both through one teardown
   helper), keeping `remove_unit`'s contract.

## Tests
A `step` interleaved with a concurrent `stop`/`set_mode` on the same unit does not
raise / corrupt state (deterministically exercise the race with an await seam or an
injected barrier); independent units still step concurrently; `remove_unit` fully
tears down (no residual engine/task).

## Verification on real data
Offline / PaperBroker: run a unit under a tight step cadence while flipping
`set_mode`/`stop` concurrently and confirm no exception and a clean final state.

## Closeout
CHANGELOG (Fixed): per-unit lock guarding supervisor step vs mode/stop/remove.
ADR: per-unit `asyncio.Lock` (not a global lock) and the unified teardown path.
