# 06 — Status

_Last updated: 2026-06-28_

## Where things stand

**The E1–E10 rewrite is complete — the engine is feature-complete and hardened.**
Phase 0 (tooling) plus all ten epics shipped: `domain/` (pure, mypy-strict),
`transport/` (http/ws/ratelimit), `brokers/` (`Broker` port + `KrakenBroker` REST+WS +
`BinanceBroker` REST + port-pure `PaperBroker`), `storage/` (`SqliteStore`, money as TEXT), `application/`
(declarative `AppConfig`+`EventBus`, idempotent risk-gated `OrderRouter`,
`PositionTracker`, `reconcile`, `Strategy`+safe loader, causal `DataFeed`+`feed_for`,
`StrategyRunner`, `PerformanceService`, `RiskManager`+kill-switch, `Orchestrator`,
`run_app`, `service_factory`), and `interfaces/` (Typer `trading-bot` CLI +
read-only FastAPI `api`/Jinja2 `ui`). One `AppConfig` conducts the triptych — dccd
data (library import) + fynance signals + brokers — and `trading-bot run <config>`
runs the whole declared multi-strategy system (paper by default); `trading-bot serve`
exposes the read-only dashboard. The money-safety invariants (reconcile convergence,
idempotency, kill-switch) are **proven under fault injection** (`tests/hardening/`).
**Live trading is off by default** behind an explicit `live_enabled` opt-in +
credentials + the go-live runbook (`doc/dev/09-go-live.md`) — **no real order is ever
sent from the repo**. ~617 tests green via the project `.venv`; ruff + mypy clean
across the whole package.

**Post-0.2.0 — E11 (Binance) shipped:** `BinanceBroker` (spot REST) is the **2nd live
venue** behind the `Broker` port (HMAC-SHA256 signing vs Binance's vector; composite
venue-id for symbol-scoped cancel; `newClientOrderId` idempotency; **testnet-capable**
with an opt-in real round-trip E2E on `testnet.binance.vision`). Public market data is
key-free; the private path is mock+vector+testnet-E2E proven. This is the execution
venue for the **multi-asset / LS1** epic. Mainnet real-key enablement stays deferred
behind the opt-in.

**Post-0.2.0 — multi-asset / portfolio-strategy unit shipped:** a native
`PortfolioStrategy`/`PortfolioRunner` drives a whole universe from a **weight vector**
(`PortfolioSignalFn` → `weights_to_signals` → N idempotent risk-gated maker-LIMIT legs),
fed by a common-index, freshness-gated `PortfolioFeed` over the dccd Binance store
(daily via the resample-on-read `ResamplingDccdClient`). **LS1 is runnable by config**
(`configs/ls1.yaml` + `examples/ls1_signal.py`, `fynance_research` lazily imported) —
delta-correctness verified on **real** dccd Binance bars; an opt-in Binance **testnet**
rebalance round-trip is ready (gated on a testnet key). The engine stays generic (LS1 is
config + a generic weight-oracle adapter). Two follow-ups are tracked in `07-roadmap.md`
(engine O(n²) drain; a dccd `inventory()` API drift in two network tests).

**Remaining (maintainer decisions, see `07-roadmap.md`):** the **final project name**
(kept `trading_bot`), and **real-key live enablement** (validate Kraken private
endpoints + venue-level idempotency against a real-key sandbox, then flip
`live_enabled`).

### Bootstrap (Phase 0, historical)

The dccd/fynance developer standard: `pyproject.toml`, ruff/mypy/pytest/interrogate,
pre-commit, GitHub Actions CI (3.11–3.13), Git Flow (`develop`/`master`), `CLAUDE.md`,
`.claude/` workflow + hooks, and this `doc/dev/` pack.

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
