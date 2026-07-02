---
plan: audit-remediation-wave2/01-storage-async-io
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/storage-async-io
pr: ""
---

# Leaf 01 — storage-async-io

## Goal
Move SQLite writes off the event loop and harden the store. Fixes `A-2`, `D-8`, `D-9`.

## Files
`trading_bot/storage/sqlite_store.py`, `trading_bot/application/service_factory.py`
(the `attach`/bus handler wiring), tests under `trading_bot/tests/storage/` and
`trading_bot/tests/application/`.

## Steps
1. `A-2` (High): the bus handler in `SqliteStore.attach` runs a synchronous
   open→write→commit→close **inside the async event loop** on every order and every
   fill (hot path), blocking all runners. Move the writes off the loop —
   `asyncio.to_thread` / a run-in-executor, or a dedicated single writer task/queue
   draining append-only writes. Preserve ordering, append-only semantics, and
   idempotency; the store stays the reconciliation source of truth.
2. `D-8` (Medium): open the SQLite connection with WAL journal mode + an explicit
   `busy_timeout` (don't rely on the implicit 5s default) for concurrency safety.
3. `D-9` (Medium): `fill_id` is the sole primary key across all modes, so a paper
   fill and a live fill sharing a venue id collide (`INSERT OR IGNORE` keeps the
   first, dropping the other) — breaking the separate-series / no-double-count
   guarantee. Make the fill identity composite (e.g. `(fill_id, mode)` or
   `(fill_id, venue, mode)`) with an idempotent migration.

## Tests
Concurrent order/fill writes don't block a concurrent coroutine (no event-loop
stall); WAL + busy_timeout set; a paper fill and a live fill with the same
`fill_id` both persist (no collision); migration is idempotent on an old-schema DB.

## Verification on real data
Offline / PaperBroker: drive many rapid paper fills and confirm the book/PnL are
correct and no write is lost; open an old-schema DB and confirm the fill-id
migration upgrades it. Do NOT hit a real venue.

## Closeout
CHANGELOG (Fixed): SQLite writes off the event loop; WAL + busy_timeout; fill-id
mode isolation. ADR: async-store-write strategy (executor vs writer task) + the
composite fill identity.
