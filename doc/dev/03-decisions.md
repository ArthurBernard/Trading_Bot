# 03 — Decisions (ADR journal)

Newest first. Each entry: the decision, the *why*, and (when relevant) what was
rejected. `/finish-task` appends accepted decisions; `/abandon-task` records
rejected approaches as tombstones.

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
