---
plan: audit-remediation/02-domain-hardening
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/domain-hardening
pr: ""
---

# Leaf 02 — domain-hardening

## Goal
Apply the domain's own money/instrument guards where they'd catch a mistake. Fixes
`D-1`, `D-4`, `D-7`, `D-3`, `D-10`.

## Files to change
- `trading_bot/domain/order.py`, `fill.py`, `signal.py`, `money.py`,
  `instrument.py` + matching tests under `trading_bot/tests/domain/`.

## Steps
1. `D-1`: route every money field (`price`, `qty`, `limit_price`, fees, signal
   target, …) through `money()` inside `__post_init__` so a raw `float` is rejected
   (or coerced via `Decimal(str(x))`) at construction — decide reject-vs-coerce and
   be consistent; a `float` must never enter the PnL source of truth.
2. `D-4`: pin an explicit `decimal.Context` (or `localcontext`) around
   `avg_fill_price` (`order.py:346`) and average-entry math (`position.py:211`) so a
   repeating quotient rounds deterministically at a defined precision, not under the
   process-global 28-digit context.
3. `D-7`: reject non-finite (`NaN`/`±Inf`) in `SignalMode.TARGET_QTY` (and any
   money field) — raise a domain error, not `decimal.InvalidOperation`.
4. `D-3`: fix `parse_kraken_pair` so `XTZUSD` → `XTZ/USD` (Tezos), not `XT/USD`;
   add cases for other X/Z-prefixed altnames.
5. `D-10`: tighten `normalise` so it only strips the legacy leading `X`/`Z` for
   actual Kraken legacy codes, not every 4-char code starting with `X`.

## Tests
Negative tests: `Order`/`Fill`/`Signal` with a `float` money field raise; NaN/Inf
target raises; `parse_kraken_pair("XTZUSD")==XTZ/USD` and a table of pairs;
`normalise` leaves non-legacy X-codes intact; determinism of avg-price under the
pinned context.

## Verification on real data
Offline. Feed a realistic set of Kraken altname pairs through `parse_kraken_pair`
and assert the resulting instruments match the venue's real base/quote. Keep the
domain pure (no I/O) and mypy-strict clean.

## Closeout
CHANGELOG (Fixed): domain money guards in ctors, pinned Decimal context on
avg-price, non-finite rejection, Kraken pair mis-parse. ADR: domain applies its own
`money()` guard at construction (defence in depth) — reject-vs-coerce rationale.
