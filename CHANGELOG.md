# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Accounting invariant checker** (`application/accounting.py`) — a pure,
  side-effect-free recomputation of the book's self-consistency: per-instrument
  `position_drift` (tracker vs Σ signed store fills, `error`), per-order
  `order_fill_mismatch` and `status_incoherent` (tolerance-aware, `warn`), and
  `duplicate_venue_ids` (`warn`). Typed `Violation` results carry exact
  `str(Decimal)` figures. Verified on copies of the live books: flags the
  27 pre-v0.12.0 frozen rows and the legacy id duplicates, zero position
  drift, and comes back clean (legacy warns aside) post-healing. Wiring into
  the engine/API/UI follows in the next `accounting-guardrail` leaves. (#197)

### Changed

### Fixed

### Deprecated

### Removed

## [0.12.0] - 2026-07-11

### Added

- **Restart/replay end-to-end regression test** — two engine lifetimes over one
  SQLite store (`tests/application/test_restart_replay_e2e.py`): asserts unique
  venue/fill ids across lifetimes, no fill swallowed (positions == signed fold
  of stored fills), every filled row terminal + the resting row's orphan-close
  persisted, and realised PnL equal to an independent fold over the store's
  fills. Locks the three `paper-integrity` fixes together (closes the epic). (#194)

### Fixed

- **Reconcile's orphan-closes are persisted.** `reconcile()` now emits an
  `OrderEvent` per orphan it cancels (same `event_bus` guard as its summary
  log), so the store row moves to the terminal `cancelled` instead of keeping
  its stale pre-restart status forever — order history stays truthful after
  the engine corrects its in-memory view. (#193)
- **Fills now reach the tracked order — and its store row.** New
  `application/order_fill_sync.py` (`OrderFillSync`, wired in `build_engine`):
  every venue-confirmed fill is applied to the router's tracked `Order`
  (stash-and-drain, since the paper `FillEvent` fires before the router tracks
  the order) and the updated order is re-emitted so the store persists it.
  Filled orders no longer freeze at `open`/`filled_qty=0` in the store and the
  dashboard's Filled/Avg-fill columns. On startup, `replay()` heals restored
  rows from persisted fills (skipping the prefix already covered by the
  persisted `filled_qty`) **before** reconcile — a genuinely-filled restored
  order is terminal, so the orphan rule no longer mis-cancels it. (#191)
- **PaperBroker ids are unique across engine lifetimes.** Synthetic ids now
  embed a per-instance lifetime token (`PAPER-{token}-{n}`,
  `PAPER-FILL-{token}-{n}`; `uuid4` fragment by default, injectable in tests).
  A rebuilt engine (unit restart, `set_mode`, daemon restart) re-minted
  `PAPER-FILL-1`, and the fill-id idempotency dedup in tracker / performance /
  store then **silently swallowed real simulated fills** — the paper book lost
  a fill per instrument on the first post-restart rebalance. (#190)

## [0.11.1] - 2026-07-10

### Fixed

- **The genesis funding event carries the real funding time.** `ensure_genesis`
  stamped the ledger's genesis at the `ts=0` sentinel ("sorts before every
  fill"), which the capital block's ledger trail rendered as **1970-01-01**.
  With fills on the wall clock, the sentinel is unnecessary: the genesis now
  records the wall clock at first seeding (a fresh unit funds before its first
  fill, so the value-timeline ordering is preserved; idempotency is untouched —
  only the first call ever writes). The ledger UI renders any legacy `ts=0` row
  as "at deploy" instead of the epoch. (#184)
- **Paper fills now carry the real wall clock.** The factory-built `PaperBroker`
  used the simulator's *default* deterministic clock (a fixed 2024-01-01 base
  advancing +1 ms per fill — meant for reproducible tests), so every live-test
  paper fill was stamped in 2024: the equity-chart x axis rendered a
  milliseconds-wide window in January 2024, the Fills table showed 2024 dates,
  and — silently — the `max_daily_loss` breaker's `realised_pnl_since` midnight
  window never matched a single paper fill. `build_engine` now injects
  `time.time()`-based epoch-ms into the engine's PaperBroker; the deterministic
  default stays for direct/test construction. (#182)

## [0.11.0] - 2026-07-10

### Added

- **The capital block — the money story reconciles on screen (closes the
  strategy-capital epic).** The strategy detail page opens with a CAPITAL card:
  starting capital + PnL (realised / unrealised) = total value, joined by
  explicit `+`/`=` glyphs with a Δ%-since-funding readout; a withdrawable line;
  a Reinvest/Cashout segmented toggle (live mode asks for confirmation); one
  shared Adjust-capital modal (deposit/withdraw, display-only preview, an
  `op_id` minted once per open so a double-click never double-counts; a 422
  renders the server's exact withdrawable inline); and a ledger expander — the
  audit trail of every funding/deposit/withdrawal. The Strategies roster gains
  a Total value column so the same figure reads at every altitude
  (strategy-capital leaf 08). (#179)
- **Capital control plane: fund, cash out, flip the policy — live from the
  dashboard API.** `GET/POST /api/strategies/{name}/capital` (deposit/withdraw,
  amounts as Decimal strings — a JSON float is refused) and
  `POST /api/strategies/{name}/policy` (`fixed`/`compound`, persisted to the
  manifest and hot next tick). Every mutation is idempotent by a caller-assigned
  `op_id` (the client-order-id analogue — a retried POST never double-counts);
  withdrawals are capped at `withdrawable = max(0, total_value − committed to
  positions − reserved by open orders)` and a too-large request 422s with the
  exact figure. Paper/testnet unconstrained; a live-mode capital op returns 409
  (deferred to real-key enablement). New `WithdrawalTooLarge` /
  `LiveCapitalOpsDeferred` domain errors (strategy-capital leaf 07). (#178)
- **The capital ledger drives the engine.** New
  `application/capital_service.py`: the declared `allocation` (or a portfolio's
  `capital`) seeds a deterministic genesis `FUNDING` event once
  (`<strategy>:funding`, idempotent — re-deploys never double-fund; the config
  value is inert thereafter), and both runners now read their sizing base
  through a **lazy `capital_provider` called once per tick** — `fixed` sizes on
  contributed capital `C`, `compound` on `C + realised PnL` (floored at 0,
  never unrealised) — so a deposit/withdrawal/policy flip is hot with no engine
  rebuild. The KPI anchor `v0` repoints to the genesis amount while the ratios
  stay on the **fill-only** equity curve (a deposit never reads as a return —
  guardrail-tested). `/api/strategies` rows and `/api/pnl`'s `current` gain
  `allocation` / `contributed` / `unrealised` / `total_value` /
  `capital_policy` (strategy-capital leaf 06). (#177)
- **Per-strategy detail page.** Every strategy now has a deep-linkable home,
  `GET /strategies/{name}` (404 for an unknown unit): header with mode badge /
  run pill / Start-Stop-Mode-Remove controls (incl. the typed go-live modal),
  the uPlot equity chart + per-mode stats (moved from the PnL tab, strategy
  pre-bound — no dropdown), and that strategy's positions, recent orders and
  fills. The deploy form moves to its own `/strategies/new` page
  (strategy-capital leaf 05). (#176)
- **Per-strategy `allocation` + `capital_policy` config fields.** Both
  `StrategyConfig` and `PortfolioStrategyConfig` gain an optional, strictly
  positive `allocation` (the declared capital base — single-instrument
  strategies had no money field at all) and a
  `capital_policy: "fixed" | "compound"` (default `fixed`). `allocation`
  supersedes a portfolio's `capital` when set; unset = exactly today's
  behaviour, every existing manifest validates unchanged. Inert until the
  capital service wires them (strategy-capital leaf 04). (#175)
- **Capital ledger storage.** `SqliteStore` gains the append-only
  `capital_events` table (composite PK `(event_id, strategy, mode)` +
  `INSERT OR IGNORE` — the fills idempotency discipline; money as
  `str(Decimal)`; `mode`/`venue` context tags) with `record_capital_event()` /
  `capital_events()` and a presence-probing `_migrate_capital_events`, so an
  existing store upgrades in place. Dormant until the capital service lands
  (strategy-capital leaf 03). (#174)
- **Domain capital primitives.** New pure `domain/capital.py`: `CapitalEvent`
  (immutable money movement — `FUNDING`/`DEPOSIT`/`WITHDRAWAL`, strictly-positive
  Decimal amount with the sign in the type, mirroring `Fill`'s discipline) plus
  the folds `contributed_capital(events)` and `value_series(fills, events)`
  (both streams interleaved by `ts`, event-before-fill on ties;
  `value = contributed + realised`). Reconciles point-for-point with
  `pnl_series.equity_series` when the only event is a genesis funding — verified
  exact over 477 real paper fills (strategy-capital leaf 02). (#173)
- **Dashboard shows WHEN things happened.** `/api/strategies` rows carry
  `last_eval_ts` (wall-clock of the last tick attempt) and `last_asof_ts` (as-of
  of the last completed evaluation); the Strategies table gains a **Last eval**
  column (relative time, with the absolute eval instant + as-of data date on
  hover). Every order row served by `/api/orders` now carries `ts` (epoch ms,
  first-persisted time; `null` when the order predates any store) — the Orders
  page's Orders table and the Overview's open-orders table both gain a **Time**
  column (mirroring the Fills table's formatting), and order history now renders
  most-recent-first.
- **Dashboard read-API enrichment.** `/api/strategies` rows and `/api/kpi` rows now
  carry `span` (bar cadence, seconds) and `quote` (currency; `null` when an
  aggregate row folds mixed quote currencies); `/api/health` gains `next_tick_ts`
  / `tick` (the daemon's APScheduler cadence, wired through a new
  `schedule_info` hook — `null`/`null` outside the daemon); merged SSE frames on
  `/api/events` are tagged with `strategy` and a server-side `ts` (epoch ms) so
  the Logs page can attribute and timestamp events (ui-ux-overhaul leaf 01).
- **Dashboard: visible evaluation cadence + freshness.** The health chip counts
  down live (1 s tick) to the daemon's next scheduler check (`next check 42s` /
  `12m05s`; a tooltip explains serve-only mode when there is no scheduler), the
  Overview summary strip's next-check chip reuses the same shared countdown, the
  Strategies table gains **Cadence** (humanized bar span) and **Next bar**
  (countdown to the next epoch-aligned bar close, absolute local time on hover)
  columns, and every data card on Overview / Strategies / Orders carries a muted
  `updated HH:MM:SS` stamp set after each successful load — all driven by one
  shared per-page 1 s interval over `data-countdown-ts` elements (`tbTime`
  helpers in base.html), never a timer per cell (ui-ux-overhaul leaf 04).

### Changed

- **Dashboard IA: 5 tabs → 4; the PnL tab retired into the detail page.**
  The nav is now Overview · Strategies · Orders · Logs; `GET /pnl` 303-redirects
  to `/` so old bookmarks keep working. The Strategies page slims to a linked
  roster (name links to the detail page; the mode `<select>`, Remove and the
  go-live modal move there; quick Start/Stop stays), and strategy names across
  Overview and Orders are links to `/strategies/{name}`
  (strategy-capital leaf 05). (#176)
- **Dashboard display formatting.** A new dependency-free `static/format.js`
  (`tbFmt` namespace) rounds, thousands-groups and unit-labels every figure on
  every page (money by quote currency, quantities by base asset, max-drawdown as
  a `%`, ratios fixed-precision) while the exact Decimal string the API sent
  always survives in a `title` tooltip; applied across Overview / Strategies /
  Orders / PnL / Logs and the legacy single-engine dashboard (ui-ux-overhaul
  leaf 02).
- **Overview: positions by strategy + summary strip.** Positions now group by
  strategy by default (dropping the redundant Strategy column from both header
  and rows in that view), the group-by and KPI-level choices persist across
  reloads (`localStorage`), and a summary strip above the KPI card shows
  running/total strategies, open orders, total realised PnL (server-exact, no
  client-side math) and the next scheduled tick (ui-ux-overhaul leaf 03).
- **Dashboard: tables & event-feed polish (closes the ui-ux-overhaul epic).**
  The Strategies/Overview/Orders tables no longer visibly rebuild on an
  unchanged poll nor wipe an open mode `<select>` mid-interaction; order
  statuses render as colour-coded badges (Orders page + Overview open-orders);
  a history read at the server's default cap shows a "showing the most recent
  200" caption; the Fills table gains a seconds-precision timestamp with a
  relative ("3m ago") tooltip; the Logs feed stamps lines with the event's own
  server time, tags them with the emitting strategy, and gains All/Orders/
  Fills/Logs filter chips plus a min-level select (applied to both incoming
  and already-buffered lines); the PnL stats table gains a Return column
  (realised PnL ÷ starting capital) and a starting-capital note (ui-ux-overhaul
  leaf 05).
- **`start` defaults to the dashboard manifest.** With no `--config`,
  `trading-bot start` now loads `configs/dashboard.yaml` when it exists (the
  same persistent manifest `dashboard` reads and rewrites) instead of a bare
  empty paper config — so a plain `trading-bot start --serve` runs and serves
  the book the dashboard manages, no path to remember. Absent that file, the
  bare-config fallback is unchanged; `run`/`serve` keep requiring an explicit
  path (auto-picking up real strategies there would surprise).

### Fixed

- **Login: stable CSRF cookie + open favicon route.** `GET /login` no longer
  rotates the `tb_csrf` double-submit cookie on every render — a background
  request auth-redirected to `/login` (typically the browser's `/favicon.ico`
  probe) used to rotate the cookie under the form the user was looking at, so
  every submit 403'd. The render now reuses a well-formed existing cookie
  (shape-checked so an arbitrary value is never echoed into the form), and
  `/favicon.ico` is an open route (308 → `/static/favicon.svg`) so the probe
  never bounces through the auth redirect at all.
- **Clean Ctrl-C shutdown.** `start --serve`'s Ctrl-C/SIGTERM handling now
  completes the supervisor teardown instead of the process dying mid-shutdown
  (uvicorn 0.49's own signal capture re-raised the captured signal after
  `serve()` returned, killing the process before the daemon's `finally` ran);
  the dashboard's `/api/events` SSE stream also no longer spams an ERROR-level
  cancellation traceback on every shutdown while a client holds it open.
- **`start --serve` honours the manifest's `ui:` section.** `--serve-host` /
  `--serve-port` / `--serve-token` now fall back to the manifest's `ui.host` /
  `ui.port` / `ui.token` (like `dashboard` already does) instead of always
  defaulting to loopback `:8000` with no token; explicit flags/env still
  override.
- **Daemon tick performance.** Idle daemon ticks no longer reload each unit's
  full data history on every scheduler cadence (a daily-bar portfolio polled
  every 60s was doing a multi-second, multi-GB reload ~1439 times a day for
  nothing) — `StrategyRunner.step_latest` / `PortfolioRunner.rebalance_latest`
  now gate on a cheap, bounded tail probe and skip the full reload when no new
  bar/common date has appeared; when a full reload *is* needed, it runs off the
  event loop (`asyncio.to_thread`) so the dashboard stays responsive during a
  real rebalance.

### Deprecated

### Removed

- **Legacy single-engine dashboard retired.** `create_app` (the read-only
  one-engine FastAPI) with its `dashboard.html`/`app.js`/`style.css` assets and
  the `run --serve` flag are gone — the unified dashboard (`start --serve` /
  `dashboard` / `serve`) is the single web code path, and `run` is console-only;
  the login page carries its own inline styles (strategy-capital leaf 01). (#172)

## [0.10.0] - 2026-07-02

### Added

### Changed

- **Tooling, CI & packaging hygiene.** CI now runs `ruff format --check` + `mypy` + `interrogate` alongside `ruff check`/pytest, with SHA-pinned actions; the tree is `ruff format`-clean; pytest polices warnings (`filterwarnings=error`) and pins the asyncio loop scope; dead `MANIFEST.in` directive, unused `interrogate` wiring and a doc install-extra inconsistency fixed; a flaky wall-clock daemon test made deterministic (audit T-5/T-6/T-7/T-8/T-9/T-10/T-11/G-13). (#152)

### Fixed

- **Dashboard web-surface hardening.** `UIConfig` now rejects a non-loopback host without a token; `run --serve` gets the same bind guard; request bodies are capped, the session/rate-limit maps bounded, the login limiter keyed on the real peer; security headers (`no-store`/`nosniff`/frame/referrer) + login CSRF + `SameSite=strict` added; a client `X-Forwarded-Proto` is no longer trusted for the `Secure` cookie; a discarded deploy `mode` is rejected (audit A-4/I-4/I-5/I-6/I-7/I-9/I-10/I-11/I-13). (#147)
- **Engine/order robustness.** `cancel` is now idempotent (a repeated/concurrent cancel is a no-op, no venue re-hit); `rebalance_latest` reads only the latest window instead of draining the whole feed each tick; the aggregate KPI equity anchor ignores units with no fills in the requested mode (audit A-6/A-7/A-8/A-9). (#148)
- **Transport hardening + optional strict PaperBroker.** The async HTTP client now sets pool limits, distinct connect/read/write timeouts, `trust_env=False` and a response-size cap; an unknown venue order-type is rejected on rebuild (not coerced to `LIMIT`); the PaperBroker gains an opt-in `strict` mode that rejects sub-min-notional/over-precise sizes and dedups a retried client-order-id (audit B-11/B-12/B-13). (#149)
- **Domain hygiene.** An over-fill within `fill_tolerance` now clamps-and-closes instead of raising (a genuine over-fill still raises); the order factories set the client-order-id at construction so the `Order` aggregate is truly immutable; the dead `InsufficientFunds` error is removed and a docstring corrected (audit D-6/D-11/D-12/D-13/A-12). (#150)
- **Cover money-critical error branches + drop dead code.** New tests take the kill-switch cancel-failure, order-router forbidden-transition, reconcile-divergence and WS-reconnect branches to full coverage; the dead recorded-value daily-PnL path is removed (the breaker uses the day-scoped provider only); dead dashboard assets (`control.js`/`control.html`) and pre-rewrite artifacts (`data_base/`, `execution_scripts/`, `general_config_example.yaml`) removed; impl-detail-coupled tests de-coupled via a `PaperBroker.seed_fills` seam (audit T-4/T-12/I-12/A-11/G-14). (#151)
- **`trading-bot dashboard`/`serve` quit promptly on the first Ctrl-C.** Every uvicorn serve path now sets a bounded `timeout_graceful_shutdown`, so a browser holding the `/api/events` SSE stream open no longer pins uvicorn's (default unbounded) graceful shutdown — the server force-closes the stream and exits in ~3s on the first SIGINT instead of hanging until a second one. (#145)

### Deprecated

### Removed

## [0.9.0] - 2026-07-02

### Added

- **Config-driven portfolio data source.** `DataSourceConfig` gains `source_span` (resample a finer stored span up to `span` — e.g. a **daily** portfolio over a **1-minute** dccd store) and `data_path` (the dccd **store root** to read); `build_portfolio_runners` wraps the client in a `ResamplingDccdClient` when `source_span` is set, so a supervisor/dashboard reads a live 1m store as daily bars with no injected client. Verified end-to-end on the real store (every leg routed). (#138)

### Changed

### Fixed

- **Supervisor lifecycle no longer races stepping.** A per-unit `asyncio.Lock` guards start/stop/step/set_mode/remove so a scheduler tick can't step a half-built or torn-down unit (it previously could double-build an engine or hit `None`); independent strategies still run concurrently; stop/remove share one teardown path (audit A-3/A-10). (#141)
- **SQLite writes moved off the event loop.** Order/fill persistence hands off to a dedicated FIFO writer thread (append-only, drained on shutdown) instead of a synchronous open→write→commit→close inside the async loop that blocked all runners; connections use WAL + an explicit `busy_timeout`; fills are keyed `(fill_id, venue, mode)` so a paper and a live fill sharing a venue id no longer collide (audit A-2/D-8/D-9). (#140)
- **Weight-aware Binance rate limiter.** The limiter now charges each endpoint its request-weight, tracks the rolling 1-minute budget (honouring `X-MBX-USED-WEIGHT-1M`), and backs off on 418 (IP ban) / 429 (`Retry-After`); a `retry=False` 429 surfaces `Retry-After` without ever blind-retrying a submit; a Binance error inside a JSON array is now detected (audit B-6/B-9/B-7). (#142)
- **True average fill price on partial fills + Kraken WS reconcile-on-gap.** `open_orders` derives a partial fill's average price from the venue's executed cost/qty (Binance `cummulativeQuoteQty/executedQty`, Kraken `cost/vol_exec`), never the resting limit price; the Kraken private-WS reconciles on a sequence gap / (re)connect and rejects an unparseable timestamp instead of silently zeroing it (audit B-8/B-10). (#143)
- **Engine reads a real dccd store synchronously.** `_make_client` primes the dccd `Client`'s store/registry (the read-only half of dccd's async `__aenter__`), so the feed's sync `read()` works from inside the async step — it previously raised `Client must be used inside 'async with Client()'` (dccd API drift), starving every real-dccd read. Verified against 4.66M real 1m bars. (#137)
- **Dashboard can launch strategies with a local `signal.ref`.** `trading-bot` now puts the CWD on `sys.path` (a group callback), so a manifest referencing a gitignored local strategy package (e.g. `strategies.<yourpkg>.signal:...`) resolves — previously the console script couldn't import it and the unit was silently skipped at start. (#135)

### Deprecated

### Removed

## [0.8.0] - 2026-07-02

### Added

- **Config-driven dashboard web settings (remote access, the dccd way).** A manifest
  can carry a **`ui:`** section (`host` / `port` / `token` / `read_only`), and
  `trading-bot dashboard` reads it — so you set the host + token **once** in
  `configs/dashboard.yaml` and a bare `dashboard` serves remotely every launch, no
  flags to remember (mirrors dccd's `ui_host` / `ui_auth_token`). CLI flags (and
  `TRADING_BOT_UI_TOKEN`) override the config; defaults stay **loopback + no auth**,
  and a non-loopback host still **requires a token** (from the config or the flag).
  `doc/dev/10-deploy.md` updated. (#125)

- **Per-strategy store isolation** — a `StrategyConfig` / `PortfolioStrategyConfig`
  may set its own `db_path`, so several strategies in **one** dashboard manifest each
  keep their **own** SQLite store (isolated book, fills and PnL — no commingling). The
  supervisor applies the override when slicing a unit; the dashboard's `POST
  /api/strategies` **auto-assigns** `…/dashboard/<name>.sqlite` when none is given, so
  UI-deployed strategies are isolated by default. Absent → the global store (backward
  compatible). Needed for one dashboard running multiple strategies. (#123)

- **Dashboard Orders/Fills + Logs pages.** An **Orders/Fills** history page —
  `GET /api/fills` (fill history across every unit's store) and `GET /api/orders?history=true`
  (open + recent) with **crypto / exchange / strategy filters** + `limit` — and a
  **Logs** page streaming the merged engine activity over `/api/events` (SSE) with a
  recent-event buffer. Completes the unified dashboard's five pages. (#120)

- **Dashboard PnL chart (uPlot, self-hosted).** The PnL page draws a per-strategy
  **equity-over-time chart** with **live and testnet as separate, colour-coded
  series** (fake vs real money never combined), fed by `/api/pnl` — a strategy
  selector, a per-mode legend/toggle, a stats table, empty-state + resize + polling.
  Uses **uPlot v1.6.31** vendored into `static/` (MIT; no CDN, no build). And the
  **aggregate ratio KPIs** (Sharpe/Sortino/Calmar/maxDD at `level=exchange|total`)
  are now computed on the combined equity curve — filling the `null`s the Overview
  left — degrading to `null` without `fynance`. (#119)

- **PnL time-series (per strategy, per mode).** Persisted fills now carry a **mode +
  venue** storage tag (idempotent migration; existing rows default to `paper`; the
  domain `Fill` stays pure), and a new `application/pnl_series.py` folds a strategy's
  fills in timestamp order into an equity curve (`equity(t) = starting_capital + Σ
  realised PnL`, via the domain `Position` fold). `supervisor.pnl_series(name)` +
  `GET /api/pnl?strategy=&mode=live|testnet|paper|all` return **live and testnet as
  separate series** (fake vs real money never combined) with v0 / current equity /
  unrealised. The data foundation for the dashboard PnL chart. Verified: the derived
  final equity equals the engine's own realised PnL exactly. (#118)

- **Manage strategies from the dashboard (persistent control plane).** The dashboard
  now owns a **manifest** (`configs/dashboard.yaml`, the default for `trading-bot
  dashboard` when no `-c` — gitignored, local-only) that it reads on startup and
  **rewrites on every change**, so deployments survive a restart. New endpoints
  (`403` under `--read-only`): `POST /api/strategies` (deploy — a name + kind + venue
  + mode + **signal ref** + symbol/universe + capital + risk), `DELETE
  /api/strategies/{name}`, and `GET /api/signals` (builtins + a scan of
  `strategies/*/signal.py`). `StrategySupervisor` gains dynamic membership
  (`add_unit` — validated + atomic + never auto-starts / `remove_unit` / `manifest`)
  and `AppConfig` gains `to_yaml` + add/remove-entry helpers. The UI **deploys signals
  that already exist in code** — it never authors the signal's Python (that stays in
  `strategies/`), exactly as dccd's UI configures jobs, not the collector. A small
  "Deploy a strategy" form + per-row Remove land on the Strategies page. (#117)

- **Dashboard Strategies page + a book that survives a restart.** The unified
  dashboard now serves the control surface — `GET /api/strategies` and `POST
  /api/strategies/{name}/start|stop|mode` (shared with the old control app; live
  needs `confirm:true` → `403` otherwise; writes refused when `--read-only`) — and a
  Strategies page grouped by exchange with a mode select, start/stop, and the typed
  live-confirm modal. The `trading-bot dashboard` command now `start_all()`s the
  declared strategies (a unit that can't start is skipped with a warning, never
  crashing the dashboard), and **`supervisor.start()` replays the store's fills into
  a paper unit's tracker/perf** so its book (positions + realised PnL) survives a
  restart — live/testnet still reconcile from the broker (no double-count). Verified
  end-to-end: a restarted dashboard shows the restored paper portfolio positions and
  the live-mode switch is refused (403) without a typed confirmation. (#115)

- **Dashboard Overview + KPI at 3 levels.** The dashboard app now serves aggregate
  reads over the supervisor's per-strategy engines — `GET /api/positions` &
  `/api/orders` (`?group_by=crypto|exchange|strategy`), `GET /api/kpi?level=strategy|
  exchange|total` (realised PnL + fees at each level; Sharpe/Sortino/Calmar/maxDD per
  strategy; aggregate ratios deferred), and a **merged `/api/events` SSE** fanning
  every running unit's engine bus onto one feed (dedup by id). The Overview page
  renders a KPI strip (level toggle), a positions table (group-by crypto/exchange) and
  an open-orders table, live via SSE with a polling fallback. Verified against a real
  paper portfolio book: the accessors equal the engine's own realised PnL/fees exactly.
  (#114)

- **Unified dashboard skeleton** — one `create_dashboard_app` factory + a
  dccd-style **self-contained `base.html` shell** (nav Overview / Strategies /
  Orders / PnL / Logs; brand + version + health chip + connection dot; all shared
  CSS + JS helpers in one file) + a `trading-bot dashboard` command that **quits
  cleanly on Ctrl-C** (uvicorn owns SIGINT; the supervisor drains in `finally`).
  Read-only is a runtime posture (`--read-only`), not a second app. Stub pages for
  now; data lands in the following leaves. First leaf of the unified-dashboard epic
  that will retire the split read-only/control apps. (#113)

- **Portfolio-strategy config support (multi-asset units run by config).** A
  multi-asset portfolio strategy can be declared entirely in a manifest — a
  `universe` plus a `signal.ref` pointing at a `PortfolioSignalFn` — and run by
  config through the generic portfolio adapter, with no per-strategy engine code.
  Concrete strategies stay **local-only** under the gitignored `strategies/` tree
  (strategy IP lives outside the engine repo; the engine stays generic). Paper by
  default; the wiring is validated offline by the generic adapter tests.

### Changed

- **Groom the dev-doc pack to match the shipped engine** — real module set, the dashboard framed as a control plane, the Binance adapter documented, stale `legacy/`/`scheduler.py`/`BrokerRegistry` references removed, and 12 finished plan trees archived (audit G-1…G-9). (#133)
- **The split web apps are retired onto one `dashboard` command.** `trading-bot serve`
  is now an alias that serves the unified dashboard **read-only**, and `trading-bot
  start --serve` serves the same `create_dashboard_app` (single code path) alongside
  the scheduler; `create_control_app` is a thin backward-compat wrapper over it. One
  app, one primary command (`trading-bot dashboard`). `doc/dev/10-deploy.md` +
  `deploy/trading-bot.service` now lead with `dashboard`. Completes the unified
  dccd-style dashboard epic. (#120)

- **Private read-only endpoints validated live on mainnet for both venues** — the
  go-live runbook's *Proven vs pending* now records that `balances` / `open_orders`
  / `fills` were exercised read-only against **real Kraken** (37 assets, 50 trades
  parsed) and **real Binance** (mainnet read key + testnet), with **no order ever
  sent or cancelled**. Supersedes the earlier "`balances` needs Query Funds" caveat.
  (#106)

### Fixed

- **Hermetic, enforced test gate.** An autouse `conftest.py` runs each test from a temp CWD and scrubs `TRADING_BOT_*` env, so the suite no longer reads a developer's local `configs/dashboard.yaml`; `--exitfirst` is dropped from `addopts` (all failures reported) and coverage is floored at `--cov-fail-under=90` (audit T-1/T-2/T-3). (#132)
- **Domain money & instrument guards.** `Order`/`Fill`/`Signal` reject a raw `float` money field at construction, average-price divisions run under a pinned `Decimal` context, non-finite (`NaN`/`Inf`) signal targets are rejected, and `parse_kraken_pair` no longer mis-parses Tezos `XTZUSD` to `XT/USD` (audit D-1/D-3/D-4/D-7/D-10). (#128)
- **Broker order-path live-readiness.** Order qty/price are quantized to the venue lot/tick (round-down, sub-min rejected) before submit, Kraken uses a monotonic lock-guarded nonce, venue error codes map to domain errors (retriable Kraken HTTP-200 errors retried; `AddOrder` never blind-retried), and the client-order-id is forwarded to both venues so reconcile matches on the value sent (audit B-2/B-3/B-4/B-5/B-15). (#129)
- **Redact secrets from transport logs & errors.** A signed Binance request URL (with `&signature=<hmac>` + api key) is masked in every `transport/http.py` log line and exception message, so a 429/5xx/timeout no longer leaks the request signature (audit B-1). (#127)
- **Dashboard gate hardening (server-side).** The typed live-confirmation (`I UNDERSTAND`) is enforced on the server (a bare `confirm:true` no longer flips a strategy to live), the deploy `db_path` is rejected on absolute/traversal paths, and the deploy `signal.ref` is allow-listed to a set of module roots with an audit log on import (audit I-1/I-2/I-3). (#130)
- **`max_daily_loss` is now genuinely daily.** The day-loss breaker is scoped to realised PnL since UTC midnight and auto-resets at the day boundary (it previously used cumulative session PnL and latched the kill-switch permanently); plus an idempotent `orders`-table schema migration, a persisted order timestamp, and a restored reject reason on reload (audit A-1/D-2/D-5/D-14). (#131)
- **A paper unit's book no longer commingles testnet/live fills.** `start()`'s
  paper-book replay folded **all** stored fills into the paper simulator; once a store
  held fills from a different deployment mode (a strategy run testnet/live, then
  switched back to paper), that mixed **fake and real money** into the paper PnL — and
  a testnet round trip landing on the same instrument as a large open paper position
  realised a spurious close against the wrong entry price. The replay now filters on
  the storage `mode` tag (paper-only). Surfaced by the PnL-time-series real-data run.
  (#118)

- **`StrategySupervisor.set_mode` no longer leaves a unit on the wrong mode when a
  switch is refused.** It mutated `unit.mode` *before* validating the target slice, so
  a testnet/live switch that raised `ConfigError` (e.g. no broker for the venue) left
  the unit on the new mode anyway. The slice is now validated first — a refused switch
  changes nothing, matching the live-confirm gate. (#115)

- **Binance testnet now authenticates with testnet credentials.** The engine's
  testnet path (`testnet: true` on a Binance broker) hard-pinned the testnet URL
  but still read the default `BINANCE_API_KEY` / `BINANCE_API_SECRET` — which, once
  separate mainnet + testnet keys coexist in `.env`, is the **mainnet** key and is
  rejected by `testnet.binance.vision` (`-2015`). It now reads
  `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET` (falling back to the
  generic pair for the older single-key setup). Verified read-only against real
  Binance testnet via `build_engine` (balances). (#108)

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
