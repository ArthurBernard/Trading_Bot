---
plan: audit-remediation-wave2/03-broker-limiter-robustness
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/broker-limiter-robustness
pr: ""
---

# Leaf 03 — broker-limiter-robustness

## Goal
Make the Binance rate-limiter weight-aware and the error handling robust. Fixes
`B-6`, `B-9`, `B-7`. Standing constraint: **no real orders** — verify offline / fakes.

## Files
`trading_bot/transport/ratelimit.py`, `trading_bot/transport/http.py`,
`trading_bot/brokers/binance.py`, tests under `trading_bot/tests/transport/` and
`trading_bot/tests/brokers/`.

## Steps
1. `B-6` (High): the Binance limiter is a flat per-second token bucket that ignores
   Binance's **request-weight** budget and 418/`Retry-After` semantics
   (`ratelimit.py:54-62`, `binance.py:56-61,268-270`). Make it weight-aware: charge
   each endpoint its **weight**, track the rolling weight budget (honour
   `X-MBX-USED-WEIGHT[-1m]` response headers), back off on **418** (IP ban) and
   **429** honouring `Retry-After`. Keep the Kraken call-counter path intact.
2. `B-9` (Medium): a `retry=False` 429 currently raises immediately, discarding
   `Retry-After` (`http.py:371-382`). Surface `Retry-After` to the caller (on the
   exception / limiter) so the next call waits — without ever blind-retrying an order
   submit (preserve the ambiguous-submit → reconcile guarantee).
3. `B-7` (Medium): `_raise_on_error` cannot detect a Binance error returned inside a
   **JSON array** (`binance.py:328-341,651-652`) — handle the array-wrapped error
   shape so a batch/list response error isn't silently ignored.

## Tests
Weighted calls deplete the budget at the right rate and pace correctly; a 418
triggers a ban-backoff; a 429 with `Retry-After` waits that long; an
array-wrapped Binance error is detected and mapped; an order submit still never
blind-retries on an ambiguous 429.

## Verification on real data
Offline / fake-transport only (NO real or testnet venue): drive the limiter with
synthetic weighted requests + `X-MBX-USED-WEIGHT` headers and assert the pacing /
backoff; feed an array-wrapped error and assert it surfaces.

## Closeout
CHANGELOG (Fixed): weight-aware Binance limiter (418/`Retry-After`), array-wrapped
error detection, `Retry-After` surfaced on 429. ADR: weight-budget limiter design.
