# 06 — Status

_Last updated: 2026-06-20_

## Where things stand

**Phase 0 (bootstrap) — done / in this branch.** The repo now has the dccd/fynance
developer standard: `pyproject.toml`, ruff/mypy/pytest/interrogate, pre-commit,
GitHub Actions CI (3.11–3.13), Git Flow (`develop`/`master`), `CLAUDE.md`,
`.claude/` workflow + hooks, and this `doc/dev/` pack. The package imports and a
smoke test passes.

**Foundation (E1–E3) and the execution engine (E4) are complete.**
`trading_bot/domain/` (money, instrument, errors, order, fill, position, signal,
performance — pure, mypy-strict), `trading_bot/transport/` (`AsyncHTTPClient`,
`WebSocketBase`, `RateLimiter` + `KrakenCallCounter`), `trading_bot/brokers/` (the
`Broker` port + registry + `KrakenBroker` REST + `KrakenPrivateWS` + the **port-pure
`PaperBroker`**), and `trading_bot/application/` (`AppConfig` + `EventBus`,
`OrderRouter` with idempotent submit, `PositionTracker` from fills, `reconcile`) are
in — the whole order→fill→position→reconcile path runs in-process. The later layers
(strategy runner, storage, perf/risk, interfaces) are pending — next is **E5
(strategy runner)**. See `07-roadmap.md` /
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
- **dccd↔trading_bot orchestration depth** (library import vs driving a service) —
  decided when the orchestration epic (E8) is planned.
- **`trading-bot` console script** — not declared until the CLI module exists (E7).
- **`AddOrder` idempotency at the venue** — the transport retries POSTs on
  5xx/network errors, but `AddOrder` carries no venue idempotency key, so a retry
  after an *ambiguous* failure (order placed, response lost) could double-submit.
  Today idempotency is engine-side only (`OrderRouter` client-order-id dedup);
  venue-level idempotency / reconcile-on-ambiguous-failure is go-live hardening
  (groundwork in E4's order-router, finished in E10).
