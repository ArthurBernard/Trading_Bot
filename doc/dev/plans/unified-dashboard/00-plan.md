---
plan: unified-dashboard
kind: global
status: planning
roadmap: "**Unified dccd-style dashboard.** One FastAPI app (monitor + control), one `dashboard` command, a shared `base.html` shell, SSE live, KPI per strategy/exchange/total, a per-strategy PnL chart (live vs testnet) — replacing the split read-only/control apps."
release_on_done: true
---

# Unified dccd-style dashboard

## Goal

Replace trading_bot's **two split web apps** — `create_app` (read-only:
positions/orders/KPI/SSE, launched by `serve` / `run --serve`) and
`create_control_app` (read+write: strategies start/stop/mode + login, launched by
`start --serve`) — with **one** cohesive FastAPI dashboard that both **monitors and
controls**, modelled on dccd's UI recipe (one `base.html` shell with all shared CSS +
JS helpers, small per-page Jinja shells whose vanilla JS fetches `/api/*`, SSE where
live matters). Served by **one** command (`trading-bot dashboard`) that **quits
cleanly on Ctrl-C** (the current split feels unquittable / orphans the process).

The bones are already right (hexagonal, real API with `/docs`); this epic fixes the
**UI layer + launch UX** and adds the features the maintainer asked for. Reference:
dccd `../Download_Crypto_Currencies_Data/dccd/interfaces/{api/app.py, ui/templates/base.html, ui/static}`.

## Target feature set (maintainer requirements)

- **Strategies**: start/stop, switch mode **live or test(net)**, grouped by exchange.
- **KPI**: PnL / fees / Sharpe-Sortino-Calmar-maxDD at **three levels — per strategy,
  per exchange, total**.
- **Positions**: net per crypto, groupable **by crypto and/or exchange**.
- **Orders / Fills**: open + history, filterable by crypto / exchange / strategy.
- **PnL chart**: a per-strategy equity/PnL time-series, with **live vs live-test
  (testnet) drawn as separate series**.
- **Logs**: live activity feed (SSE).

## Design decisions (chosen with the maintainer)

- **Charting**: **uPlot**, self-hosted (~40KB, MIT) vendored into `static/` — no npm /
  build. Small departure from dccd's zero-JS-file purity, bought for zoom / tooltips /
  multi-series. [decision]
- **PnL time-series**: **realised PnL derived from fills** (the source of truth),
  **tagged by mode** so **live and testnet are separate series** (testnet is fake
  money — never combined) + a current unrealised (mark-to-market) end point. Full
  continuous mark-to-market is out of scope for v1. [decision]
- **Run/stop model**: a clean **foreground** `dashboard` (reliable Ctrl-C) + the
  existing **systemd** service for persistent running (`systemctl start/stop/status`).
  No half-baked home-grown daemon. [decision]
- **One app, read-only as a runtime posture** (`--read-only` / no supervisor), not a
  second app. [decision]

## Decomposition

1. **01 — shell + command + clean run/stop**: `create_dashboard_app(supervisor, *,
   auth_token, read_only)` + a dccd-style `base.html` shell (nav: Overview /
   Strategies / Orders / PnL / Logs) + the single `trading-bot dashboard` command with
   **reliable Ctrl-C** and a documented systemd path. Stub pages. Foundation.
2. **02 — Overview + KPI (3 levels)**: supervisor accessors aggregating across the
   per-strategy engines; Overview page with **KPI cards at per-strategy / per-exchange
   / total**, a positions table groupable by crypto/exchange, live via merged SSE.
3. **03 — Strategies page**: start/stop/mode (live or test), grouped by exchange, the
   typed live-confirm modal, token auth (login/logout) — in the shell. (+ a paper unit
   restores its book from the store on start; the command `start_all()`s.)
4. **04 — Manage strategies from the UI**: the dashboard owns a **persistent manifest**
   (`configs/dashboard.yaml`, the default for `trading-bot dashboard`); `POST
   /api/strategies` (create) / `DELETE /api/strategies/{name}` / `GET /api/signals`;
   dynamic `supervisor.add_unit`/`remove_unit`; the UI **deploys existing signals**
   (never authors signal code). One dashboard common to all strategies it declares,
   persisted across restarts.
5. **05 — PnL time-series data model**: **tag fills with mode + venue** in the store;
   derive a per-strategy, per-mode realised-PnL/equity curve; `GET /api/pnl` returning
   the live + testnet series (+ current unrealised point). Backend for the chart and
   for aggregate ratio KPIs.
6. **06 — PnL chart (uPlot)**: vendor uPlot into `static/`; a PnL/equity chart on the
   PnL page (and a per-strategy panel) drawing **live vs testnet as separate series**,
   fed by `/api/pnl`; wire the aggregate ratio KPIs on the combined curve.
7. **07 — Orders/Fills filtering + Logs + retire the split**: orders/positions filters
   (crypto / exchange / strategy), a fills-history + Logs (SSE) page; retire
   `serve` / `start --serve` onto `dashboard`; update deploy docs. Last leaf.

## Leaf checklist

- [x] 01 shell-and-command — feat/dashboard-shell — high
- [x] 02 overview-kpi — feat/dashboard-overview — high
- [x] 03 strategies-page — feat/dashboard-strategies — medium
- [x] 04 strategy-management — feat/dashboard-strategy-mgmt — high
- [ ] 05 pnl-data-model — feat/dashboard-pnl-data — high
- [ ] 06 pnl-chart — feat/dashboard-pnl-chart — medium
- [ ] 07 orders-logs-cleanup — feat/dashboard-orders-logs — medium

## Dependencies

- 01 is the foundation. **04 depends on [03]** (extends the Strategies page + the
  supervisor). **05 depends on [01]**; **06 depends on [05]**; **07 depends on
  [01..06]** (last — retires the split once every page exists).
- Run **serially** (leaves share `base.html` nav + the app factory + the
  supervisor/store — safer than parallel worktrees).

## Done criteria

- One `trading-bot dashboard -c <cfg>` serves a single dashboard that monitors AND
  controls; **Ctrl-C quits cleanly**; systemd path documented.
- KPI at per-strategy / per-exchange / total; positions & orders groupable by crypto /
  exchange; a per-strategy PnL chart with **live vs testnet separate**.
- Old `serve` / `start --serve` split retired or aliased; deploy docs updated.
- Safety gates preserved: live needs typed confirmation + `confirm:true` (else 403);
  loopback default; non-loopback requires a token.
- Verified against the **alloc1 paper** store / a running supervisor — paper/testnet
  only, **no real order**. Full suite + ruff + mypy green under `trading_bot_env`.
