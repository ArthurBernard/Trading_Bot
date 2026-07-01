---
plan: unified-dashboard/03-strategies-page
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/dashboard-strategies
pr: ""
---

# Unified dashboard — Strategies control page (start/stop/mode, auth)

## Goal

The Strategies page: the full control surface — per-strategy **start/stop**, **mode**
switch (paper/testnet/live), grouped **by exchange**, with the typed **live-confirm
modal** — rebuilt in the leaf-01 shell, plus the token **auth** (login/logout,
sign-out in the shell header). This ports everything the old `create_control_app` +
`control.html`/`control.js` did into the unified dashboard, preserving the safety
gates.

## Files to change

- `trading_bot/interfaces/api/app.py` — in `create_dashboard_app`: `GET
  /api/strategies` (per-strategy status incl. `exchange`), `POST
  /api/strategies/{name}/start`, `.../stop`, `.../mode` (module-level `_ModeBody`,
  live→403 without `confirm:true`). These already exist in `create_control_app`;
  move/share them so the dashboard app owns them (delete the duplication once leaf 04
  retires `create_control_app`). Guard writes when `read_only=True` (405/403).
- `trading_bot/interfaces/ui/templates/strategies.html` — the control page in the
  shell: a table grouped by exchange (group header rows), mode `<select>`, start/stop
  buttons, and the live-confirm modal (type `I UNDERSTAND` → `confirm:true`). Port the
  logic from `static/control.js` into the page's `{% block scripts %}` (or a slim
  `static/strategies.js`), using the `base.html` helpers.
- `trading_bot/interfaces/ui/templates/base.html` — add the sign-out control in the
  header when `auth` is enabled (moved from `control.html`).
- `trading_bot/tests/interfaces/test_dashboard.py` — extend (was
  `test_control_api.py`'s coverage).

## Steps

1. Read the current `create_control_app` endpoints + `_ModeBody` + `_status_dict`
   (with `exchange`) and `static/control.js` (exchange grouping + modal +
   `confirm:true`). Read `_install_control_auth` (already reused in leaf 01).
2. Add the `/api/strategies` + start/stop/mode endpoints to `create_dashboard_app`
   (share the handlers with — or lift them from — `create_control_app`; the latter is
   retired in leaf 04). Keep the **live gate**: `mode=="live"` without `confirm:true`
   → `LiveTradingNotEnabled` → 403, changing nothing. When `read_only`, refuse writes.
3. Build `strategies.html`: fetch `/api/strategies`, render exchange-grouped rows with
   mode select + start/stop, wire the modal for the live switch (typed confirmation),
   post to the endpoints, refresh on success + on a short poll.
4. Add the sign-out form to `base.html` header (`{% if auth %}`).
5. `pytest` + `ruff` + `mypy` green under `trading_bot_env`.

## Tests

- `/api/strategies` lists the supervised strategies with `exchange`; start/stop change
  status; `mode` to paper/testnet works; `mode` to live without `confirm` → 403 (no
  change); with `confirm:true` → attempted (paper/testnet fixtures). Page renders the
  grouped table + modal markup. `read_only=True` → writes refused.
- Auth: page redirects to `/login` without a session; login sets the cookie; the
  control endpoints require it; rate-limit still applies.

## Verification on real data

Run `trading-bot dashboard -c strategies/alloc1/binance.yaml`. In the browser (or via
`curl` with a session): the Strategies page lists **alloc1** under its **binance**
group; **start** it and confirm the Overview positions begin to reflect it; switch
**paper→testnet** and confirm it is accepted; attempt **→live** and confirm the modal
/ endpoint **refuses without the typed confirmation** (403, nothing changes). **Paper
/ testnet only — never send a real order.**

## Closeout

- CHANGELOG (Added): "Dashboard Strategies page — start/stop/mode control grouped by
  exchange, live-confirm modal, token auth — in the unified shell."
- ADR only if a non-trivial choice arises (e.g. read-only write-guard semantics).
- Do NOT remove the roadmap line (deferred to leaf 04).
