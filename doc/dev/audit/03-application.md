# Audit — application layer (2026-07-02)

## Scope & method

Deep read-through audit of every file in `trading_bot/application/` (~8 960 lines)
and its tests under `trading_bot/tests/application/` and
`trading_bot/tests/hardening/`. The engine trades real money, so the review is
weighted toward the standing invariants: paper-by-default + live gating,
idempotent order submission, reconcile-don't-assume, fills-as-PnL-truth,
risk-gate-every-order + kill-switch, Decimal-only money, async correctness, and
layering.

Method: read all 21 source files completely, then read-only static checks —
`grep` for `interfaces` imports (layering), `float`/`from_float` (money leaks),
`asyncio` task/lock patterns, `TODO/FIXME`, and the SqliteStore event-bus wiring
(blocking I/O). Ran `python -m pytest trading_bot/tests/application
trading_bot/tests/hardening -q` offline (no `-m network`): **380 passed, 4
deselected**. No source was modified; no network tests were run; no servers were
started.

Cross-cutting positives worth recording: **no `interfaces` import** anywhere in
the layer (layering is clean); **no `TODO/FIXME/HACK`**; money is Decimal
end-to-end through the order/fill/position/PnL paths (the only `float` crossings
are the KPI-ratio boundary — documented and correct — and cosmetic `0.0`
defaults); the idempotency in-flight-future guard in `order_router.submit` is
carefully correct; the `_replay_paper_book` per-mode filter (the past
commingling bug) **is fixed and its siblings — `pnl_series`, `by_mode`,
`combined_equity_series`, `_combined_ratios` — are all mode-partitioned
correctly** (no sibling regression).

## Summary

The layer is well-structured and heavily documented, and most invariants hold.
The most serious finding is **not** a duplicate-order or paper→live leak (those
gates are solid) but a **semantic gap in `max_daily_loss`**: the factory wires
the day-loss circuit breaker to `PerformanceService.realised_pnl()`, which is
**cumulative session PnL, not the current day's**, and *nothing wires
`reset_day`* — so the "daily" loss limit is really a whole-run cumulative-loss
limit that never resets at a day boundary, and once tripped it escalates to the
kill-switch and halts the book for good (A-1). The second material issue is that
**every persisted order/fill drives a synchronous SQLite open→write→commit→close
inside the async event loop** via `SqliteStore.attach`'s bus handler, on the hot
submit/fill path (A-2). Beyond those, a cluster of Medium concurrency/robustness
items around the un-locked supervisor (`step` vs `set_mode`/`stop` races,
per-tick full-feed drains) and a few config-fidelity gaps (UI token guard not
enforced at the config layer; `to_yaml` writes all defaults and drops comments;
`combined_equity_series` over-anchors `v0`).

