# 06 — Status

_Last updated: 2026-06-20_

## Where things stand

**Phase 0 (bootstrap) — done / in this branch.** The repo now has the dccd/fynance
developer standard: `pyproject.toml`, ruff/mypy/pytest/interrogate, pre-commit,
GitHub Actions CI (3.11–3.13), Git Flow (`develop`/`master`), `CLAUDE.md`,
`.claude/` workflow + hooks, and this `doc/dev/` pack. The package imports and a
smoke test passes.

**E1–E7 are complete — the MVP "first light" is reached.** The engine runs from the
command line: `trading-bot run` runs a fynance-backed strategy over a `DataFeed`,
routing **risk-gated** orders through the `OrderRouter` to a (paper) broker and
reporting positions / PnL / KPIs; `status`/`kpi` read a persisted `SqliteStore`. Layers:
`domain/` (pure, mypy-strict), `transport/` (http/ws/ratelimit), `brokers/` (`Broker`
port + `KrakenBroker` REST+WS + port-pure `PaperBroker`), `storage/` (`SqliteStore`,
money as TEXT), `application/` (`AppConfig`+`EventBus`, idempotent risk-gated
`OrderRouter`, `PositionTracker`, `reconcile`, `Strategy`+safe loader, causal
`DataFeed`, `StrategyRunner`, `PerformanceService`, `RiskManager`+kill-switch,
`Orchestrator`, `service_factory`), and `interfaces/cli/` (Typer `trading-bot`). The
pre-2026 `legacy/` tree is **deleted** (history in git); the whole package is
linted/typed/tested (no exclusions). 461 tests green via the project `.venv`.

Pending: **E8** (triptych orchestration — one config wiring dccd+fynance+brokers), E9
(web UI), E10 (go-live hardening + final name). Next is **E8**. See `07-roadmap.md` /
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
- ~~**dccd↔trading_bot orchestration depth**~~ — **resolved (E8)**: library import,
  not a service (`feed_for` uses `dccd.Client.read`/`backfill` in-process). See ADR.
- **`AddOrder` idempotency at the venue** — the transport retries POSTs on
  5xx/network errors, but `AddOrder` carries no venue idempotency key, so a retry
  after an *ambiguous* failure (order placed, response lost) could double-submit.
  Today idempotency is engine-side only (`OrderRouter` client-order-id dedup);
  venue-level idempotency / reconcile-on-ambiguous-failure is go-live hardening
  (groundwork in E4's order-router, finished in E10).
