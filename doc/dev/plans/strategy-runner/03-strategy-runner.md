---
plan: strategy-runner/03-strategy-runner
kind: leaf
status: done
complexity: high
depends: [01, 02]
parallel: false
branch: feat/strategy-runner
pr: "#33"
---

# StrategyRunner — the live loop

## Goal

`StrategyRunner` wires it together: pull bars from a `DataFeed`, evaluate the
`Strategy`'s signal, turn the domain `Signal` into a target position change, and
route the resulting `Order`(s) through the E4 `OrderRouter` — emitting events and
reading the current position from the `PositionTracker`. Replaces the legacy
`StrategyBot` iterator. **Last E5 leaf — closes the E5 roadmap line.**

## Files to change

- `trading_bot/application/strategy_runner.py` — new; `StrategyRunner`.
- `trading_bot/application/__init__.py` — export it.
- `trading_bot/tests/application/test_strategy_runner.py` — new.
- `doc/dev/07-roadmap.md` — remove the E5 line. `doc/dev/06-status.md` — mark E5 done.

## Steps

1. Read `application/strategy.py` (`Strategy.evaluate`), `application/data_feed.py`
   (`DataFeed`), `application/order_router.py` (`OrderRouter.submit`),
   `application/position_tracker.py` (`position(instrument)`),
   `domain/signal.py` (`Signal.delta_to(position, reference_qty)`),
   `domain/order.py` (`Order`).
2. `StrategyRunner(strategy, feed, router, tracker, *, event_bus=None, order_factory=None)`:
   - `async run(max_steps=None)` (and/or `async step()`): for each bar window from
     `feed`, `signal = strategy.evaluate(bars)`; `current = tracker.position(instrument)`;
     `delta = signal.delta_to(current, reference_qty=strategy.reference_qty)`; if
     `delta != 0`, build an `Order` (market by default, or via `order_factory`) with
     a **deterministic, unique `client_order_id`** (e.g. `f"{strategy.name}-{step}"`)
     and `await router.submit(order)`. Emit a `LogEvent`/`OrderEvent`.
   - Respect **warmup** (skip until `lookback` bars). Honour `feed`'s causality —
     the runner never peeks ahead.
   - No order when `delta == 0` (already at target).
3. The `client_order_id` scheme must make a re-run idempotent at the router (same
   step → same id → no duplicate), tying into E4's idempotency.

## Tests (via `.venv`, fully offline)

- End-to-end: `InMemoryFeed` of a known OHLC series + the MA-crossover strategy +
  `OrderRouter`→`PaperBroker` + `PositionTracker` → the position track follows the
  signal (long after the up-cross, flat/short after the down-cross); orders match
  the deltas; money exact `Decimal`.
- `delta == 0` step emits no order.
- Warmup: no orders before `lookback` bars.
- **Idempotent re-run**: running the same steps twice (same client-order-ids) does
  not double-submit (one paper order per step).
- **Causality**: the strategy never saw a future bar (spy on the signal_fn).

## Verification on real data

Fully offline (the engine's "real data" is the in-memory feed + PaperBroker). Run a
realistic OHLC series end to end and assert the resulting positions/PnL match a
hand-computed expectation from the signal, with **no lookahead**. Optionally, with
`-m network`, drive the runner from a `DccdFeed` over real stored bars. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.StrategyRunner` — the live loop (data → signal → target position → orders)."
- ADR: the runner loop + the per-step `client_order_id` idempotency scheme + warmup/causality handling.
- Status/roadmap: **remove the E5 line** from `07-roadmap.md`; mark E5 done in `06-status.md`.
