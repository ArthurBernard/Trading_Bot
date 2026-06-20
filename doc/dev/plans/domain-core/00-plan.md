---
plan: domain-core
kind: global
status: planning
roadmap: "- [ ] **E1 — Domain core.** `Order` (+ lifecycle state machine), `Position`, `Fill`, `Signal`, `Instrument`, `Money` (Decimal), pure PnL/KPI, errors."
release_on_done: false
---

# E1 — Domain core

## Goal

Build `trading_bot/domain/` — the **pure, zero-I/O, mypy-strict** vocabulary every
other layer speaks: exact-Decimal money, venue-neutral instruments, the `Order`
aggregate + lifecycle state machine, broker-confirmed `Fill`s, `Position` rebuilt
from fills, the strategy `Signal`, pure PnL/KPI, and the error hierarchy. No async,
no imports from `transport`/`brokers`/`storage`. The pre-2026 tree
(`trading_bot/legacy/`) is mined for domain knowledge, not copied.

## Decomposition

1. **primitives** — `Money` (Decimal), `Instrument`/`Symbol` (+ Kraken normalisation), `errors`.
2. **order** — `Order` + lifecycle state machine + order types (market/limit/stop-loss/best-limit).
3. **fill-position** — `Fill` (PnL source of truth) + `Position.from_fills`.
4. **signal** — `Signal` (venue-neutral strategy target) + delta-to-position.
5. **performance** — pure PnL/KPI functions, KPI delegated to fynance.

## Leaf checklist

- [x] 01 primitives — feat/domain-primitives — medium
- [ ] 02 order — feat/domain-order — high (depends on 01)
- [ ] 03 fill-position — feat/domain-fill-position — medium (depends on 02)
- [ ] 04 signal — feat/domain-signal — low (depends on 01)
- [ ] 05 performance — feat/domain-performance — high (depends on 03)

## Dependencies

- 02 depends on 01
- 03 depends on 02
- 04 depends on 01 (independent of 02/03 — may run alongside, but serial is the safe default)
- 05 depends on 03

## Done criteria

- `trading_bot/domain/` exposes `money`, `instrument`, `errors`, `order`, `fill`,
  `position`, `signal`, `performance` with a clean public `__init__`.
- `mypy trading_bot/` passes **strict** on `trading_bot.domain.*`.
- `ruff` + `pytest` green; domain tests cover state-machine transitions, Decimal
  exactness, position-from-fills (incl. a flip), and PnL/KPI vs an independent
  computation.
- **No** `domain/` module imports `transport`/`brokers`/`storage`.
- Last leaf (05) removes the E1 line from `07-roadmap.md` and updates `06-status.md`.
