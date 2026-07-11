---
plan: venue-minimums/03-strict-paper
kind: leaf
status: planned
complexity: medium
depends: [02]
parallel: false
branch: feat/strict-paper-default
pr: ""
---

# 03 — factory paper brokers are strict by default

## Goal

With the upstream policy in place (leaf 02), the simulator can finally
enforce what the venue would without spraying rejects: flip the
**factory-built** paper broker to `strict=True`, behind a config flag.

- `trading_bot/application/config.py`: `paper_strict: bool = True` (on the
  same config level as the mode/broker settings — read where `_build_broker`
  gets its inputs and put it there; document that OFF restores the historical
  permissive simulator).
- `trading_bot/application/service_factory.py` (`_build_broker`, paper
  branch): pass `strict=config.<paper_strict>` to the `PaperBroker`
  constructor. The **constructor default stays `False`** — direct/test
  construction keeps the permissive historical model; only the factory flips.
- Strict paper + bare instruments must stay harmless: `prepare_order_values`
  with all-None spec fields is a passthrough (verify in `instrument.py` and
  assert it in a test) — so units whose specs failed to resolve keep trading
  exactly as before.

## Files to change

- `trading_bot/application/config.py` — the flag.
- `trading_bot/application/service_factory.py` — the paper branch.
- Existing factory/config test modules — tests below.

## Steps

1. Read `_build_broker`'s paper branch + the config models. Implement.
2. Sweep the existing suite for tests that build engines through the factory
   AND rely on permissive fills of sub-minimum orders (there should be none
   — factory instruments are bare so strict is a passthrough — but verify;
   fix any that break by declaring `paper_strict=False` in their config, not
   by weakening the default).
3. All three gates green.

## Tests

- `test_factory_paper_is_strict_by_default`: factory-built engine (default
  config) → its broker rejects a hand-built sub-minimum order
  (`OrderTooSmall`) when the instrument carries a spec.
- `test_paper_strict_flag_off_restores_permissive`: `paper_strict=False` →
  the same order fills.
- `test_strict_with_bare_instrument_is_passthrough`: default engine, bare
  instrument (no spec) → order fills exactly as before (no behaviour change
  for unresolved specs).
- `test_constructor_default_unchanged`: `PaperBroker()` direct → permissive
  (regression pin).

## Verification on real data

Scratch engine over a copy of the Binance book with REAL resolved specs
(leaf 01): hand-submit the audit's dust order (0.0000079 BTC @ market) through
`OrderRouter.submit` → the broker rejects with `OrderTooSmall`, the router
records the reject (read the order's `reject_reason` and the store row), the
engine survives (submit another valid order right after). Report the reject
message and the store row. Originals untouched.

## Closeout

- CHANGELOG `[Unreleased] > Changed`: "factory-built paper brokers are
  strict by default (`paper_strict: true`) — sub-minimum/over-precise orders
  are rejected exactly as the venue would; direct `PaperBroker()`
  construction stays permissive (#XX)."
- ADR: default-ON rationale (paper must predict the venue; the upstream
  policy makes the reject path exceptional) — or fold into leaf 02's entry.
- Tick leaf 03 in `00-plan.md`; archive per `/finish-task`.
