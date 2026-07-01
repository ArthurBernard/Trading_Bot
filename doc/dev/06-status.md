# 06 — Status

_Last updated: 2026-06-29_

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
sent from the repo**. ~718 tests green under the `trading_bot_env` pyenv-virtualenv; ruff + mypy clean
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
(daily via the resample-on-read `ResamplingDccdClient`). A **concrete strategy** is
wired purely by reference (a signal wrapper + a config), via the generic
`as_portfolio_signal` adapter — and **kept local-only** under the gitignored
`strategies/` tree (never committed; the engine stays generic). On real dccd data the
routed per-coin deltas equal `weightᵢ × capital / priceᵢ`; opt-in venue order tests run
locally (Binance **testnet** with a key; Kraken public-data + PaperBroker, no real order).
One follow-up is tracked in `07-roadmap.md` (engine O(n²) drain; a dccd `inventory()`
API drift in two network tests).

**Post-0.2.0 — pre-production safety hardening (audit):** a full pre-prod audit found
the money-safety *machinery* existed and was hardening-tested but several pieces were
not wired into the run loop. Now closed: **reconcile-on-startup** runs before the first
order (`run_app`); the **daily-loss circuit breaker** is fed live PnL and a breach
escalates to the kill-switch (cancel resting + halt); **real-money live requires explicit
risk limits** (`max_order`/`max_position`/`max_daily_loss`, else a `BrokerError`); fills
are **de-duplicated by `fill_id`**; order dedup **survives a restart** (the router is
restored from the persisted store before the first order); and a portfolio's **dccd
store-key convention is pinned** by config (`store_key_format`). Hygiene in the same pass:
removed the dead `BrokerRegistry`, and the limit-at-close price is exact `Decimal` (no
float). Remaining venue-side idempotency token is the real-key-sandbox prerequisite.

**Remaining:** **real-key live enablement** (validate Kraken private endpoints +
venue-level idempotency against a real-key sandbox, then flip `live_enabled`) — the one
maintainer step left, see `07-roadmap.md`. The **project name is decided** (kept
`trading_bot`, with `dccd` / `fynance`; no rename). Engine layers, the triptych wiring
(one `AppConfig` →
`run_app` → one engine → runners via the `Orchestrator`; `trading-bot serve` for the
read-only dashboard), and the Phase-0 dev standard (packaging, CI 3.11–3.13, Git Flow,
`.claude/` workflow, this `doc/dev/` pack) are all in place — see the paragraphs above
and `CHANGELOG.md` for what shipped.

## Done

- The pre-2026 implementation is **deleted** (git history only; no in-tree `legacy/`).
- Modern packaging + tooling + CI + Git Flow.
- Claude Code workflow wired (`/pick-task` … `/release` resolve against this repo).
- Developer brief (`doc/dev/`) and rewrite roadmap.
- **E1–E11 + the multi-asset/portfolio unit** shipped; the pre-production safety
  hardening (audit) is wired in (see *Where things stand*). `CHANGELOG.md` + git log are
  authoritative for what shipped per release.

## Pending

The engine is feature-complete and the safety machinery is wired. What remains is **not**
engine code: **real-key live enablement** (validate Kraken private endpoints +
venue-level idempotency against a real-key sandbox, then flip `live_enabled`) — the one
maintainer step in [`07-roadmap.md`](07-roadmap.md).

## Known gaps / deferred

- ~~**Final project name**~~ — **decided**: kept as `trading_bot` (with `dccd` /
  `fynance`); no rename.
- ~~**KPI ratios need a positive starting capital**~~ — **resolved (E10)**:
  `AppConfig.starting_capital` (default 100000) is wired into `PerformanceService(v0=)`.
- ~~**Same-instrument strategies commingle in `run_app`**~~ — **resolved (E10)**:
  `build_runners` **rejects** two strategies on the same symbol (`ConfigError`, alias-aware).
- ~~**dccd↔trading_bot orchestration depth**~~ — **resolved (E8)**: library import
  (`feed_for` uses `dccd.Client.read`/`backfill` in-process). See ADR.
- ~~**Reconcile / kill-switch / daily-loss / fill-dedup were not wired**~~ — **resolved
  (audit)**: reconcile-on-startup, the daily-loss circuit breaker, mandatory live risk
  limits, `fill_id` dedup, and restart-safe order dedup are all wired (PRs #71–#75).
- ~~**Portfolio store-key convention unpinned**~~ — **resolved (audit, #76)**:
  `PortfolioStrategyConfig.store_key_format`.
- **`AddOrder` idempotency at the venue** — *mostly closed*: `place_order` sends `AddOrder`
  **at most once** (`post(retry=False)` → `AmbiguousRequestError`), and order dedup now
  **survives a restart** (the router is restored from the persisted store before the first
  order — #75). Still **not fully closed**: there is no *venue-side* dedup token, so an
  order that filled in the crash gap *before any persist* could still double-submit — that
  needs a real-key sandbox to build/validate (the only live-trading prerequisite left).
