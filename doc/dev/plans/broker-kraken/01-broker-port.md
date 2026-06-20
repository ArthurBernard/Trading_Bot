---
plan: broker-kraken/01-broker-port
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: feat/broker-port
pr: ""
---

# Broker port + registry

## Goal

The venue-neutral **`Broker`** contract every exchange adapter implements, plus a
capability model and a `BrokerRegistry`. Pure interface layer (typed, async); the
only concrete thing here is a tiny in-test stub. Mirrors dccd's `sources/base.py` +
`sources/registry.py`.

## Files to change

- `trading_bot/brokers/__init__.py` — new; export the port + registry + errors.
- `trading_bot/brokers/base.py` — new; the `Broker` protocol/ABC + `Capability`/`BrokerError`.
- `trading_bot/brokers/registry.py` — new; `BrokerRegistry`.
- `trading_bot/tests/brokers/__init__.py` — new (empty).
- `trading_bot/tests/brokers/test_base.py` — new (a stub broker + registry tests).

## Steps

1. Read dccd's `sources/base.py` and `sources/registry.py` for the pattern.
2. `brokers/base.py`: define the async **`Broker`** surface over **domain types**:
   - `async place_order(order: Order) -> str` — submit; returns the venue order id.
   - `async cancel_order(venue_order_id: str) -> None`.
   - `async open_orders() -> list[Order]` — reconstruct domain `Order`s.
   - `async balances() -> dict[str, Money]` — asset → free balance (Decimal).
   - `async fills(since_ms: int | None = None) -> list[Fill]` — domain `Fill`s.
   - `async ticker(instrument: Instrument) -> Money` — last/mark price (public).
   - `capabilities() -> set[...]` — what the adapter actually supports (declare honestly).
   - `name: str` (the venue key).
   Use a `Protocol` (runtime-checkable) or an ABC — pick the cleaner; document it.
   Money/quantities are `Decimal` (`domain.money`).
3. `brokers/registry.py`: `BrokerRegistry.register(name, broker)` / `get(name) -> Broker`
   (raises `NoCapability`/`BrokerError` if absent). Mirror dccd's registry.
4. Capability model: a small enum/set declaring `{PLACE_ORDER, CANCEL, OPEN_ORDERS,
   BALANCES, FILLS, TICKER, PRIVATE_WS, ...}`; the engine rejects operations a broker
   hasn't declared.

## Tests

- A `StubBroker` implementing the port: registry `register`/`get` round-trip;
  `get` of an unknown venue raises.
- Capability declaration: a broker that doesn't declare `PLACE_ORDER` is rejected
  by a helper that checks capability before use.
- The port is satisfiable purely with domain types (a stub returns domain `Order`/`Fill`).

## Verification on real data

Interface layer — no live I/O. Verify the contract is *implementable and coherent*:
the `StubBroker` exercises every method with domain objects and round-trips an
`Order` through `place_order` → `open_orders`. `pytest`/`ruff`/`mypy` green.

## Closeout

- CHANGELOG (Added): "`brokers.Broker` port + `BrokerRegistry` + capability model."
- ADR: the Broker surface + capability-declaration rule (and Protocol-vs-ABC choice).
- Status/roadmap: deferred to leaf 03.
