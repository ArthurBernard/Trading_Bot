---
plan: perf-persistence-risk/01-storage
kind: leaf
status: done
complexity: high
depends: []
parallel: false
branch: feat/storage
pr: ""
---

# Storage — SQLite order/fill history + engine state

## Goal

Open `trading_bot/storage/`: a SQLite append-only store for orders, fills and a bit
of engine state — the **reconciliation source** (E4's `reconcile` ultimately reads
truth from the broker, but the local store records what the engine has seen/done).
All money persisted as **TEXT** (exact `Decimal`, never float).

## Files to change

- `trading_bot/storage/__init__.py` — new; export the store.
- `trading_bot/storage/sqlite_store.py` — new; `SqliteStore` (stdlib `sqlite3`, WAL).
- `trading_bot/tests/storage/__init__.py` — new (empty).
- `trading_bot/tests/storage/test_sqlite_store.py` — new.

## Steps

1. Read dccd's `storage/runs_sqlite.py`
   (`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/storage/runs_sqlite.py`)
   for the stdlib-`sqlite3` pattern (WAL pragma, `row_factory`, a `_conn` context
   manager, `CREATE TABLE IF NOT EXISTS`, parametrised SQL). Read `domain/order.py`
   + `domain/fill.py` for the fields to persist.
2. `SqliteStore(db_path)`: open with WAL; create tables if absent:
   - `orders` (client_order_id PK, venue_order_id, instrument, side, type, qty TEXT,
     limit_price TEXT?, stop_price TEXT?, status, filled_qty TEXT, avg_fill_price TEXT?,
     ts) — **upsert** by `client_order_id` (an order's row reflects its latest state).
   - `fills` (fill_id PK, client_order_id, instrument, side, qty TEXT, price TEXT,
     fee TEXT, ts) — **append-only** (insert-or-ignore on fill_id; fills never mutate).
   - `state` (key PK, value TEXT) — small key/value for engine state (e.g. last
     reconcile ts).
   All money/qty columns are **TEXT** holding `str(Decimal)`; convert back with
   `money(...)` on read.
3. Write API: `upsert_order(order)`, `record_fill(fill)`, `set_state(k, v)` /
   `get_state(k)`. Read API: `get_order(cid)`, `orders()`, `fills(since_ms=None)` →
   rebuild domain `Order`/`Fill` objects (exact Decimal).
4. Optional bus integration: `attach(event_bus)` subscribing to `OrderEvent`
   (→ `upsert_order`) and `FillEvent` (→ `record_fill`) so the store fills itself
   from the engine's event stream. Keep it a thin adapter; the store works standalone.

## Tests (via `.venv`)

- Round-trip: `upsert_order` then `get_order` returns an equal domain `Order`
  (status, Decimal qty/prices exact); a re-`upsert` of the same `client_order_id`
  updates the row (one row, latest status).
- `record_fill` then `fills()` returns equal domain `Fill`s (Decimal exact);
  re-recording the same `fill_id` is a no-op (append-only, no dup).
- `fills(since_ms=...)` filters by ts.
- `set_state`/`get_state` round-trips; missing key → `None`.
- **No float**: assert stored columns are TEXT and reading yields `Decimal`
  (e.g. a price like `0.1` round-trips exactly).
- Bus integration: emitting `OrderEvent`/`FillEvent` populates the store.

## Verification on real data

Drive a realistic engine sequence (submit orders + fills through
`OrderRouter`→`PaperBroker` with the store `attach`ed to the bus), then **reopen the
SQLite file** in a fresh `SqliteStore` and assert the persisted orders/fills match —
positions rebuildable from the stored fills via `Position.from_fills`. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`storage.SqliteStore` — append-only SQLite order/fill history + state (Decimal as TEXT)."
- ADR: the schema + money-as-TEXT decision + upsert-orders/append-only-fills rule.
- Status/roadmap: deferred to leaf 03.
