---
plan: canary-roundtrip/03-venue-oracle-and-testnet
kind: leaf
status: planned
complexity: high
depends: [02]
parallel: false
branch: feat/canary-venue
pr: ""
---

# 03 — the venue oracle, testnet for real

## Goal

The canary against a REAL venue: same scenario, different oracle (pinned in
`00-plan.md`):

- **identity oracle** (exact): OUR recorded fills == the venue-reported
  fills (ids, qtys, prices — via `broker.fills(...)` re-fetch); OUR
  balances delta == the venue-reported before/after delta (per asset,
  exact); position flat at the end (base delta == 0 within the venue's
  dust rounding — READ how reconcile treats venue balance precision and
  mirror; if the venue reports base dust after the round trip, the check
  reports it exactly and compares against the fills math, not against
  zero);
- **bounded cost**: total quote cost (balance delta) ≤ `--max-cost`;
  reported exactly;
- probes run against the real venue too (far-off limit cancel = a REAL
  cancel — the road-to-1.0 #1 kill-switch validation; idempotency
  re-submit = the venue-side client-order-id dedup validation).

Wiring:

- `--mode testnet`: builds the engine against the venue's testnet adapter
  (READ how `_build_broker` selects testnet — `testnet: true` config — and
  which venues support it: Binance yes, Kraken no); keys from the
  environment (`BINANCE_TESTNET_*`), never logged;
- `--mode live`: behind the EXISTING `live_enabled` gate + the CLI asks a
  typed confirmation (mirror the dashboard's go-live confirmation
  pattern); this leaf implements the path but its real-data verification
  is testnet-only — the live run is the operator's go-live act
  (road-to-1.0 #1), not CI;
- `doc/dev/09-go-live.md`: add the canary as the named validation vehicle
  for the "Proven vs pending" items (venue idempotency, real cancel,
  balance reconciliation) — a short section, linking the CLI invocation.

## Files to change

- `trading_bot/application/canary.py` — the venue oracle + the
  before/after venue snapshots (balances/fills re-fetch through the
  broker port; no adapter-specific code here).
- `trading_bot/interfaces/cli/main.py` — `--mode testnet|live` wiring +
  the live confirmation.
- `doc/dev/09-go-live.md` — the section (docs-of-record EXCEPTION: this
  leaf may edit 09-go-live.md — it is the leaf's deliverable, not
  /finish-task bookkeeping).
- Tests: venue-oracle unit tests over a fake broker (identity holds /
  broken fills mismatch / balance mismatch / cost bound breach); testnet
  path smoke behind `@pytest.mark.network`.

## Steps

1. READ `_build_broker`'s testnet branch, the `live_enabled` gate, the
   broker port's `fills`/`balances` signatures, and reconcile's
   venue-truth patterns.
2. Implement; all three gates green (network tests deselected by default).

## Tests

Above; the fake-broker suite must cover every oracle branch.

## Verification on real data (MANDATORY — Binance testnet, real API, fake money)

Run `trading-bot canary --exchange binance --mode testnet` FOR REAL with
the `BINANCE_TESTNET_*` keys from the repo's `.env` (they exist — memory:
validated read-only previously; testnet is fake money by construction).
Paste the full evidence table: the real testnet fills, the balance deltas,
the identity checks, the probe results (real cancel accepted, duplicate
client-order-id deduped venue-side), the bounded cost. If the testnet is
unreachable/out of funds, report exactly what happened and how far the run
got — do NOT fake it, and do NOT touch mainnet keys or `--mode live`.
No `var/`, no port 8000.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "the canary runs against real venues —
  identity oracle (fills/balances == venue-reported) + bounded cost,
  testnet wired, live behind the existing gates; go-live doc names it as
  the road-to-1.0 #1 vehicle (#XX)."
- ADR: the identity-not-absolute-PnL oracle for venues; testnet as the
  proving ground.
- `doc/dev/06-status.md`: epic shipped note.
- Last leaf: remove the `canary-roundtrip` roadmap line, set the global
  done, archive the tree per `/finish-task`.
