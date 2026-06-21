---
plan: execution-engine
kind: global
status: planning
roadmap: "- [ ] **E4 — Order router + PaperBroker.** Idempotent submit (client-order-id), routing, reconciliation; `PaperBroker` simulation behind the same port; `PositionTracker` from confirmed fills."
release_on_done: false
---

# E4 — Execution engine

## Goal

Open `trading_bot/application/` — the engine that turns intent into managed orders.
A small **kernel** (`AppConfig` + async `EventBus`), a **`PaperBroker`** (in-process
fill simulation behind the E3 `Broker` port — the default), an **`OrderRouter`**
(idempotent submit, drives the domain `Order` state machine, emits events), a
**`PositionTracker`** (net positions from confirmed fills), and **reconciliation**
(converge local state with the broker on startup/reconnect). Mirrors dccd's
`application/` (`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/application/`).

**Invariants** (from `CLAUDE.md`): idempotent submit (client-order-id), reconcile-
don't-assume, fills are the PnL truth, all money `Decimal`. Everything is verifiable
**in-process** (PaperBroker) — no network/key needed.

## Decomposition

1. **app-kernel** — `application/config.py` (`AppConfig` pydantic) + `events.py` (async `EventBus`).
2. **paper-broker** — `brokers/paper.py` `PaperBroker` (fill simulation, the default broker).
3. **order-router** — `application/order_router.py` (idempotent submit, drives Order, emits events).
4. **position-tracker** — `application/position_tracker.py` (positions from fills).
5. **reconciliation** — `application/reconcile.py` (converge local state with the broker).

## Leaf checklist

- [x] 01 app-kernel — feat/app-kernel — medium
- [ ] 02 paper-broker — feat/paper-broker — medium
- [ ] 03 order-router — feat/order-router — high (depends on 01, 02)
- [ ] 04 position-tracker — feat/position-tracker — medium (depends on 03)
- [ ] 05 reconciliation — feat/reconciliation — high (depends on 03, 04)

## Dependencies

- 03 depends on 01 + 02; 04 depends on 03; 05 depends on 03 + 04.
- 01 and 02 are independent (run serially, the safe default).

## Done criteria

- `application/` exposes the kernel, router, tracker, reconciliation; `PaperBroker`
  in `brokers/`. `ruff`/`mypy`/`pytest` green (via `.venv`).
- Idempotent submit proven (duplicate client-order-id → one broker order); a routed
  order flows order→fill→position end-to-end through the PaperBroker; reconciliation
  converges a diverged local state to the broker with no lost/duplicated orders.
- Last leaf (05) removes the E4 line from `07-roadmap.md` and updates `06-status.md`.
