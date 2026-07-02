---
plan: audit-remediation/04-broker-live-readiness
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/broker-live-readiness
pr: ""
---

# Leaf 04 — broker-live-readiness

## Goal
Make the broker order path safe for live: quantized sizes, monotonic nonce, a real
error taxonomy, and a preserved client-order-id. Fixes `B-2`, `B-3`, `B-4`, `B-5`,
`B-15`. Standing constraint: **no real orders** — verify against fakes/PaperBroker.

## Files to change
- `trading_bot/brokers/kraken.py`, `binance.py`, `base.py`,
  `trading_bot/domain/errors.py` (if new error types), `application/reconcile.py`
  (only if the client-order-id match needs it) + tests under
  `trading_bot/tests/brokers/`.

## Steps
1. `B-2`: quantize `qty`/`limit_price`/`stop_price` to the venue tick/lot via
   `Instrument.quantize()` / `price_precision`/`qty_precision` **before** building
   the request, on both venues (`kraken.py:482-494`, `binance.py:584-606`). Round in
   the safe direction (never round *up* a sell qty past holdings). Reject sub-min
   notional/lot with a clear domain error rather than sending a doomed request.
2. `B-3`: give Kraken a monotonic, locked nonce (a counter guarded by a lock,
   seeded from `time`, guaranteed strictly increasing across concurrent calls and a
   clock step-back) (`kraken.py:237-242, 300-304`).
3. `B-4`: add a venue-code→domain-error mapping (insufficient funds, invalid pair,
   invalid nonce, rate limit, service unavailable, …) for both venues
   (`kraken.py:246-266`, `binance.py:328-341`); retry the retriable Kraken HTTP-200
   errors (`EService:Unavailable`, `EAPI:Rate limit`) with backoff — but NEVER
   blindly retry `AddOrder` (keep the ambiguous-submit → reconcile guarantee).
4. `B-5`/`B-15`: pass the domain `client_order_id` to BOTH venues
   (Binance `newClientOrderId`, Kraken `userref`/`cl_ord_id`); when it doesn't fit
   the venue charset/length, deterministically transform it (don't silently drop) so
   `reconcile()` can still match on it. Confirm reconcile matches on the value
   actually sent.

## Tests
Fake-transport unit tests: over-precise qty is quantized to the instrument step;
sub-min-lot raises; nonce strictly increases under concurrent calls and after a
simulated clock rollback; each venue error string maps to the right domain error;
retriable Kraken 200-error is retried, `AddOrder` ambiguous 5xx still raises; the
client-order-id sent to the venue round-trips through reconcile.

## Verification on real data
Offline / PaperBroker only. Drive `place_order` through a fake transport that echoes
the request, and assert the wire payload carries a quantized size and the
client-order-id, and that an echoed venue error surfaces as the mapped domain error.
Do NOT hit any real or testnet venue in this leaf.

## Closeout
CHANGELOG (Fixed): quantize order size to venue tick/lot, monotonic Kraken nonce,
venue-error taxonomy + retriable-error retry, preserve client-order-id on both
venues. ADR: broker order-path live-readiness — quantization + idempotency-token
fidelity are go-live prerequisites.
