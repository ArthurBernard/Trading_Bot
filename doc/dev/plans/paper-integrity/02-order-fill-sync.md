---
plan: paper-integrity/02-order-fill-sync
kind: leaf
status: planned
complexity: high
depends: []
parallel: true
branch: fix/order-fill-sync
pr: ""
---

# 02 — OrderFillSync: fills reach the tracked Order (and the store)

## Goal

Nothing ever calls `Order.apply_fill` on the `OrderRouter`'s tracked instance,
so every filled order is frozen in store/UI at `status='open'`, `filled_qty=0`,
`avg_fill_price=NULL` (27/27 filled orders in the dashboard DBs). Add a new
application component that closes the loop:

- **live path**: on `FillEvent`, apply the fill to the tracked order and
  re-emit `OrderEvent` so `SqliteStore.upsert_order` persists the new state;
- **paper ordering**: `EventBus.emit` dispatches sync handlers inline, and
  `PaperBroker.place_order` fills synchronously — the `FillEvent` fires
  *before* `OrderRouter._do_submit` runs `order.submit(); order.open(venue_id)`
  and tracks the order (`order_router.py:283-293`). So fills for an unknown or
  not-yet-fillable order are **stashed by `client_order_id` and drained when
  its `OrderEvent` arrives** with a fillable status;
- **restart healing**: a `replay(fills)` entry point applies persisted fills
  to freshly-restored orders in `supervisor._start_locked`, **between**
  `router.restore(...)` and `reconcile(...)` — a genuinely-filled order is
  then terminal before the orphan rule runs, so reconcile stops
  mis-cancelling filled orders, and the healed row is persisted via the
  re-emitted `OrderEvent`.

Fill ingestion is deliberately outside `OrderRouter` (its module docstring
declares the fill boundary out of scope) — this is a new component, not a
router method.

## Files to change

- `trading_bot/application/order_fill_sync.py` — **new**. Class
  `OrderFillSync(router: OrderRouter, bus: EventBus)`; subscribes itself to
  `bus` on construction. State: `self._seen_fill_ids: set[str]`,
  `self._pending: dict[str, list[Fill]]` (keyed by `client_order_id`).
  Behaviour:
  - `FillEvent(fill)`: skip if `fill.fill_id` in seen. If
    `router.get(fill.client_order_id)` returns an order whose status is
    fillable (`OPEN`/`PARTIALLY_FILLED`) → `order.apply_fill(fill.qty,
    fill.price)`, mark seen, re-emit `OrderEvent(order)`. Else stash in
    `_pending`.
  - `OrderEvent(order)`: if pending fills exist for `order.client_order_id`
    and the status is fillable → drain them in list order (apply + mark
    seen), then re-emit **one** `OrderEvent(order)`. If the event's order is
    terminal, drop its pending entry (bounded memory). Guard against
    self-recursion: the re-emitted event finds no pending fills → no-op (add
    a test proving no infinite loop).
  - `replay(fills: Iterable[Fill]) -> int`: for each fill (in ts order), same
    apply-if-tracked-and-fillable logic, mark seen, re-emit one `OrderEvent`
    per *healed order* (not per fill); returns the number of orders healed.
    Fills for untracked/terminal orders are recorded as seen and skipped.
  - Every `apply_fill` call is wrapped: on `OrderError`/`OrderStatusError`,
    log a warning (module logger) and continue — a malformed fill must never
    break the bus or the other consumers. Money stays `Decimal` end-to-end
    (the events carry domain objects; no re-encoding).
- `trading_bot/application/service_factory.py` — construct
  `fill_sync = OrderFillSync(router, bus)` in `build_engine` right after the
  router; add a `fill_sync` field to the `Engine` dataclass.
- `trading_bot/application/supervisor.py` — in `_start_locked`, after
  `engine.router.restore(engine.store.orders())` and **before**
  `await reconcile(...)`:
  `engine.fill_sync.replay(r.fill for r in engine.store.stored_fills() if r.mode == unit.mode)`
  (same mode filter as `_replay_paper_book`; fills sorted by `ts`). Update
  the `order_history()` docstring note (`supervisor.py:1412-1414`) that
  documents the old evict-as-orphan behaviour for filled orders.
- `trading_bot/tests/application/test_order_fill_sync.py` — **new** (tests
  below).

## Steps

1. Write `OrderFillSync` (docstring: the ordering rationale above — why
   stash-and-drain, why not in the router; numpydoc style).
2. Wire it in `build_engine` + `Engine`.
3. Add the `replay` call in `_start_locked`; adjust the stale docstring.
4. Tests; `python -m pytest && ruff check trading_bot/` green.

## Tests

`test_order_fill_sync.py` (build with a real `EventBus`, real `OrderRouter`
over a `PaperBroker` — the point is the real synchronous event ordering):

- `test_paper_immediate_fill_updates_order`: submit a limit order that fills
  at placement → after `router.submit()` returns, the tracked order is
  `FILLED` with `filled_qty == qty` and correct `avg_fill_price`.
- `test_store_row_reaches_terminal`: same, with a `SqliteStore` attached →
  `store.orders()` row shows `filled`/final qty/avg price (this is the
  27-frozen-rows regression).
- `test_partial_fills_accumulate`: broker `fill_model="partial"` → order goes
  `PARTIALLY_FILLED` then `FILLED`; `avg_fill_price` is the quantity-weighted
  average (Decimal-exact).
- `test_idempotent_by_fill_id`: re-emitting the same `FillEvent` twice
  changes nothing.
- `test_no_event_loop`: the re-emitted `OrderEvent` does not recurse
  (bounded emit count on a counting subscriber).
- `test_replay_heals_restored_orders`: store with a filled-but-`open` order
  row + its fill (hand-built, mirroring the real dashboard DBs) → `restore`
  then `replay` → order is `FILLED` and the store row is updated; reconcile
  afterwards does NOT orphan-cancel it.
- `test_replay_skips_untracked_and_terminal`: replay marks such fills seen,
  heals nothing, returns 0.

## Verification on real data

Copy `var/dashboard/alloc1-binance.sqlite` to scratch. Drive a real
restore+replay through the actual seams (`SqliteStore` + `OrderRouter` +
`OrderFillSync` — a small script or pytest marked `-m` local, NOT committed
with hardcoded personal paths; keep the script in the PR description or under
scratch). Read back `store.orders()` and report: all 14 wave-1 rows move
`open → filled` with `filled_qty == qty` and the fill's price as
`avg_fill_price`; the 14 unfilled wave-2 rows are untouched. Same check on
`alloc1-kraken.sqlite` (13+13). Compare tracker positions before/after: MUST
be unchanged (healing touches orders, never positions).

## Closeout

- CHANGELOG `[Unreleased] > Fixed`: "Fills now update the tracked order and
  its store row (`OrderFillSync`) — filled orders no longer sit at
  `open`/`filled_qty=0` forever; restart heals historical rows."
- ADR: fill ingestion as a dedicated component (router boundary), the
  stash-and-drain ordering contract, heal-before-reconcile placement.
- Tick leaf 02 in `00-plan.md`.
