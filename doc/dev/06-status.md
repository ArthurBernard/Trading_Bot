# 06 — Status

_Last updated: 2026-07-10_

## Where things stand

**The E1–E10 rewrite is complete — the engine is feature-complete and hardened.**
Phase 0 (tooling) plus all ten epics shipped: `domain/` (pure, mypy-strict),
`transport/` (http/ws/ratelimit), `brokers/` (`Broker` port + `KrakenBroker` REST+WS +
`BinanceBroker` REST + port-pure `PaperBroker`), `storage/` (`SqliteStore`, money as TEXT), `application/`
(declarative `AppConfig`+`EventBus`, idempotent risk-gated `OrderRouter`,
`PositionTracker`, `reconcile`, `Strategy`+safe loader, causal `DataFeed`+`feed_for`,
`StrategyRunner`, `PerformanceService`, `RiskManager`+kill-switch, `Orchestrator`,
`run_app`, `service_factory`), and `interfaces/` (Typer `trading-bot` CLI + a
FastAPI `api`/Jinja2 `ui` **control-plane dashboard** — start/stop/mode/deploy, not
read-only; the one deliberate exception is orders, where there is **no POST order
route** so a web client cannot place an order). One `AppConfig` conducts the
triptych — dccd data (library import) + fynance signals + brokers — and
`trading-bot run <config>` runs the whole declared multi-strategy system (paper by
default); `trading-bot dashboard` serves the control plane and `trading-bot serve`
its read-only alias. The money-safety invariants (reconcile convergence,
idempotency, kill-switch) are **proven under fault injection** (`tests/hardening/`).
**Live trading is off by default** behind an explicit `live_enabled` opt-in +
credentials + the go-live runbook (`doc/dev/09-go-live.md`) — **no real order is ever
sent from the repo**. 898 tests collected and green under the `trading_bot_env`
pyenv-virtualenv (907 total, 9 network E2E deselected by default); ruff + mypy clean
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

