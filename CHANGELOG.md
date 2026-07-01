# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

- **Private read-only endpoints validated live on mainnet for both venues** — the
  go-live runbook's *Proven vs pending* now records that `balances` / `open_orders`
  / `fills` were exercised read-only against **real Kraken** (37 assets, 50 trades
  parsed) and **real Binance** (mainnet read key + testnet), with **no order ever
  sent or cancelled**. Supersedes the earlier "`balances` needs Query Funds" caveat.
  (#106)

### Fixed

### Deprecated

### Removed

## [0.7.0] - 2026-06-30

### Added

- **Control dashboard authentication (for remote access).** `create_control_app(...,
  auth_token=…)` gates the dashboard behind a **token login** (dccd-style): `/login`
  exchanges the token for an HttpOnly, `Secure`-over-HTTPS session cookie; an auth-guard
  middleware refuses unauthenticated requests (`401` for `/api/*`, redirect to `/login`
  for pages); login is **rate-limited**; `/api/*` also accepts `Bearer <token>` / `?token=`
  for scripts; constant-time token check; a **sign-out** in the header. `trading-bot start
  --serve --serve-token …` (or `TRADING_BOT_UI_TOKEN`) enables it and **refuses a
  non-loopback bind without a token**. With no token (default) the app stays open —
  loopback / SSH-tunnel only. `doc/dev/10-deploy.md` covers the token + HTTPS reverse
  proxy. (#102)

### Changed

- **Control dashboard groups strategies by exchange**, and each strategy now uses the
  **broker matching its own venue** on testnet/live (`StrategyStatus.exchange`; a
  strategy's `data.exchange` / a portfolio's `venue`). Switching to testnet/live is
  refused if no broker is configured for that exchange. (#101)

- **Control dashboard visual pass** — mode **badges** (paper/testnet/live, colour-coded),
  a running/stopped status pill, start/stop buttons styled by action, a header summary
  (N strategies · M running), and a proper **typed-confirmation modal** for going live
  (replaces the browser `prompt`). Aligned with dccd's dark palette/pill language; no
  shipped font assets. (#100)

### Fixed

- **CI was red since the daemon landed** — `apscheduler` was in the `daemon` extra but
  missing from `dev`, while the daemon test (`test_daemon_starts_ticks_and_stops_cleanly`)
  drives `_run_daemon`, which imports it. CI installs `.[dev]`, so it hit
  `ModuleNotFoundError: No module named 'apscheduler'` (masked locally by a dev env that
  also had `[daemon]`). Added `apscheduler` to the `dev` extra, like the other daemon
  runtime deps the suite already exercises. (#103)

### Deprecated

### Removed

## [0.6.0] - 2026-06-30

### Added

- **systemd deployment** — `deploy/trading-bot.service` (a unit modelled on dccd's,
  `Restart=on-failure`, hardened, **pyenv**-based `ExecStart`, loopback control UI) plus
  `doc/dev/10-deploy.md` (install recipe, SSH-tunnel to the dashboard, operational
  notes). The daemon is restart-safe, so the supervisor recovers state on every restart.
  Completes the control-plane daemon. (#97)
- **Control plane — the daemon's dashboard can start/stop strategies and switch mode.**
  `interfaces.api.create_control_app(supervisor)` serves a **read+write** dashboard over
  the `StrategySupervisor`: `GET /api/strategies`, `POST /api/strategies/{name}/start`,
  `.../stop`, `.../mode`. `trading-bot start --serve` runs it (loopback by default — it
  can change what trades) alongside the scheduler. The dashboard (`control.html` +
  `control.js`) lists each strategy with a mode selector + start/stop buttons.
  **Real money is gated**: switching to `live` requires a typed confirmation in the UI
  and `confirm: true` on the endpoint — the server returns `403` otherwise, changing
  nothing. (#96)
- **`trading-bot start` — the trading daemon.** A long-running process (systemd's
  `ExecStart`) that builds a `StrategySupervisor`, starts every declared strategy (in its
  configured mode — **paper by default**), and re-evaluates them on an `--interval`
  (idempotent ticks) or `--cron` schedule via `apscheduler`, until `SIGINT`/`SIGTERM`
  (then shuts every unit down gracefully). Each strategy runs in its own engine, so they
  can be switched paper/testnet/live independently from the control plane. Starting never
  trades real money by itself (the live gates still apply). `StrategySupervisor` gains
  `start_all` / `step_all` for the daemon's boot/tick. (#95)
- `application.StrategySupervisor` — manages each declared strategy/portfolio as an
  **independent unit** in its **own** engine (own broker/mode), so a strategy can be
  started/stopped and switched between **paper / testnet / live independently**
  (`start` / `stop` / `set_mode` / `step` / `status`). Restart-safe (restore + reconcile
  per engine on start). **Real money is gated**: `set_mode(..., "live")` raises unless an
  explicit `confirm_live=True` is passed (the control plane's deliberate acknowledgement);
  paper ↔ testnet need none. The control-plane core behind the daemon + dashboard. (#94)
- `StrategyRunner.step_latest()` / `PortfolioRunner.rebalance_latest()` — a single,
  idempotent re-evaluation over the feed's **latest** data (vs `run`, which drains the
  feed once). The primitive a scheduler-driven daemon calls each tick: a tick over
  unchanged data trades nothing (already on target). Foundation for the control-plane
  daemon. (#93)

### Changed

### Fixed

- **`trading_bot.__version__` now reads from installed metadata** instead of a
  hand-bumped constant that the release flow never updated — it had been stuck at
  `"0.2.0"` across 0.3/0.4/0.5, so `trading-bot version` and the dashboard showed a
  stale version. Now always matches `pyproject.toml` (the single release source). (#92)

### Deprecated

### Removed

## [0.5.0] - 2026-06-29

### Added

- **Live monitoring — `trading-bot run --serve`.** Runs the declared system **and**
  serves the read-only dashboard over the **same** engine in one process, so positions
  / orders / PnL update in real time (engine bus + SSE) while the strategies run — not a
  separate, freshly-built engine. uvicorn owns `SIGINT` (Ctrl-C ends serve, then the
  orchestrator drains); a finite paper run keeps the dashboard up on its final state
  until Ctrl-C. The dashboard stays **read-only** (never places an order).
  `application.prepare_system` / `PreparedSystem` factor the build (engine + an
  orchestrator loaded with runners) shared by `run_app` and the serve path. (#89)
- **Live fill streaming.** `application.LiveFillStreamer` pumps a venue's private
  fill stream (e.g. `KrakenPrivateWS`) onto the engine bus — each confirmed
  execution becomes a `FillEvent`, so the tracker / performance service / store
  update from the venue in real time (fill-id dedup guards the snapshot replayed on
  resubscribe). `KrakenPrivateWS` gains an `on_connected` hook awaited after each
  (re)connect's subscribe, and `run_app` wires it: a **real-money live Kraken** run
  builds the streamer with an `on_connected` that **reconciles on every reconnect**
  and hosts it in the orchestrator (paper/testnet add nothing). Validated read-only
  against real Kraken (executions snapshot streamed + parsed; no order sent). (#87)

### Changed

### Fixed

- **Kraken `GetWebSocketsToken` was missing from the call-counter cost table**, so
  `cost_of` raised "Unknown method" and the private executions WebSocket could never
  fetch a token (it was wholly non-functional). Added (cost 1); the live WS read path
  now works. Found by the read-only live validation. (#87)

### Deprecated

### Removed

## [0.4.0] - 2026-06-29

### Added

- `Position.with_fill(fill)` + `Position.flat(instrument)` — the exact **incremental**
  fold (`from_fills` is now a fold of `with_fill`). (#82)

### Changed

- **Tracker & performance drain is now O(n)**, not O(n²). The `PositionTracker` and
  `PerformanceService` keep a **running** `Position` per instrument advanced one fill at
  a time (`Position.with_fill`) instead of recomputing `Position.from_fills` over the
  whole accumulated fill history on every fill (the perf service did it *twice* per
  fill). Behaviour is identical — `from_fills` is now implemented as the same fold, so
  one-shot and incremental results agree by construction (existing equivalence tests stay
  green) — but a long run / replay through the engine is no longer quadratic. (#82)

### Fixed

- The two `-m network` real-dccd replay tests now use `async with dccd.Client() as c`
  (current dccd's `Client` is an async context manager — `inventory()`/`read` require it
  entered); they previously called `inventory()` on a non-entered client. (#83)

### Deprecated

### Removed

## [0.3.0] - 2026-06-29

### Added

- `PortfolioStrategyConfig.store_key_format` (`"venue"` \| `"hyphen"` \| `"slash"`,
  default `"venue"`) — pins how each universe pair is rendered to the **dccd store
  key** its bars are read under, threaded through `build_portfolio_runners` into the
  `PortfolioFeed`'s `symbol_for`. A real `trading-bot run <portfolio>.yaml` against a
  hyphen-keyed (`BTC-USDT`) or slash-keyed store is no longer locked to the venue's
  native `BTCUSDT`/`XBTUSD` convention. (#76)

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

- **Real-money live now requires explicit risk limits.** `build_engine` refuses a
  `mode: live` + `live_enabled` config (with credentials) whose `RiskConfig` leaves
  any of `max_order` / `max_position` / `max_daily_loss` unset — a `BrokerError`
  naming the gaps, checked **after** the credential gate. An all-`None` `RiskConfig`
  is *unconstrained*; trading real money with no size/exposure/daily-loss cap is
  refused. Paper and testnet (paper money) are exempt. (#73)
- `[triptych]` extra documents `fynance-research` as an editable sibling install
  (`pip install -e ../fynance-research`, like `dccd`) — the source of validated
  portfolio signals (LS1); kept out of the hard deps. (#66)

### Fixed

- **Order idempotency now survives a restart.** `OrderRouter.restore(orders)` seeds the
  dedup map from persisted orders (no events emitted), and `run_app` calls it on startup
  from the configured store — so a re-submit of any previously-recorded `client_order_id`
  is de-duplicated even after the in-memory map is lost, closing the crash-restart
  double-submit window for a venue (like Kraken) that issues no venue-side idempotency
  token. Runs before the startup reconcile. (#75)
- **Fills are now de-duplicated by `fill_id`** in both the `PositionTracker` and the
  `PerformanceService`: a re-applied execution (e.g. a private-WS snapshot replay after
  a reconnect) is ignored instead of silently double-counting the position / corrupting
  the running realised PnL. `PositionTracker.reset` clears the seen-id set so a reconcile
  rebuild from the broker's fills still folds them. (#74)
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
- `run_app`'s limit-at-close order factory prices via `money(str(...))` instead of
  `from_float(float(...))` — exact `Decimal`, never through `float` (matching
  `PortfolioRunner`; carries full precision if the dccd close column is `Decimal`). (#78)

### Deprecated

### Removed

- `brokers.BrokerRegistry` — removed as dead code. Venue selection is an explicit
  per-venue dispatch in `service_factory.build_engine`, never a registry; the class was
  unused by any non-test code (only its own tests exercised it). Adapter/port docstrings
  updated to drop the registry references. (#77)

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
