---
plan: domain-core/02-order
kind: leaf
status: done
complexity: high
depends: [01]
parallel: false
branch: feat/domain-order
pr: ""
---

# Order aggregate + lifecycle state machine

## Goal

The `Order` aggregate with an **explicit lifecycle state machine** and the order
types (market, limit, stop-loss, best-limit). Pure, Decimal, mypy-strict. Replaces
the legacy `{None, 'open', 'canceled', 'closed'}` ad-hoc status with a typed
machine.

## Files to change

- `trading_bot/domain/order.py` — new.
- `trading_bot/domain/__init__.py` — export the new names.
- `trading_bot/tests/domain/test_order.py` — new.

## Steps

1. Enums: `OrderSide{BUY,SELL}`, `OrderType{MARKET,LIMIT,STOP_LOSS,BEST_LIMIT}`,
   `OrderStatus{NEW,SUBMITTED,OPEN,PARTIALLY_FILLED,FILLED,CANCELLED,REJECTED}`.
2. `Order` dataclass: `client_order_id` (mandatory — idempotency invariant),
   `instrument`, `side`, `qty`, `type`, `limit_price?`, `stop_price?`, `filled_qty`,
   `avg_fill_price`, `status`, `venue_order_id?`. All amounts via `domain.money`.
   Construction validation: LIMIT requires `limit_price`; STOP_LOSS requires `stop_price`.
3. **State machine**: an allowed-transition table + methods `submit()`,
   `open(venue_order_id)`, `apply_fill(qty, price)`, `cancel()`, `reject(reason)`.
   `apply_fill` accumulates `filled_qty` and recomputes `avg_fill_price` (Decimal),
   moving to `PARTIALLY_FILLED`/`FILLED`. Port the legacy `check_vol_exec` tolerance
   ("fully filled within tol") from `legacy/orders.py`. Illegal transitions raise
   `OrderStatusError` (from `domain.errors`).
4. Keep pure: no I/O, no async.

## Tests

- Legal path `NEW→SUBMITTED→OPEN→PARTIALLY_FILLED→FILLED`; `cancel` from `OPEN`;
  `reject` from `SUBMITTED`.
- Illegal transitions raise `OrderStatusError` (e.g. `apply_fill` on a `CANCELLED`).
- `apply_fill` accumulates qty + weighted avg price **exactly** (Decimal);
  full-fill-within-tolerance closes to `FILLED`.
- Construction validation for each `OrderType`.

## Verification on real data

Pure layer. Replay a realistic partial-fill sequence (several partials summing to
the order qty, taken from the legacy semantics) and assert final `status==FILLED`
and `avg_fill_price` exact to the Decimal. `pytest` green, `mypy` strict clean.

## Closeout

- CHANGELOG (Added): "Order aggregate + lifecycle state machine and order types."
- ADR: short note — "explicit `OrderStatus` state machine replaces the legacy
  `{None,open,canceled,closed}`; idempotent `client_order_id` mandatory."
- Status/roadmap: deferred to leaf 05.
