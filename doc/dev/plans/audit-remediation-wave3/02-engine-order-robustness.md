---
plan: audit-remediation-wave3/02-engine-order-robustness
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/engine-order-robustness
pr: ""
---

# Leaf 02 — engine-and-order-robustness

## Goal
Fix KPI/feed/order-router robustness. Fixes `A-6`, `A-7`, `A-8`, `A-9`. Standing
constraint: **no real orders** — verify offline / PaperBroker.

## Files
`trading_bot/application/supervisor.py`, `portfolio_runner.py`, `order_router.py`,
tests under `trading_bot/tests/application/`.

## Steps
- `A-6`: `combined_equity_series` sums `v0` over **all** named units even those with
  zero fills in the requested mode, inflating the equity anchor and skewing aggregate
  exchange/total KPI ratios — only anchor units that actually have fills in the mode.
- `A-7`: `rebalance_latest` drains the **whole** feed each daemon tick (O(total bars))
  just to keep the last cross-section — read only the latest window (keep causality).
- `A-8`: `cancel` is not idempotent and has no in-flight guard — a double/concurrent
  cancel re-hits the venue and can raise on the local transition; make it idempotent.
- `A-9`: `router.restore` seeds the dedup map but never restores in-flight `_inflight`;
  reduce the residual double-submit window on a crash mid-submit (document the
  remaining venue-idempotency-token dependency).

## Tests
Aggregate KPI ratios ignore zero-fill units in a mode; `rebalance_latest` reads only
the latest window (assert bounded reads / not O(n)); a double cancel is a no-op (not a
venue re-hit / raise); restore rebuilds in-flight state as specified.

## Verification on real data
Offline / PaperBroker: drive fills across modes and confirm the aggregate KPI anchor
is correct; a repeated cancel doesn't re-submit; a simulated mid-submit restart
converges without a duplicate.

## Closeout
CHANGELOG (Fixed). ADR: cancel idempotency + in-flight restore semantics.
