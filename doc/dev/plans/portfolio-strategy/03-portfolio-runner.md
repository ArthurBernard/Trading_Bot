---
plan: portfolio-strategy/03-portfolio-runner
kind: leaf
status: done
complexity: high
depends: [01]
parallel: false
branch: feat/portfolio-runner
pr: "#65"
---

# Portfolio runner — weight vector → N idempotent risk-gated orders

## Goal

The multi-asset analogue of `StrategyRunner`: a `PortfolioRunner` that, each
rebalance tick, calls the `PortfolioSignalFn` for the **whole book**, converts the
weight vector to per-coin target quantities, computes each coin's delta vs the
**shared** `PositionTracker`, and routes **N** idempotent, risk-gated orders
through the **shared** `OrderRouter` — then holds to the next (daily) tick.

## Files to change

- `trading_bot/application/portfolio_runner.py` — new (`PortfolioRunner`).
- `trading_bot/tests/application/test_portfolio_runner.py` — new.

## Steps

1. Read `application/strategy_runner.py` (the single-instrument loop: feed →
   signal → `delta_to` → `order_factory` → `OrderRouter.submit`; the per-step
   `client_order_id = f"{name}-{step}"`; the cooperative `run(stop_event=...)`),
   and `application/order_router.py` (idempotent `submit`, risk gate) to mirror.
2. `PortfolioRunner(strategy: PortfolioStrategy, feed: PortfolioFeed, router,
   tracker, *, event_bus, order_factory)`:
   - per tick *t*: `frames = next(feed)`; `asof = feed asof ms`;
     `prices = {sym: latest close of frames[sym]}`;
     `weights = strategy.signal_fn(asof, frames)`;
     `signals = weights_to_signals(weights, prices=prices, capital=strategy.capital, asof_ms=asof)`;
   - for each `Symbol` in the **universe** (so a coin dropped from `weights` this
     tick is targeted **flat** — Σ must cover the whole book):
     `delta = signal.delta_to(tracker.position(instrument))`; if `delta == 0` skip;
     else build an order via `order_factory` and `await router.submit(order)`.
   - **Idempotent ids:** `client_order_id = f"{strategy.name}-{symbol}-{step}"` so a
     re-run/retry dedups per coin per rebalance (mirror the single-instrument
     scheme, namespaced by symbol).
   - **Maker LIMIT orders:** the order factory prices each leg as a LIMIT at the
     coin's latest close (LS1 pays the 0.10% maker fee; also lets the paper broker
     fill self-contained). A live broker fills at the venue.
   - Count orders submitted; surface per-coin failures without aborting the whole
     rebalance (collect + report, like the orchestrator).
   - Cooperative stop: `run(stop_event=...)` holds between ticks (daily cadence is
     driven by the feed/scheduler, not a busy loop).
3. **Optional engine-side gross guard (ADR):** if `strategy.gross_cap` is set,
   assert Σ|wᵢ| ≤ gross_cap before sizing and raise/clip per the documented policy
   (LS1's signal already caps at 2×; this is defence in depth, default **off**).

## Tests

- With the **fake `PortfolioSignalFn`** (leaf 01 fixture) + a `PaperBroker` engine
  + a fake feed: one rebalance from flat → N orders whose sizes equal
  `weightᵢ × capital / priceᵢ` (exact `Decimal`); sides match weight signs.
- Second rebalance with changed weights → orders equal the **delta** vs the now-non-flat
  positions (read from the shared tracker after the first fills), not the absolute
  target. A coin whose weight goes to 0 is targeted flat (full close).
- Idempotency: re-submitting the same rebalance step (same `step`) places **no**
  duplicate orders (router dedup on the per-coin id).
- Risk gate: an order exceeding `max_order` raises `RiskLimitBreached` and never
  reaches the broker; the other legs still route (or the documented all-or-nothing
  policy — decide + test).
- Reconciliation honesty: after fills, `tracker.position(coin)` equals the routed
  cumulative qty for each coin.

## Verification on real data

**Mandatory.** Drive the runner with the **fake** signal but a **real**
`PaperBroker` and assert the **broker-reported** fills (via the bus/tracker) match
the intended per-coin targets — i.e. read what the broker confirmed, not local
optimism (the project's core testing rule). The real LS1 signal + real Binance
data/testnet round-trip is leaf 05.

## Closeout

- CHANGELOG (Added): "`application.PortfolioRunner` — rebalance loop turning a
  weight vector into N idempotent, risk-gated maker-LIMIT orders on the shared engine."
- ADR: the per-coin idempotency id scheme (`{name}-{symbol}-{step}`); whole-universe
  targeting (a dropped coin → flat); per-leg failure policy; the optional gross-cap
  guard (default off — trust the signal's cap).
- Status/roadmap: deferred to leaf 05.
