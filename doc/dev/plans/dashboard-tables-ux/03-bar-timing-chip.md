---
plan: dashboard-tables-ux/03-bar-timing-chip
kind: leaf
status: planned
complexity: medium
depends: [02]
parallel: false
branch: feat/ui-bar-timing
pr: ""
---

# 03 — honest bar timing: "last bar X ago → next bar in Y"

## Goal

Replace the dishonest epoch-aligned "Next bar" guess with server-truth
timing. One chip, two surfaces:

- content: `last bar 3h ago → next in 21h` — last = the strategy row's
  `last_asof_ts` (the as-of of the last completed evaluation, already on
  `/api/strategies`), next = `last_asof_ts + span × 1000` (the honest
  derivation from the same truth); tooltip: both absolute instants + the
  wording "data through <last>" (keep the existing tooltip language);
- surfaces: the strategies roster (REPLACING the current "Next bar" column
  — audit finding: `nextBarCloseMs(span)`'s epoch-aligned guess has no
  relation to the real cadence; retire that helper if nothing else uses it)
  and the strategy-detail header (next to the pills);
- `last_asof_ts == null` (never evaluated) → the chip renders `—` with an
  explanatory tooltip, never a fake countdown;
- live countdown refresh: reuse the existing `data-countdown-ts` /
  `tbTime.countdown()` mechanism (base.html) that the health/next-check
  chips already use — no new timer machinery.

## Files to change

- `trading_bot/interfaces/ui/templates/base.html` — a small
  `barTimingChipHtml(s)` helper (mirror `healthPillHtml`'s shape); retire
  `nextBarCloseMs` IF unused after this leaf (grep first).
- `trading_bot/interfaces/ui/templates/strategies.html` — the roster column
  swap (header + cell).
- `trading_bot/interfaces/ui/templates/strategy_detail.html` — the header
  chip.
- `trading_bot/tests/interfaces/test_dashboard.py` — hook tests (helper in
  base, both surfaces carry it, the old next-bar hook gone); adjust
  existing tests referencing the removed column (state each).

## Steps

1. READ the current `nextBarCell`/`nextBarCloseMs` usage, the countdown
   mechanism, and the "Last eval" column tooltip (its wording is the model).
2. Implement helper → roster → header; keep the "Last eval" column intact
   (it shows evaluation wall-clock; the chip shows DATA time — different
   things, both stay).
3. All three gates green.

## Tests

Hook tests as above; suite green.

## Verification on real data

Same harness (real books + real dccd store, one tick, TestClient): execute
the extracted helper JS against the real `/api/strategies` rows — assert
the chip HTML shows last = the real dccd store's last daily bar
(2026-07-XX 00:00 UTC at run time) and next = exactly +span seconds; a
fabricated `last_asof_ts: null` row renders `—`. Report the generated chip
HTML for both real units. Originals and port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added` + `Removed` (the epoch-guess column):
  "honest bar-timing chip — last bar/next bar derived from `last_asof_ts` +
  span, on the roster and detail header; the epoch-aligned Next-bar guess
  is retired (#XX)."
- ADR: none expected; state so.
- Tick leaf 03 in `00-plan.md`; archive per `/finish-task`.
