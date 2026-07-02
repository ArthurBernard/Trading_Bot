---
plan: audit-remediation-wave3/03-broker-transport-robustness
kind: leaf
status: executing
complexity: medium
depends: []
parallel: true
branch: fix/broker-transport-robustness
pr: ""
---

# Leaf 03 — broker-transport-robustness

## Goal
Harden the transport client and the PaperBroker. Fixes `B-11`, `B-12`, `B-13`.
Standing constraint: **no real orders** — verify offline / fakes.

## Files
`trading_bot/transport/http.py`, `trading_bot/brokers/paper.py`,
`trading_bot/brokers/kraken.py`, `binance.py` (order-type rebuild), tests under
`trading_bot/tests/transport/` and `trading_bot/tests/brokers/`.

## Steps
- `B-11`: `AsyncHTTPClient` has no connection-pool limits, no explicit
  `trust_env`/proxy policy, no response-size cap, and doesn't distinguish connect vs
  read timeout — set sane pool limits + explicit timeouts (connect/read/write) + a
  response-size guard; a read-timeout on a submit stays ambiguous → reconcile (never
  auto-retry).
- `B-12`: an unknown venue order-type is silently coerced to `LIMIT` on rebuild
  (`kraken.py`/`binance.py`) — reject/flag the unknown type instead of guessing.
- `B-13`: the PaperBroker diverges from live semantics (instant full fill, no
  min-notional/precision rejection, no client-order-id dedup) — bring it closer to
  live so a paper-validated strategy behaves the same on a real venue (at least:
  reject sub-min-notional/precision like the live quantize path; honour the
  client-order-id dedup).

## Tests
Transport: pool limits + connect/read timeout distinction + response cap; a
read-timeout on submit raises the ambiguous error (no retry). Unknown order-type is
rejected. PaperBroker rejects a sub-min-notional order and dedups a repeated
client-order-id.

## Verification on real data
Offline / fake-transport + PaperBroker only (NO real/testnet venue): assert the
timeout/pool/cap behaviour and the PaperBroker rejections/dedup.

## Closeout
CHANGELOG (Fixed). ADR: PaperBroker↔live fidelity (which live checks it now mirrors).
