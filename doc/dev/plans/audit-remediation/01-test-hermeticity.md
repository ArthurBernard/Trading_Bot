---
plan: audit-remediation/01-test-hermeticity
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/test-hermeticity
pr: ""
---

# Leaf 01 — test-hermeticity

## Goal
Make the pytest gate trustworthy: hermetic tests (no CWD/env bleed), no
first-failure masking, enforced coverage. Fixes `T-1`, `T-2`, `T-3`.

## Files to change
- `trading_bot/tests/conftest.py` (new) — autouse fixture isolating CWD + env.
- `pyproject.toml` — `[tool.pytest.ini_options] addopts`, coverage config.
- `.github/workflows/ci.yml` — coverage artifact/threshold if relevant.

## Steps
1. `T-1`: add a `conftest.py` with an autouse fixture that runs each test from a
   temp CWD (or otherwise guarantees `configs/dashboard.yaml` in the repo root is
   never read) and clears `TRADING_BOT_UI_TOKEN`/related env so the dashboard/serve
   tests cannot pick up a developer's local manifest. Confirm the 5 offending tests
   (`test_dashboard.py` around the `_DEFAULT_MANIFEST` path) pass even when a
   `configs/dashboard.yaml` exists in the repo root.
2. `T-2`: remove `--exitfirst` from `addopts` so a full run reports all failures
   (keep it available ad-hoc via `-x`).
3. `T-3`: add `--cov-fail-under=<N>` (pick a floor at/below the current 96%, e.g.
   90) so coverage regressions fail the gate.

## Tests
- Add a test proving hermeticity: with a dummy `configs/dashboard.yaml` present in
  the repo root, the dashboard default-manifest tests still use the intended
  fixture manifest.
- Full suite green with the new addopts.

## Verification on real data
Offline only. Create a throwaway `configs/dashboard.yaml` in the repo root, run the
full suite, confirm 0 failures (proving isolation), then remove it. No broker.

## Closeout
CHANGELOG (Fixed): hermetic test gate — autouse CWD/env isolation, drop
`--exitfirst`, enforce coverage floor. ADR: the gate was red locally / green in CI
(non-hermetic); decision to isolate CWD/env in a repo `conftest.py`.
