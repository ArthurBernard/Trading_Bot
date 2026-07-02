---
plan: audit-remediation-wave3
kind: global
status: executing
roadmap: "Audit remediation — long tail (Medium/Low)."
release_on_done: true
---

# Audit remediation — wave 3 (the long tail)

## Goal

Burn down the remaining Medium/Low audit findings (`doc/dev/audit/`) — robustness,
hygiene, tooling and coverage debt left after waves 1–2. Grouped into coherent
leaves (one PR = one concern); pure Info observations are skipped. Standing
constraint: **paper/testnet only, no real orders** — verification offline / paper.

## Decomposition (6 leaves, largely file-disjoint)

1. **dashboard-web-hardening** — `I-4` `X-Forwarded-Proto`/`Secure`-cookie trust,
   `I-5` `run --serve` non-loopback token guard, `I-6` request-body cap, `I-7`
   unbounded session/rate maps, `I-9` deploy `mode` ignored, `I-10` rate-limit
   keying behind a proxy, `I-11` security headers (`Cache-Control`/`X-Content-Type`
   /CSP), `I-13` login CSRF, `A-4` `UIConfig` non-loopback-requires-token enforced
   at the config layer.
2. **engine-and-order-robustness** — `A-6` `combined_equity_series` over-anchors
   `v0`, `A-7` `rebalance_latest` drains the whole feed each tick, `A-8` cancel not
   idempotent / no in-flight guard, `A-9` in-flight not restored on `router.restore`.
3. **broker-transport-robustness** — `B-11` connection-pool limits / connect-vs-read
   timeout / response-size cap, `B-12` unknown venue order-type coerced to `LIMIT`,
   `B-13` PaperBroker divergence from live semantics (min-notional/precision reject).
4. **domain-hygiene** — `D-6` over-fill rejected within `fill_tolerance`, `D-11`
   `_check_aligned` docstring, `D-12` non-finite money raises `decimal.*` not a domain
   error, `D-13` dead `InstrumentMismatch`/`InsufficientFunds`, `A-12` `Order`
   client-order-id mutated after construction (aggregate not truly immutable).
5. **tooling-ci-packaging** — `T-5` pre-commit↔CI parity (`ruff format --check` +
   mypy in CI), `T-6` dead `MANIFEST.in` directive, `T-7` dep/action pinning, `T-8`
   `filterwarnings` + `asyncio_default_fixture_loop_scope`, `T-9` wall-clock sleep,
   `T-10` unused `interrogate`, `T-11` `parents[3]` walk comment, `G-13` install-extra
   inconsistency.
6. **test-coverage-and-deadcode** — `T-4` cover the money-critical error branches
   (kill-switch cancel-failure, forbidden reject transition, reconcile divergence,
   WS reconnect), `T-12` de-couple impl-detail-coupled tests, `I-12` remove dead
   dashboard assets, `A-11` dead daily-PnL recorded-value code, `G-14` drop
   pre-rewrite tracked artifacts.

## Leaf checklist

- [ ] 01 dashboard-web-hardening — fix/dashboard-web-hardening — high
- [ ] 02 engine-and-order-robustness — fix/engine-order-robustness — high
- [ ] 03 broker-transport-robustness — fix/broker-transport-robustness — medium
- [ ] 04 domain-hygiene — fix/domain-hygiene — medium
- [ ] 05 tooling-ci-packaging — chore/tooling-ci-packaging — medium
- [ ] 06 test-coverage-and-deadcode — chore/test-coverage-deadcode — medium

## Dependencies

All independent (`parallel: true`). CHANGELOG/ADR entries added at PR closeout
(orchestrator) to avoid `[Unreleased]` conflicts.

## Done criteria

Six PRs open into `develop`, each `pytest`/`ruff`/`mypy` green. On merge → `/release`
(v0.10.0). Only pure Info observations remain from the audit.
