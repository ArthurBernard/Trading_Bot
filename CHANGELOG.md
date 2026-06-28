# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Hardening test suite (`tests/hardening/`) — proves the money-safety invariants
  under **fault injection** (a `FaultyBroker` over `PaperBroker`): reconciliation
  converges after a simulated disconnect (no order duplicated/lost), idempotent submit
  survives retries/ambiguous failures, and the kill-switch cancels + halts mid-run.

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
