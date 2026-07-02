---
plan: audit-remediation
kind: global
status: executing
roadmap: "Audit remediation — wave 1 (Critical + top High)."
release_on_done: true
---

# Audit remediation — wave 1

## Goal

Close the Critical and top-High findings from the 2026-07-02 audit (`doc/dev/audit/`)
so that (a) the test gate is trustworthy again and (b) the money-safety invariants
hold before any strategy is flipped to `live`. Every leaf ships as one atomic PR into
`develop`. Standing constraint: **paper/testnet only, no real orders** — all
verification is offline or against `PaperBroker`.

## Decomposition

Seven leaves, largely file-disjoint so they run in parallel isolated worktrees.
Leaf 01 (test hermeticity) is the foundation but not a hard blocker: agents work in
fresh worktrees where the local `configs/dashboard.yaml` is absent, so their gates
are already clean.

1. **test-hermeticity** — `T-1` autouse CWD/env isolation (a `conftest.py`), `T-2`
   drop `--exitfirst` from `addopts`, `T-3` enforce coverage (`--cov-fail-under`).
2. **domain-hardening** — `D-1` apply `money()` in `Order`/`Fill`/`Signal` ctors,
   `D-4` pin a `Decimal` context on avg-price math, `D-7` reject non-finite
   `TARGET_QTY`, `D-3` fix `parse_kraken_pair("XTZUSD")`, `D-10` `normalise` X-strip.
3. **transport-secret-redaction** — `B-1` scrub the signed query string from
   `http.py` error messages and logs.
4. **broker-live-readiness** — `B-2` quantize qty/price to venue tick/lot, `B-3`
   monotonic locked Kraken nonce, `B-4` venue-code→domain-error taxonomy + retry
   retriable Kraken HTTP-200 errors, `B-5`/`B-15` preserve & forward the
   client-order-id on both venues + reconcile on it.
5. **dashboard-gate-hardening** — `I-2` enforce the typed live-confirm server-side,
   `I-3` sanitise `db_path` in deploy, `I-1` constrain/telemeter `signal.ref`.
6. **engine-risk-storage** — `A-1` make `max_daily_loss` actually daily (reset at
   boundary), `D-2` add an `orders` table migration.
7. **docs-groom** — `G-1`…`G-9` fix stale dev docs, archive finished plan trees,
   note `chmod 600 .env`.

## Leaf checklist

- [ ] 01 test-hermeticity — fix/test-hermeticity — high
- [ ] 02 domain-hardening — fix/domain-hardening — high
- [ ] 03 transport-secret-redaction — fix/transport-secret-redaction — medium
- [ ] 04 broker-live-readiness — fix/broker-live-readiness — high
- [ ] 05 dashboard-gate-hardening — fix/dashboard-gate-hardening — high
- [ ] 06 engine-risk-storage — fix/engine-risk-storage — high
- [ ] 07 docs-groom — docs/groom-audit — low

## Dependencies

All leaves are file-disjoint and independent (`parallel: true`). CHANGELOG entries
are added at PR closeout (orchestrator), not by the agents, to avoid `[Unreleased]`
merge conflicts across the parallel branches.

## Done criteria

Seven PRs open into `develop`, each with `pytest`/`ruff`/`mypy` green in its
worktree and its findings verified fixed. Wave-2 items (`A-2`, `A-3`, `B-6`, long
tail) stay on the roadmap. On merge of all seven → `/release` (v0.8.0).
