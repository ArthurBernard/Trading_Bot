---
plan: audit-remediation/03-transport-secret-redaction
kind: leaf
status: executing
complexity: medium
depends: []
parallel: true
branch: fix/transport-secret-redaction
pr: ""
---

# Leaf 03 — transport-secret-redaction

## Goal
Stop leaking the Binance request signature (and full signed query) into logs and
exception messages. Fixes `B-1`.

## Files to change
- `trading_bot/transport/http.py` + tests under `trading_bot/tests/transport/`.

## Steps
1. Introduce a redaction helper that strips/masks sensitive query params
   (`signature`, `api_key`/`apiKey`, `token`, `nonce`?) from any URL before it is
   put into an `HTTPError`/`AmbiguousRequestError` message or a `logger.*` call
   (`http.py:63-67, 90-97, 374, 377-419, 387`).
2. Ensure the redaction covers the raw request URL, params repr, and any exception
   `__str__`/`__repr__` path. Prefer signing in headers where the API allows, but at
   minimum never emit the signature.
3. Audit every log line in `http.py` (and confirm brokers don't re-log the full
   URL) for residual leakage.

## Tests
- A test that triggers a 429/5xx/timeout on a signed Binance-style URL and asserts
  the raised exception message and captured log records contain the masked
  placeholder and NOT the signature/api-key value (use `caplog`).
- Redaction helper unit tests over representative URLs.

## Verification on real data
Offline. Construct a realistic signed Binance URL string, run it through the error
and logging paths, and grep the emitted text to prove no secret substring survives.
Never use real keys — synthesise a fake `signature=<hex>`.

## Closeout
CHANGELOG (Fixed / Security): redact signed query params from transport error
messages and logs. ADR: "secrets never logged" enforced at the transport boundary
via a redaction helper.
