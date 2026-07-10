---
plan: strategy-capital/03-storage-capital-events
kind: leaf
status: planned
complexity: medium
depends: [02]
parallel: false
branch: feat/storage-capital-events
pr: ""
---

# 03 — Storage: append-only `capital_events` table

## Goal

Persist the capital ledger in the existing per-strategy `SqliteStore` with the
exact discipline the `fills` table already proves out: append-only,
`INSERT OR IGNORE` on a composite PK, money as `str(Decimal)`, idempotent
migration probing. Ships **dormant** — nothing writes to it until leaf 06.

## Files to change

- `trading_bot/storage/sqlite_store.py`:
  - `_SCHEMA` (≈ lines 96–144): new table
    ```sql
    CREATE TABLE IF NOT EXISTS capital_events (
      event_id   TEXT NOT NULL,
      strategy   TEXT NOT NULL,
      event_type TEXT NOT NULL,          -- CapitalEventType.value
      amount     TEXT NOT NULL,          -- str(Decimal), positive magnitude
      ts         INTEGER NOT NULL,       -- epoch ms
      note       TEXT NOT NULL DEFAULT '',
      mode       TEXT NOT NULL DEFAULT 'paper',
      venue      TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (event_id, strategy, mode)
    );
    CREATE INDEX IF NOT EXISTS idx_capital_events_ts ON capital_events (ts);
    ```
    Same composite-PK rationale as the fills PK (`sqlite_store.py:134`): ids
    minted in different modes must never collide.
  - `record_capital_event(event: CapitalEvent) -> bool` — `INSERT OR IGNORE`,
    stamping `mode`/`venue` from the store context exactly like
    `_record_fill_tagged` (`sqlite_store.py:388-444`); returns whether a row was
    inserted (False = idempotent replay).
  - `capital_events(strategy: str | None = None, since_ms: int | None = None)
    -> list[CapitalEvent]` — ordered by `ts, event_id`; row → domain object via a
    `_row_to_capital_event` following `_row_to_fill` (`sqlite_store.py:791`).
  - `_migrate_capital_events()` — presence-probing migration following the
    `_migrate_orders_columns` template (`sqlite_store.py:919-937`), run
    unconditionally in `__init__` (`sqlite_store.py:276-280`). A fresh DB gets
    the table from `_SCHEMA`; an existing DB gets `CREATE TABLE IF NOT EXISTS`.
- `trading_bot/tests/storage/test_sqlite_store.py` — extend.

## Steps

1. Read the fills write/read/migration paths first; clone their shape.
2. Implement schema + write + read + migration; keep writes off the event loop
   consistent with the store's existing sync-call discipline (capital events are
   control-plane writes, not bus-driven — **no** `_WriteJob` branch needed).
3. Tests, then `python -m pytest trading_bot/tests/storage/ -v` + `ruff check`.

## Tests

- Round-trip: record → read back equal `CapitalEvent` (Decimal exact, note/ts
  preserved).
- Idempotency: same `(event_id, strategy, mode)` twice → one row, second call
  returns False; same `event_id` under a different `mode` → two rows.
- Ordering: reads come back `ts`-ordered with a deterministic tie-break.
- Filters: `strategy=`, `since_ms=` behave like `fills(since_ms=)`.
- Migration: opening a pre-existing DB file created without the table (fixture:
  create a store, `DROP TABLE capital_events`, reopen) adds it idempotently;
  double-open is a no-op.
- Mode tagging: `set_context(mode="live")` stamps rows exactly as fills.

## Verification on real data

Open a **copy** of the real archived store
(`var/dashboard/backup-2026-07-10/alloc1-kraken.sqlite` copied to a temp path):
the migration adds `capital_events` without touching the existing `fills`
(row-count before == after, `stored_fills()` unchanged), then a genesis event
records and reads back. Record row counts in the PR.

## Closeout

- CHANGELOG (Added): append-only `capital_events` ledger table (dormant until
  the capital service lands).
- Tick leaf 03 in `00-plan.md`.
