---
plan: venue-minimums
kind: global
status: planning
roadmap: "3. [ ] **`venue-minimums` — venue minimums end-to-end.** (UI consistency & accounting integrity, 2026-07-11)"
release_on_done: false
---

# venue-minimums — orders respect the venue, upstream

## Goal

The engine can build and (in permissive paper) fill orders a real venue would
reject: the 2026-07-11 audit caught a 0.0000079 BTC (~0.50 USDT) dust buy —
far below Binance's ~5 USDT notional minimum — filling happily in paper.
Handle minimums **upstream, in order preparation** (maintainer decision,
2026-07-11): round UP to the venue minimum when the delta is close, SKIP the
leg (no submit, one log) when far below, never fail the rebalance — the next
rebalance recomputes the residual naturally. Then flip paper to `strict` so
the simulator enforces exactly what the venue would.

## Scouted foundations (all verified 2026-07-11)

- `Instrument` already models the spec (`min_qty`/`min_notional`/precisions)
  + `prepare_order_values` (domain/instrument.py:398-505) — "unknown until
  the venue's metadata is loaded". Nothing loads it on the runner path:
  `signals_from_weights` builds bare `Instrument(symbol)` (portfolio.py:209).
- Both live adapters already fetch real specs from PUBLIC endpoints via
  `async def instrument(self, symbol) -> Instrument` — Kraken `AssetPairs`
  (kraken.py:465-506, `ordermin`/`costmin`), Binance `exchangeInfo`
  (binance.py:532-573, `LOT_SIZE`/`NOTIONAL`). Adapter-level (NOT on the
  `Broker` port), and both adapters construct keylessly for public calls
  (api_key optional, env-fallback to `""`).
- `PaperBroker(strict=True)` (paper.py:184-196) quantizes + rejects
  sub-minimum orders with `OrderTooSmall`; `build_engine`
  (service_factory.py) leaves the default `strict=False`.
- `portfolio_runner.py:439-465` already submits per leg under
  `try/except (RiskLimitBreached, BrokerError)` and continues the other legs.

## Pinned policy (leaves implement, do not re-decide)

Per rebalance leg with a resolved spec (min = the binding one of `min_qty` /
`min_notional/price`, after lot quantization):

- `|delta| ≥ min` → submit as today (quantized).
- `min > |delta| ≥ threshold × min` → **round up** to the minimum.
  `threshold` is a config field (`min_order_ratio`, Decimal in `(0, 1]`,
  default `0.5`).
- `|delta| < threshold × min` → **skip**: no submit, no reject noise; one
  `LogEvent(level="info")` naming the leg, the delta and the minimum.
- **Spot-sell cap**: a sell that reduces a long position is capped at the held
  quantity; if the cap falls below the venue minimum → skip. Never oversell
  into a venue reject or an unintended short.
- **No spec resolved** (unknown exchange, fetch failure, missing fields) →
  today's permissive behaviour + ONE `LogEvent(level="warning")` per unit
  lifetime (not per tick). The policy must degrade, never block trading.

## Decomposition

1. `01-spec-resolver` — `application/instrument_specs.py`: per-exchange
   resolver over the adapters' existing keyless public builders, cached per
   `(exchange, symbol)` for the process lifetime; permissive fallback.
2. `02-order-prep-policy` — pure `application/order_prep.py` policy helper +
   `portfolio_runner` integration (enrich instruments via the resolver, apply
   round-up-or-skip + sell cap), `min_order_ratio` config field.
3. `03-strict-paper` — factory-built paper brokers get `strict=True` behind a
   new config flag (`paper_strict`, default ON); constructor default stays
   `False` for direct/test construction.
4. `04-e2e-real-specs` — E2E over copies of the live books with REAL fetched
   specs: dust legs skipped, close legs rounded, no sub-minimum submit ever,
   convergence across two rebalances.

## Leaf checklist

- [x] 01 spec-resolver — feat/instrument-spec-resolver — medium (#203)
- [ ] 02 order-prep-policy — feat/order-prep-policy — high
- [ ] 03 strict-paper — feat/strict-paper-default — medium
- [ ] 04 e2e-real-specs — chore/venue-minimums-e2e — medium

## Dependencies

Serial: 01 → 02 → 03 → 04 (02 consumes 01's resolver; 03 must not land
before 02 — strict paper without the upstream policy would spray
`OrderTooSmall` rejects on every dust leg; 04 locks the whole chain).

## Done criteria

- A rebalance over the real books' copies with real venue specs submits no
  sub-minimum order; the audit's 0.0000079-BTC dust case is skipped with a
  log, not submitted; close-to-min legs are rounded up; spot sells never
  exceed the held position.
- Factory paper is strict: a hand-built dust order is rejected
  (`OrderTooSmall`) exactly as live would.
- Roadmap line removed by the last leaf.
