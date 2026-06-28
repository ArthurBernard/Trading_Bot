# 03 — Decisions (ADR journal)

Newest first. Each entry: the decision, the *why*, and (when relevant) what was
rejected. `/finish-task` appends accepted decisions; `/abandon-task` records
rejected approaches as tombstones.

---

### 2026-06-28 Web UI: pure HTTP client of the API; `serve` over a paper engine

**Decision.** `interfaces/ui/` is a Jinja2 **shell** (header: brand + version + mode
badge; three cards — Positions / Open orders / PnL+KPI — with stable tbody ids) served
by the same FastAPI app at `GET /`. All engine state is fetched **client-side** from
`/api/{positions,orders,kpi}` and live-updated via the `/api/events` SSE stream; the
dependency-free `app.js` renders money **verbatim** from the API's Decimal strings (no
`parseFloat` of money). The templates/JS never touch the application layer — the UI is
a pure HTTP client and inherits the API's read-only guarantee (no path to place an
order; POST → 405). `trading-bot serve [--config --host --port]` builds a paper-default
engine (`_build_serve_app` seam, testable with `uvicorn.run` patched) and serves it;
templates/static ship via `[tool.setuptools.package-data]`.

**Why.** A pure-client UI keeps the only money-moving surface (order placement) entirely
off the web — even fully exposed, the dashboard can't trade. Rendering Decimal strings
verbatim preserves exactness in the browser. `serve` over a freshly-built engine + the
persisted store is the MVP data path; attaching to a separately-running live system is
E10/future work. Mirrors dccd's UI = pure HTTP client of its api.

---

### 2026-06-25 Web API: read-only, money as Decimal strings, SSE via EventBus

**Decision.** `interfaces/api/app.py` `create_app(engine)` is a **read-only** HTTP
surface over the engine: `GET /api/{health,positions,orders,kpi}` and an SSE
`GET /api/events`. There is **no** route to place/cancel an order — the only
money-moving surface stays off the HTTP boundary entirely (a POST to an order path
returns 405). All money serializes as **`str(Decimal)`** via a `_DecimalJSONResponse`
encoder (exact, never float); KPI ratios go out as JSON numbers, and a ratio fynance
can't define on a given curve degrades to `0.0` rather than 500-ing. SSE registers an
`EventBus.add_queue()` and `remove_queue()`s in a `finally` (mirrors dccd).

**Why.** A dashboard must never become a trading surface — keeping the API observe-only
means the web can't move money even if exposed. Decimal-string JSON preserves exactness
to the browser (the UI renders the strings verbatim, no JS float parsing of money).

---

### 2026-06-25 Single-entrypoint orchestration: one config, one shared engine, N runners

**Decision.** `application/run_app.py` `run_app(config)` is the **system** assembly+run
seam: `build_engine(config)` builds **one** shared engine (one broker, bus, tracker,
perf, risk gate); `build_runners` loads every `StrategyConfig` into a `StrategyRunner`
(signal resolved from `SignalRefConfig` via a closed builtin registry
`{ma_crossover: …}` **or** a safe `module:function` import — no exec sink; feed via
`feed_for`); an `Orchestrator` runs them concurrently. All fills fan out on the one bus
and aggregate in the one tracker/perf, **independent per instrument** (the tracker keys
by instrument). The CLI `run` delegates here when the config declares strategies,
keeping the no-config synthetic quick path and the `--live` ack+creds guard. A new
`domain.errors.ConfigError` flags config-resolution failures.

**Why.** Lifts the factory's "single wiring point" to the *system* level — CLI, tests
and a future daemon bring the whole declared system up identically through one seam.
One shared engine means one risk gate / one PnL view across all strategies, while the
per-instrument tracker keeps distinct-symbol strategies isolated. This is E8: config →
engine → per-strategy runners → Orchestrator, fulfilling the execution **and**
orchestration scope (Trading_Bot conducts dccd + fynance + brokers).

---

### 2026-06-25 dccd integration depth — RESOLVED: library import, not a service

**Decision.** (Resolving the long-deferred decision.) Trading_Bot consumes dccd as an
**in-process library**, not a separate service/daemon/IPC. `application/data_provider.py`
`feed_for(strategy, *, client=None, backfill=False)` is the single seam:
`dccd.Client.read` supplies the bars a `DccdFeed` replays (read path), and
`dccd.Client.backfill` lets trading_bot **drive dccd's collection** before reading (the
orchestrator role). The dccd type stays behind a tiny injectable `DccdClient` Protocol;
the real `dccd.Client` is imported **lazily** inside `_make_client`, so the whole
offline path (and test suite) runs dccd-free with a fake.

