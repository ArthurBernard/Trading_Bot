---
plan: canary-roundtrip/01-scenario-and-paper-oracle
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: feat/canary-scenario
pr: ""
---

# 01 — the canary scenario and its exact paper oracle

## Goal

New `trading_bot/application/canary.py` implementing the pinned design (see
`00-plan.md` — authoritative):

- `@dataclass(frozen=True) class CanaryCheck`: `name: str`, `expected: str`,
  `observed: str`, `passed: bool`.
- `@dataclass class CanaryReport`: ordered `checks: list[CanaryCheck]`,
  `passed` property (all), `cost: Money | None` (realised total cost),
  plus enough context (exchange, mode, instrument, qty) to print.
- `async run_canary(engine, instrument, *, qty, probe_offset_pct, ...) ->
  CanaryReport` — drives the engine's OWN seams (`OrderRouter.submit`,
  `router.cancel`, `broker.balances()`, `engine.tracker`, `engine.perf`,
  `engine.store`): snapshot balances → **cancel probe** (far-below limit
  buy, cancel, assert no fill/no balance move/`CANCELLED` persisted) →
  **idempotency probe** (re-submit an identical client-order-id, assert the
  router/venue deduped — no second live order, no second store row) →
  **round-trip** (market buy qty, confirm the fill(s) landed in
  tracker+store; market sell qty, confirm; assert position flat) → final
  balance snapshot. Every step appends `CanaryCheck`s; a failed check stops
  placing further orders but still snapshots and reports.
- `paper_oracle(report_inputs...) -> list[CanaryCheck]` — the EXACT
  assertions (Decimal strings): realised PnL == −Σ fees; flat position;
  both round-trip orders `FILLED` with `filled_qty == qty` in the STORE
  (not just memory); quote balance delta == −(Σ fees) and base delta == 0
  (paper fills at the same mark both ways — buy and sell notionals cancel
  exactly, only fees remain — state this assumption in the docstring);
  fill count == expected.
- Engine construction is the CALLER's job (leaf 02) — this module takes a
  ready engine; for tests, build one via `build_engine` with
  `starting_balances` (READ how PaperBroker takes `starting_balances` and
  how the factory would pass them — if the factory cannot yet, construct
  the PaperBroker directly in the test harness and assemble the engine the
  way existing tests do; do NOT change the factory in this leaf unless a
  ≤5-line additive config field is enough — decide by reading, state the
  choice).
- Mark price: paper market orders need a mark — inject via
  `PaperBroker(prices={instrument: ...})` or the broker's price-update seam
  (READ paper.py for the canonical way).

## Files to change

- `trading_bot/application/canary.py` — **new** (full numpydoc; module
  docstring carries the two-oracle design and the sizing/cost philosophy).
- `trading_bot/tests/application/test_canary.py` — **new**:
  - the full-scenario E2E on a fresh paper engine (default suite): report
    passes, every check green, exact PnL == −Σ fees;
  - cancel-probe failure detection (broker that "fills" the far-off order →
    the check fails and the run stops placing);
  - idempotency-probe detection (duplicate created → fail);
  - oracle catches a seeded imbalance (drop a fill → red);
  - report printing (stable, exact strings).

## Steps

1. READ `order_router.submit/cancel` semantics (incl. idempotent re-submit
   behaviour — what happens TODAY on a duplicate client-order-id), strict
   paper dedup (`_venue_id_by_cid`), `PaperBroker` prices/balances seams,
   and `test_restart_replay_e2e.py`'s engine harness.
2. Implement; all three gates green
   (`~/.pyenv/versions/trading_bot_env/bin/python -m pytest`, `-m ruff
   check trading_bot/`, `-m ruff format --check trading_bot/`).

## Tests

Above.

## Verification on real data

Run the paper canary FOR REAL on a scratch engine wired like production
(factory-built, strict paper, real venue spec for BTC/USDT via the epic-C
resolver — public endpoint — so the qty sizing exercises real minimums):
report the full evidence table (every check's expected/observed exact
strings) and the final cost (== 2 × fee on the venue-legal minimum size at
the injected mark). No book files, no `var/`, no port 8000 involved at all.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "the canary scenario — deterministic
  round-trip + cancel/idempotency probes with an exact paper oracle
  (`application/canary.py`) (#XX)."
- ADR: the two-oracle design (exact paper vs identity+bounded venue), the
  sequential-not-simultaneous rationale, probes-before-money ordering.
- Tick leaf 01 in `00-plan.md`; archive per `/finish-task`.
