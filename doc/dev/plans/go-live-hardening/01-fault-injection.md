---
plan: go-live-hardening/01-fault-injection
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: test/go-live-hardening
pr: ""
---

# Fault injection — prove the safety invariants adversarially

## Goal

A dedicated `tests/hardening/` suite that *demonstrates* the money-safety invariants
under **fault injection**, not just in happy-path unit tests: reconciliation
converges after a simulated disconnect, idempotent submit survives retries/ambiguous
failures, and the kill-switch cancels + halts mid-run. All offline (PaperBroker + a
fault-injecting wrapper) — no real venue.

## Files to change

- `trading_bot/tests/hardening/__init__.py` — new (empty).
- `trading_bot/tests/hardening/_faulty_broker.py` — new; a `FaultyBroker` wrapping
  `PaperBroker` (or implementing the `Broker` port) that can be told to fail/raise on
  the next call, drop a response (ambiguous failure), or simulate a disconnect.
- `trading_bot/tests/hardening/test_reconciliation.py` — new.
- `trading_bot/tests/hardening/test_idempotency.py` — new.
- `trading_bot/tests/hardening/test_kill_switch.py` — new.

## Steps

1. Read `application/reconcile.py` (`reconcile(broker, router, tracker)` + `ReconResult`),
   `application/order_router.py` (`submit` idempotency, `tracked_orders`), `application/risk.py`
   (`RiskManager.check`/`trip`/`kill`), `brokers/paper.py`, `brokers/base.py`.
2. `FaultyBroker`: wraps/extends a `PaperBroker`; switches to: raise a `BrokerError`
   on the next `place_order`; "ambiguous failure" (the order IS recorded by the broker
   but `place_order` raises/times out as if the response was lost); a "disconnect" that
   makes the local engine miss some venue orders/fills (so reconcile has real work).
   Deterministic, injectable.
3. **Reconciliation under disconnect**: seed the `FaultyBroker` with orders/fills the
   local engine never saw (simulating a disconnect window), run `reconcile`, assert
   local orders + positions converge to the broker exactly, **no order duplicated or
   lost**, and a second `reconcile` is a no-op (idempotent).
4. **Idempotency under retry / ambiguous failure**: submit an order whose `place_order`
   suffers an ambiguous failure; assert the engine does not silently create a duplicate
   on a retry of the same `client_order_id` (the dedup holds); and that reconcile after
   the ambiguous failure reconciles the truth (the order is adopted if the venue has it,
   not double-submitted). Document what the engine guarantees vs what needs the E10-02
   live policy.
5. **Kill-switch mid-run**: with a `RiskManager`, run a strategy/sequence; `trip()` /
   `kill(router, broker)` partway; assert open orders are cancelled, further submits are
   refused (`RiskLimitBreached`), and no order is placed after the trip — engine state
   stays consistent.

## Tests (via `.venv`)

The suite **is** the deliverable. Each property above is a test asserting the
money-safety guarantee under the injected fault. Keep them readable — each test tells
the story of the fault and the invariant it protects.

## Verification on real data

In-process adversarial scenarios (the `FaultyBroker` + PaperBroker are the engine's
real data under fault). Run the full `tests/hardening/` suite and confirm every
safety property holds; capture the scenarios exercised. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "Hardening test suite — reconciliation/idempotency/kill-switch proven under fault injection."
- ADR: the fault-injection methodology + what's proven offline vs what still needs a real-key sandbox.
- Status/roadmap: deferred to leaf 03.
