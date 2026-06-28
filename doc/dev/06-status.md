# 06 — Status

_Last updated: 2026-06-20_

## Where things stand

**Phase 0 (bootstrap) — done / in this branch.** The repo now has the dccd/fynance
developer standard: `pyproject.toml`, ruff/mypy/pytest/interrogate, pre-commit,
GitHub Actions CI (3.11–3.13), Git Flow (`develop`/`master`), `CLAUDE.md`,
`.claude/` workflow + hooks, and this `doc/dev/` pack. The package imports and a
smoke test passes.

**E1–E9 are complete — only go-live hardening (E10) remains.** Trading_Bot conducts
the triptych: one `AppConfig` declares data (dccd) + strategies (fynance) + brokers +
risk; `trading-bot run <config.yaml>` runs the whole declared multi-strategy system
(paper by default) via `run_app` → one shared engine → a `StrategyRunner` per strategy
through the `Orchestrator`; and `trading-bot serve` exposes a **read-only** web
dashboard (FastAPI `/api/*` + SSE, Jinja2 UI as a pure HTTP client — money as Decimal
strings, never trades). dccd is integrated by **library import** (`feed_for`). Layers:
`domain/` (pure, mypy-strict), `transport/` (http/ws/ratelimit), `brokers/` (`Broker`
port + `KrakenBroker` REST+WS + port-pure `PaperBroker`), `storage/` (`SqliteStore`,
money as TEXT), `application/` (declarative `AppConfig`+`EventBus`, idempotent
risk-gated `OrderRouter`, `PositionTracker`, `reconcile`, `Strategy`+safe loader,
causal `DataFeed`+`feed_for`, `StrategyRunner`, `PerformanceService`, `RiskManager`+
kill-switch, `Orchestrator`, `run_app`, `service_factory`), `interfaces/` (Typer
`trading-bot` CLI + read-only FastAPI `api`/Jinja2 `ui`). The pre-2026 `legacy/` tree
is deleted; the whole package is linted/typed/tested. **521 tests** green via the
project `.venv`.

Pending: **E10** — go-live hardening (prove reconciliation / kill-switch / idempotency
under fault injection; resolve the venue-idempotency + KPI-v0 + same-instrument known
gaps; explicit live enablement) and the **final project name**. Next is **E10**. See
`07-roadmap.md` /
`08-program-plan.md`.

## Done

- Legacy implementation parked under `trading_bot/legacy/` (excluded from tooling).
- Modern packaging + tooling + CI + Git Flow.
- Claude Code workflow wired (`/pick-task` … `/release` resolve against this repo).
- Developer brief (`doc/dev/`) and rewrite roadmap.
- **E1 — Domain core**: `domain/` (money, instrument, errors, order, fill,
  position, signal, performance) — pure, mypy-strict, tested.

## Pending

Everything remaining in [`07-roadmap.md`](07-roadmap.md): the
Kraken broker + paper broker, the order router, the strategy runner, performance/
risk, the CLI, the orchestration layer, and (later) the UI and go-live hardening.

## Known gaps / deferred

- **Final project name** — kept as `trading_bot` for now (deferred decision).
- **Default paper-vs-live beyond MVP** — paper-first for now; revisit at go-live.
- **KPI ratios need a positive starting capital** — `service_factory.build_engine`
  wires `PerformanceService(v0=0)`, and fynance refuses an equity curve that crosses
  zero, so the Sharpe/Sortino/Calmar shown by the API (and the CLI `kpi` absent
  `--capital`) degrade to `0.0`. Robust (never errors), but the ratios are meaningless
  until a config-driven starting capital is wired into `build_engine` (small follow-up).
- **Same-instrument strategies commingle in `run_app`** — the orchestrated system
  shares one engine, and the `PositionTracker`/`PerformanceService` key state **by
  instrument**, so two strategies declared on the *same* symbol fold their fills into
  one shared position/PnL (silently commingled). Fine for the nominal one-strategy-
  per-symbol case; per-strategy attribution (or rejecting duplicate symbols) is a
  later refinement. Distinct-symbol strategies are fully isolated.
- ~~**dccd↔trading_bot orchestration depth**~~ — **resolved (E8)**: library import,
  not a service (`feed_for` uses `dccd.Client.read`/`backfill` in-process). See ADR.
- **`AddOrder` idempotency at the venue** — the transport retries POSTs on
  5xx/network errors, but `AddOrder` carries no venue idempotency key, so a retry
  after an *ambiguous* failure (order placed, response lost) could double-submit.
  Today idempotency is engine-side only (`OrderRouter` client-order-id dedup);
  venue-level idempotency / reconcile-on-ambiguous-failure is go-live hardening
  (groundwork in E4's order-router, finished in E10).