**Post-0.2.0 — multi-strategy dashboard UX overhaul shipped:** the unified
Overview / Strategies / Orders / PnL / Logs dashboard is now legible for an
operator, not just correct. Every figure is display-rounded, thousands-grouped
and unit/currency-labelled (`static/format.js`'s `tbFmt` namespace) while the
exact Decimal string the API sent always survives in a `title` tooltip —
rounding never leaks into a computation. Evaluation cadence is visible: the
health chip and Overview summary strip count down live to the daemon's next
scheduler tick, and the Strategies table shows each unit's bar cadence and a
countdown to its next bar close. Overview's positions grid groups by strategy
by default, with a summary strip (running/total strategies, open orders, total
realised PnL, next tick) above the KPI card. Table polish closes the epic:
polling refreshes on Strategies/Overview/Orders no longer visibly rebuild an
unchanged table nor wipe an open control mid-interaction, order statuses render
as colour-coded badges, a history read at the server's cap says so, and the
merged Logs event feed is stamped with server time, tagged by emitting
strategy, and filterable (event type + minimum log level). Plan tree:
`doc/dev/plans/ui-ux-overhaul/` (all five leaves shipped).

**Post-0.2.0 — per-strategy capital + dashboard IA reorg shipped (the
`strategy-capital` epic, 8 leaves, PRs #172–#179):** each strategy now has an
**append-only capital ledger** (`capital_events` — the fills discipline:
composite PK, `INSERT OR IGNORE`, money as TEXT) seeded once from the config
`allocation` (or a portfolio's `capital`) as a deterministic genesis event,
after which the ledger is the single source of truth. Every figure is derived,
never stored: contributed `C`, realised `R` (fills, unchanged), best-effort
unrealised `U`, `total_value = C+R+U`,
`withdrawable = max(0, total − committed − reserved)`. Sizing reads a **lazy
`capital_provider` once per tick** (`fixed → C`, `compound → max(0, C+R)`), so
**deposit / withdraw / policy-flip are hot** — one idempotent ledger write
(caller `op_id`), no engine rebuild. The KPI anchor `v0` is the genesis amount
and the ratios stay on the **fill-only** curve (a deposit never reads as a
return). The IA reorg gives the money a home: the legacy single-engine
dashboard is retired (one web code path), the nav is 4 tabs (Overview ·
Strategies · Orders · Logs), every strategy has a deep-linkable
`/strategies/{name}` **detail page** (controls + equity chart +
positions/orders/fills + the **capital block**: starting capital + PnL = total
value, withdrawable, Reinvest/Cashout toggle, Deposit/Withdraw modal, ledger
audit trail), deployment lives on `/strategies/new`, and the roster carries a
Total-value column. Live capital ops return **409** until real-key enablement
lands the funds gate. Plan tree: `doc/dev/plans/strategy-capital/` (archived).

**Remaining:** **real-key live enablement** (validate Kraken private endpoints +
venue-level idempotency against a real-key sandbox, then flip `live_enabled`) — the one
maintainer step left, see `07-roadmap.md`. The **project name is decided** (kept
`trading_bot`, with `dccd` / `fynance`; no rename). Engine layers, the triptych wiring
(one `AppConfig` →
`run_app` → one engine → runners via the `Orchestrator`; `trading-bot dashboard` for
the control-plane dashboard, `serve` for its read-only alias), and the Phase-0 dev
standard (packaging, CI 3.11–3.13, Git Flow,
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
- **`paper-integrity` (2026-07-11, PRs #190–#194)**: paper ids unique across
  engine lifetimes, fills reach the tracked order + store row (`OrderFillSync`,
  startup healing), reconcile persists orphan-closes, restart/replay E2E.
  Verified on copies of the live books: 14 (Binance) + 13 (Kraken) frozen rows
  heal `open → filled`, 14 + 13 resting rows persist `open → cancelled`; the
  healing applies to the real books at the next daemon restart. Caveat: the
  pre-fix soak (started 2026-07-10) silently dropped one simulated fill per
  instrument on the first post-restart rebalance — unrecoverable (never
  persisted); the book is self-consistent and the next rebalance self-corrects,
  but KPIs measured before 2026-07-11 carry that drift.
- **`accounting-guardrail` (2026-07-11, PRs #197–#201)**: the book proves
  itself continuously — pure invariant checker (`application/accounting.py`),
  run at unit startup + on a 60 s TTL behind dashboard reads, new violations
  alert once on the SSE stream, per-strategy `health`/`health_detail` on the
  API (folding the kill-switch, previously unreadable) down to a roster/detail
  pill. The live books surface a *stable* `warn` for the 14+13 legacy
  pre-v0.12.0 duplicate venue ids (disappears if the books are reset).
- **`venue-minimums` (2026-07-11, PRs #203–#206)**: sub-minimum orders are
  structurally impossible on the portfolio path — venue specs fetched keyless
  from the public endpoints (cached, degradable), each rebalance leg
  lot-quantized and rounded up to the minimum when ≥ `min_order_ratio`
  (default 0.5) of it or skipped with a log, spot sells capped at the held
  position, and factory paper is strict (`paper_strict: true`) so the
  simulator rejects like the venue. Verified on the live books' copies with
  real specs: the audit's whole dust rebalance (14 legs) skips; Kraken
  round-ups land exactly on `ordermin`; two-pass convergence proven.
- **`api-completeness` (2026-07-12, PRs #209–#214)**: the API says what the
  engine knows — per-engine mark cache (bar closes published at rebalance),
  position rows with `mark`/`mark_asof_ts`/`mark_source`/`value`/
  `unrealised`/`fee_ccy` under ONE mark policy (aggregate == Σ rows,
  Decimal-exact on the real books), server-side display currency
  (never-guess-a-rate), `GET /api/balances`, `last_asof_ts` documented, and
  an additive-only contract sweep across the six frozen endpoints — ready
  for the road-to-1.0 #5 freeze. Known seam: paper broker balances are empty
  across restarts (the simulator's internal ledger is not persisted;
  `/api/balances` relays it honestly) — the canary/guardrail funding hook.
- **`dashboard-tables-ux` (2026-07-12, PRs #215–#218)**: the dashboard renders
  the full 2026-07-11 UX design — asset-first positions with price (as-of) /
  value / unrealised, shared expandable rows (SSE-surviving), orders with
  Filled % / Avg fill / Value and click-to-expand fills (standalone fills
  table retired from the detail page), honest last→next bar chip, timezone
  label, filtered-vs-empty states. The `reject_reason` follow-up shipped in
  #219.
- **`canary-roundtrip` (2026-07-12, PRs #221–#223)**: `trading-bot canary` —
  the deterministic platform self-test. Probes first (real cancel = the
  kill-switch path; duplicate client-order-id = the idempotency invariant),
  then a sequential round-trip; paper oracle exact (PnL == −Σ fees, flat,
  persisted terminals, balance deltas), venue oracle = the accounting
  identity (fills/balances == venue-reported, fee-denomination-aware) +
  bounded cost. **Proven on real Binance testnet: 16/16 PASS** (cost
  0.00511 USDT). Two real findings hardened the domain: `Fill.fee_asset`
  (Binance charges market-buy fees in BASE) and Binance HTTP-400 → domain
  error mapping. `09-go-live.md` names the live canary as the road-to-1.0
  #1 validation vehicle. Cadence: paper per release, testnet at will, live
  once per venue at go-live.

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
