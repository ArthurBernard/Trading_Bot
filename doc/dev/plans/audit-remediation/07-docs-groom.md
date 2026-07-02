---
plan: audit-remediation/07-docs-groom
kind: leaf
status: executing
complexity: low
depends: []
parallel: true
branch: docs/groom-audit
pr: ""
---

# Leaf 07 — docs-groom

## Goal
Bring the dev docs of record back in line with the shipped engine. Fixes `G-1`…`G-9`
(+ archive stale plan trees).

## Files to change
- `doc/dev/README.md`, `01-overview.md`, `02-architecture.md`, `04-brokers.md`,
  `05-testing.md`, `06-status.md`, `08-program-plan.md`, `CONTRIBUTING.md`.
- Move finished plan trees out of `doc/dev/plans/` (see below).

## Steps
1. `G-1`: `doc/dev/README.md:13-16` — drop the "Rewrite in progress" banner and the
   `trading_bot/legacy/` reference; align with `CLAUDE.md`/`06-status.md`.
2. `G-2`: `04-brokers.md` — update the capability matrix: Kraken shipped (REST+WS),
   Binance shipped (spot), PaperBroker; remove `legacy/` refs and "planned" labels.
3. `G-3`/`G-5`: `02-architecture.md`, `01-overview.md` — reflect the real module set
   (remove `scheduler.py`, `application/performance.py`, `registry` that don't
   exist; add the ~12 shipped modules); the dashboard is a **control plane**, not
   read-only.
4. `G-4`/`G-8`: `05-testing.md:3-4,8`, `CONTRIBUTING.md:41` — remove the false
   `--ignore=trading_bot/legacy` claim; describe the real `-m 'not network'` addopts.
5. `G-6`: `08-program-plan.md` — reframe the completed rewrite as done; drop the open
   "rename" leaf (the name decision is closed).
6. `G-9`: `06-status.md` — refresh the test count and "Last updated".
7. Archive finished plan trees: move the completed `doc/dev/plans/<epic>/` dirs
   (`domain-core`, `broker-kraken`, `transport`, `cli`, `execution-engine`,
   `strategy-runner`, `perf-persistence-risk`, `triptych-orchestration`, `web-ui`,
   `binance-adapter`, `portfolio-strategy`, `go-live-hardening`) to
   `doc/dev/_archive/plans/` so `plans/` holds only in-flight work
   (`audit-remediation`). Verify each is actually complete before moving.
8. Note (in the PR body, not a code change): `chmod 600 .env` (`G-7`) — a local op
   for the maintainer, not committed.

## Tests
No code. Verify every command referenced in the edited docs actually parses
(`trading-bot --help` + subcommands, the pytest/ruff/mypy invocations), and that no
edited doc references a file that doesn't exist.

## Verification on real data
N/A (docs only). Run a link/command sanity pass.

## Closeout
CHANGELOG (Changed / Docs): groom dev docs to match the shipped engine; archive
finished plan trees. No ADR needed (mechanical doc alignment).
