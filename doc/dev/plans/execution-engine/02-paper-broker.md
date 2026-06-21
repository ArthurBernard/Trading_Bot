---
plan: execution-engine/02-paper-broker
kind: leaf
status: done
complexity: medium
depends: []
parallel: false
branch: feat/paper-broker
pr: "local-merge (GitHub outage)"
---

# PaperBroker — in-process fill simulation

## Goal

`PaperBroker` implements the E3 `Broker` port entirely in-process: it accepts
orders, simulates fills, and tracks balances/open-orders/fills — the **default**
broker so the whole engine runs without touching a venue. Pure-ish (no network),
deterministic, money `Decimal`.

## Files to change

- `trading_bot/brokers/paper.py` — new; `PaperBroker(Broker)`.
- `trading_bot/brokers/__init__.py` — export `PaperBroker`.
- `trading_bot/tests/brokers/test_paper.py` — new.

## Steps

1. Read `brokers/base.py` (the `Broker` port + `Capability`) and the domain
   `Order`/`Fill`/`Position`/`Money`.
2. `PaperBroker(*, prices=None, fee_bps=money("10"), fill_model="immediate", starting_balances=None)`:
   - `place_order(order)`: assign a synthetic `venue_order_id`; under `fill_model`
     `"immediate"` fully fill at the order's limit price (or an injected mark price
     for market orders) producing a `Fill` (with fee = `price*qty*fee_bps/10000`);
     support a `"partial"` model that fills in chunks. Record the fill + update
     balances; keep unfilled orders in `open_orders`.
   - `cancel_order(venue_order_id)`, `open_orders()`, `balances()`, `fills(since_ms)`,
     `ticker(instrument)` (from the injected `prices`), `capabilities()`.
   - A `set_price(instrument, price)` test hook to drive marks.
3. Deterministic ids/timestamps (injectable clock/counter seams) so tests are exact.

## Tests

- Immediate-fill limit order → one `Fill` at the limit price; `open_orders` empty;
  balances move by qty/price ± fee (exact Decimal).
- Market order fills at the injected mark price.
- Partial-fill model → multiple fills summing to qty; order closes when full.
- `cancel_order` removes an open order; fee applied as configured.

## Verification on real data

In-process simulation **is** the engine's "real data". Run a realistic order sequence
(buy, partial, sell-to-flat) through `PaperBroker`, read back `fills()`/`balances()`,
and assert they match hand-computed Decimal expectations. Gates via **`.venv`**.

## Closeout

- CHANGELOG (Added): "`brokers.PaperBroker` — in-process fill simulation (the default broker)."
- ADR: the fill model(s) + fee model, if worth noting.
- Status/roadmap: deferred to leaf 05.
