---
plan: execution-engine/05-reconciliation
kind: leaf
status: done
complexity: high
depends: [03, 04]
parallel: false
branch: feat/reconciliation
pr: "#28"
---

# Reconciliation — converge local state with the broker

## Goal

On startup and after any disconnect, **reconcile-don't-assume**: refetch the
broker's open orders, balances and fills, and converge the engine's local state
(tracked orders + positions) to the venue's truth — never leaving a duplicated or
lost order. The last E4 leaf — closes the E4 roadmap line.

## Files to change

- `trading_bot/application/reconcile.py` — new; `reconcile(...)` (+ a small `ReconResult`).
- `trading_bot/application/__init__.py` — export it.
- `trading_bot/tests/application/test_reconcile.py` — new.
- `doc/dev/07-roadmap.md` — remove the E4 line. `doc/dev/06-status.md` — mark E4 done.

## Steps

1. Read `OrderRouter` (its tracked-order map), `PositionTracker`, `brokers/base.py`.
2. `async reconcile(broker, router, tracker) -> ReconResult`:
   - Fetch `broker.open_orders()`, `broker.balances()`, `broker.fills(since)`.
   - **Orders**: adopt the venue's open orders as truth; a locally-tracked order the
     venue doesn't know about (and that isn't terminal) is flagged/closed per a clear
     rule (document it); a venue order the engine doesn't track is ingested.
   - **Positions**: rebuild the `PositionTracker` from the broker's confirmed fills
     (fills are the truth) so local positions equal the venue's.
   - Return a `ReconResult` summarising what was adopted/closed/ingested (and emit a
     `LogEvent`).
3. Idempotent: running `reconcile` twice with no venue change is a no-op.

## Tests

- Local state diverges from a **stub/PaperBroker** (extra local order not on venue;
  a venue order/fill the engine missed) → after `reconcile`, local orders + positions
  match the broker exactly; `ReconResult` reports the diffs.
- Positions are rebuilt from broker fills (match `Position.from_fills`).
- Running `reconcile` twice is a no-op the second time.

## Verification on real data

Simulate a disconnect: give the `PaperBroker` orders/fills the local engine doesn't
know about, run `reconcile`, and assert local state **converges** to the broker with
**no duplicated or lost orders** (the core safety property). Gates via **`.venv`**.

## Closeout

- CHANGELOG (Added): "`application.reconcile` — converge local order/position state with the broker."
- ADR: the reconciliation rules (orphan-order policy, positions-from-broker-fills, idempotency).
- Status/roadmap: **remove the E4 line** from `07-roadmap.md`; mark E4 done in `06-status.md`.