| ID | Severity | Location | Title |
|----|----------|----------|-------|
| A-1 | High | risk.py:242-246; service_factory.py:206-210 | `max_daily_loss` uses cumulative session PnL and never resets — "daily" is a misnomer, and `reset_day` is dead |
| A-2 | High | sqlite_store.py:481-507, 241-253; service_factory.py:214-216 | Synchronous SQLite I/O runs in the async event loop on every order/fill |
| A-3 | Medium | supervisor.py:665-685, 620-663 | `step` vs `set_mode`/`stop` are un-locked coroutines sharing `_Unit` state — a race can `step` a torn-down unit or double-build an engine |
| A-4 | Medium | config.py:443-479 | `UIConfig` does **not** enforce the "non-loopback host requires token" guard it documents — a persisted manifest can bind wide-open |
| A-5 | Medium | config.py:593-617 | `to_yaml` round-trip drops comments/key intent and writes **every default** (incl. `live_enabled: false`, full `ui:` block); manifest churn + fragile hand-edits |
| A-6 | Medium | supervisor.py:1200-1209, 1172-1209 | `combined_equity_series` sums `v0` over **all** named units even those with zero fills in the requested mode — inflated equity anchor skews aggregate KPIs |
| A-7 | Medium | portfolio_runner.py:433-457 | `rebalance_latest` drains the **whole** feed each daemon tick (O(total bars) per tick) to keep only the last cross-section |
| A-8 | Medium | order_router.py:319-353 | `cancel` is not idempotent and has no in-flight guard — a double-cancel / concurrent cancel re-hits the venue and can raise on the local transition |
| A-9 | Low | order_router.py:431-467; run_app.py:757-758; supervisor.py:558 | `router.restore` seeds the dedup map but never restores in-flight `_inflight`; a crash mid-submit before persist still leaves a residual double-submit window (documented, but no venue-side idempotency token) |
| A-10 | Low | supervisor.py:420-443 | `remove_unit` is sync but inlines async `stop`'s teardown by hand — divergence risk if `stop` ever gains awaited I/O |
| A-11 | Low | risk.py:250-277; performance_service.py | Provider-backed `RiskManager` makes `record_daily_pnl`/`reset_day` dead code; the two daily-PnL sources can silently disagree |
| A-12 | Low | strategy_runner.py:344-366; portfolio_runner.py:459-478 | Order factories mutate a "frozen-looking" `Order.client_order_id` after construction (aggregate is not actually immutable) |
| A-13 | Info | run_app.py:487 & 768; supervisor vs run_app | `_reject_commingled` runs twice per `run_app`; three runner-ish modules and two full wiring paths (`run_app` vs `supervisor`) duplicate restore+reconcile logic |
| A-14 | Info | run_app.py:285-286, 98 | `from_float(0.0)` used for `RunReport` money defaults (exact for 0, but a `float` entrypoint into a money field) |

## Findings

### A-1 [High] `max_daily_loss` uses cumulative session PnL and never resets — "daily" is a misnomer, and `reset_day` is dead

**Location.** `service_factory.py:206-210` (wiring); `risk.py:242-246`
(`_daily_pnl`); `risk.py:216-228` (`_check_max_daily_loss`); `risk.py:268-277`
(`reset_day`).

**Evidence.** The factory wires the breaker to the *cumulative* realised PnL:

```python
# service_factory.py
risk = RiskManager(
    config.risk,
    position_tracker=tracker,
    daily_pnl_provider=perf.realised_pnl,   # cumulative, session-lifetime
)
```

`PerformanceService.realised_pnl()` returns `self._realised_pnl`, the running
total across **all** fills since the engine was built — never per-day. The risk
manager's own docstring concedes this: *"'Daily' here is the run session (no
clock); a multi-day reset wires `reset_day` to a scheduler."* But **no scheduler
calls `reset_day` anywhere** (grep finds zero call sites outside tests), and even
if one did, `reset_day` only zeroes `self._recorded_daily_pnl` — the fallback
that a provider-backed manager **ignores** (`_daily_pnl` returns the provider's
value when a provider is set, `risk.py:242-246`). So there is no code path that
can ever reset the day for the wired configuration.

**Impact.** The documented invariant "max daily loss … Daily-loss reset
semantics (timezone? midnight boundary?)" is not met. In a real multi-day live
run, `max_daily_loss` behaves as a **cumulative run-lifetime loss cap**: a slow
bleed over a week trips it as if it were one day's loss. Worse, because the
router escalates a `max_daily_loss` breach to `RiskManager.kill()`
(`order_router.py:269-277`) — cancel all + trip the switch — hitting the
cumulative threshold **permanently halts the whole book** with no daily
recovery, and there is no wired way to reset the day short of a manual
`reset()`/restart. This is a genuine risk-control correctness gap on real money.

**Recommendation.** Give the breaker a real day boundary. Either (a) have
`PerformanceService` expose `realised_pnl_since(ts)` / a per-UTC-day realised
PnL and wire *that* as the provider, or (b) wire the scheduler (already a
dependency — `apscheduler`) to call `reset_day` at the configured day boundary
**and** make `reset_day` reset the provider's day, not just the unused
`_recorded_daily_pnl`. Pin the reset timezone explicitly (UTC vs venue-local).
Add a test: submit a losing day, cross midnight, assert the breaker re-arms.

