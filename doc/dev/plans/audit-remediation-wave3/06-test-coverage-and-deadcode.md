---
plan: audit-remediation-wave3/06-test-coverage-and-deadcode
kind: leaf
status: executing
complexity: medium
depends: []
parallel: true
branch: chore/test-coverage-deadcode
pr: ""
---

# Leaf 06 — test-coverage-and-deadcode

## Goal
Cover the money-critical error branches and remove dead code/assets. Fixes `T-4`,
`T-12`, `I-12`, `A-11`, `G-14`.

## Files
tests under `trading_bot/tests/` (new coverage), `trading_bot/application/risk.py`
(dead daily-PnL code), `trading_bot/interfaces/ui/static/` + `templates/` (dead
assets), tracked pre-rewrite artifacts (`data_base/`, `general_config_example.yaml`,
`execution_scripts/`), `hardening`/coupled tests.

## Steps
- `T-4`: add tests for the uncovered money-critical error branches — kill-switch
  cancel-on-failure (`risk.py`), order-router forbidden reject transition, reconcile
  divergence, transport/WS reconnect. Raise real coverage on these paths.
- `T-12`: de-couple the tests coupled to implementation details (private `_fills`,
  exact log/error strings) — assert behaviour, not internals.
- `I-12`: remove the dead/legacy single-engine dashboard assets (`app.js`,
  `control.js`, `control.html`, stale `style.css`) that no route renders.
- `A-11`: remove the dead provider-backed `RiskManager` `record_daily_pnl`/`reset_day`
  recorded-value path (dead since the day-scoped provider wiring), or wire it — pick
  one; keep the daily-loss breaker behaviour intact.
- `G-14`: drop the pre-rewrite tracked artifacts (`data_base/`,
  `general_config_example.yaml`, `execution_scripts/`) that are dead in the current
  engine (confirm nothing imports them).

## Tests
New tests exercise the four error branches (coverage rises on them); the de-coupled
tests still pass asserting behaviour; the suite is green after removing dead assets
(nothing references them).

## Verification
Offline: `python -m pytest` green with the new tests + removals; grep confirms the
removed assets/artifacts have no references; the daily-loss breaker still trips+resets.

## Closeout
CHANGELOG (Removed + Fixed/Added-tests). No ADR (cleanup + coverage) unless A-11
wire-vs-remove is non-trivial.
