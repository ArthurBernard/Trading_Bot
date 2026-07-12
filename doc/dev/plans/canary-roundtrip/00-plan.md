---
plan: canary-roundtrip
kind: global
status: planning
roadmap: "6. [ ] **`canary-roundtrip` — deterministic self-test strategy.** (UI consistency & accounting integrity, 2026-07-11)"
release_on_done: false
---

# canary-roundtrip — the platform proves itself with a known trade

## Goal

A deterministic self-test the operator can run against any venue mode: a
tiny **sequential round-trip** (market buy *x* → confirm the fill → market
sell *x* → confirm) plus two **free probes** (a far-off-limit cancel — the
real kill-switch path — and a client-order-id idempotency re-submit), with
an **oracle computed in advance**. Maintainer design, 2026-07-11; the live
canary is the validation vehicle for road-to-1.0 #1 (real-key enablement).

## Pinned design (implement, do not re-decide)

- **Sequential, never simultaneous** — self-trade prevention would make a
  crossed pair non-deterministic.
- **Two oracles**:
  - *paper*: EXACT Decimal equality — realised PnL == −Σ fees, position
    flat, both orders `FILLED` with `filled_qty == qty`, store rows
    consistent, broker balances delta == −Σ fees (quote) / 0 (base);
  - *venue (testnet/live)*: the **accounting identity**, exact — OUR fills ==
    venue-reported fills, OUR balances delta == venue-reported balances
    delta, position flat — plus a **bounded total cost** (`max_cost`,
    config, default 2 quote units). NEVER a precomputed absolute PnL
    (spread/drift are not deterministic).
- **Probes** (both modes): cancel — place a limit buy far below market
  (config `probe_offset_pct`, default 50 % below), cancel it, assert no
  fill, no balance change, `CANCELLED` persisted; idempotency — re-submit
  the SAME client-order-id, assert no duplicate order was created.
- **Funding**: paper brokers start unfunded (epic D finding) — the canary
  engine is built with explicit `starting_balances` (quote budget, config
  `budget`, default 100 quote units paper / the real balance on venues).
- **Gates**: live mode requires the existing `live_enabled` opt-in +
  credentials — the canary NEVER weakens a gate; testnet uses the testnet
  endpoints/keys (`BINANCE_TESTNET_*`). Cost/size: qty sized from
  `budget` and the venue minimums (the epic-C resolver) — the smallest
  venue-legal size, never more than `max_cost` at risk.
- **Evidence report**: every step's expectation vs observation (exact
  strings), machine-readable + human-printable; the run FAILS on the first
  violated expectation but still reports everything it measured.
- Cadence (documented, not automated here): paper per release; testnet at
  will; live once per venue at go-live + after adapter changes.

## Decomposition

1. `01-scenario-and-paper-oracle` — `application/canary.py`: the scenario
   runner (round-trip + probes) over a real `Engine`, the report dataclass,
   the exact paper oracle; E2E test green in the default suite.
2. `02-canary-cli` — `trading-bot canary` (Typer): builds the canary
   engine/config, runs the scenario, prints the evidence table, exit code
   0/1; paper by default.
3. `03-venue-oracle-and-testnet` — the venue identity oracle + cost bound,
   testnet/live wiring behind the existing gates, go-live doc linkage;
   verified for real against Binance testnet (fake money, real API).

## Leaf checklist

- [x] 01 scenario-and-paper-oracle — feat/canary-scenario — high (#221)
- [x] 02 canary-cli — feat/canary-cli — medium (#222)
- [ ] 03 venue-oracle-and-testnet — feat/canary-venue — high

## Dependencies

Serial: 01 → 02 → 03.

## Done criteria

- `trading-bot canary --mode paper` runs green end-to-end with the exact
  oracle, on a fresh engine, in seconds.
- The venue path runs against Binance testnet for real: identity oracle
  holds (our fills/balances == venue-reported), probes pass, cost bounded.
- `doc/dev/09-go-live.md` names the live canary as the road-to-1.0 #1
  validation vehicle.
- Roadmap line removed by the last leaf.
