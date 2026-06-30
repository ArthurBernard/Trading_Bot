# 03 — Decisions (ADR journal)

Newest first. Each entry: the decision, the *why*, and (when relevant) what was
rejected. `/finish-task` appends accepted decisions; `/abandon-task` records
rejected approaches as tombstones.

---

### 2026-06-30 Control plane: per-strategy engines + a gated `StrategySupervisor` (PR #94)  [accepted]

**Choice.** The control plane (start/stop a strategy, switch its mode from the UI) is
built on a `StrategySupervisor` that splits the config into one **unit per strategy/
portfolio**, each running in its **own** `build_engine` (own broker / mode / tracker /
PnL). `start` / `stop` / `set_mode` / `step` / `status` per unit; `step` calls
`step_latest` / `rebalance_latest`. Real money is gated: `set_mode(..., "live")` raises
`LiveTradingNotEnabled` unless `confirm_live=True` (the UI's typed acknowledgement);
paper ↔ testnet are free. The factory's existing gates (credentials, mandatory risk
limits) still fire when the live engine is actually built on `start`.

**Why.** Per-strategy **mode** (one strategy in testnet, another in live) is impossible
under the single shared engine (`run_app`), which has one broker for the whole system.
A per-strategy engine is just `build_engine` over a one-strategy config slice — the
factory already does the wiring — so the supervisor is a thin manager over N engines.
Per-strategy engines also dissolve the commingling problem (each unit has its own
tracker), so the single-engine `_reject_commingled` concern doesn't arise. Gating live
at `set_mode` preserves the "no real order by accident" invariant now that a UI can flip
modes — matching the chosen posture (paper/testnet free, prod a deliberate confirmation).

**Rejected alternatives.** One engine with multiple brokers and per-strategy routing
(the `OrderRouter` binds to one broker; per-strategy routing is effectively per-strategy
engines anyway, with more coupling); letting the UI flip to real money with no
confirmation (rejected by the maintainer — keep prod deliberate); a heavyweight
process-per-strategy supervisor (in-process units are lighter and share the daemon's
loop/scheduler — wired next).

---

### 2026-06-29 Live monitoring via `run --serve` over the same engine (PR #89)  [accepted]

**Choice.** A `--serve` flag on `trading-bot run` serves the read-only dashboard
(`create_app(engine)`) under uvicorn **concurrently** with the orchestrator, over the
**same** `Engine`. The build is factored into `prepare_system(config) -> PreparedSystem`
(engine + an orchestrator loaded with runners), shared by `run_app` (run + report) and
the serve path. uvicorn owns `SIGINT`: `await server.serve()` blocks until Ctrl-C, then a
`finally` stops the orchestrator (set its stop event, then await/cancel). The dashboard
keeps the existing read-only API + SSE — it never places an order.

**Why.** The dashboard's pre-existing `serve` builds a *fresh* engine and reads the
persisted store — it cannot show a *running* `run`'s live in-memory state, which is what
"monitor the strategies in prod" needs. Sharing the one engine (and its bus, so SSE is
live) is the smallest change that makes the dashboard reflect the running system in real
time. Factoring `prepare_system` avoids duplicating the restore/reconcile/build sequence.

**Rejected alternatives.** Two processes coordinating via the SQLite store (lossy,
lagged, no live SSE — only persisted snapshots); a separate long-lived daemon exposing
the engine over IPC (far heavier than a flag); cloning dccd's full multi-page UI
(custom fonts/logo/auth) for a single read-only dashboard — disproportionate; the
existing clean dark dashboard is kept, a visual restyle left as reviewable polish.
uvicorn-owns-SIGINT over installing the orchestrator's own handlers (avoids two handlers
racing for Ctrl-C).

---

### 2026-06-29 Live fill streaming via a `LiveFillStreamer` hosted by the orchestrator (PR #87)  [accepted]

