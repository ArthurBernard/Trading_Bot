---
plan: strategy-runner/02-data-feed
kind: leaf
status: executing
complexity: high
depends: []
parallel: false
branch: feat/data-feed
pr: "#32"
---

# DataFeed — bars to a strategy (in-memory + dccd-backed)

## Goal

A `DataFeed` abstraction that yields market **bars** to a strategy: an **in-memory**
feed (always offline-testable) and a **dccd-backed** feed reading real bars via
`dccd.Client.read(...)`. **Causal:** at step *t* the feed exposes only bars `≤ t` —
never future data.

## Files to change

- `trading_bot/application/data_feed.py` — new; `DataFeed` protocol + `InMemoryFeed` + `DccdFeed`.
- `trading_bot/application/__init__.py` — export them.
- `trading_bot/tests/application/test_data_feed.py` — new.

## Steps

1. Read the dccd `Client.read` signature
   (`read(exchange, symbol, data_type="ohlc", span=None, start_ns=None, end_ns=None) -> polars.DataFrame`)
   and `inventory()`. dccd is installed in `.venv`.
2. `DataFeed` protocol (async or sync iterator — pick to match how the runner
   consumes; document): yields a growing **window** of bars (a frame containing all
   bars up to and including the current step), so the strategy's `signal_fn` always
   gets a causal frame. Provide `bars_so_far()` / iteration that advances one bar at
   a time.
3. `InMemoryFeed(frame)`: wraps a fixed bars frame and replays it bar-by-bar,
   yielding the causal prefix `frame[: t+1]` at each step. Deterministic — the
   backbone of offline tests and backtests.
4. `DccdFeed(client, exchange, symbol, span, ...)`:
   - **historical mode**: `client.read(...)` once, then replay like `InMemoryFeed`
     (so a backtest over real stored bars is reproducible).
   - **live mode**: advance as new bars arrive (poll `read` for the latest closed
     bar on the span cadence, or bridge a live source); only emit a bar once it is
     **closed** (no partial/lookahead). Keep the dccd coupling thin and injectable
     so tests don't need dccd.
5. Normalise the frame columns the strategy expects (document the schema:
   timestamp + OHLC[V]); reuse `domain.instrument` for the symbol.

## Tests (via `.venv`)

- `InMemoryFeed`: replaying N bars yields N causal prefixes; the frame at step *t*
  contains exactly bars `0..t` and **never** a bar `> t` (assert the last timestamp).
- A strategy `signal_fn` fed by `InMemoryFeed` only ever sees causal frames
  (instrument a spy `signal_fn` that records the max timestamp it saw).
- `DccdFeed` historical mode with an **injected fake dccd client** (returns a canned
  polars frame) → replays the same causal prefixes; no real dccd needed.
- Live mode emits a bar only once closed (with a faked clock/poll).

## Verification on real data

`-m network` (opt-in): if dccd `inventory()` has any stored OHLC, `DccdFeed`
historical reads it via `Client.read` and replays at least a few causal prefixes
(assert monotonic timestamps, no lookahead). If inventory is empty, document that
the real-data check is gated on stored data and rely on the injected-client test.
**Run** the offline suite via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.DataFeed` — causal bars feed (in-memory + dccd-backed)."
- ADR: the causal-window contract + the thin dccd coupling (injected client) + closed-bar-only live rule.
- Status/roadmap: deferred to leaf 03.
