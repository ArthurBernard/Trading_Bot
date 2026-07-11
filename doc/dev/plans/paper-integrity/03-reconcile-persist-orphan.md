---
plan: paper-integrity/03-reconcile-persist-orphan
kind: leaf
status: planned
complexity: medium
depends: [02]
parallel: false
branch: fix/reconcile-persist-orphan
pr: ""
---

# 03 — reconcile persists its orphan-closes

## Goal

`reconcile()` drives an orphaned order to `CANCELLED` in memory
(`_close_orphan`, `trading_bot/application/reconcile.py:241-259`) and evicts
it (`router.forget`), but emits no `OrderEvent` — the store row keeps the
stale pre-restart status forever, so order *history* lies even after the
engine corrected itself. Emit `OrderEvent(order)` after each orphan-close so
`SqliteStore.upsert_order` persists the terminal.

Depends on leaf 02: with `replay()` healing running before `reconcile()` in
`_start_locked`, a genuinely-filled restored order is already terminal and the
orphan rule skips it (`order.is_terminal → continue`). Landing this leaf
*without* 02 would persist `CANCELLED` onto filled-but-stale rows — actively
wrong history instead of merely stale.

## Files to change

- `trading_bot/application/reconcile.py` — in the orphan loop
  (`reconcile.py:197-210`), after `_close_orphan(order)` +
  `router.forget(cid)`, emit `OrderEvent(order)` when `event_bus is not None`
  (same guard as the existing summary `LogEvent`). Emit even if
  `_close_orphan`'s transition was refused (its except-path) — persisting the
  order's actual current state is still more truthful than the stale row.
  Update the module docstring's orphan-policy section to say the close is now
  persisted.
- `trading_bot/tests/application/test_reconcile.py` (existing reconcile test
  module — locate by `grep -rl "reconcile" trading_bot/tests/`) — tests below.

## Steps

1. Add the emit (import `OrderEvent` alongside the existing `LogEvent`
   import).
2. Update the module docstring (orphan policy paragraph + the
   `supervisor.order_history()` cross-reference if it restates
   non-persistence).
3. Tests; `python -m pytest && ruff check trading_bot/` green.

## Tests

- `test_orphan_close_emits_order_event`: router tracking a non-terminal order
  the (empty) broker doesn't report → after `reconcile`, a subscriber
  captured an `OrderEvent` whose order is that `client_order_id` with
  `status == CANCELLED`.
- `test_orphan_close_persists_to_store`: same scenario with a real
  `SqliteStore` attached to the bus → `store.orders()` row for the orphan now
  reads `cancelled` (this is the persistence regression).
- `test_no_event_bus_still_works`: `event_bus=None` path unchanged (no
  crash, orphan still evicted).
- `test_terminal_orders_untouched`: a `FILLED` tracked order (healed by leaf
  02's replay) is neither cancelled nor re-emitted by the orphan loop.

## Verification on real data

On the scratch copies of `var/dashboard/alloc1-{binance,kraken}.sqlite`
(after leaf 02's healing pass): run the full `_start_locked`-equivalent
sequence (restore → replay → reconcile) through the real seams and read back
`store.orders()`. Report: every wave-2 resting order row moved
`open → cancelled` (Binance 14, Kraken 13), every healed wave-1 row stayed
`filled`, and `ReconResult.closed_orphans` matches the wave-2 count exactly.

## Closeout

- CHANGELOG `[Unreleased] > Fixed`: "Reconcile's orphan-closes are persisted
  (an `OrderEvent` per close) — order history no longer shows cancelled
  orders as open."
- ADR: fold into leaf 02's entry (one decision journal note for the epic's
  lifecycle contract) unless a separate nuance emerged.
- Tick leaf 03 in `00-plan.md`.
