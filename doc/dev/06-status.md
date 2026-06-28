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

Only **E10-03** (go-live runbook + an explicit, off-by-default live opt-in guard) — see
[`07-roadmap.md`](07-roadmap.md). After that the rewrite is feature-complete; real live
trading still needs a real-key sandbox validation, and the **final project name** stays
deferred.

## Known gaps / deferred

- **Final project name** — kept as `trading_bot` for now (deferred decision).
- **Default paper-vs-live beyond MVP** — paper-first; live behind an explicit
  off-by-default opt-in (E10-03).
- ~~**KPI ratios need a positive starting capital**~~ — **resolved (E10)**:
  `AppConfig.starting_capital` (default 100000) is wired into `PerformanceService(v0=)`,
  so the ratios are meaningful; CLI `kpi --capital` overrides it.
- ~~**Same-instrument strategies commingle in `run_app`**~~ — **resolved (E10)**:
  `build_runners` now **rejects** two strategies on the same symbol (`ConfigError`,
  alias-aware). A per-strategy book (to *allow* it) remains a future refinement.
- ~~**dccd↔trading_bot orchestration depth**~~ — **resolved (E8)**: library import,
  not a service (`feed_for` uses `dccd.Client.read`/`backfill` in-process). See ADR.
- **`AddOrder` idempotency at the venue** — *transport guard added (E10)*:
  `place_order` now sends `AddOrder` **at most once** (`post(retry=False)` → raises
  `AmbiguousRequestError` so the caller reconciles before retrying), closing the
  blind-retry double-submit window. Still **not fully closed**: there is no
  *venue-side* dedup token, so a retry the engine *forgot* (e.g. a crash before the
  reject was persisted) could still double-submit — that needs a real-key sandbox to
  build/validate (the only live-trading prerequisite left).
