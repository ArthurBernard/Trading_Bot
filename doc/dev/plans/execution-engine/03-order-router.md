---
plan: execution-engine/03-order-router
kind: leaf
status: done
complexity: high
depends: [01, 02]
parallel: false
branch: feat/order-router
pr: ""
---

# OrderRouter — idempotent submit + state-machine driving

## Goal

`OrderRouter` is the engine's write path: it submits domain `Order`s to a `Broker`
**idempotently** (client-order-id dedup), drives the order through its state machine
from the broker's responses, and emits events on the `EventBus`. The safety core of
execution.

## Files to change

- `trading_bot/application/order_router.py` — new; `OrderRouter`.
- `trading_bot/application/__init__.py` — export `OrderRouter`.
- `trading_bot/tests/application/test_order_router.py` — new.

## Steps

1. Read `application/events.py` (EventBus + OrderEvent), `brokers/base.py`, domain
   `Order`/`OrderStatus`/`errors`.
2. `OrderRouter(broker, event_bus)`:
   - `async submit(order: Order) -> Order`: **idempotency** — if `order.client_order_id`
     was already submitted, return the existing tracked order and do **not** call the
     broker again (dedup map keyed by client-order-id). Otherwise `order.submit()`,
     `venue_id = await broker.place_order(order)`, `order.open(venue_id)`, track it,
     emit an `OrderEvent`. On `BrokerError`/`OrderError` → `order.reject(reason)` +
     emit, and surface a clear error.
   - `async cancel(order_or_id)`: `await broker.cancel_order(...)`, `order.cancel()`, emit.
   - `apply_fill(...)` / consume broker fills to advance the order (or leave fill
     ingestion to the tracker — document the split).
   - Keep money `Decimal`; never lose/duplicate an order.

## Tests

- **Idempotent submit**: submitting the same `client_order_id` twice → broker
  `place_order` called **once**; second call returns the same tracked order.
- Submit drives `Order` `NEW→SUBMITTED→OPEN` (venue id set) and emits one `OrderEvent`.
- A `BrokerError` on submit → `Order` `REJECTED` + a reject event; the error surfaces.
- `cancel` cancels on the broker and transitions the order.

## Verification on real data

Route a realistic order sequence through `OrderRouter` → **`PaperBroker`** (leaf 02);
assert (a) a duplicate client-order-id produces exactly one paper order/fill, and
(b) the emitted events match the order lifecycle. Gates via **`.venv`**.

## Closeout

- CHANGELOG (Added): "`application.OrderRouter` — idempotent order submission + lifecycle driving + events."
- ADR: the idempotency mechanism (client-order-id dedup) + where fill ingestion lives.
- Status/roadmap: deferred to leaf 05.
