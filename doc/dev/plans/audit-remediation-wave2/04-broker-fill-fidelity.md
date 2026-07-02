---
plan: audit-remediation-wave2/04-broker-fill-fidelity
kind: leaf
status: executing
complexity: medium
depends: []
parallel: true
branch: fix/broker-fill-fidelity
pr: ""
---

# Leaf 04 — broker-fill-fidelity

## Goal
Fix wrong PnL basis on partial fills and harden the Kraken private-WS. Fixes `B-8`,
`B-10`. Standing constraint: **no real orders** — verify offline / fakes.

## Files
`trading_bot/brokers/binance.py`, `trading_bot/brokers/kraken.py`,
`trading_bot/brokers/kraken_ws.py`, tests under `trading_bot/tests/brokers/`.

## Steps
1. `B-8` (Medium): `open_orders` derives a partially-filled order's average fill
   price from the **limit/stop price** rather than the venue's actual average fill
   (`binance.py:691-696`, `kraken.py:570-576`) — a wrong PnL basis. Use the venue's
   reported executed-qty / cummulative-quote (Binance `executedQty` /
   `cummulativeQuoteQty`; Kraken `vol_exec` / `cost`/`price`) to compute the true
   average fill price. Money stays `Decimal`.
2. `B-10` (Medium): the Kraken private-WS carries **no reconcile-on-fill-gap**, and a
   `ts` parse failure silently becomes `0` (`kraken_ws.py:145-158,416,433`). On a
   detected gap / after a reconnect, trigger a reconcile (fills are the PnL source of
   truth — a missed fill must be recovered, not assumed); make `ts` parsing robust
   (reject/flag rather than silently zeroing).

## Tests
A partially-filled venue order yields the venue's true average fill price (not the
limit price) on both venues; a bad `ts` is handled (not silently 0); a simulated
WS fill-gap / reconnect triggers a reconcile.

## Verification on real data
Offline / fake-transport only (NO real or testnet venue): feed a partial-fill
`open_orders` payload and assert the avg fill price equals the venue's executed
cost/qty; simulate a WS gap and assert reconcile fires.

## Closeout
CHANGELOG (Fixed): true average fill price on partial fills; Kraken WS
reconcile-on-gap + robust `ts`. ADR note only if a non-trivial choice arises.