**Why.** `dccd.Client` already exposes *both* read (`read`) and collection-driving
(`backfill`/`stream`) in one in-process API, so a library import covers the full
orchestrator role with **no IPC, no second process, no serialization boundary** —
simpler, faster, and directly testable. A separate dccd *service* would add operational
weight (a daemon to run/monitor) and a network/RPC seam for zero functional gain at this
scale. **Rejected:** driving dccd as an out-of-process service. The editable-from-sibling
install (chosen at bootstrap) already treats dccd/fynance as libraries; this stays
consistent.

---

### 2026-06-25 Declarative config: signal-by-reference, data-source-per-strategy

**Decision.** `AppConfig` grows so one YAML fully declares a runnable system: each
`StrategyConfig` carries its **data source** (`DataSourceConfig`: dccd exchange/span/
start/data_type), its **signal by reference** (`SignalRefConfig.ref` = a
`module:function` dotted string **or** a builtin name like `ma_crossover`, plus a
`params` dict — resolved by the safe `load_strategy` loader, no arbitrary-file exec),
and its sizing (`reference_qty` exact `Decimal`, `lookback`). A top-level
`StorageConfig` separates state (`db_path`) from market-data (`data_path`). Every new
field is optional/defaulted — legacy minimal configs validate unchanged.

**Why.** A self-contained per-strategy declaration is what makes one entrypoint run a
whole multi-strategy system (E8-03). Signal-by-reference keeps the no-arbitrary-exec
safety of the strategy loader. Additive optional fields keep older configs valid as
the schema grows.

---

### 2026-06-23 MVP "first light" reached; legacy tree retired

**Decision.** With the Typer CLI + async orchestration in place, the rewrite is
complete through the **MVP**: `trading-bot run` runs a fynance-backed strategy over a
data feed, routing risk-gated orders to a (paper) broker and reporting positions/PnL —
the whole `dccd data → fynance signal → managed orders → fills → PnL` loop, runnable
from the command line. The superseded `trading_bot/legacy/` tree (23 modules) is
**deleted** (history in git) and the legacy exclusions removed from all tooling, so
ruff/mypy/pytest/interrogate now cover the entire package.

**Why.** Every legacy capability has a native replacement, and nothing live imported
`legacy/` — keeping a dead, untested tree around only invites drift and confuses the
"what's real" picture. Retiring it makes the rewrite *the* implementation. The
remaining epics (E8 triptych orchestration, E9 UI, E10 go-live) build on the MVP, not
the legacy code.

---

### 2026-06-23 Orchestrator: gather (non-fail-fast), cooperative stop, opt-in signals

**Decision.** `application/orchestrator.py` `Orchestrator` runs N `StrategyRunner`s
concurrently with `asyncio.gather(..., return_exceptions=True)` — **not** a TaskGroup —
so one runner raising does **not** auto-cancel its siblings (independent strategy books
keep trading); outcomes are aggregated after, a lone failure re-raised, multiple wrapped
in `RunnerGroupError`. Graceful shutdown is **one shared cooperative `asyncio.Event`**
each runner checks **between** steps (never mid-submit) — no forced cancellation;
`StrategyRunner.run` gained a minimal `stop_event=` hook (+ a per-iteration `sleep(0)`
only when a stop-event is present, so a no-order live loop yields). SIGINT/SIGTERM
handling is **opt-in** via `install_signal_handlers(loop)` (loop-native, `signal.signal`
fallback) — importing installs nothing. Replaces the legacy multiprocessing server.

**Why.** Strategy loops are independent books — a fail-fast TaskGroup would cancel
healthy strategies on one's error. Cooperative stop-between-steps guarantees no order is
torn mid-flight on shutdown (a money-safety property). Opt-in signals keep the module
import-safe and tests signal-free.

---

### 2026-06-23 CLI run/status/kpi: double live-guard, fills-as-source for status/kpi

**Decision.** `trading-bot run` defaults to **paper**; `--live` requires **both** an
explicit `--yes-i-understand` ack (or interactive `typer.confirm`) **and** venue
credentials — either missing → clear non-zero exit with **no broker built and no order
placed**. `run` takes a `--bars` file (CSV/parquet → polars → `InMemoryFeed`) or a
built-in synthetic feed; it prices the example MA-crossover with a LIMIT-at-close order
factory so the paper run fills offline without a mark feed. `status`/`kpi` read a
persisted `SqliteStore` (`--db`) and rebuild positions/KPIs from the stored **fills**
(the PnL source of truth), not a re-run engine. `kpi --capital` anchors `v0 > 0` so the
equity curve doesn't sign-cross (fynance's annual-return math requires it).

**Why.** A two-gate live opt-in makes "no live by accident" structural at the interface
too, not just in the factory. Rebuilding from stored fills makes `status`/`kpi`
inspect real history a prior `run --db` produced (genuinely testable) rather than
re-simulating. The positive-capital anchor is a fynance constraint, not a choice about
PnL (realised PnL/fees are capital-independent).