**Choice.** A new `application.LiveFillStreamer` consumes a structural `FillSource`
(`fills()` async-iterator + `stop()`; `KrakenPrivateWS` satisfies it) and emits a
`FillEvent` per fill onto the engine bus. It exposes the runner's
`run(stop_event=...) -> int` contract, so the `Orchestrator` hosts it as just another
concurrent task; consumption is raced against the stop event and **cancelled** on stop
(a quiet stream never delays shutdown). `KrakenPrivateWS` gains an injected
`on_connected` async hook (awaited after each (re)connect's subscribe); `run_app` builds
the streamer **only** for a real-money live Kraken broker, with `on_connected` =
reconcile, so the engine re-syncs to the venue after every reconnect.

**Why.** The audit's "after-disconnect reconcile + live fills not streamed" follow-up.
The private WS already re-runs `on_connect` on every reconnect, so an injected hook is
the clean reconcile trigger (the WS layer stays ignorant of reconcile — pure DI). The
streamer matches the runner contract so the existing orchestrator hosts it with no new
lifecycle machinery, sharing the one cooperative stop event. Validated **read-only**
against real Kraken (executions snapshot streamed + parsed; **no order sent**).

**Found + fixed in passing.** The read-only live validation exposed that
`GetWebSocketsToken` was absent from the Kraken call-counter cost table — `cost_of`
raised "Unknown method", so the private WS could *never* fetch a token. Added (cost 1).

**Rejected alternatives.** Hooking reconcile inside the transport WS layer (a layering
violation — the WS would import application reconcile); a bespoke lifecycle for the
streamer outside the orchestrator (the runner contract already fits); streaming for
testnet/Binance too (Binance has no private-fill WS adapter; testnet is paper money).
The live **order** path (AddOrder/cancel against a real venue) stays unvalidated by
deliberate choice — read-only live testing only, per the go-live runbook.

---

### 2026-06-29 Project name finalized — keep `trading_bot` (no rename) (PR #84)  [accepted]

**Choice.** The triptych keeps its names: **`trading_bot`** (execution/orchestration),
**`dccd`** (data), **`fynance`** (research). The long-deferred "final project name"
decision is **closed** — there is no rename of the package / repo / docs.

**Why.** The maintainer reviewed naming and chose to keep the existing names. The names
are clear, already established across the three repos and their tooling, and a rename
would churn the package import path, the repo, CI, and every cross-repo reference for no
functional gain. Closing the decision removes a standing "deferred" item that was
otherwise blocking the roadmap from reaching zero open work before go-live.

**Rejected alternatives.** A themed rename of the project/package (considered earlier in
the session, then dropped — "on garde ces noms là"): pure churn, no benefit.

---

### 2026-06-29 Linear position drain via an incremental `Position.with_fill` (PR #82)  [accepted]

**Choice.** Add `Position.with_fill(fill) -> Position` (+ `Position.flat`); reimplement
`from_fills` as a fold of `with_fill` from `flat`; have `PositionTracker` and
`PerformanceService` keep a **running** per-instrument `Position` advanced one fill at a
time, dropping the per-instrument fill lists.

**Why.** The audit's O(n²) follow-up: both `apply` paths recomputed `Position.from_fills`
over the full accumulated fill list on every fill (the performance service did it
*twice*), so draining N fills was Σk = O(n²) — a multi-year daily run/replay through the
engine was slow. Incremental folding is O(1) per fill, O(n) overall.

**Why it stays exact.** `from_fills` is now *implemented* as the same `with_fill` fold, so
the one-shot and incremental results are identical **by construction** — the existing
per-step equivalence tests (`apply` == `from_fills` at every prefix) remain the guard and
stay green; the two can't drift.

**Rejected alternatives.** A bespoke incremental updater duplicated in each service
(re-implements the close/flip/fee logic — invites divergence; instead the single source of
truth is the pure `Position.with_fill`); prefix-memoised `from_fills` (still O(n) list
memory, more complex, same result).

---

### 2026-06-29 Remove the unused `BrokerRegistry` (delete, don't wire) (PR #77)  [accepted]

**Choice.** Delete `brokers/registry.py` (`BrokerRegistry`) rather than wire it into the
factory.

**Why.** The audit found it entirely bypassed: `service_factory.build_engine` selects
venues with an explicit per-venue dispatch (paper / testnet / live + the credential and
opt-in gates), and only the registry's own tests exercised the class. Dead infrastructure
contradicts the repo's "no dead code" stance.

**Rejected alternatives.** Wiring the factory through the registry (no benefit at two-to-
three venues; the explicit dispatch is clearer and already carries the live/testnet/paper
gating that a generic registry would not). If the venue count grows it can be reintroduced
from git history.

---

### 2026-06-29 Portfolio store-key convention is pinned by config, not guessed (PR #76)  [accepted]

**Choice.** `PortfolioStrategyConfig` gains `store_key_format`
(`"venue"` | `"hyphen"` | `"slash"`, default `"venue"`); `build_portfolio_runners`
maps it to a `Symbol -> str` renderer and threads it into the `PortfolioFeed`'s existing
`symbol_for` hook. The universe stays written in canonical `BASE/QUOTE`; the field pins
how those pairs render to the dccd store keys (`BTCUSDT` / `BTC-USDT` / `BTC/USDT`).

**Why.** The audit's open follow-up: `PortfolioFeed` had a `symbol_for` hook but the
config/`run_app` path never exposed it, so a real `trading-bot run <portfolio>.yaml` was
locked to `to_venue_symbol` (`BTCUSDT`/`XBTUSD`) — which doesn't match a hyphen-keyed
dccd store and is ambiguous to invert. LS1 runs were therefore only verified via the test
harness, not raw `run_app`. A declared format removes the guess.

**Rejected alternatives.** Auto-detecting the store key by existence-checking the dccd
store (what a strategy's local test client does — convenient but implicit and untestable
in the engine); putting the field on `DataSourceConfig` (single-instrument strategies
read under the exact `symbol` string given, so they have no re-render ambiguity — the
field is portfolio-specific); a free-form format string (a `Literal` catches typos at
config time).

---

### 2026-06-29 Order dedup state is restored from the store on startup (PR #75)  [accepted]

**Choice.** `OrderRouter.restore(orders)` seeds the in-memory dedup map (`_orders`)
from the append-only `SqliteStore` on startup — emitting **no** events and never
clobbering an id the router already tracks — and `run_app` calls it (when a store is
configured) **before** the startup reconcile. A re-submit of any persisted
`client_order_id` then dedups in `submit` (returns the recorded order, no broker call).

**Why.** The audit's most dangerous interaction: Kraken issues no venue-side idempotency
token, so dedup rests entirely on the in-memory map. A crash between `AddOrder` success
and recording it loses that map; on restart a deterministic id (`{name}-{step}`) would
re-submit and **duplicate the order at the venue**. Reconcile-on-startup (PR #71) only
recovers orders the venue still reports **open**; the store recovers *every* recorded id
(including filled/rejected) — together they close the common crash-restart window.

**Rejected alternatives.** Reusing `ingest` to seed (it emits an `OrderEvent`, so a
dashboard/store would see historical orders as fresh activity — `restore` is silent);
deduping by object identity (a re-submit is a new `Order` with the same id). **Residual
gap (unclosed):** an order that *filled during the crash gap, before any persist* leaves
no local record and reconcile sees no open order — only a venue-side dedup token closes
that, which Kraken lacks (the documented real-key-sandbox prerequisite).

---

### 2026-06-29 Fills are de-duplicated by `fill_id` in the read-side views (PR #74)  [accepted]

**Choice.** `PositionTracker.apply` and `PerformanceService.apply` each keep a
`set[str]` of folded `fill_id`s and ignore a fill whose id was already seen (the
tracker returns the standing position; the service makes no PnL/fee/equity change).
`PositionTracker.reset` clears the set so a reconcile rebuild from the broker's fills
re-folds them.

**Why.** The audit found both `apply` paths explicitly skipped dedup, trusting "the
broker's fill stream is the de-duplicated source." That holds for the PaperBroker today,
but a live **private fill WS** can re-emit an execution (Kraken v2 resends a snapshot on
resubscribe after a reconnect). With no dedup, the tracker would double the position and
the service's *incremental* running realised PnL would be silently corrupted with no
detectable error. Dedup-by-id makes both views safe to feed from a live stream.

**Rejected alternatives.** Relying on the upstream stream to never duplicate (fragile —
exactly the reconnect case we must survive); deduping by object identity (a re-emit is a
*new* `Fill` object with the same id); keying the set by `(instrument, fill_id)` (venue
trade ids are unique per venue — a flat id set is enough and simpler). Not adding a perf
`reset` (the service accumulates over the run session; the id set growing is bounded by
fills seen and is fine).

---

### 2026-06-29 Real-money live requires all three risk limits (PR #73)  [accepted]

**Choice.** On the real-money live path (`mode: live` + `live_enabled` + credentials),
`build_engine` refuses to return the live adapter unless `RiskConfig` sets all of
`max_order`, `max_position` and `max_daily_loss` (a `BrokerError` naming the gaps).
The check sits **after** the credential gate, so the existing "no creds → BrokerError"
ordering is unchanged; paper and testnet return earlier and are exempt.

**Why.** A pre-production audit found `RiskConfig` defaults every limit to `None`
(unconstrained), so a live config with no `risk:` block would place orders with no
size, exposure or daily-loss cap. For real money the limits must be deliberate, not
opt-in-by-omission.

**Rejected alternatives.** A pydantic `model_validator` on `AppConfig` (fires at config
construction, which would change the error surface of several broker-selection tests
that build `live_enabled` configs without limits, and would couple config validity to a
risk policy); requiring limits for testnet too (testnet is paper money — over-strict).
`ConfigError` vs `BrokerError` — kept `BrokerError` to match the factory's existing
"refusing to trade live without X" vocabulary.

---

### 2026-06-29 Daily-loss limit is wired to live PnL and escalates to the kill-switch (PR #72)  [accepted]

**Choice.** `build_engine` passes `daily_pnl_provider=perf.realised_pnl` to the
`RiskManager`, and the `OrderRouter` escalates a `max_daily_loss` breach to
`RiskManager.kill(router=self)` — cancel resting orders + trip — before re-raising.

**Why.** A pre-production audit found `max_daily_loss` was inert: the factory wired no
provider, so the gate read a constant zero and never halted; and `kill()` / `trip()`
had no production trigger. `max_daily_loss` is documented as the day's *halt* threshold
("trading halts for the day"), so reaching it must stop the whole book, not merely
refuse the one breaching order.

**Rejected alternatives.** Tripping inside `RiskManager.check` (it is synchronous and
holds no router/broker, so it cannot cancel resting orders — the async escalation
belongs in the router, which owns the broker handle); a separate fill-stream monitor
(more moving parts; the breach is already observed at the gate on the next order).
"Daily" is the run session (the manager owns no clock); a multi-day reset wires
`reset_day` / a perf reset to a scheduler — deferred.

---

### 2026-06-29 Reconcile-on-startup is wired into `run_app` (PR #71)  [accepted]

**Choice.** `run_app` calls `reconcile(broker, router, tracker, event_bus=…)`
immediately after `build_engine`, before any runner starts (new opt-out
`reconcile_on_start: bool = True`). A freshly-built engine has **empty** local maps;
the venue may already hold open orders + a fill history (a restart). Reconciling first
ingests venue-open orders, closes orphans, and rebuilds positions from confirmed fills,
so the engine never re-submits a venue-held order or trades a stale (zero) position.
A no-op on a fresh `PaperBroker`; the safety backstop on a live/testnet venue.

**Why.** A pre-production audit found the *reconcile, don't assume* invariant was fully
implemented and hardening-tested but had **zero production callers** — it was inert.
The single system entrypoint, before the orchestrator runs, is the lowest-risk wiring
point and keeps every caller (CLI, daemon, tests) converging identically.

**Rejected alternatives.** Reconcile inside `build_engine` (the factory is sync and
does no I/O — keep it pure); reconcile per-runner (the engine/broker is shared, one
pass suffices). The **post-disconnect** half of the invariant (reconcile on a WS
reconnect) is **deferred**: the private fill WS is not wired into the run loop yet, so
there is no reconnect to hook — it lands with live fill streaming (tracked in the
roadmap).

---

### 2026-06-29 Strategies are local-only — never committed to the engine repo (PR #70)

**Decision.** Concrete **strategies** — the signal wrapper, the strategy config(s),
and the strategy's e2e tests — are **never committed** to `trading_bot`. They live
**local-only** under a gitignored `strategies/` tree (`strategies/*` ignored, with the
`example/` + `another_example/` templates and `README.md` negated/tracked). The engine
ships only the **generic** machinery — the portfolio abstraction (`application/
portfolio*.py`, `AppConfig.portfolios`, `ResamplingDccdClient`) and the generic
`as_portfolio_signal` adapter — which runs any strategy **by reference**
(`module:function` + a YAML config) without importing it. This corrects an earlier
slip where the LS1 wrapper/configs/tests were pushed via PRs #67/#69; they were moved
out (the engine-generic adapter tests were retained, moved into
`test_portfolio_signal.py`). The `v0.2.0` tag was never affected (it predates LS1).

**Why.** `trading_bot` is the **shareable execution engine** of the triptych; strategy
logic/IP (and deployment specifics) belong with the research (`fynance-research`) /
the operator's local setup, not in the engine repo. Keeping the engine free of any
concrete strategy also keeps it genuinely generic and its test suite free of strategy
data dependencies. Operationally: a request to *test a strategy locally* means run it
and report — not commit/push/merge. **Rejected:** committing strategies "because the
live test is useful" (leaks IP into the engine repo, couples the engine to one
strategy); a history purge of the earlier slip (the content is only on `develop`, not
in any tag — a tip-level removal + gitignore is enough; force-rewriting a shared
branch's history is not warranted).

---

### 2026-06-29 LS1 multi-venue live tests; Kraken is public+PaperBroker (no testnet) (PR #69)

**Decision.** LS1 is now wired for **both venues** by config — `ls1_kraken_signal`
(calls the research oracle with `venue="kraken"`, USD pairs) alongside the Binance
`ls1_portfolio_signal`, each a thin `examples/ls1_signal.py` wrapper over the generic
`as_portfolio_signal` adapter; `configs/ls1_kraken.yaml` mirrors `configs/ls1.yaml`
on the `-USD` universe + dccd Kraken store. The **live tests** differ by venue because
the venues differ: **Binance** has a testnet, so its order round-trip is the opt-in
`network` testnet test (real orders, paper money); **Kraken has no public spot
testnet**, so its live test (`test_ls1_kraken_real_e2e`) runs the **real** LS1 Kraken
signal over the **real** dccd Kraken store + a **live Kraken public-ticker** sanity
check, but routes the rebalance through the **`PaperBroker`** — **no real order is ever
placed** (the maintainer chose public+PaperBroker over a real-money Kraken order test).

**Why.** A live test on Kraken that *places* orders is real money (no sandbox exists),
which violates the project's "no real order by accident" posture; running the real
signal + real data + live public prices through the simulator validates the whole
chain (data → signal → sizing → delta → routing → risk) bar the venue's order
acceptance, with zero financial risk. Real Kraken order placement stays the deliberate
go-live opt-in (`doc/dev/09-go-live.md`). **Rejected:** a real-money Kraken order test
(footgun — a stray `-m network` run with a real key trades for real); a Kraken testnet
(does not exist).

**Finding (tracked in `07-roadmap.md`): venue symbol renders are ambiguous to invert.**
`to_venue_symbol("kraken")` yields `XBTUSD` (which `parse_binance_symbol` mis-reads as
`XB/TUSD` — the `TUSD` quote) and `TRXUSD` (which `parse_kraken_pair` mis-reads as
`TR/USD` — the trailing `X` looks like a legacy prefix); no single parser inverts both.
The test's fake dccd client disambiguates by trying both parsers and picking the
candidate dir that **exists** in the store — but this exposes that the **production**
config → real-dccd path has no agreed store-key convention (the default `PortfolioFeed`
render does not match a hyphen-keyed `BASE-QUOTE` store for either venue; only the test
client normalises). A real `trading-bot run configs/ls1*.yaml` against a live dccd
client needs that convention pinned.

---

### 2026-06-29 Testnet is a third broker path; it bypasses live_enabled by being mainnet-incapable (PR #68)

**Decision.** A `BrokerConfig.testnet: true` selects a venue's **testnet/sandbox**
(paper money on the real testnet venue) as a path distinct from both paper (the
in-process simulator) and live (real mainnet). Under `mode: live`, a `testnet: true`
broker builds the venue adapter **hard-pinned** to the testnet URL — the base URL is
passed explicitly (`BinanceBroker(base_url=TESTNET_API_BASE)`), overriding any
`BINANCE_API_BASE` env — so it is structurally incapable of reaching mainnet. Because
it cannot touch real money, this path is **exempt from the `live_enabled` opt-in**
(checked *before* that gate); it still requires (testnet) credentials. Only venues with
a testnet qualify (`_TESTNET_VENUES = ("binance",)`); **Kraken raises** (no public spot
sandbox). Paper mode still wins (testnet ignored). `BinanceBroker` gained `base_url`/
`is_testnet` read-only introspection.

**Why.** The maintainer wanted to live-test orders on the engine path *safely and
without ceremony*. The previous route — `mode: live` + `live_enabled: true` + setting
`BINANCE_API_BASE` to the testnet — overloaded the real-money opt-in for a paper-money
sandbox and risked **mainnet by omission** (the base URL defaults to mainnet, so a
forgotten env var trades for real). Pinning the URL from the flag makes "testnet
cannot become mainnet" structural, which is precisely what justifies skipping
`live_enabled` — the gate exists to prevent *real-money* accidents, and there are none
here. **Rejected:** a separate `mode: "testnet"` (more surface; the venue, not the
mode, is what has a sandbox); honouring `BINANCE_API_BASE` under the flag (re-opens the
mainnet-by-omission hole); making testnet require `live_enabled` (the friction the
request set out to remove). Kraken testnet was rejected because it does not exist.

---

### 2026-06-29 LS1 wired by config via a generic weight-oracle adapter (PR #67)

**Decision.** LS1 runs end-to-end **without any LS1 code in the engine**. A generic
`application.as_portfolio_signal(weights_callable)` bridges an *argument-free*
research oracle (`() -> {pair: weight}`, optionally `(weights, asof)`) to the
`PortfolioSignalFn` contract: it **ignores** the passed frames (the oracle reads its
own store) — the frames still drive the runner's freshness gate and per-leg prices —
and normalises pair-string keys → canonical `Symbol` (`parse_binance_symbol`,
hyphen/concat both) and weights → exact `Decimal`. The LS1 glue is a thin
`examples/ls1_signal.py:ls1_portfolio_signal` that binds
`fynance_research.strategies.ls1_live:target_weights` through the adapter with a
**lazy** import (config load + ref resolution need no research dep); `configs/ls1.yaml`
(paper) points `signal.ref` at it. Real daily bars come from the
`ResamplingDccdClient` (1m→1d). Real-data verification ran against the live dccd
Binance store with a deterministic weight vector (asof 2026-06-27, 3-coin book):
routed per-coin deltas == `wᵢ·capital/priceᵢ` on real closes, broker-confirmed.

**Why.** The triptych split is research → signal, trading_bot → execution; keeping
the only LS1-aware glue a *generic, parameter-free adapter* loaded by reference means
the engine never imports the research repo and any future weight oracle plugs in the
same way. Lazy-importing `fynance_research` keeps the engine installable and the
offline suite green without it. **Rejected:** an LS1-specific runner/signal in
`trading_bot` (couples the engine to one strategy); a hard `fynance-research` dep
(forces the research repo on every install); passing the frames into the oracle (its
API reads its own store — the frames' role is the gate + prices, not the weights).

**Known follow-ups surfaced by this epic (tracked in `07-roadmap.md`):** (1) the
PaperBroker/engine drain is **superlinear** (~O(n²)) over accumulated ticks, so a
full multi-year daily backtest through `run_app` is slow — the real-data tests assert
on a single latest-cross-section rebalance instead; (2) a **dccd API drift** breaks two
`-m network` data-feed tests (`Client().inventory()` outside `async with`).

---

### 2026-06-29 Portfolio config + run_app wiring; resample-on-read daily seam (PR #66)

**Decision.** A portfolio is declarable via `PortfolioStrategyConfig` (universe +
`SignalRefConfig` + `capital` + daily `DataSourceConfig` + optional `gross_cap` +
`venue`) on `AppConfig.portfolios`; `run_app`'s `build_portfolio_runners` resolves
the signal by reference, builds a `PortfolioFeed` + `PortfolioRunner` on the **shared**
engine, and reports per-coin. **Overlap detection** is widened: no instrument may be
claimed by two runners — strategy↔strategy (existing), strategy↔portfolio, or
portfolio↔portfolio — because the shared per-instrument tracker has no attribution. A
portfolio is exempt from the *intra*-single-instrument `_reject_commingled` (it owns
its whole universe) but its universe must not intersect any other runner's.

The daily-bars seam: `ResamplingDccdClient` wraps a real dccd client, reads the
**1-minute** store and aggregates OHLCV to daily (`open=first, high=max, low=min,
close=last, volume=sum` via `group_by_dynamic(every="1d", closed="left",
label="left")`), causal — closed days only, the still-forming last day dropped, OHLC
carried **exact** (no float). It is **injectable, not auto-wrapped** by `run_app`:
the caller wraps the real client when a daily portfolio reads a 1m store; offline
tests inject a fake *daily* client directly. `fynance-research` is documented as an
editable `[triptych]` extra (LS1's signals live there; the engine stays generic).

**Why.** dccd serves only 1m (it does not resample — `read(span=86400)` → 0 rows), so
a daily consumer must resample, mirroring `fynance_research/data.py`. Making the
resampler an injectable client keeps `PortfolioFeed`/`run_app` agnostic and the
offline tests resample-free; auto-wrapping was rejected because `run_app` can't tell
the store's native span from the requested span without a new config signal — deferred
until needed. Widened overlap detection prevents two runners silently fighting over
one instrument's shared position. **Rejected:** resampling inside `PortfolioFeed`
(couples it to the store's native span); auto-wrapping in `run_app` (needs a
source-span config field — premature); a hard `fynance-research` dependency (would
force the research repo on every install).

---

### 2026-06-29 PortfolioRunner — whole-universe targeting, per-leg failure isolation (PR #65)

**Decision.** `application.PortfolioRunner` mirrors `StrategyRunner` for a whole
universe: per tick it sizes the weight vector (`weights_to_signals`) and routes one
leg per coin through the **shared** `OrderRouter`/`PositionTracker`/`EventBus`.
Three choices: (1) **whole-universe targeting** — it iterates `strategy.universe`,
not the weight keys, synthesising a **0-weight (flat)** target for any coin the
signal omits, so a dropped name is closed rather than left stranded (the book always
covers the full universe). (2) **Per-coin idempotency id** `f"{name}-{symbol}-{step}"`
— a re-run/retry dedups per coin per rebalance at the router. (3) **Per-leg failure
isolation** — a leg that raises `RiskLimitBreached`/`BrokerError` is caught, recorded
as a `RebalanceFailure` on the `RebalanceResult`, and the **other legs still route**
(not all-or-nothing). Maker-LIMIT legs priced at each coin's latest close (fee +
self-contained paper fills). The optional gross guard stays **off** (trust the
signal's cap, per [[the leaf-01 portfolio-signal ADR]]).

**Why.** A cross-sectional book is only correct as a *set* — leaving an omitted coin
at its old position silently changes the realised exposure, so omitted ⇒ flat. Per-leg
isolation: aborting the whole rebalance because one coin breached a limit would leave
the book half-rebalanced and let one bad name veto every good one; recording the
failure and continuing keeps the rest of the book on target and surfaces the breach.
Idempotency at the position level (delta vs the shared tracker) already makes a
deterministic re-run a no-op *before* id-dedup matters; the namespaced id covers the
retry-after-ambiguous case. **Rejected:** all-or-nothing rebalance (one bad leg
freezes the book); iterating the weight keys (would strand omitted coins);
re-implementing submission instead of reusing the risk-gated `OrderRouter`.

---

### 2026-06-29 PortfolioFeed — common-index inner-join, no forward-fill; dccd serves 1m only (PR #64)

**Decision.** `application.PortfolioFeed` assembles the N-coin cross-section by an
**inner-join on the bar timestamp**: a rebalance date is emitted only when **every**
coin has that day's closed bar. A coin lagging the universe is **logged, never
raised**, and its missing tail days are simply not emitted — a stale close is
**never forward-filled** into the cross-section. It reuses the single-coin
`DccdFeed` per coin (no new dccd read logic), stays causal (`frame[:t+1]` per coin,
no lookahead), and is client-injectable (offline tests dccd-free). A `symbol_for`
hook renders a canonical `Symbol` to the store's pair-key convention (the real
Binance store is keyed `BTC-USDT`; `to_venue_symbol("binance")` yields `BTCUSDT`).

**Finding (load-bearing downstream): dccd serves only 1-minute bars.** dccd's
`read` is keyed by `span` and does **not** resample — `read(span=86400)` against the
real Binance store returns 0 rows. Daily bars are a **consumer** responsibility
(mirroring `fynance_research/data.py`, which resamples 1m→1d via polars
`group_by_dynamic(every="1d")`). `PortfolioFeed` is kept **agnostic** — it consumes
whatever daily bars the injected client returns — so the **live wiring (leaf 04)
must inject a resample-on-read dccd client (1m→1d)**, and leaf 05's LS1 run must
resample before feeding. The real-data verification ran against the live store with
a resample-on-read client: 10 coins, 2011 daily dates (2020-08-18 → 2026-06-28),
common index, causality verified.

**Why.** A cross-sectional signal ranks each coin *relative to* the others *as of
the same day*; a missing/stale bar on one coin silently shifts the ranking (today's
BTC vs yesterday's ZEC) — a quiet corruption. Inner-join + no-forward-fill makes
"all coins fresh, or the day is skipped" structural. **Rejected:** forward-filling a
stale close (corrupts the cross-section); raising on a lagging coin (a single late
sync would halt the whole book — degrade to the common-complete prefix instead);
re-implementing the dccd read here (would duplicate/diverge from the single-coin path).

---

### 2026-06-28 Portfolio signal contract — weight vector + explicit-qty sizing (PR #63)

**Decision.** The multi-asset strategy contract is a `PortfolioSignalFn`
`(asof_ms, {Symbol: frame}) -> {Symbol: weight}` — a **weight vector**, weight =
**signed fraction of capital**, returned for the whole universe in one shot
(`application.portfolio`). Sizing is `qty = weight × capital / price`, emitted as
an **explicit-quantity** `Signal.target_qty` (not a fractional
`SignalMode.EXPOSURE`). The signal is loaded **by reference** (`module:function`,
safe `importlib`+`getattr`, no loose-file exec), exactly like the single-instrument
signal; there is no builtin portfolio registry. The engine **does not re-normalise**
the vector (it trusts the signal's own `Σ|w|` cap); `gross_cap` is recorded,
advisory.

**Why.** LS1 (the first validated multi-asset strategy) is a *vector* of target
weights over ~10 coins, gross-capped 2× — the single-instrument `(bars) -> Signal`
can't express it, and per-coin weight can exceed 1 under leverage, which a bounded
`[-1, 1]` exposure signal would reject. An explicit-qty signal carries its own
scale, so `delta_to(position)` needs no `reference_qty` and the runner diffs
directly against live positions. Keeping the loader by-reference keeps the engine
**generic** — LS1 lives in config, not in `trading_bot`. **Rejected:** a fractional
exposure vector (can't express `|w|>1`); the engine re-normalising the book (would
silently override the strategy's risk sizing); a frame-coupled sizer (kept
`weights_to_signals` pure by passing prices explicitly).

---

### 2026-06-28 BinanceBroker — 2nd live venue; composite id, fills scoping, newClientOrderId, testnet (PR #61)

**Decision.** `BinanceBroker` (spot REST) is the second adapter behind the
venue-neutral `Broker` port, proving the multi-exchange design end-to-end. Four
Binance-specific choices were made to fit the symbol-free, Kraken-shaped port:
- **Composite venue-order-id `"<SYMBOL>:<orderId>"`.** Binance's `cancel`/order
  endpoints require the **symbol**, but `Broker.cancel_order(venue_order_id)`
  carries only an id. The adapter therefore makes its venue id the composite
  `symbol:orderId` (produced identically by `place_order` *and* `open_orders`, split
  on the **last** `:`), so reconcile→cancel works. The id stays opaque text to the
  router/store.
- **`fills()` over a configured symbol set.** Binance has no account-wide trade
  history; `myTrades` is per-symbol. `BinanceBroker(symbols=…)` queries each; with no
  symbols it raises a clear `BrokerError` rather than silently returning `[]`.
- **`newClientOrderId` venue-level idempotency.** The domain `client_order_id`
  (`f"{name}-{step}"`) is forwarded as `newClientOrderId` when it fits Binance's
  `[.A-Za-z0-9:/_-]{1,36}` constraint — a real venue-side dedup (Binance rejects a
  duplicate with `-2010`), kept **alongside** the `retry=False` reconcile-on-ambiguous
  policy (defence in depth; improves on Kraken, which has no usable client id).
- **Testnet-capable base URL.** `base_url` (env `BINANCE_API_BASE`) toggles
  `testnet.binance.vision`, enabling an opt-in `network` E2E real round-trip in paper
  money — Binance offers a real spot testnet (Kraken did not), so this is the cheapest
  path to real-venue order verification.

Two supporting changes: a small public `transport.AsyncHTTPClient.request(method, …)`
seam (Binance signs `DELETE` for cancels — GET/POST-only was insufficient), same
retry/ambiguity semantics; and `STOP_LOSS` maps to Binance `STOP_LOSS_LIMIT` resting
at the stop price (Binance has no symbol-free stop-market that fits the domain shape).
Posture unchanged: public data key-free, private path proven by mocks + the signing
vector + a key-gated testnet E2E; **no mainnet order is ever sent**; paper stays the
default behind the `live_enabled` opt-in.

**Why.** A second venue is the real test of the port abstraction. Binance breaks two
of Kraken's conveniences (symbol-free cancel, account-wide trade history); solving both
inside the adapter (composite id, configured symbol set) keeps the port and the engine
unchanged — the abstraction holds. `newClientOrderId` is a genuine idempotency upgrade
worth taking even though live is still off. **Rejected:** widening the `Broker` port to
pass a symbol into `cancel_order`/`fills` (would leak a venue quirk into every adapter
and the engine); a per-call DELETE hack in the adapter instead of the transport seam
(bypasses the shared retry loop).

---

### 2026-06-28 Binance symbol parsing — longest-first quote-suffix table (PR #60)

**Decision.** `domain.instrument.parse_binance_symbol` splits Binance's
separator-less pair codes (`BTCUSDT`) by matching the **longest** known quote
asset as a suffix, from a module `_BINANCE_QUOTES` table (`FDUSD/USDT/USDC/…/USD`,
ordered longest-first), leaving the remainder as the base; an explicit separator
(`/`, `-`, `_`) short-circuits first. Mirrors `parse_kraken_pair`. Binance uses
canonical asset codes (no Kraken `X`/`Z` prefixes, no `XBT` alias), so `normalise`
passes them through and `to_venue_symbol`'s generic `f"{base}{quote}"` branch
already renders Binance pairs — only the inverse parse was missing.

**Why.** A Binance symbol carries no separator and no embedded base/quote split,
so the quote must be inferred. A longest-first suffix table disambiguates
`USDT`/`USD` and `FDUSD`/`USD` deterministically and keeps the domain **pure**
(no network). **Rejected:** resolving base/quote via a `/exchangeInfo` call
(impure — couples the domain to I/O); a regex (less explicit, same ambiguity
risk).

---

### 2026-06-28 Go-live is an off-by-default opt-in; rewrite feature-complete

**Decision.** Going live is an explicit, documented, **off-by-default** opt-in:
`mode: live` alone is insufficient — `AppConfig.live_enabled` (default `False`) is a
second gate that `build_engine` checks **before** credentials, raising
`LiveTradingNotEnabled` (pointing at `doc/dev/09-go-live.md`) when not set; the CLI
`run --live` mirrors it (interactive ack **and** `live_enabled`, else a clear refusal
that places nothing). Even when enabled with credentials, the live `KrakenBroker` is
only *constructed*, never called — **no real order is ever sent from this repo**. The
runbook documents the deliberate enable steps, the pre-trade safety checklist, and a
proven-vs-pending table. This closes **E10** and the E1–E10 rewrite: a feature-complete
hexagonal engine (paper-validated, hardened under fault injection) conducting the
triptych via CLI + web UI.

**Why.** Paper-by-default + a second explicit opt-in makes "no live by accident"
structural at every layer (config, factory, CLI). The one remaining live prerequisite —
**validating private endpoints + venue-level idempotency against a real-key sandbox** —
is intentionally out of the repo (needs a real key, maintainer decision). **Still
deferred:** the **final project name** (kept `trading_bot`) and real-key live
enablement; both stay open in the roadmap.

---

### 2026-06-28 Close known gaps: KPI capital, reject same-symbol, AddOrder non-retry

**Decision.** Three recorded gaps closed (offline; live still off):
(a) **KPI v0** — `AppConfig.starting_capital` (default `money("100000")`, strictly
positive) is wired into `PerformanceService(v0=)`; a fill-driven equity curve anchored
at 0 sign-crosses on the first fee, making fynance's ratio estimators degenerate, so a
positive anchor makes Sharpe/Sortino/Calmar meaningful (CLI `--capital` > config > the
built-in default; the API `_safe_ratio` also coerces `inf`/`nan` → `0.0`, since a winning
curve can now make fynance *return* `inf`). (b) **Same-instrument** — `build_runners`
**rejects** two strategies on the same normalised `Symbol` with a `ConfigError` (catches
the `XBT/USD`≡`BTC/USD` alias) rather than silently commingling them in the shared
per-instrument tracker. (c) **AddOrder retry** — `AsyncHTTPClient.post` gains
`retry=False` (at-most-once, raising `AmbiguousRequestError`); `KrakenBroker.place_order`
uses it so a `AddOrder` is never blind-retried after an ambiguous failure, pointing the
caller at `reconcile`; idempotent reads keep retrying.

**Why.** These were the gaps safe to close without a real venue. (a) and (b) make the
observability/orchestration honest; (c) closes the transport-level double-submit window
the router's in-memory dedup didn't cover. **Still deferred to a real-key sandbox:**
true *venue-level* idempotency (a venue dedup token surviving an engine crash). The
**final project name** and **default paper-vs-live** also remain deferred.

---

### 2026-06-28 Hardening: prove safety invariants under fault injection (offline)

**Decision.** `tests/hardening/` *demonstrates* the money-safety invariants
adversarially via a `FaultyBroker` wrapping `PaperBroker` — deterministic one-shot
faults (`fail_next_place`, `ambiguous_next_place` = the venue records the order but the
response is "lost", `disconnect`/seed) that change only what the *caller* observes,
never the venue's recorded truth. Eleven tests prove: reconcile converges after a
disconnect (no order duplicated/lost; second pass a no-op); engine-side idempotency
holds under sequential **and** concurrent retries; an ambiguous failure surfaces rather
than fabricating success, and `reconcile` is the recovery (adopt the live order, never
re-submit); the kill-switch cancels open orders + halts new ones mid-run.

**Why.** A money-handling engine's safety can't rest on happy-path unit tests — the
dangerous cases are faults (lost responses, disconnects, concurrent retries). **Proven
offline:** engine-side idempotency, ambiguous-failure surfacing, reconcile-as-recovery,
kill-switch. **Still needs a real-key sandbox (E10-02 / live):** *venue-level*
idempotency — a venue-side dedup token so a retry the engine *forgot* (e.g. a crash
before the reject was persisted) can't create a second order at the exchange. Today the
dedup is in-memory engine-side only; this gap is documented at the ambiguous-failure
tests.

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