---

### A-2 [High] Synchronous SQLite I/O runs in the async event loop on every order/fill

**Location.** `sqlite_store.py:481-507` (`attach` → sync `_on_event`);
`sqlite_store.py:241-253` (`_conn` opens/commits/closes a connection **per
operation**); `service_factory.py:214-216` (`store.attach(bus)`); triggered from
`order_router._do_submit` (`emit(OrderEvent)` while inside `await submit`) and
the broker's `emit(FillEvent)`.

**Evidence.** `EventBus.emit` dispatches handlers **synchronously**
(`events.py:156-160`). `SqliteStore.attach` subscribes a synchronous handler:

```python
def _on_event(event: Event) -> None:
    if isinstance(event, OrderEvent):
        self.upsert_order(event.order)     # opens a NEW sqlite3 connection,
    elif isinstance(event, FillEvent):
        self.record_fill(event.fill)       # executes, commits, closes — blocking
```

and every write goes through `_conn`, which does `sqlite3.connect(...)` →
`execute` → `commit` → `close` **on each call** (no pooled/held connection). The
router emits an `OrderEvent` inside `submit` (an `async` method on the venue I/O
path), and the paper broker / live fill streamer emit `FillEvent`s on the same
bus. So each order lifecycle change and each fill performs a full
open-write-commit-close cycle of blocking disk I/O **on the event-loop thread**.

**Impact.** On the hottest paths (order submit, fill ingestion) the single
asyncio loop is blocked for the duration of a SQLite commit. With multiple
concurrent strategy runners (the whole `Orchestrator`/supervisor design), a slow
disk or a `PRAGMA synchronous=NORMAL` fsync stalls *every* runner, the
cooperative `stop_event` check, and the live WS fill consumer at once — degrading
the promptness of graceful shutdown and, on a live venue, the latency of
reacting to fills. It is not a correctness bug today (SQLite is fast and calls
are short) but it violates "no blocking I/O in async paths" and will bite under
load / on network-backed storage.

**Recommendation.** Move store writes off the loop: either run `_on_event`'s
writes via `asyncio.to_thread` / a dedicated writer task draining a queue, or
have the store register an `add_queue()` async consumer instead of a sync
`subscribe` handler and drain it in a background task. At minimum, hold one
connection per store instead of connect-per-call. Add a test asserting the store
consumer does not run on the loop thread (or a latency guard).

---

### A-3 [Medium] `step` vs `set_mode`/`stop` are un-locked coroutines sharing `_Unit` state

**Location.** `supervisor.py:665-685` (`step`), `620-663` (`stop`/`set_mode`),
`532-584` (`start`); no `asyncio.Lock` anywhere in the layer (grep confirms).

**Evidence.** `step` reads and dispatches on shared mutable `_Unit` fields
without a lock:

```python
async def step(self, name):
    unit = self._unit(name)
    if not unit.running or unit.runner is None:
        return None
    ...
    assert isinstance(unit.runner, StrategyRunner)   # unit.runner may be None now
    return await unit.runner.step_latest()
```

`set_mode` does `await self.stop(name)` (which sets `unit.runner = None`,
`unit.engine = None`) then `await self.start(name)` (rebuilds). `step`,
`set_mode`, `stop` and `start` are all `async` and share `self._units` with **no
mutual exclusion**. The daemon's scheduler calls `step`/`step_all`
(`supervisor.py:692-704`) concurrently with control-plane `set_mode`/`stop`
calls from the dashboard.

