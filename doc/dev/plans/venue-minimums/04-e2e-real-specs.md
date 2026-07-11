---
plan: venue-minimums/04-e2e-real-specs
kind: leaf
status: planned
complexity: medium
depends: [01, 02, 03]
parallel: false
branch: chore/venue-minimums-e2e
pr: ""
---

# 04 — E2E: a rebalance never submits below the venue minimum

## Goal

Lock the whole chain (resolver → policy → strict paper) with a permanent E2E
test plus the epic's final real-data verification.

## Files to change

- `trading_bot/tests/application/test_venue_minimums_e2e.py` — **new**.
  Offline E2E (injected fake resolver with realistic specs — 5 USDT
  min_notional, lot steps): a portfolio unit over a scratch store runs TWO
  rebalance ticks with weights crafted to produce, in one tick: an
  above-min leg, a round-up leg, a dust leg, and a spot-sell-cap leg.
  Assert:
  - every order that reached the store satisfies `qty ≥ min_qty` and
    `qty × price ≥ min_notional` (lot-quantized);
  - the dust leg produced no order and one info LogEvent;
  - the round-up leg's stored qty == the exact minimum;
  - the capped sell's qty == the held position (and no oversell);
  - strict paper rejected nothing during the run (the upstream policy made
    rejects unreachable) — count OrderEvents with `reject_reason`;
  - tick 2 converges (the residuals shrink, no oscillating churn: the same
    leg must not flip submit/skip between the two ticks with unchanged
    prices).
- No production code changes; if one proves necessary, STOP and report.

## Steps

1. Read the leaf-02/03 implementations as merged; reuse the existing
   portfolio-runner test harness/fixtures.
2. Write the E2E; all three gates green.

## Tests

The E2E above IS the test (default suite, offline — the network-marked
resolver test from leaf 01 already covers the real fetch).

## Verification on real data

On scratch copies of BOTH live books with REAL public-endpoint specs:
1. One full rebalance tick per book through the real seams (real resolver,
   strict paper, real stored prices) — report the per-leg decision table
   (symbol, delta, binding minimum, action, final qty).
2. Assert: zero submitted sub-minimum orders; zero `OrderTooSmall` rejects;
   the historical dust pattern (sub-1-USDT deltas) lands on `skip`.
3. Run a second tick and report the residual deltas (convergence evidence).
4. Originals under `var/` and port 8000 untouched (live daemon).
Paste the two decision tables in the PR.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "venue-minimums E2E — a rebalance never
  submits a sub-minimum order; dust skipped, close-to-min rounded, spot
  sells capped (#XX)."
- `doc/dev/06-status.md`: epic shipped note (+ the audit dust case now
  structurally impossible).
- Last leaf: remove the `venue-minimums` roadmap line, set the global
  `00-plan.md` done, archive the tree per `/finish-task`.