---

### 2026-06-23 service_factory: single wiring point, no live broker by accident

**Decision.** `application/service_factory.py` `build_engine(config, *, db_path=None)
-> Engine` is the **one** place the engine is assembled: one `EventBus` shared by
`PositionTracker`, `PerformanceService`, `OrderRouter` (holding the `RiskManager` gate)
and (when `db_path`) `SqliteStore`. Broker selection enforces **paper-by-default** —
`PaperBroker` unless `config.mode == "live"` **and** the configured venue
(`KrakenBroker`) `has_credentials`; a credential-less / unknown / unconfigured live
venue raises `BrokerError` (never a silent paper fallback, never a live broker that
can't trade). `Engine` is a frozen dataclass the CLI/orchestration consume. The Typer
`app` needs a no-op `@app.callback()` so a single command stays a multi-command group.

**Why.** Centralising construction (order matters: tracker subscribes before fills
flow, the router carries the gate) means the CLI, tests and a future daemon all build
an identically-wired engine, and the interfaces layer never news-up a use-case. The
broker rule makes the paper-default invariant structural — going live is an explicit,
credential-checked choice. Mirrors dccd's `application` wiring.

---

### 2026-06-22 RiskManager: pre-trade gate in the router, resulting-net limits, kill-switch

**Decision.** `application/risk.py` `RiskManager.check(order)` is wired into
`OrderRouter._do_submit` **before** `broker.place_order` and before any state
transition — the last safety block before a venue sees an order. Checks, in order:
**kill-switch** (hard halt, first), `max_order` (the order's own size), `max_position`
(the **resulting** absolute net = current net + signed order qty, so a *reducing*
order is never blocked), `max_daily_loss` (halts new orders once the day's *realised*
loss ≥ cap — it does not predict the order's PnL). `None` limits are unconstrained.
A breach raises `RiskLimitBreached`: **no broker call, the order is left untracked**
(not a `REJECTED` record), so a later submit of the same id is a fresh attempt and
idempotency is intact. Kill-switch: `trip`/`reset`; `async kill(router|broker)`
cancels open orders **then** trips (cancel-before-trip, since cancelling is a reducing
action and must not be self-refused). Daily loss is sourced through a thin injected
`daily_pnl_provider` callable (or `record_daily_pnl`); "daily" resets via an explicit
caller-driven `reset_day()` — the manager owns no clock.

**Why.** Risk limits + kill-switch must gate **every** order (a hard live-trading
invariant); placing the gate inside the router's single submit path guarantees no
order bypasses it. Gating the *resulting* net (not order size) is the correct
position limit. **Known limitation:** `max_position` reads the *confirmed* position
from the tracker, so it does not count the unfilled qty of still-open orders —
pending exposure can momentarily exceed the cap between submit and fill; tightening
to pending-aware exposure is go-live hardening (E10).

---

### 2026-06-22 OrderRouter: consume the in-flight future's exception (log hygiene)

**Decision.** The router's per-id in-flight `asyncio.Future` (the concurrency guard)
gets a done-callback that retrieves a stored exception, so a refused/failed submit
with **no** concurrent waiter no longer triggers asyncio's "Future exception was
never retrieved" at GC. A real waiter still receives the exception via `await`.

**Why.** Surfaced when the risk gate made `submit` raise routinely: the dangling
future logged spurious noise. In a trading engine, log noise masks real incidents —
the guard must be silent when it has nothing to report.

---

### 2026-06-22 PerformanceService: fill-driven realised-PnL equity, two views

**Decision.** `application/performance_service.py` `PerformanceService` observes the
`FillEvent` stream (read-side; never trades) and keeps **two views of one stream**: a
global arrival-order fill list driving an aggregate **realised-PnL equity curve**
(`equity_k = v0 + cumulative realised PnL through fill k`, one point per fill), and
per-instrument lists driving `position()` via `Position.from_fills`. KPIs
(Sharpe/Sortino/max-drawdown/Calmar) delegate to `domain.performance` (fynance) over
that curve; a series with `< 2` points returns `0.0` (guarded, no raise).
`domain.performance.equity_curve` (single-instrument mark-to-market) was **not** used —
the same `v0 + cumulative-PnL` shape is rebuilt from fills.

**Why.** Realised PnL is additive across instruments and fills, so the aggregate total
equals the sum of per-instrument `Position.from_fills(...).realised_pnl` — a fill-driven
step curve needs no external mark prices (which we don't have for a cross-instrument
aggregate stream). Short-series-safe KPIs keep the service callable from the first fill.

---

### 2026-06-22 Storage: SQLite, money as TEXT, orders UPSERT / fills append-only

**Decision.** `trading_bot/storage/sqlite_store.py` `SqliteStore` (stdlib `sqlite3`,
WAL) persists `orders` (UPSERT by `client_order_id` — latest state), `fills`
(`INSERT OR IGNORE` by `fill_id` — immutable, append-only) and a `state` key/value.
**All money/qty columns are TEXT** holding `str(Decimal)`, rebuilt with `money(...)` —
never SQLite's REAL/float. Reads **reconstruct the domain object directly** from the
row (no state-machine replay — the stored row is the truth). `attach(event_bus)`
subscribes `OrderEvent→upsert_order` / `FillEvent→record_fill`.

**Why.** SQLite's only numeric types are INTEGER/REAL (binary float); a price through
REAL reintroduces exactly the rounding error `money` refuses — so TEXT is the only
lossless option. Orders are stateful aggregates (one evolving row); fills are
immutable facts (replay/refetch must be a no-op, never a duplicate). Replaying the
state machine on read could disagree with what was recorded, so the row wins.

---

### 2026-06-22 StrategyRunner: the live loop, per-step idempotent ids, warmup in one place

**Decision.** `application/strategy_runner.py` `StrategyRunner(strategy, feed, router,
tracker, *, event_bus, order_factory)` is an async driver over the sync `DataFeed`:
per causal window, `signal = strategy.evaluate(bars)`; `delta =
signal.delta_to(tracker.position(instrument), reference_qty)`; if `delta != 0`, submit a
MARKET order (side/qty from the delta) with a **deterministic per-step
`client_order_id`** `f"{strategy.name}-{step}"` so a re-run dedups to one venue order
(the runner half of E4 idempotency). `delta == 0` → no order. Warmup is **not**
re-implemented — `Strategy.evaluate` returns flat below `lookback`, so flat-vs-flat →
no order. `order_factory`'s id is always overridden by the runner.

**Why.** This closes the loop: dccd data → fynance signal → target position → managed
orders, lookahead-free by construction (the runner only reads the window it's handed).
Keeping warmup in the strategy and idempotency in the runner avoids duplicated logic.
The step index advances even on no-order steps so ids stay 1:1 with bars (a re-run
reproduces them exactly).

---

### 2026-06-21 DataFeed: causal windows, thin injectable dccd coupling

**Decision.** `application/data_feed.py` `DataFeed` is a sync `Iterable[polars.DataFrame]`
of **growing causal windows** — at step *t* it yields `frame[:t+1]` (all bars ≤ t,
never a future bar), so any feed-driven backtest is lookahead-free by construction.
`InMemoryFeed` replays a fixed frame; `DccdFeed` reads real bars via `dccd.Client.read`
(mapping dccd's `TS,open,high,low,close,volume` → `time,o,h,l,c,v`) and replays the same
way, with an `async live_windows` that emits a bar only once **closed**
(`now − bar.time ≥ span`). The dccd client is injected (typed against a minimal
protocol) — nothing imports dccd, so offline tests use a fake returning a canned frame.

**Why.** Causality is the hard strategy invariant (mirrors fynance walk-forward); making
the window prefix the data structure removes a whole class of lookahead bugs. A sync
iterator fits the backtest loop; closed-bar-only keeps live identical to replay. Thin
injectable coupling keeps the offline suite dccd-free and isolates dccd's column quirks.

---

### 2026-06-21 Strategy contract: bars→Signal callable + safe loader (no file exec)

**Decision.** `application/strategy.py` models a strategy as `(instrument,
signal_fn)` where `signal_fn: polars.DataFrame(bars) → domain Signal` — unifying on
the existing venue-neutral `Signal` (the runner turns `Signal` + `Position` into an
order) instead of the legacy `get_signal(data) → {-1,0,1}` int. `evaluate` enforces
the instrument invariant and returns a **flat** signal during warmup (`< lookback`
bars) rather than guessing. `load_strategy` accepts a passed callable **or** an
already-importable `"module:function"` string (`importlib.import_module` + `getattr`)
— **never** an arbitrary-file `exec_module` like the legacy `StrategyBot`. The
built-in `ma_crossover_signal` uses `fynance.sma` (causal trailing average).

**Why.** A `bars→Signal` callable is the minimal strategy surface and reuses the
domain vocabulary end to end. The safe loader removes the legacy code-execution sink
(loose-file `exec`). Warmup-flat keeps an undertrained signal from taking a position.
**Rejected:** loose-file importlib loading (security); a bespoke signal int (diverges
from the `Signal` value object).

---

### 2026-06-21 Reconciliation: venue is truth, rebuild positions from fills, idempotent

**Decision.** `application/reconcile.py` `reconcile(broker, router, tracker)` reads the
broker's open orders + balances + fills and converges local state, writing **only**
locally (never to the venue). **Orders — venue open set is truth:** ingest unknown
venue-open orders (no broker call, no lifecycle transition), keep already-tracked ones
(ingest idempotent, engine object authoritative), **close-and-forget** non-terminal
orphans the venue has no record of (drive `CANCELLED` + evict), leave terminal locals.
**Positions — broker fills are truth:** rebuild the `PositionTracker` from
`broker.fills()` so positions equal `Position.from_fills` per instrument. **Idempotent:**
no venue change ⇒ second pass is a no-op. Added minimal helpers `OrderRouter.{tracked_orders,ingest,forget}`
and `PositionTracker.reset` (no private-state reaching).

**Why.** *Reconcile, don't assume* is a hard live-trading invariant: after any
disconnect the venue's records — not local optimism — must win, with no order
duplicated or lost. Rebuilding positions from fills (rather than diffing) is trivially
correct and never double-counts. Idempotency makes it safe to run on every reconnect.

---

### 2026-06-21 PositionTracker from fills; PaperBroker made port-pure

**Decision.** `application/position_tracker.py` `PositionTracker` keeps a per-instrument
ordered fill list and recomputes the live `Position` via `domain.Position.from_fills`
(no own money logic). Fill ingestion boundary: the **broker emits `FillEvent`s**, the
tracker **subscribes** and folds them — router = write path, tracker = read-back path,
neither knows the other. **And:** `PaperBroker` was made **port-pure** — `place_order`
no longer mutates the caller's `Order` (it reads only the order's data, returns a
synthetic venue id, records `Fill`s, and `open_orders()` returns a *reconstructed*
venue view). The `OrderRouter`'s "tolerate a self-driving broker" branches were removed.

**Why.** Fixes the `Broker`-port contract violation surfaced in the order-router leaf:
the `OrderRouter` owns the order state machine; a broker that mutates the caller's
`Order` would corrupt that single source of truth and diverge from how a real venue
behaves (it reports a *separate* record). Delegating PnL to `Position.from_fills` keeps
one implementation of the fold.

---

### 2026-06-21 OrderRouter: engine-side idempotency; broker must not drive the order

**Decision.** `application/order_router.py` `OrderRouter` makes order submission
**idempotent engine-side**: a dedup map keyed by `client_order_id` (a second submit
of a known id returns the tracked `Order`, no second broker call) plus a per-id
in-flight `asyncio.Future`, so two *concurrent* submits of the same id still yield
exactly one `broker.place_order`. It drives the domain `Order` state machine
(`submit`→`open(venue_id)`; `reject` on `BrokerError`) and emits `OrderEvent`s. Fill
ingestion is **not** here — it lives in the PositionTracker (leaf 04): the write path
(intent→venue) and the read-back path (executions→position) are the clean split.

**Why.** Idempotency is a money-safety invariant; the dedup map + in-flight future
cover the sequential and concurrent retry cases. Venue-level idempotency stays
deferred (see the #23 ADR / `06-status`). **Contract note:** the `Broker` port must
**not** mutate the caller's `Order` — the router owns the state machine. `PaperBroker`
currently self-drives the order (a leaf-02 contract violation); the router tolerates
it for now, to be fixed in leaf 04 (see `06-status` known gaps).

---

### 2026-06-21 PaperBroker: the default in-process broker

**Decision.** `brokers/paper.py` `PaperBroker` (`name="paper"`) implements the
`Broker` port entirely in-process — the **default** broker so the engine runs with
no venue or key. `fill_model="immediate"` fully fills on placement at the order's
limit (or an injected mark for market); `"partial"` slices into equal chunks (the
last absorbing the remainder, so slices sum exactly). Fee = `price*qty*fee_bps/10000`
(Decimal, quote units). Balances thread buy/sell and may go negative (a simulator,
not a funding gate). Ids/clock are seamed so tests are exact.

**Why.** A behind-the-port simulator lets the whole order→fill→position path be
verified end-to-end with no network — the basis for testing the router,
reconciliation and strategies before any live broker is wired.

---

### 2026-06-21 Venue-level order idempotency deferred (PR #23)

**Decision.** `KrakenBroker` does **not** forward the domain `client_order_id` to
Kraken (its `cl_ord_id` needs a UUID, `userref` a 32-bit int — neither fits an
arbitrary id). Idempotent submission is enforced **engine-side** by the
`OrderRouter` (client-order-id dedup). The transport's blanket POST retry means an
`AddOrder` lost-response could still double-submit at the venue; that risk is
**accepted for now** (no live trading; private path mock-only) and recorded as a
go-live gap.

**Why.** The MVP runs paper-only, so venue double-submit cannot lose real money
yet, and a correct fix (a Kraken-acceptable `userref` mapping + not retrying a
non-idempotent `AddOrder`, reconciling on ambiguous failure instead) is go-live
hardening (E10), with groundwork in the E4 order-router. Surfacing the risk now —
honest docstring + a `06-status.md` known gap — beats a silent false claim that the
client id was sent. **Rejected:** sending the raw `client_order_id` as `cl_ord_id`
(Kraken rejects non-UUID values).

---

### 2026-06-21 Application kernel: paper-default config + fan-out EventBus (PR #22)

**Decision.** `application/config.py` `AppConfig` is pydantic v2 with
`mode: Literal["paper","live"] = "paper"` — a fresh/empty config never trades real
money; going live is an explicit edit. `application/events.py` `EventBus` carries
frozen `OrderEvent`/`FillEvent`/`LogEvent` (domain objects by reference, so `Decimal`
money stays intact) to a *set* of per-consumer async queues (fan-out, not steal) plus
sync subscribers; `emit` never blocks or raises — bad handlers are logged+swallowed,
full queues drop.

**Why.** Paper-by-default is the live-trading safety invariant made structural.
Fan-out lets the router, position tracker and a future UI consume the same stream
independently; a non-blocking `emit` keeps a slow consumer from stalling the engine.

---

### 2026-06-20 Kraken private WS: token-auth via on_connect, mock-verified (PR #19)

**Decision.** `brokers/kraken_ws.py` `KrakenPrivateWS` streams Kraken v2 `executions`
on `transport.WebSocketBase`, parsing frames into domain `Fill`s (trade execs) and
`OrderUpdate`s. Kraken's private WS is not per-frame signed: `on_connect` fetches a
short-lived **WebSocket token** (private REST `GetWebSocketsToken`, signed) via an
injected `token_provider` seam and subscribes with it; re-running on reconnect
re-fetches/re-subscribes (self-heal). **No key here** — token fetch + parsing are
verified against realistic canned v2 frames; the live private connection is deferred.

**Why.** Token-in-`on_connect` makes the private subscription idempotent across drops;
the seam keeps the whole path offline-testable without credentials. Tokens are secrets
and are never logged.

---

### 2026-06-20 KrakenBroker REST: signing verified, env credentials, mock+public posture (PR #18)

**Decision.** `brokers/kraken.py` implements the `Broker` port over Kraken REST.
`_sign` (nonce + `SHA256(nonce+postdata)` → `HMAC-SHA512(b64decode(secret))` → b64)
is verified against **Kraken's published signature test vector**. Credentials come
from env (`KRAKEN_API_KEY`/`KRAKEN_API_SECRET`), never logged; the broker is
constructible without them (public market data works key-free), and a private call
without both raises `BrokerError` before any I/O. Order mapping: side→buy/sell,
type→ordertype (`BEST_LIMIT`→limit), qty→volume, price from limit/stop; money stays
`Decimal` from Kraken's string amounts. Private endpoints are **mock-tested only**
(no key); real private verification is deferred.

**Why.** The published test vector proves the signing matches Kraken's spec exactly
without a key, so real orders would sign correctly. Env-only secrets keep keys out of
the repo and logs; Decimal-from-string avoids float error on venue amounts.

---

### 2026-06-20 Broker port: runtime-checkable Protocol + capability declaration (PR #17)

**Decision.** `brokers/base.py` defines the venue-neutral async `Broker` as a
`runtime_checkable` `Protocol` (`place_order`/`cancel_order`/`open_orders`/
`balances`/`fills`/`ticker` over domain types), with a `Capability` enum each adapter
declares via `capabilities()`; `require(broker, cap)` gates every operation and raises
`NoCapability`. A `BrokerRegistry` maps venue name → adapter. `BrokerError` was added
to `domain.errors`.

**Why.** A port has no shared implementation to inherit, so structural typing keeps
adapters decoupled from the port (no import back-coupling) and registry-friendly.
Honest support is the declared capability *set*, not the class hierarchy — the engine
never asks a venue for an undeclared operation.

---

### 2026-06-20 Transport rate-limiting: token-bucket + Kraken call-counter (PR #15)

**Decision.** `transport.RateLimiter` holds one `TokenBucket` per exchange (injected
clock/sleep seams); `KrakenCallCounter` models Kraken's decaying counter —
per-endpoint costs and tier limits (starter 15/3s, intermediate 20/2s, pro 20/1s)
ported from `legacy/tools/call_counters.py`. The port **fixes a legacy under-wait**:
it waits `overshoot * time_down` (not a raw `overshoot`) so the counter actually
decays clear of the limit before the next call.

**Why.** Proactive throttling keeps the engine under each venue's published budget —
a rate-limit ban during live trading is unacceptable. The legacy raw-overshoot sleep
under-waited and could still trip the limit.

---

### 2026-06-20 Transport WS: subscriptions via on_connect; send() needs a live socket (PR #14)

**Decision.** `transport.WebSocketBase.stream_raw()` reconnects with increasing,
capped backoff (the attempt counter resets after a successful connect). `send()`
raises if not connected; subscriptions/auth go through the `on_connect` hook, which
re-runs on every (re)connect so they **self-heal** across drops.

**Why.** A frame queued while disconnected would either miss its window or land on
the wrong post-reconnect socket. Putting subscriptions in `on_connect` makes them
idempotent across reconnects with no bookkeeping.

---

### 2026-06-20 Transport HTTP: increasing backoff + Protocol limiter seam (PR #13)

**Decision.** `transport.AsyncHTTPClient` retries transient failures (5xx, network
errors, 429) with **monotonically increasing** exponential backoff
(`backoff_base * 2**attempt`, capped at 60s) — review caught and fixed an initial
`base**attempt` form that *decreased* with `base=0.5`. 429 honours `Retry-After`.
The optional rate limiter is typed as a `Protocol` (awaited before each request),
so transport doesn't hard-depend on the not-yet-built `ratelimit` module; `sleep`
is an injected seam for deterministic retry-timing tests. `HTTPError` stays a plain
`Exception` (transport decoupled from `domain.errors`).

**Why.** Backoff must lengthen under a sustained outage, never shorten — a
shortening backoff hammers the venue and risks a ban. The Protocol seam keeps the
leaf order (http before ratelimit) dependency-free.

---

### 2026-06-20 Performance: pure PnL core, KPI delegated to fynance (PR #11)

**Decision.** `domain/performance.py` keeps the PnL/cum-PnL/equity core pure
(numpy/Decimal) and delegates KPI (Sharpe, Sortino, max-drawdown, Calmar) to
**fynance**, imported **lazily** inside each wrapper (`PerformanceDependencyError`
if absent). KPI tests are `pytest.importorskip("fynance")`-gated. mypy stays strict
on the domain via a **typed wrapper** at the boundary (no `pyproject` override) —
fynance results coerced through `float()`.

**Why.** Reuse fynance's vetted metric implementations rather than reimplement
them; the lazy import + skip-gate keep the module importable and the suite green
where fynance (numba) isn't installed, while CI exercises the parity. Decimal money
math stays in the pure core where exactness matters.

---

### 2026-06-20 Signal: two-mode venue-neutral target (PR #10)

**Decision.** `domain/signal.py` expresses a strategy target in one of two
validated modes — fractional exposure in `[-1, 1]` or an explicit signed target
quantity (named constructors `Signal.exposure` / `Signal.target_qty`).
`delta_to(position, reference_qty=None)` returns the signed change to reach the
target; fractional mode **requires** a positive `reference_qty` (max position
size) to scale the exposure into a quantity. This unifies the legacy `signal`
(cumulative target) and `delta_signal` (per-step change) vocabulary.

**Why.** Strategies think in normalised exposure; execution needs concrete
quantities. Carrying both modes (with explicit scaling) lets one `Signal` feed the
order router and the PnL layer without a divergent vocabulary.

---

### 2026-06-20 Position PnL & flip convention from fills (PR #9)

**Decision.** `Position.from_fills` folds an **ordered** fill sequence: realised
PnL only on exposure-reducing fills — long reduce `(exit−entry)·closed`, short
reduce `(entry−exit)·closed`. Every fill's `fee` accrues into `fees_paid` and is
subtracted from realised PnL (fees count on opening fills too). A **flip** (a fill
exceeding the open qty in the opposite direction) closes the whole position
(realising PnL vs the old average), then re-opens the remainder at the flipping
fill's price. `Fill.ts` is `int` milliseconds since the Unix epoch (UTC); fills are
folded in caller-supplied order, not re-sorted.

**Why.** Fills are the source of truth for PnL; a deterministic, explicit fold
keeps positions reconstructable from history alone. ms-epoch ints keep the pure
layer tz-free and match Kraken's granularity.

---

### 2026-06-20 Order lifecycle as an explicit state machine (PR #8)

**Decision.** `domain/order.py` models the order as a **mutable, stateful
aggregate** with a typed `OrderStatus` machine (NEW→SUBMITTED→OPEN→
PARTIALLY_FILLED→FILLED, plus CANCELLED/REJECTED) — replacing the legacy
`{None,'open','canceled','closed'}`. State changes only through five guarded
transitions; `apply_fill` accumulates an exact Decimal quantity-weighted average
price and closes to FILLED when the unfilled fraction is below `fill_tolerance`
(0.1%, ported from legacy `check_vol_exec`). Over-fills are rejected;
`client_order_id` is mandatory (idempotency invariant).

**Why.** An order has stable identity and a long fill-by-fill life; a mutable
aggregate guarded by explicit transitions keeps the machine the single source of
truth without copy-threading noise. The tolerance handles venues leaving sub-tick
dust. **Rejected:** immutable copy-on-transition (noise without added safety).

---

### 2026-06-20 Decimal-guarded Money & Kraken normalisation in `domain/` (PR #7)

**Decision.** `domain/money.py` accepts only `str`/`int`/`Decimal`; a `float`
(and `bool`) raises `TypeError`. The single sanctioned float entry point is
`from_float()`, which routes through `str(value)` to get the shortest
round-tripping decimal (`from_float(0.1) == Decimal("0.1")`). `quantize` defaults
to `ROUND_DOWN` so an order never overshoots a tick/lot. `domain/instrument.py`
normalises Kraken assets with the alias table **mined from the dccd Kraken
adapter** (`XBT→BTC`, `XDG→DOGE`) plus Kraken's legacy 4-char `X`/`Z` prefix rule.

**Why.** Money correctness is a core invariant — `Decimal(0.1)` already carries
binary error, so floats must never enter silently. Reusing dccd's table keeps the
two repos consistent instead of inventing a divergent mapping.

---

### 2026-06-20 Bootstrap the dev environment & Claude workflow (Phase 0)

**Decision.** Bring the repo up to the dccd/fynance standard *before* writing any
new trading code: `pyproject.toml` (replacing `setup.py`), ruff/mypy/pytest/
interrogate, pre-commit, GitHub Actions CI (3.11–3.13), Git Flow (`develop`/
`master`), `CLAUDE.md`, `.claude/` (workflow.json + hooks + settings), and this
`doc/dev/` orientation pack.

**Why.** The tooling is a no-regret prerequisite, identical across the triptych,
and independent of any product decision. Doing it first lets every later epic flow
through the `/pick-task → /plan → /execute-leaf → /finish-task` loop. Product
orientation is fixed only at the "north star" level for now and deepened epic by
epic, to avoid premature over-design.

---

### 2026-06-20 Full rewrite, not incremental modernization

**Decision.** Rebuild trading_bot from scratch as a hexagonal, async-first engine.
Park the pre-2026 code under `trading_bot/legacy/` (reference/spec only, excluded
from tooling) and replace it module by module.

**Why.** The legacy code (2019–2020, Python 3.7/3.8, `multiprocessing` server/
clients over a socket, "not yet working") is far behind dccd/fynance and unsafe to
extend for live trading. A clean rewrite maximises harmony with dccd and lets the
safety invariants be designed in rather than retrofitted. **Rejected:** keeping the
multiprocessing architecture and modernizing in place — too much risk carried
forward for a money-handling system.

---

### 2026-06-20 Scope: execution **and** orchestration

**Decision.** trading_bot is both the execution engine *and* the orchestrator that
wires dccd (data) + fynance (signals) + brokers into one end-to-end trading app.

**Why.** It is the natural conductor of the triptych — the only pillar that needs
all three at runtime. **Rejected:** a thin execution-only library (would still need
an orchestrator somewhere) and a fully standalone bot embedding its own data/
backtest (duplicates dccd/fynance).

---

### 2026-06-20 Multi-exchange by design, Kraken first

**Decision.** Define a `Broker` port + registry so multiple exchanges are
first-class, but implement **only Kraken** (plus a `PaperBroker`) at MVP; other
venues declare capabilities and raise early until built.

**Why.** Multi-exchange shape is cheap to design up front and expensive to retrofit;
implementing one venue well (Kraken, the legacy target) keeps the MVP focused.

---

### 2026-06-20 Paper-trading by default; Decimal money; reconcile-don't-assume

**Decision.** `PaperBroker` is the default; live requires explicit opt-in +
credentials + confirmation. All monetary values are `Decimal`. The engine
reconciles local state against broker-reported orders/balances/fills on startup
and after any disconnect; fills are the sole source of truth for PnL. Every order
passes a `RiskManager` (limits + kill-switch). Orders carry a client-order-id for
idempotent submission.

**Why.** This is a money-handling system; correctness and safety dominate. These
are the hard invariants in `CLAUDE.md`. Whether live becomes a default beyond the
MVP is **deferred**.

---

### 2026-06-20 Keep the name `trading_bot` for now

**Decision.** Defer choosing a final "nice" project name; keep the `trading_bot`
package and repo for the rewrite.

**Why.** Naming is reversible and non-blocking; revisiting it later (a go-live
milestone) avoids churn now. Tracked as a deferred decision to reopen.
