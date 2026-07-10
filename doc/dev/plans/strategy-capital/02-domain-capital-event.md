---
plan: strategy-capital/02-domain-capital-event
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/domain-capital-event
pr: ""
---

# 02 — Domain: `CapitalEvent` + pure capital folds

## Goal

The pure accounting core of the capital ledger: an immutable `CapitalEvent`
value object (the `Fill` analogue for money movements) and the folds that derive
contributed capital and a value curve from `events ⊕ fills`. Zero I/O — domain
stays pure.

## Files to change

- `trading_bot/domain/capital.py` — **new**:
  - `CapitalEventType` enum-by-value (like `OrderSide`): `FUNDING`, `DEPOSIT`,
    `WITHDRAWAL`.
  - `CapitalEvent` frozen dataclass mirroring `Fill`'s discipline
    (`domain/fill.py:44`): `event_id: str` (non-empty), `strategy: str`
    (non-empty), `event_type: CapitalEventType`, `amount: Money`
    (**strictly positive** — the sign lives in the type, exactly how
    `Fill.side` + `qty` factor), `ts: int` (epoch ms, same unit as `Fill.ts`),
    `note: str = ""`. Validators raise `ValueError` like `Fill`'s.
  - `contributed_capital(events) -> Money` — fold:
    `Σ amount [FUNDING|DEPOSIT] − Σ amount [WITHDRAWAL]`.
  - `value_series(fills, events, ...) -> list[ValuePoint]` — fold **both**
    streams interleaved by `ts` (stable on ties: event before fill), producing
    per-point `(ts_ms, contributed, realised_pnl, value)` with
    `value = contributed + realised_pnl`. Reuses the exact realised-PnL fold of
    `application/pnl_series.equity_series` (`Position.with_fill` per
    instrument, net of fees) — the two must reconcile to the cent when the only
    event is a genesis `FUNDING(v0)`.
- `trading_bot/domain/__init__.py` — export the new names.
- `trading_bot/tests/domain/test_capital.py` — **new**.

## Steps

1. Read `domain/fill.py` and `application/pnl_series.py` first; copy their
   validator/fold idioms rather than inventing new ones (the realised fold may
   be shared by importing `Position` — do **not** import from `application/`).
2. Implement the module with numpydoc docstrings (repo style), `Money`
   arithmetic via `domain/money.py` helpers, no float anywhere.
3. Property-style unit tests (see Tests).
4. `python -m pytest trading_bot/tests/domain/ -v` and `ruff check` green;
   `mypy trading_bot/domain/` clean (domain is mypy-strict).

## Tests

- Validators: empty `event_id`/`strategy`, zero/negative `amount`, float
  `amount` → `ValueError` (mirror `test_fill.py` patterns).
- `contributed_capital`: genesis only → allocation; deposit/withdraw sequences;
  empty → 0.
- `value_series` reconciliation: with only `FUNDING(v0)` at t0, the series
  equals `equity_series(fills, v0)` point-for-point (import the fixture fills
  from the pnl_series tests). A mid-stream `DEPOSIT` shifts `value` by exactly
  the amount from that ts on, `realised_pnl` untouched.
- The worked example from `00-plan.md` §design: fund 1000 → +100 realised →
  withdraw 100 ⇒ `C=900, R=100, V(last)=1000`.
- Ordering: events and fills with equal `ts` — event applies first,
  deterministic output.

## Verification on real data

Fold the **real paper fills** from the archived dashboard store
(`var/dashboard/backup-2026-07-10/alloc1-binance.sqlite`, read via
`SqliteStore.stored_fills()`) with a synthetic genesis `FUNDING(100000)`:
`value_series` must reconcile to `pnl_series.equity_series(fills, v0=100000)`
to the cent on every point. Record the point count and final value in the PR.

## Closeout

- CHANGELOG (Added): domain `CapitalEvent` + pure capital folds
  (`contributed_capital`, `value_series`).
- Tick leaf 02 in `00-plan.md`.