**Impact.** A `step` that passes the `unit.running`/`unit.runner is None` guard
can be interleaved (at its `await unit.runner.step_latest()` suspension, or after
the guard but before dispatch) with a `set_mode`/`stop` that nulls `unit.runner`
/ `unit.engine`, yielding an `AssertionError` or an `AttributeError` on
`None.step_latest()`, or a step routed through a half-torn-down engine. In the
`set_mode`→live path this could also let a `step` run against the *old* (paper)
runner after the mode flip is thought complete. Error containment holds
(exceptions don't cross units), but a control action can crash an in-flight step.

**Recommendation.** Add a per-unit `asyncio.Lock` and take it around
`start`/`stop`/`set_mode`/`step` (or a supervisor-wide lock if simpler). Add a
test that races a `set_mode`/`stop` against a `step` on the same unit and asserts
no exception and a consistent end state.

---

### A-4 [Medium] `UIConfig` does not enforce the "non-loopback host requires token" guard it documents

**Location.** `config.py:443-479` (`UIConfig`; only a `_non_empty_host`
validator — no cross-field guard). The real guard lives in the CLI
(`interfaces/cli/main.py:783`, `1017`).

**Evidence.** The `UIConfig` docstring states plainly: *"A non-loopback host
(e.g. `0.0.0.0` or a Tailscale IP) **requires** `token` — the dashboard … refuses
to bind wide open with no auth."* But the model has no `model_validator` — the
only validator is `_non_empty_host`. So
`UIConfig(host="0.0.0.0", token=None)` validates successfully, and
`AppConfig.to_yaml`/`from_yaml` will round-trip a wide-open-no-auth manifest
without complaint. The refusal is only enforced at CLI bind time.

**Impact.** The control surface can change what trades. A manifest written by the
dashboard's "save config" (`AppConfig.to_yaml`) or a hand-edited YAML can encode
`host: 0.0.0.0` with no token and pass all config validation; anything that binds
the dashboard *without* going through the specific CLI guard paths (a future API
entrypoint, a test harness, a direct `uvicorn` call) would expose the control
plane unauthenticated. Defence-in-depth for a real-money control surface should
not rely on a single call site.

**Recommendation.** Add a `@model_validator(mode="after")` to `UIConfig` that
raises unless `host` is loopback (`127.0.0.1`/`::1`/`localhost`) or a `token` is
set (allowing the env-var `TRADING_BOT_UI_TOKEN` to satisfy it, matching the CLI
semantics). Keep the CLI guard too. Add a test asserting a non-loopback + no-token
`UIConfig` is rejected at validation.

---

### A-5 [Medium] `to_yaml` round-trip drops comments and writes every default

**Location.** `config.py:593-617` (`to_yaml`).

**Evidence.**

```python
data = self.model_dump(mode="json")
yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
```

`model_dump` emits **all** fields including defaults, so a re-saved manifest gains
`live_enabled: false`, a full `ui:` block, `risk:` with three `null`s,
`storage:` nulls, `data_type: ohlc`, `store_key_format: venue`, etc., regardless
of what the source file contained. YAML comments in a hand-authored file are lost
(pydantic has no notion of them). The docstring's claim is only that money
Decimals round-trip exactly (they do — `mode="json"` stringifies them, and
`from_yaml` re-parses to identical `Decimal`s), which is correct and good.

**Impact.** Not a correctness bug — a round-trip *is* semantically faithful. But
every dashboard-driven save (add/remove strategy via `manifest()` → `to_yaml`)
rewrites the user's file: comments vanish, defaults balloon, and a live operator
loses the annotations that explain a real-money config. It also makes diffs
noisy and hand-edits fragile (e.g. someone re-adding a comment that the next save
deletes). For a live-trading control file this is operational risk, not just
cosmetics.

**Recommendation.** Consider `model_dump(mode="json", exclude_defaults=True)` for
`to_yaml` (verify `from_yaml` still reconstructs — it should, since omitted
fields fall back to the same defaults), or adopt `ruamel.yaml` round-trip mode if
comment preservation is a requirement. Document that the manifest is
machine-owned if comments truly cannot be kept.

---

### A-6 [Medium] `combined_equity_series` over-anchors `v0` across zero-fill units

**Location.** `supervisor.py:1200-1209` (`combined_equity_series`), consumed by
`_combined_ratios` (`947-1004`) for the exchange/total KPI rows.

**Evidence.**

```python
for name in wanted:
    unit = self._unit(name)
    combined_v0 += self._v0_of(unit)                      # every unit's v0 added
    buckets = by_mode(self._stored_fills_of(unit))
    combined_fills.extend(buckets.get(mode, []))          # only this mode's fills
points = equity_series(combined_fills, v0=combined_v0)
```

`combined_v0` sums the `starting_capital` of **every** named unit, but
`combined_fills` only includes fills that match the requested `mode`. A unit with
no fills in that mode (e.g. a freshly-started paper unit that has not traded, or a
unit whose fills are all in a *different* mode) still contributes its full `v0` to
the anchor.

**Impact.** The combined equity curve is anchored too high, and its **returns
path is distorted** — the first-point base includes capital that has no PnL
motion behind it, so the Sharpe/Sortino/Calmar/maxDD computed on it
(`_combined_ratios`) are skewed at the `exchange` and `total` aggregation levels.
Money display isn't affected (PnL/fees are summed separately and correctly), but
the aggregate risk ratios the dashboard shows are wrong whenever the group mixes
traded and untraded units in a mode. Not money-loss, but a misleading risk figure
on a live dashboard.

**Recommendation.** Only add a unit's `v0` when it contributes at least one fill
in the requested `mode` (or document that the combined curve is capital-weighted
by declared capital regardless of activity, and confirm that is intended for the
ratio math). Add a test with one traded + one untraded unit and assert the anchor
and Sharpe match a hand-computed value.

---

### A-7 [Medium] `rebalance_latest` drains the whole feed each daemon tick

**Location.** `portfolio_runner.py:433-457`.

**Evidence.**

```python
latest = None
for frames in self._feed:      # iterates the ENTIRE feed
    latest = frames            # keeping only the last cross-section
if latest is None:
    return None
return await self.rebalance(latest)
```

A live `PortfolioFeed` re-reads the dccd store and re-aligns the whole universe on
`__iter__` (`portfolio_feed.py:269-281`), then this loop walks every yielded
causal cross-section only to discard all but the last.

**Impact.** Each scheduler tick pays O(total common dates × universe size) work
(and a full multi-coin store read + inner-join alignment) just to obtain the
latest cross-section. `PortfolioFeed` already exposes `latest()` (the full
aligned frames) and `asof_ms()` for exactly this. For a daily rebalance over a
long history this is a large, avoidable per-tick cost on the daemon loop (and,
via A-2, it also drives store reads). `StrategyRunner.step_latest` gets this right
by calling `self._feed.latest()` directly — the portfolio path diverged.

**Recommendation.** Replace the drain with `self._feed.latest()` (guarding the
empty/None case), mirroring `StrategyRunner.step_latest`. Confirm
`PortfolioFeed.latest()` returns the same final cross-section the drain would
(it does — both go through `_aligned(_read_all())`). Add a test asserting
`rebalance_latest` reads the feed at most once.

---

### A-8 [Medium] `cancel` is not idempotent and has no in-flight guard

**Location.** `order_router.py:319-353`.

**Evidence.** Unlike `submit` (dedup map + `_inflight` future), `cancel` has no
guard:

```python
order = self._resolve(order_or_id)
if order.venue_order_id is None:
    raise MissingOrder(order.client_order_id)
await self._broker.cancel_order(order.venue_order_id)   # always hits the venue
order.cancel()                                          # local transition, may raise
```

A second `cancel` of the same id (or two concurrent cancels, e.g. a manual
dashboard cancel racing the kill-switch's `_cancel_via_router`,
`risk.py:367-377`) both call `broker.cancel_order` again, and the second
`order.cancel()` transition can raise `OrderError` (already terminal).

**Impact.** Redundant venue cancel calls (some venues error on cancelling an
already-cancelled/filled order; that error is *not* caught here and would
propagate out of `cancel`), and a possible `OrderError` on the local transition.
The kill-switch's `_cancel_via_router` does wrap each `router.cancel` in
`try/except Exception` (`risk.py:374-377`), so the kill path is contained — but a
direct control-plane double-cancel is not. Cancel is a *reducing* action and
should be safely repeatable.

**Recommendation.** Make `cancel` idempotent: if `order.is_terminal`, return it
without a venue call; catch a venue "unknown/already-done order" error and treat
it as success; optionally add a per-id in-flight cancel guard symmetric with
`submit`. Add a hardening test that double-cancels and concurrently-cancels one
order.

---

### A-9 [Low] `restore` recovers the dedup map but not `_inflight`; residual crash-mid-submit window

**Location.** `order_router.py:431-467` (`restore`), `234-290` (`_do_submit`
persists via the bus **after** the broker call), `run_app.py:757-758`,
`supervisor.py:558`.

**Evidence.** `restore` re-seeds `self._orders` from persisted orders so a
re-submit of a recorded id is deduped. But an order is only recorded (bus
`emit(OrderEvent)` → store `upsert_order`) **after** `broker.place_order`
succeeds and the local transitions run (`_do_submit`, lines 279-289). If the
process crashes *between* the venue accepting the order and the store write, the
id is on the venue but absent from the store; on restart `restore` does not know
it, and only the startup `reconcile` (which ingests venue-open orders) closes the
gap.

**Impact.** Small and largely covered: `reconcile` on start ingests the
venue-open order, so a resubmit of that id would be deduped *after* reconcile
runs — but a runner that regenerates the *same* deterministic id
(`f"{name}-{step}"`) only collides if the step index re-aligns, and reconcile
runs before the first order, so in practice the window is closed for the
strategy-runner id scheme. The module docstring already flags venue-side
idempotency (a Kraken `AddOrder` dedup token) as deferred go-live work. Noting it
for completeness as the documented residual.

**Recommendation.** No code change required now; keep the deferred venue-side
idempotency token on the go-live checklist. If tightened later: persist an
order's *intent* (client id) before the broker call, not only after.

---

### A-10 [Low] `remove_unit` is sync but hand-inlines async `stop`'s teardown

**Location.** `supervisor.py:420-443`.

**Evidence.**

```python
def remove_unit(self, name):          # sync
    unit = self._unit(name)
    if unit.running:
        unit.running = False          # inlined copy of stop()'s body
        unit.runner = None
        unit.engine = None
    del self._units[name]
    self._base = self._base.remove_entry(name)
```

with a comment acknowledging it duplicates `stop`'s effect because `stop` is
async but only clears in-memory state.

**Impact.** Correct today, but a latent maintenance hazard: the moment `stop`
gains any awaited teardown (flush a store, close a WS, cancel a task), this
sync copy silently skips it, leaking a resource on `remove_unit`. It also means
`remove_unit` and `stop` can drift.

**Recommendation.** Make `remove_unit` async and call `await self.stop(name)`,
or extract a shared sync `_teardown(unit)` helper both call, so the teardown
lives in one place.

---

### A-11 [Low] Provider-backed RiskManager makes `record_daily_pnl`/`reset_day` dead; two PnL sources can disagree

**Location.** `risk.py:242-246` (`_daily_pnl` prefers the provider),
`250-277` (`record_daily_pnl`/`reset_day` touch only `_recorded_daily_pnl`).

**Evidence.** When a `daily_pnl_provider` is injected (the factory always does,
`service_factory.py:209`), `_daily_pnl` returns the provider value and never
reads `_recorded_daily_pnl`. So `record_daily_pnl` and `reset_day` are inert for
the production wiring — a caller that calls `record_daily_pnl(...)` expecting to
influence the gate would be silently ignored.

**Impact.** Dead/confusing API surface and a footgun: two ways to feed daily PnL
that don't compose. Compounds A-1 (the reset that exists doesn't apply to the
wired path).

