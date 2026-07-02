---
plan: audit-remediation-wave3/05-tooling-ci-packaging
kind: leaf
status: executing
complexity: medium
depends: []
parallel: true
branch: chore/tooling-ci-packaging
pr: ""
---

# Leaf 05 — tooling-ci-packaging

## Goal
Close the tooling / CI / packaging hygiene findings. Fixes `T-5`, `T-6`, `T-7`,
`T-8`, `T-9`, `T-10`, `T-11`, `G-13`.

## Files
`pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`,
`MANIFEST.in`, `CONTRIBUTING.md`, `CLAUDE.md`/`README.md` (extras note), a flaky test.

## Steps
- `T-5`: pre-commit↔CI parity — CI runs `ruff check` only; add `ruff format --check`
  (+ ideally mypy) so a pre-commit auto-format can't wedge CI. Align the two.
- `T-6`: remove the dead `recursive-include trading_bot *.ini` from `MANIFEST.in`.
- `T-7`: pin CI actions by SHA (not moving `@v4`/`@v5`) and note a dependency
  pinning/lock posture (supply-chain).
- `T-8`: add `filterwarnings` + `asyncio_default_fixture_loop_scope` to the pytest
  config (police warnings; pin the asyncio loop scope for pytest-asyncio).
- `T-9`: the one real wall-clock `asyncio.sleep(0.12)` test — make it deterministic
  (event/barrier instead of a timing sleep).
- `T-10`: `interrogate` is configured (`fail-under=80`) but run by nothing — wire it
  into pre-commit/CI or drop the config.
- `T-11`: fix the misleading `parents[3]` repo-root-walk comment (source-checkout-only).
- `G-13`: align the install-extra inconsistency (`[dev,daemon]` vs `[dev]`) between
  `CLAUDE.md` and `README.md`.

## Tests
`python -m pytest` still green with the new pytest config; `ruff format --check`
passes on the tree; the de-flaked test is deterministic.

## Verification
Run the full offline suite + `ruff check` + `ruff format --check` + `mypy` locally;
confirm `MANIFEST`/CI changes are coherent (a `python -m build` sdist still includes
templates+static — don't regress packaging).

## Closeout
CHANGELOG (Changed). No ADR (mechanical tooling alignment) unless the dep-pinning
posture warrants a note.
