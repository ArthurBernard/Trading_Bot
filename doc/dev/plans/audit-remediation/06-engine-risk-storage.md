---
plan: audit-remediation/06-engine-risk-storage
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/engine-risk-storage
pr: ""
---

# Leaf 06 — engine-risk-storage

## Goal
Make `max_daily_loss` actually daily, and give the reconciliation source (`orders`)
a migration path. Fixes `A-1`, `D-2`.

## Files to change
- `trading_bot/application/risk.py`, `service_factory.py`,
  `application/performance_service.py` (daily PnL source),
  `trading_bot/storage/sqlite_store.py` + tests under
  `trading_bot/tests/application/` and `trading_bot/tests/storage/`.

## Steps
1. `A-1`: `max_daily_loss` currently reads `PerformanceService.realised_pnl()`
   (cumulative session PnL) and `reset_day` is dead — so the "daily" breaker never
   resets and latches the kill-switch permanently. Wire the breaker to a **current
   day's** realised PnL (a day-scoped source), reset at the UTC day boundary, and
   make the reset actually fire (`risk.py:242-246, 250-277`;
   `service_factory.py:206-210`). Define the day boundary explicitly (UTC).
2. `D-2`: add a schema migration for the `orders` table mirroring the existing
   `_migrate_fills_tags` (`sqlite_store.py:82-121`), so a v0 / drifted DB is
   upgraded instead of hard-failing `upsert_order` with `OperationalError`. Handle a
   partially-migrated DB idempotently. While here, fix `D-5` (`orders.ts` written
   NULL → persist the order timestamp) and `D-14` (restore `reject_reason` /
   `fill_tolerance` in `_row_to_order`) if low-risk.

## Tests
- Daily-loss breaker trips within a day, then RESETS after the boundary and allows
  trading again; cumulative loss across days does not falsely latch.
- Opening an old-schema `orders` DB migrates cleanly; `upsert_order` then succeeds;
  re-running the migration is a no-op; `orders.ts` round-trips; a reloaded rejected
  order keeps its reason.

## Verification on real data
Offline / PaperBroker. Build an engine with a small `max_daily_loss`, drive paper
fills to trip it, advance the clock past the boundary, confirm the breaker resets.
Create a fixture SQLite at the old schema and confirm the migration upgrades it.

## Closeout
CHANGELOG (Fixed): `max_daily_loss` is now genuinely daily (UTC reset); `orders`
table migration. ADR: daily-loss breaker semantics (day-scoped source + UTC reset)
and the orders-table migration policy.
