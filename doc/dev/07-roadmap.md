# 07 — Roadmap

The single source *index* of open work. Each unchecked item is a candidate for
`/pick-task` → `/plan` (which expands it into a `plans/<epic>/` tree) →
`/execute-leaf` → `/finish-task`. History of what shipped stays in git + CHANGELOG.

> Order is roughly sequential (E1 → E10); dependencies noted inline. Re-slice
> freely — an epic may ship as several small PRs.
>
> **Full decomposition** — every epic broken into its leaves, branches,
> complexity and dependencies: [`08-program-plan.md`](08-program-plan.md).

**The E1–E10 rewrite is complete.** The hexagonal engine conducts the triptych
(dccd data + fynance signals + brokers) via the `trading-bot` CLI and a read-only web
dashboard; paper-by-default, hardened under fault injection, live behind an explicit
off-by-default opt-in. History in git + `CHANGELOG.md`; see `06-status.md`.

## Open / deferred (maintainer decisions)

- [ ] **Final project name.** Kept `trading_bot` for now — choose and apply a final
  name when ready (touches the package/repo/docs).
- [ ] **Real-key live enablement.** Validate Kraken private endpoints + venue-level
  order idempotency against a **real-key sandbox**, then flip `live_enabled` — the one
  remaining prerequisite before real-money trading. See `doc/dev/09-go-live.md`.
