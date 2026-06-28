---
plan: go-live-hardening
kind: global
status: done
roadmap: "- [ ] **E10 — Go-live hardening & final name.** Live-trading checklist (reconciliation, kill-switch, idempotency proven under fault injection); choose and apply the final project name."
release_on_done: false
---

# E10 — Go-live hardening

The last epic. **Scoped (maintainer decision): hardening only.** We prove the
money-safety invariants under fault injection, close the safe-to-fix known gaps, and
write the go-live runbook + an explicit live opt-in guard — but **no real live
trading, no API key, and no order is ever sent to a real venue**. The **final project
name stays deferred** (out of E10 scope): the package remains `trading_bot`.

## Goal

A trading engine whose safety properties are *demonstrated*, not just asserted in
unit tests: reconciliation converges and the kill-switch + idempotency hold under
adversarial fault injection; the recorded known gaps are fixed or guarded; and going
live is a deliberate, documented opt-in that is **off by default** (paper stays the
default; the live path raises a clear "not enabled — read the runbook" until
explicitly turned on, with a real-key sandbox the only thing left to do later).

## Decomposition

1. **fault-injection** — `tests/hardening/`: prove reconcile/kill-switch/idempotency hold under a fault-injecting broker.
2. **close-known-gaps** — fix KPI-v0 (config starting capital), detect same-instrument commingling, guard/document the venue-idempotency live-submit policy.
3. **go-live-runbook** — the go-live checklist/runbook + a `LiveTradingNotEnabled` opt-in guard (paper default; no real order ever sent).

## Leaf checklist

- [x] 01 fault-injection — test/go-live-hardening — high
- [x] 02 close-known-gaps — feat/close-known-gaps — high
- [x] 03 go-live-runbook — feat/go-live-opt-in — medium (depends on 01, 02)

## Dependencies

- 01 and 02 are independent (run serially, the safe default); 03 depends on 01 + 02.

## Done criteria

- A `tests/hardening/` suite demonstrates: reconcile converges after a simulated
  disconnect (no order duplicated/lost), idempotent submit under retry/ambiguous
  failure, kill-switch cancels + halts mid-run — all offline.
- KPI ratios are meaningful (config-driven starting capital wired through);
  `run_app` rejects/flags duplicate-symbol strategies; the venue non-idempotent-submit
  policy is a guarded, documented code path.
- A go-live runbook exists; the live path is an explicit opt-in that is **off by
  default** and raises a clear error until enabled. No real order is ever sent.
- `ruff`/`mypy`/`pytest` green via `.venv`.
- Last leaf (03) removes the E10 line from `07-roadmap.md` and updates `06-status.md`
  — but the **deferred "final project name" decision stays open** (not closed by E10).
