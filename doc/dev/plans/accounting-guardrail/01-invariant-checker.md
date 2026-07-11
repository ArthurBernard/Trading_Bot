---
plan: accounting-guardrail/01-invariant-checker
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/accounting-checker
pr: ""
---

# 01 — the pure invariant checker

## Goal

New `trading_bot/application/accounting.py`: a **pure** module (no I/O, no bus,
no store handle — the caller passes plain domain data in) that recomputes the
book's self-consistency and returns typed violations. The taxonomy, rules and
severities are pinned in `00-plan.md` — implement exactly those four checker
kinds (`position_drift`, `order_fill_mismatch`, `status_incoherent`,
`duplicate_venue_ids`; `kill_switch_tripped` is NOT the checker's job — it is
folded in at the health layer, leaf 03).

## Files to change

- `trading_bot/application/accounting.py` — **new**:
  - `Severity = Literal["warn", "error"]` (or a tiny enum — match the
    codebase's prevailing style for closed string sets).
  - `@dataclass(frozen=True, slots=True) class Violation`: `kind: str`,
    `severity: Severity`, `subject: str` (instrument symbol or
    client_order_id), `detail: str` (one human-readable sentence with the
    exact Decimal figures), `measured: str`, `expected: str` (exact
    `str(Decimal)` — JSON-safe, no float ever).
  - `check_book(positions, fills, orders) -> list[Violation]` where
    `positions: Mapping[Instrument, Position]` (the tracker's
    `all_positions()`), `fills: Iterable[Fill]` (the store's, already
    mode-filtered by the caller), `orders: Iterable[Order]` (the store's).
    Implements, in this order:
    1. `position_drift` (error): per instrument, fold the signed fill qtys
       (`buy` +, `sell` −) exactly like `Position.with_fill` does for
       `net_qty`; compare `Decimal`-exact to the passed position's `net_qty`.
       Also flag an instrument with fills but NO tracked position (drift from
       zero) and a non-flat position with no fills at all.
    2. `order_fill_mismatch` (warn): per client_order_id having fills, Σ fill
       qty vs `order.filled_qty` — a difference beyond the order's
       `fill_tolerance` fraction of `qty` (mirror `Order.apply_fill`'s
       tolerance semantics; read `domain/order.py` first) is a violation.
       Orders absent from the `orders` iterable but present in fills → also a
       mismatch ("fills without an order row").
    3. `status_incoherent` (warn): fills cover `order.qty` within tolerance
       but `status` is not `FILLED`; or `status is FILLED` but
       `filled_qty != qty` (beyond tolerance).
    4. `duplicate_venue_ids` (warn): one violation per duplicated
       `venue_order_id` value (subject = the venue id, detail lists the
       client_order_ids sharing it). `None`/empty venue ids are skipped.
  - Full numpydoc docstrings; module docstring explains why the checker is
    pure (testable, callable from anywhere, no side effects — it only
    reports) and points to `00-plan.md`'s taxonomy table.
- `trading_bot/tests/application/test_accounting.py` — **new** (tests below).

## Steps

1. Read `domain/position.py` (`with_fill` signed fold), `domain/order.py`
   (`apply_fill` tolerance semantics), `domain/fill.py` — mirror their exact
   arithmetic; do not re-invent tolerance handling.
2. Implement the module; keep every comparison `Decimal`-exact (`money()`
   guards where inputs could be raw).
3. Tests; all three gates green
   (`~/.pyenv/versions/trading_bot_env/bin/python -m pytest`, `-m ruff check
   trading_bot/`, `-m ruff format --check trading_bot/`).

## Tests

`test_accounting.py`, pure-domain fixtures (no store, no broker):

- `test_clean_book_no_violations`: consistent positions/fills/orders → `[]`.
- `test_position_drift_detected`: drop one fill from the fold → exactly one
  `position_drift` error with the exact measured/expected Decimals.
- `test_drift_from_zero_and_orphan_position`: fills with no position, and a
  non-flat position with no fills → one error each.
- `test_order_fill_mismatch`: order row `filled_qty=0` with a full fill →
  warn; within-tolerance rounding difference → NO violation.
- `test_status_incoherent_both_directions`: covered-but-open, and
  FILLED-but-short.
- `test_duplicate_venue_ids`: two orders sharing `PAPER-1` → one warn naming
  both client ids; unique ids → none; `None` ids skipped.
- `test_all_figures_are_exact_strings`: violations serialize measured/expected
  via `str(Decimal)` (no float repr artifacts).

## Verification on real data

Copies of BOTH `/home/arthur/dev/Trading_Bot/var/dashboard/alloc1-binance.sqlite`
and `alloc1-kraken.sqlite` in scratch (originals NEVER touched — live daemon).
Load each with `SqliteStore` (paper mode context), build the tracker fold like
`_replay_paper_book` does, then run `check_book`:

- **Pre-heal state** (the books as they are today, daemon not yet restarted):
  expect per book — 14 / 13 `order_fill_mismatch`+`status_incoherent` warns
  (the frozen rows), 14 / 13 `duplicate_venue_ids` warns, and **zero**
  `position_drift` (positions == stored fills — the swallowed fill never
  reached the store either, the book is self-consistent).
- **Post-heal state**: re-run after a restore→replay→reconcile pass (the
  #191/#193 seams): the mismatch/incoherent warns disappear; only the stable
  legacy `duplicate_venue_ids` warns remain.
- Report exact counts per kind per book.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "Accounting invariant checker
  (`application/accounting.py`) — pure recomputation of
  position/fill/order-book self-consistency, typed violations (#XX)."
- ADR: taxonomy + purity decision (checker reports, never mutates; severity
  policy incl. legacy duplicate-id warns).
- Tick leaf 01 in `00-plan.md`; archive per `/finish-task`.
