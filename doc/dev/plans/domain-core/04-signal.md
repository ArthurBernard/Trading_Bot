---
plan: domain-core/04-signal
kind: leaf
status: done
complexity: low
depends: [01]
parallel: false
branch: feat/domain-signal
pr: "#10"
---

# Signal (venue-neutral strategy target)

## Goal

`Signal` — a strategy's target expressed venue-neutrally (a normalised target
exposure, or an explicit target quantity), plus the diff against a current
`Position`. This is the value a future `StrategyRunner` turns into orders. Pure,
typed.

## Files to change

- `trading_bot/domain/signal.py` — new.
- `trading_bot/domain/__init__.py` — export.
- `trading_bot/tests/domain/test_signal.py` — new.

## Steps

1. Frozen `Signal(instrument, target, ts, strength?)` where `target` is a normalised
   exposure in `[-1, 1]` (short..long) **or** an explicit target qty (one mode,
   validated). Decimal.
2. `delta_to(position)` → the desired position change (used later by the order
   router). Pure.
3. Map the legacy `signal`/`delta_signal` concept (`legacy/performance.py`) onto
   this type so PnL (leaf 05) and the runner share one vocabulary.

## Tests

- Target validation (reject `|target| > 1` in fractional mode; reject double mode).
- `delta_to` computes the correct desired change from a `Position`.
- Flat target (`0`) from a long position → full-close delta.

## Verification on real data

Pure layer. Build a series of signals over a realistic `Position` series and assert
the deltas. `pytest` green, `mypy` strict clean.

## Closeout

- CHANGELOG (Added): "Signal domain type (venue-neutral target + delta-to-position)."
- ADR: none.
- Status/roadmap: deferred to leaf 05.
