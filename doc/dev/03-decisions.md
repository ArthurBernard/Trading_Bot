# 03 — Decisions (ADR journal)

Newest first. Each entry: the decision, the *why*, and (when relevant) what was
rejected. `/finish-task` appends accepted decisions; `/abandon-task` records
rejected approaches as tombstones.

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
