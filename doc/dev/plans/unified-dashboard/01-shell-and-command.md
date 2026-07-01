---
plan: unified-dashboard/01-shell-and-command
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: feat/dashboard-shell
pr: ""
---

# Unified dashboard — app factory + dccd-style shell + `dashboard` command

## Goal

The foundation: one `create_dashboard_app(supervisor, *, auth_token=None,
read_only=False)` FastAPI factory that will host both monitoring and control, a
dccd-style `base.html` shell (all shared CSS + JS helpers in one file, a nav across
Overview / Strategies / Orders / PnL / Logs, brand + version + health chip + connection
dot), and a single `trading-bot dashboard` command that serves it and **quits
cleanly on Ctrl-C**. This leaf ships the shell + stub pages + health only — the page
data lands in leaves 02-04. No behaviour of the existing `serve` / `start --serve`
is removed yet (retired in leaf 04).

## Files to change

- `trading_bot/interfaces/api/app.py` — add `create_dashboard_app(supervisor, *,
  auth_token=None, read_only=False)`. Reuse the existing `_install_control_auth`
  (login/logout/session/rate-limit) and the `_DecimalJSONResponse`. `GET /` renders
  the Overview shell; `GET /strategies`, `/orders`, `/logs` render their (stub) page
  shells; `GET /api/health` returns `{status, mode, strategies, read_only}`. Mount
  `/static` + `Jinja2Templates` once. Set `app.state.supervisor`,
  `app.state.read_only`, `app.state.auth_enabled`.
- `trading_bot/interfaces/ui/templates/base.html` — **new**. The shared shell,
  ported from dccd's `base.html` structure: `<head>` + all CSS (move the useful
  parts of `static/style.css` into a `<style>` block, dccd-style dark theme), a top
  nav with links + active-tab highlight, brand + `v{{ version }}` + a health chip +
  connection dot, and a `<script>` block of shared helpers (`api()` fetch wrapper,
  `fmtNum/fmtMoney/fmtDate`, `toast()`, `initNav()`, an SSE `connect()` helper).
  `{% block content %}` + `{% block scripts %}` for pages.
- `trading_bot/interfaces/ui/templates/overview.html`, `strategies.html`,
  `orders.html`, `pnl.html`, `logs.html` — **new**, each `{% extends "base.html" %}`
  with a stub `{% block content %}` (empty containers + a heading) for now.
- `trading_bot/interfaces/cli/main.py` — add a `dashboard` command:
  `--config/-c`, `--host` (default `127.0.0.1`), `--port` (default `8000`),
  `--token` (envvar `TRADING_BOT_UI_TOKEN`), `--read-only`. Builds the
  `StrategySupervisor` from config (or a paper default), refuses a non-loopback host
  without a token (same guard as `start`), and serves `create_dashboard_app(...)`.
  **Clean shutdown**: run uvicorn so Ctrl-C reliably stops it and the supervisor is
  shut down in a `finally` (see Steps for the signal handling).
- `pyproject.toml` — ensure the new templates ship (`[tool.setuptools.package-data]`
  already covers `interfaces/ui/templates/*` + `static/*`; extend the glob if needed).
- `trading_bot/tests/interfaces/test_dashboard.py` — **new**.

## Steps

1. Read dccd's shell for the exact recipe:
   `../Download_Crypto_Currencies_Data/dccd/interfaces/ui/templates/base.html`
   (nav, the `<style>` block, the shared `<script>` helpers, how pages extend it),
   and how `dccd/interfaces/api/app.py` mounts Jinja2 + `/static` and renders pages.
   Read the current `trading_bot/interfaces/api/app.py` (`create_app`,
   `create_control_app`, `_install_control_auth`, `_status_dict`) to reuse, not
   duplicate.
2. Write `base.html` as the single shell (nav + brand + version + health chip +
   connection dot; all CSS in one `<style>`; shared JS helpers in one `<script>`).
   Port the useful rules from `static/style.css` into the `<style>` block (badges,
   run-pill, buttons, modal, table, login) so `base.html` is self-contained like
   dccd's; keep `static/` for any assets (favicon) only.
3. Add `create_dashboard_app(supervisor, *, auth_token, read_only)`: mount templates
   + `/static`; `GET /` → `overview.html`; `GET /strategies|/orders|/logs` → their
   shells; `GET /api/health` → `{status:"ok", mode, strategies: supervisor.names()
   length, read_only}`. When `auth_token`, call `_install_control_auth`. Pass
   `{version, read_only, auth}` to every template render.
4. Add the `dashboard` CLI command. For **clean Ctrl-C**: prefer
   `uvicorn.run(app, host=host, port=port)` (uvicorn owns SIGINT and returns on
   Ctrl-C) wrapped so the supervisor is built before and `supervisor` +
   any scheduler are shut down after (a `try/finally` around `uvicorn.run`). If a
   supervisor must run concurrently (scheduler ticks), use `uvicorn.Server` with
   `install_signal_handlers=True` and **do not** also register competing
   `loop.add_signal_handler(SIGINT, …)` (that override is what makes `start --serve`
   feel unquittable) — let uvicorn own SIGINT, and drain the supervisor in `finally`.
   For this leaf a scheduler is not required (control-only), so `uvicorn.run` is
   fine; document the choice.
5. `pytest` (`fastapi.testclient.TestClient`): the app builds; `GET /` and each page
   return 200 and contain the nav; `GET /api/health` returns the expected JSON;
   `--read-only` sets `read_only:true`; auth path (token) redirects `/` to `/login`.
6. `ruff check trading_bot/` + `mypy trading_bot/` clean. Run all under the
   `trading_bot_env` pyenv env (`python -m pytest`).

## Tests

- `test_dashboard.py`: `create_dashboard_app(supervisor)` — `GET /`, `/strategies`,
  `/orders`, `/logs` each 200 + contain the shared nav; `/api/health` JSON shape
  (`status/mode/strategies/read_only`); `read_only=True` reflected; with `auth_token`
  the page redirects to `/login` and `/api/health` needs auth (401 without).
- A CLI test that `dashboard` builds the app and calls `uvicorn.run` (patched), like
  the existing `serve` test, asserting host/port + the non-loopback-without-token
  refusal.

## Verification on real data

Launch it for real against the **alloc1 paper** config:
`trading-bot dashboard -c strategies/alloc1/binance.yaml` (loopback). Confirm:
`curl http://127.0.0.1:8000/` returns the shell HTML with the nav; `curl
http://127.0.0.1:8000/api/health` returns `{"status":"ok",...}`; the five pages load;
**Ctrl-C stops the process cleanly the first time** (prints a shutdown line, exits 0
— the key UX fix; the maintainer hit an orphaned, unstoppable `start` process).
Confirm a second run under systemd (`deploy/trading-bot.service`, adjusted
`ExecStart`) starts + `systemctl --user stop` stops cleanly. No order is placed
(paper; this leaf has no controls yet).

## Closeout

- CHANGELOG (Added): "Unified dashboard skeleton — one `create_dashboard_app` +
  dccd-style `base.html` shell + a `trading-bot dashboard` command (clean Ctrl-C)."
- ADR: record the decision to **merge the two web apps into one** (read-only as a
  runtime posture, not a second app) and the Ctrl-C/signal-handling choice (let
  uvicorn own SIGINT).
- Do NOT remove the roadmap line (deferred to leaf 04, the last leaf).
