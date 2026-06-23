# 07 — Roadmap

The single source *index* of open work. Each unchecked item is a candidate for
`/pick-task` → `/plan` (which expands it into a `plans/<epic>/` tree) →
`/execute-leaf` → `/finish-task`. History of what shipped stays in git + CHANGELOG.

> Order is roughly sequential (E1 → E10); dependencies noted inline. Re-slice
> freely — an epic may ship as several small PRs.
>
> **Full decomposition** — every epic broken into its leaves, branches,
> complexity and dependencies: [`08-program-plan.md`](08-program-plan.md).

## Interfaces & orchestration

- [ ] **E8 — Orchestration of the triptych.** One `AppConfig` declaring data
  sources (dccd) + strategies (fynance) + brokers; a single entrypoint that wires
  the three. Decide library-import vs service-driving for dccd here. _(depends on E7)_

## Later

- [ ] **E9 — Web UI.** FastAPI + Jinja2 dashboard (positions/orders/PnL),
  mirroring dccd's UI. _(depends on E8)_
- [ ] **E10 — Go-live hardening & final name.** Live-trading checklist
  (reconciliation, kill-switch, idempotency proven under fault injection); choose
  and apply the final project name. _(depends on E8)_
