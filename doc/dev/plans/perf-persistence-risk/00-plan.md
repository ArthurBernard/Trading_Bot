---
plan: perf-persistence-risk
kind: global
status: planning
roadmap: "- [ ] **E6 — Performance, persistence & risk.** `PerformanceService` (PnL/KPI), `storage` (SQLite order/fill history + state for reconciliation), `RiskManager` (pre-trade limits + kill-switch)."
release_on_done: false
---

# E6 — Performance, persistence & risk

## Goal

The last safety/observability block before the MVP CLI: durable **persistence**
(SQLite order/fill history + engine state — the reconciliation source), a live
**PerformanceService** (PnL/KPI over the fill stream), and a **RiskManager**
(pre-trade limits + kill-switch) that gates every order. Opens `trading_bot/storage/`
and extends `application/`.

**Invariants** (from `CLAUDE.md`): all money is `Decimal` (stored as **TEXT** in
SQLite, never float); fills are the PnL source of truth; the **risk gate + kill-switch
gate every order**. Everything is offline-testable.

## Decomposition

1. **storage** — `trading_bot/storage/`: SQLite append-only order/fill history + state (the reconciliation source).
2. **performance-service** — `application/performance_service.py`: live PnL/KPI over the `FillEvent` stream.
3. **risk-manager** — `application/risk.py`: pre-trade limits + kill-switch, gating `OrderRouter.submit`.

## Leaf checklist

- [x] 01 storage — feat/storage — high
- [x] 02 performance-service — feat/performance-service — medium
- [ ] 03 risk-manager — feat/risk-manager — high

## Dependencies

- The three leaves are independent of each other (storage / perf / risk) — run
  serially in the main worktree (the safe default). All assume E4 (merged).

## Done criteria

- `trading_bot/storage/` persists orders/fills (Decimal as TEXT) and round-trips
  them; `application/` exposes `PerformanceService` and `RiskManager`.
- `OrderRouter.submit` is **gated** by the `RiskManager`: an order breaching a limit
  (or with the kill-switch tripped) raises `RiskLimitBreached` and is never placed;
  the kill-switch cancels open orders + halts new ones.
- `ruff`/`mypy`/`pytest` green via `.venv` (0 unexpected skips). Persistence verified
  by writing real engine events and reading them back; risk + perf verified end-to-end
  through `OrderRouter`→`PaperBroker`.
- Last leaf (03) removes the E6 line from `07-roadmap.md` and updates `06-status.md`.
