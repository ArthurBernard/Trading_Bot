---
plan: strategy-capital/01-retire-legacy-dashboard
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: chore/retire-legacy-dashboard
pr: ""
---

# 01 — Retire the legacy single-engine dashboard

## Goal

One dashboard code path. Today two parallel UIs ship: the legacy nav-less
`dashboard.html` + `static/app.js` + `static/style.css` served by
`create_app(engine)` (`interfaces/api/app.py:596`, reached only via
`trading_bot run --serve`), and the unified 5-tab shell served by
`create_dashboard_app(...)` (everything else: `serve`, `dashboard`,
`start --serve`). The legacy page has no navigation, a palette that has drifted
from `base.html`, and duplicates every API route with divergent payload shapes.
Retire it.

## Files to change

- `trading_bot/interfaces/cli/main.py` — remove the `--serve`/`--host`/`--port`
  options from the `run` command and delete `_run_and_serve` (≈ lines 442–480).
  `run` keeps its console-report behaviour. The option's help/error text points
  users at `trading-bot start --serve` / `trading-bot dashboard`.
- `trading_bot/interfaces/api/app.py` — delete `create_app` (from `def create_app`
  at ≈596 up to, not including, the shared serializer helpers reused by the
  dashboard app) and every legacy-only helper it alone used (e.g. `_safe_ratio`
  if only `create_app` consumes it — grep first). Update the module docstring
  (lines 3, 47) which still describes `create_app` as the entrypoint.
- `trading_bot/interfaces/api/__init__.py` — drop the `create_app` export and
  rewrite the docstring around `create_dashboard_app`.
- `trading_bot/interfaces/ui/templates/dashboard.html` — delete.
- `trading_bot/interfaces/ui/static/app.js` — delete.
- `trading_bot/interfaces/ui/templates/login.html` — inline the login styles it
  needs (`.login-body`, `.login-card`, `.card`, `.brand-group`, `.login-err`,
  input/button rules — copy from `static/style.css` / `base.html`'s inline CSS)
  into a `<style>` block, then drop its `<link rel="stylesheet"
  href="/static/style.css">`.
- `trading_bot/interfaces/ui/static/style.css` — delete once nothing references
  it (`grep -rn "style.css" trading_bot/` must come back empty).
- `trading_bot/interfaces/ui/__init__.py` — update docstrings/comments naming
  `app.js`/`style.css`.
- Tests: `trading_bot/tests/interfaces/test_api.py` and `test_ui.py` (both built
  on `create_app`) — port any coverage that is **not** already exercised against
  `create_dashboard_app` in `test_dashboard.py`/`test_control_api.py` (grep for
  equivalent assertions first), then delete the legacy-only tests.
  `trading_bot/tests/application/test_run_app.py:815-828` (run+serve test) —
  delete or retarget to the removed-flag error path.

## Steps

1. Grep all `create_app` / `dashboard.html` / `static/app.js` / `style.css`
   consumers; build the exact kill-list before editing.
2. Inline login styles; verify the login page renders styled with `style.css`
   gone (serve locally, GET `/login`).
3. Remove `run --serve` (+ `_run_and_serve`), delete `create_app` and dead
   helpers, delete the three static/template files.
4. Port unique test coverage to the dashboard-app test files; delete legacy
   tests; fix imports.
5. `python -m pytest` and `ruff check trading_bot/` green; `grep -rn
   "create_app\b" trading_bot/` returns only `create_dashboard_app` matches.

## Tests

- Existing `test_dashboard.py` / `test_control_api.py` suites stay green.
- New/ported: `/login` serves styled HTML with no `/static/style.css` reference;
  `run` without `--serve` unchanged; `run --serve` is an unknown-option error
  (Typer exit code 2).

## Verification on real data

Restart the daemon on the real local manifest (`configs/dashboard.yaml`, paper,
real dccd data): `trading-bot start --serve`, log in, click through Overview /
Strategies / Orders / PnL / Logs — all styled, health chip live, no 404 on any
static asset. Confirm `trading-bot run -c <config>` (a short finite paper run)
still prints its console report.

## Closeout

- CHANGELOG (`[Unreleased]` / Removed): legacy single-engine dashboard
  (`create_app`, `dashboard.html`, `app.js`, `style.css`) and `run --serve` —
  the unified dashboard (`start --serve` / `dashboard` / `serve`) is the one
  code path.
- ADR note: one dashboard code path; `run` is console-only.
- Tick leaf 01 in `00-plan.md`.
