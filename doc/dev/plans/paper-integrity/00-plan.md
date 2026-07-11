---
plan: paper-integrity
kind: global
status: planning
roadmap: "1. [ ] **`paper-integrity` — paper engine integrity.** (UI consistency & accounting integrity, 2026-07-11 — PR #188)"
release_on_done: true
---

# paper-integrity — the paper engine's book survives restarts truthfully

## Goal

Three confirmed engine bugs make the paper book lie after any engine rebuild
(unit stop/start, `set_mode`, daemon restart). Fix them so that **every id is
unique across engine lifetimes, every fill reaches the order that caused it,
and every state change reaches the store** — the invariants Epic B
(`accounting-guardrail`) will later enforce permanently.

Evidence (2026-07-11 audit, verified on `var/dashboard/*.sqlite`):

1. **Ids reset per lifetime.** `PaperBroker.__init__` seeds `count(1)` for both
   order and fill ids (`brokers/paper.py:250-251`); `build_engine` constructs a
   fresh broker per unit start (`service_factory.py`, `supervisor.py:661`). A
   rebuilt engine re-mints `PAPER-1`/`PAPER-FILL-1`. The reused **fill id** is
   silently swallowed by all three idempotency layers at once —
   `PositionTracker.apply` (`position_tracker.py:142-146`),
   `PerformanceService.apply` (`performance_service.py:198-200`), store PK
   `(fill_id, venue, mode)` INSERT OR IGNORE (`sqlite_store.py:135`) — because
   `_replay_paper_book` (`supervisor.py:720-726`) pre-seeds the old ids.
   Observed: the alloc1-binance BTC buy (2nd rebalance wave) filled in the
   simulator but never reached tracker/store/UI.
2. **Fills never reach the tracked `Order`.** Nothing calls `Order.apply_fill`
   on the router's tracked instance (broker is port-pure, `paper.py:354-361`;
   the only `FillEvent` subscribers are tracker + perf). All 27 filled orders
   across both dashboard DBs are frozen at `status='open'`, `filled_qty=0`,
   `avg_fill_price=NULL` — the UI's Filled/Avg-fill columns are structurally
   dead.
3. **Reconcile's orphan-closes are never persisted.** `reconcile()` cancels
   restored non-terminal orders in memory only (`reconcile.py:197-210`) — it
   emits no `OrderEvent`, so order history keeps lying even after the engine
   corrected itself.

**Data note — no migration.** The swallowed wave-2 fills were dropped by every
consumer *including the store*: they are unrecoverable (they existed only in
the dead broker instance's memory), and the stored book is self-consistent
without them. Positions recompute from stored fills; the next rebalance diffs
target vs actual and self-corrects. The fix is entirely forward-looking; leaf
04 verifies the *healing* of order rows on the real DBs, not fill resurrection.

## Decomposition

1. `01-paper-durable-ids` — lifetime-unique venue/fill ids in `PaperBroker`
   (`PAPER-{token}-{n}`), token injectable for deterministic tests.
2. `02-order-fill-sync` — new `OrderFillSync` application component: routes
   every `FillEvent` back to the router's tracked `Order` (stash-and-drain,
   because the paper `FillEvent` fires synchronously *before* the router
   tracks the order), re-emits `OrderEvent` so the store row updates; a
   `replay()` entry point heals restored orders from persisted fills in
   `_start_locked`, **between** `router.restore()` and `reconcile()`.
3. `03-reconcile-persist-orphan` — `reconcile()` emits `OrderEvent` on each
   orphan-close so the store learns the CANCELLED terminal. Depends on 02
   (without healing, a filled-but-stale row would be *persisted* as CANCELLED
   — worse than stale).
4. `04-restart-replay-e2e` — the missing regression scenario: two engine
   lifetimes over one store; plus real-data verification on copies of the two
   dashboard DBs.

## Leaf checklist

- [x] 01 paper-durable-ids — fix/paper-durable-ids — medium (#190)
- [ ] 02 order-fill-sync — fix/order-fill-sync — high
- [ ] 03 reconcile-persist-orphan — fix/reconcile-persist-orphan — medium
- [ ] 04 restart-replay-e2e — chore/paper-restart-e2e — medium

## Dependencies

```
01 ──┐
     ├──> 04
02 ──┼──> 03 ──> 04
     └────────────┘
```

01 and 02 are independent (disjoint files) and may run in parallel.
03 requires 02 (heal-before-reconcile ordering). 04 requires all three.

## Done criteria

- Two-lifetime pytest scenario green: unique ids across lifetimes, no fill
  swallowed, order rows reach `FILLED`/`CANCELLED` terminals in the store.
- Real-data check on copies of `var/dashboard/alloc1-{binance,kraken}.sqlite`:
  a restart heals all wrongly-`open` filled rows to `FILLED`, persists the
  orphan cancels of the resting wave-2 orders, and a scripted fresh fill cycle
  lands in store+tracker+order with nothing swallowed.
- The `paper-integrity` roadmap line is removed by the last leaf's
  `/finish-task`.
