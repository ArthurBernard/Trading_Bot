# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `BrokerConfig.testnet` — a per-venue **testnet** flag: `mode: live` + `testnet: true`
  (Binance only — Kraken has no public spot testnet) builds an adapter **hard-pinned**
  to the venue's sandbox URL (`testnet.binance.vision`), so it **cannot reach mainnet**
  and is therefore **exempt from the `live_enabled` opt-in** (still needs testnet
  credentials). The safe, low-ceremony way to live-test orders on the engine path
  without juggling `live_enabled`/`BINANCE_API_BASE`. Paper mode ignores it.
  `BinanceBroker` gains `base_url` / `is_testnet` introspection. (#68)

- `application.portfolio` — the multi-asset `PortfolioSignalFn` contract
  (`(asof_ms, frames) -> {Symbol: weight}`, weight = signed fraction of capital),
  a frozen `PortfolioStrategy` (universe + signal + capital + optional gross cap),
  a pure `weights_to_signals` sizer (`qty = weight × capital / price` →
  `Signal.target_qty`, exact `Decimal`), and a safe by-reference
  `load_portfolio_signal` loader. Groundwork for native multi-asset strategies (LS1). (#63)
- `application.PortfolioFeed` — a multi-instrument **causal** feed: replays N coins'
  daily bars from the dccd store on a **common date index** (inner-join on bar time),
  gated so a rebalance date is emitted only when **every** coin has that day's closed
  bar (never forward-filling a stale close); reuses the single-coin `DccdFeed` read
  path, injectable client, `asof_ms()` helper. Feeds the `PortfolioSignalFn`. (#64)
- `application.PortfolioRunner` — the multi-asset rebalance loop: each tick calls the
  `PortfolioSignalFn` for the whole book, sizes the weight vector to per-coin target
  quantities, and routes **N** idempotent (`{name}-{symbol}-{step}`), risk-gated
  **maker-LIMIT** legs through the shared `OrderRouter` (a coin omitted from the
  weights is targeted **flat**). Per-leg failures (`RiskLimitBreached`/`BrokerError`)
  are collected on a `RebalanceResult` and don't abort the book; cooperative
  `run(stop_event=...)`. (#65)
- `AppConfig.portfolios` + `PortfolioStrategyConfig` + `run_app` wiring — declare and
  run a native multi-asset portfolio (universe + weight-vector signal by reference +
  capital + daily dccd source) alongside single-instrument strategies on the shared
  engine; per-coin `PortfolioReport`. Overlap detection now spans strategies **and**
  portfolio universes (no instrument claimed twice). (#66)
- `application.ResamplingDccdClient` — an injectable resample-on-read dccd client
  (reads the 1-minute store, aggregates OHLCV to daily via `group_by_dynamic`,
  causal: closed days only, partial last day dropped, OHLC carried exact). The live
  daily-bars seam for the portfolio path (dccd serves only 1m). (#66)
- `application.as_portfolio_signal` — a generic adapter bridging an argument-free
  research weight oracle (`() -> {pair: weight}`) to the `PortfolioSignalFn` contract
  (normalises pair keys → `Symbol`, weights → exact `Decimal`, handles a bare mapping
  or `(mapping, asof)`). With it, a **concrete strategy** is wired purely by reference
  (`module:function` + a YAML config) and the engine never imports it — completing the
  generic multi-asset / portfolio-strategy support. **Concrete strategies (signal
  wrappers, configs, their e2e tests) are kept local-only** under the gitignored
  `strategies/` tree and are **never** committed to this engine repo. (#67)

### Changed

- `[triptych]` extra documents `fynance-research` as an editable sibling install
  (`pip install -e ../fynance-research`, like `dccd`) — the source of validated
  portfolio signals (LS1); kept out of the hard deps. (#66)

### Changed

- **Real-money live now requires explicit risk limits.** `build_engine` refuses a
  `mode: live` + `live_enabled` config (with credentials) whose `RiskConfig` leaves
  any of `max_order` / `max_position` / `max_daily_loss` unset — a `BrokerError`
  naming the gaps, checked **after** the credential gate. An all-`None` `RiskConfig`
  is *unconstrained*; trading real money with no size/exposure/daily-loss cap is
  refused. Paper and testnet (paper money) are exempt. (#73)

### Fixed

- **Daily-loss circuit breaker is now wired.** `build_engine` feeds the `RiskManager`
  the live signed realised PnL (`daily_pnl_provider=perf.realised_pnl`) — previously it
  saw a constant zero, so `max_daily_loss` never engaged. Reaching the limit now refuses
  every new order, and a `max_daily_loss` breach **escalates to the kill-switch** in the
  `OrderRouter` (cancel resting orders + trip the halt), since the limit is the day's
  *halt* threshold, not a one-order cap. (#72)
- **Reconcile-on-startup is now wired** into the run loop. `run_app` calls
  `reconcile(broker, router, tracker)` right after `build_engine` (before any runner
  starts; opt-out `reconcile_on_start=False`), so a restart converges the engine's
  empty maps to the venue's truth — ingesting venue-open orders, closing orphans, and
  rebuilding positions from confirmed fills — **before the first order**. The
  *reconcile, don't assume* invariant was implemented + hardening-tested but had **no
  production caller**; it is now enforced on every start (a no-op on a fresh
  `PaperBroker`; the safety backstop on a live/testnet venue). (#71)

### Deprecated

### Removed

## [0.2.0] - 2026-06-28

### Added

- `brokers.BinanceBroker` — Binance spot REST adapter behind the `Broker` port
  (HMAC-SHA256 signed orders/balances/fills/ticker; public market data key-free),
  the **2nd live venue**. `newClientOrderId` carries the client-order-id for
  venue-level idempotency; the non-idempotent order POST stays `retry=False`
  (reconcile-on-ambiguous). Composite venue-order-id `"<SYMBOL>:<orderId>"` lets
  the symbol-free port drive Binance's symbol-scoped cancel; `fills()` queries
  `myTrades` over a configured symbol set. **Testnet-capable** (configurable base
  URL) with an opt-in `network` E2E doing a real place→read→cancel round-trip on
  `testnet.binance.vision`. Wired into `service_factory` (`binance` ∈ live venues);
  paper stays the default, live behind the existing off-by-default opt-in. Completes
  **E11**. (#61)
- `domain.instrument.parse_binance_symbol` — parse Binance separator-less pair codes
  (`BTCUSDT` → `BTC/USDT`) into canonical `Symbol`s via a longest-first quote-suffix
  table; groundwork for the Binance adapter (E11). (#60)
- Hardening test suite (`tests/hardening/`) — proves the money-safety invariants
  under **fault injection** (a `FaultyBroker` over `PaperBroker`): reconciliation
  converges after a simulated disconnect (no order duplicated/lost), idempotent submit
  survives retries/ambiguous failures, and the kill-switch cancels + halts mid-run.
- Go-live runbook (`doc/dev/09-go-live.md`) + a `LiveTradingNotEnabled` opt-in guard:
  live trading is **off by default** — `mode: live` alone is refused; it requires
  `AppConfig.live_enabled` **and** credentials. The live adapter is only constructed,
  never called — **no real order is ever sent**. Completes the **E10 go-live
  hardening** (the final project name stays deferred).
- `AppConfig.starting_capital` (default 100000) — wired into `PerformanceService(v0=)`
  so the KPI ratios (Sharpe/Sortino/max-drawdown/Calmar) are **meaningful** (the equity
  curve no longer starts at zero and sign-crosses); CLI `kpi --capital` overrides it.

### Changed

- `service_factory` recognises `binance` as a live venue (`_LIVE_VENUES`); a `binance`
  live config without credentials raises `BrokerError` (never a silent paper fallback).
- `transport.AsyncHTTPClient.request(method, …)` — a thin public seam over the shared
  request loop for arbitrary verbs (Binance signs `DELETE /api/v3/order` for cancels),
  with the same `retry`/`AmbiguousRequestError` semantics as `post`. (#61)
- `run_app`/`build_runners` now **reject** two strategies declaring the same instrument
  (a `ConfigError`, catching aliases like `XBT/USD`≡`BTC/USD`) — the shared per-instrument
  tracker has no per-strategy attribution, so commingling is refused up front.
- `transport.AsyncHTTPClient.post(retry=...)` — a POST can opt out of retries
  (`retry=False` → at-most-once, raising `AmbiguousRequestError` on a transient failure
  so the caller reconciles before retrying). `KrakenBroker.place_order` (`AddOrder`) uses
  the non-retry path; idempotent reads keep retrying. Closes the blind-retry double-submit
  window (engine-side; venue-side dedup token still needs a real-key sandbox).

- `interfaces.api` — read-only FastAPI over the engine: `GET /api/{health,positions,
  orders,kpi}` (money as **Decimal strings**, never float) + an SSE `/api/events`
  stream fed by the `EventBus`. The web surface only observes — no order placement.
- `interfaces.ui` — Jinja2 dashboard (positions / open orders / PnL+KPI), a **pure
  HTTP client** of the API, live-updating via SSE; served by the same app. Plus a
  `trading-bot serve` CLI command (uvicorn). Completes the **E9 web UI**.
- `AppConfig` — full declarative config: each strategy declares its dccd **data
  source** (exchange/span/start), its **signal** by reference (`module:function` or a
  builtin like `ma_crossover` + params) and its sizing (`reference_qty`, `lookback`),
  plus a top-level `storage` section. Backward-compatible (new fields optional).
- `application.feed_for` — build a `DataFeed` from a strategy's dccd data source via
  **library import** (`dccd.Client.read`); optional `backfill=True` drives dccd
  collection before reading. Injectable client (offline tests run dccd-free).
- `application.run_app` + CLI — one `AppConfig` runs the whole declared
  multi-strategy system: build the shared engine, load every strategy (signal + dccd
  feed), run them concurrently via the `Orchestrator`, report per-strategy
  orders/positions/PnL. `trading-bot run <config.yaml>` brings up the declared
  (paper) system. Completes the **E8 triptych orchestration**.

- Modern packaging via `pyproject.toml`; dev tooling (ruff, mypy, pytest,
  interrogate, pre-commit) and GitHub Actions CI across Python 3.11–3.13.
- Claude Code developer workflow: `CLAUDE.md`, `.claude/` (workflow.json, hooks,
  settings), and the `doc/dev/` orientation pack + plan-tree scaffold.
- Git Flow (`develop` / `master`) with `CONTRIBUTING.md` and a `pre-push` hook.
- Domain primitives — Decimal `money` (float-guarded), venue-neutral `instrument`
  with Kraken normalisation, and the `errors` hierarchy. (#7)
- `Order` aggregate + lifecycle state machine and order types
  (market/limit/stop-loss/best-limit), with exact Decimal fill accounting. (#8)
- `Fill` and `Position` — net exposure rebuilt from an ordered fill sequence
  (flips, fee-aware realised PnL). (#9)
- `Signal` — venue-neutral strategy target (fractional exposure or explicit
  target quantity) with `delta_to(position)`. (#10)
- Pure PnL/KPI performance functions — `pnl`/`cum_pnl`/`equity_curve` (Decimal),
  with Sharpe/Sortino/max-drawdown/Calmar delegated to fynance. Completes the
  **E1 domain core**. (#11)
- `transport.AsyncHTTPClient` — async httpx wrapper (get/post, retry with
  increasing exponential backoff, `Retry-After` on 429, timeouts). (#13)
- `transport.WebSocketBase` — async WS base: `stream_raw()` + increasing
  exponential reconnect, `on_connect` hook, `send()`. (#14)
- `transport.RateLimiter` + `KrakenCallCounter` — per-exchange token-bucket plus
  Kraken's decaying call-counter (tiers, per-endpoint costs). Completes the
  **E2 transport** layer. (#15)
- `brokers.Broker` port (runtime-checkable Protocol over domain types) +
  `Capability` model + `BrokerRegistry`. (#17)
- `brokers.KrakenBroker` — Kraken REST adapter: HMAC-SHA512 request signing
  (verified vs Kraken's published vector), signed orders/balances/fills, public
  market data. Credentials via env; public data works key-free. (#18)
- `brokers.KrakenPrivateWS` — Kraken v2 private-WS `executions` parsing into domain
  `Fill`s / order updates (token-auth, mock-verified; live gated on a key).
  Completes the **E3 Kraken adapter**. (#19)
- `application` kernel — `AppConfig` (pydantic v2, paper-default) + async `EventBus`
  (fan-out queues + sync subscribers; `OrderEvent`/`FillEvent`/`LogEvent`). (#22)
- `brokers.PaperBroker` — in-process fill simulation (immediate/partial fill
  models, fee model), the default broker so the engine runs with no venue.
- `application.OrderRouter` — idempotent order submission (client-order-id dedup,
  incl. concurrent) + order-lifecycle driving + events.
- `application.PositionTracker` — live per-instrument `Position`s folded from
  broker-confirmed `FillEvent`s (delegates to `Position.from_fills`).
- `application.reconcile` — converge local order/position state with the broker on
  startup/reconnect (adopt venue open orders, ingest unknown, close orphans, rebuild
  positions from broker fills; idempotent). Completes the **E4 execution engine**.
- `application.Strategy` — declare/load a strategy (config + a signal callable
  `bars→domain Signal`) with a **safe loader** (importable `module:function`, no
  arbitrary-file exec) + a fynance-backed MA-crossover example signal.
- `application.DataFeed` — causal bars feed (`InMemoryFeed` + dccd-backed
  `DccdFeed`): growing windows `frame[:t+1]`, never a future bar; live emits only
  closed bars.
- `application.StrategyRunner` — the live loop wiring `DataFeed` → strategy signal
  → `Signal.delta_to(position)` → order → `OrderRouter`, with per-step idempotent
  client-order-ids. Completes the **E5 strategy runner**: a strategy now runs
  end-to-end (dccd data → fynance signal → managed positions on a broker).
- `storage.SqliteStore` — append-only SQLite order/fill history + key/value state
  (orders UPSERTed, fills append-only, money as TEXT — exact `Decimal`, never
  float); optional `EventBus` attach. The reconciliation source.
- `application.PerformanceService` — live realised PnL / fees / equity curve over
  the `FillEvent` stream, with Sharpe/Sortino/max-drawdown/Calmar via fynance.
- `application.RiskManager` — pre-trade gate (`max_order`/`max_position`/
  `max_daily_loss`) + kill-switch, wired into `OrderRouter.submit` so every order is
  gated; a breach raises `RiskLimitBreached` and never reaches the broker. Completes
  the **E6 performance/persistence/risk** block.
- `application.service_factory.build_engine` — single wiring point assembling the
  whole engine (bus, broker, router+risk, tracker, perf, store) from an `AppConfig`
  (paper-by-default; live needs credentials), plus a Typer `trading-bot` CLI skeleton
  and the `trading-bot` console script.
- `trading-bot` CLI commands — `run` (run a strategy over a bars file / synthetic
  feed, paper by default; `--live` needs explicit ack **and** credentials), `status`
  and `kpi` (read a persisted `--db` history; rich tables, money as Decimal).
- `application.Orchestrator` — runs multiple `StrategyRunner` loops concurrently
  with cooperative graceful shutdown (shared stop-event, opt-in SIGINT/SIGTERM) and
  per-runner failure surfacing; replaces the legacy multiprocessing server. Plus a
  `StrategyRunner.run(stop_event=...)` cooperative-stop hook.

### Fixed

- `application.OrderRouter` — a refused/failed submit with no concurrent waiter no
  longer leaves an unretrieved in-flight future (silences asyncio's "Future
  exception was never retrieved" log noise).

### Changed

- `brokers.PaperBroker` is now **port-pure**: `place_order` no longer mutates the
  caller's `Order` (the `OrderRouter` owns the state machine); it returns a venue id
  and reports fills via `fills()` / `FillEvent`s. Removed the router's
  self-driving-broker workaround.
- Bumped version to `0.2.0.dev0` to mark the start of the rewrite.

### Removed

- `setup.py` and `requirements.txt` — folded into `pyproject.toml`.
- Deleted the superseded pre-2026 `trading_bot/legacy/` tree (23 modules) — the
  rewrite is complete through the **MVP CLI**; the old implementation lives in git
  history. Removed the now-unneeded legacy exclusions from the ruff/mypy/pytest/
  coverage/interrogate config (the whole package is now linted/typed/tested).