**Recommendation.** Collapse to one source: either drop the
`record_daily_pnl`/`_recorded_daily_pnl` fallback (make the provider mandatory),
or have `reset_day`/`record_daily_pnl` operate on the provider when one is set.
Whichever, wire A-1's day reset through it.

---

### A-12 [Low] Order factories mutate `client_order_id` post-construction (aggregate not truly immutable)

**Location.** `strategy_runner.py:357` (`order.client_order_id = cid`),
`portfolio_runner.py:477` (`order.client_order_id = f"..."`),
factories at `run_app.py:360` / `portfolio_runner.py:567` build with a
`"pending"` id.

**Evidence.** The runners override the id the factory set:

```python
order = self._order_factory(self._strategy, delta, bars)
order.client_order_id = cid          # mutate the just-built Order
```

Since `client_order_id` is the idempotency key, an `Order` whose key is mutable
means the identity of a submitted order can change after creation.

**Impact.** Works as designed (the runner deliberately owns the id), and the
mutation happens before `submit`, so idempotency is intact. But it's a surprising
mutability of a value that the rest of the layer treats as an identity/dedup key,
and it means an `Order` handed to a factory is not frozen. Low risk; worth
tightening for defence.

**Recommendation.** Have the factory contract *not* set an id (or return a spec),
and construct the final `Order` with the deterministic id in the runner, rather
than build-then-mutate. Alternatively freeze `Order` and expose a
`with_client_order_id(...)` copy.

