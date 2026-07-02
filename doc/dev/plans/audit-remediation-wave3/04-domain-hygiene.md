---
plan: audit-remediation-wave3/04-domain-hygiene
kind: leaf
status: executing
complexity: medium
depends: []
parallel: true
branch: fix/domain-hygiene
pr: ""
---

# Leaf 04 — domain-hygiene

## Goal
Domain correctness, immutability and dead-code cleanup. Fixes `D-6`, `D-11`, `D-12`,
`D-13`, `A-12`.

## Files
`trading_bot/domain/order.py`, `fill.py`, `signal.py`, `performance.py`, `errors.py`,
`money.py`; `trading_bot/application/strategy_runner.py` / `portfolio_runner.py` (the
order factories for A-12); tests under `trading_bot/tests/domain/`.

## Steps
- `D-6`: an over-fill within `fill_tolerance` is rejected instead of clamping to close
  — a market order that slightly over-delivers should close, not raise. Accept within
  tolerance (clamp), keep rejecting a genuine over-fill beyond tolerance.
- `D-12`: non-finite / out-of-context money raises `decimal.InvalidOperation` rather
  than a domain error in some paths — route through the domain `MoneyError` (align
  with the wave-1 guard).
- `D-11`: `_check_aligned` docstring claims "non-empty fill series" but empty is
  accepted — fix the docstring (or the behaviour) to match.
- `D-13`: `InstrumentMismatch` / `InsufficientFunds` are exported + documented but
  never raised — either wire them where they belong or remove the dead classes.
- `A-12`: the order factories mutate `Order.client_order_id` **after** construction
  (the "frozen" aggregate isn't actually immutable) — set it at construction so the
  order is truly immutable.

## Tests
Over-fill within tolerance closes (no raise); beyond tolerance still raises; non-finite
money raises the domain error; the dead error classes are gone or exercised; an order's
`client_order_id` is set at construction and not mutated afterward.

## Verification on real data
Offline, domain stays pure (no I/O / no upward imports); mypy strict on `domain/` clean.
Run a realistic fill sequence and confirm the tolerance/close behaviour.

## Closeout
CHANGELOG (Fixed). ADR only if a non-trivial choice (e.g. D-13 wire-vs-remove).