---

### A-13 [Info] Duplicated wiring and a redundant commingle check across run paths

**Location.** `run_app.py:487` and `run_app.py:768` (`_reject_commingled` twice
per `run_app` — once in `prepare_system`, again inside `build_runners`);
`run_app.prepare_system:751-789` vs `supervisor.start:551-584` (two independent
restore + reconcile + build wiring paths).

**Evidence.** `prepare_system` calls `_reject_commingled(config)` at line 768,
then calls `build_runners`, which calls `_reject_commingled(config)` again at
line 487. Separately, the restore→reconcile→build-runners sequence exists in full
in both `run_app.prepare_system` and `supervisor.start` (the supervisor builds
one engine *per unit*, so it is not identical, but the ordering contract —
restore before reconcile, reconcile before first order, per-mode fill replay for
paper — is re-implemented rather than shared).

**Impact.** No bug (the commingle check is pure/idempotent; the wiring orders
match). But it is drift-prone: a future change to the startup ordering invariant
(e.g. inserting a step between restore and reconcile) must be made in two places
or the two run paths diverge. The three runner-ish files
(`strategy_runner`, `portfolio_runner`, plus `strategy`/`portfolio` declaration
pairs) are correctly *not* redundant — each is the single- vs multi-asset variant
— so there is no obsolete runner to delete.

**Recommendation.** Extract a shared `startup_recover(engine, *, reconcile,
replay_paper)` helper used by both `prepare_system` and `supervisor.start`.
Drop the second `_reject_commingled` call (keep it at the system entrypoint, not
inside `build_runners`, or vice-versa — one place).

---

### A-14 [Info] `from_float(0.0)` used for `RunReport` money defaults

**Location.** `run_app.py:285-286` (`from_float(0.0)` defaults),
import at line 98.

**Evidence.**

```python
realised_pnl: Money = field(default_factory=lambda: from_float(0.0))
fees_paid: Money = field(default_factory=lambda: from_float(0.0))
```

**Impact.** Harmless in value terms (`from_float(0.0)` is exactly `Decimal("0")`),
and these defaults are overwritten by the real `perf.realised_pnl()`/`fees_paid()`
in `_build_report`. But it is a `float` literal entering a money field, against
the "all money is Decimal, never float" house rule, and a stray reader could copy
the pattern with a non-zero float.

**Recommendation.** Use `money("0")` (as every other module does) for the
defaults; reserve `from_float` for genuine float-boundary conversions.
