"""Storage layer — append-only SQLite order/fill history and engine state.

This package is the engine's **persistence** boundary: it records what the
engine has seen and done (orders, broker-confirmed fills, a little key/value
state) into a stdlib-:mod:`sqlite3`, WAL-mode database — the reconciliation
source on restart. Money is persisted as ``str(Decimal)`` TEXT (never float);
orders are UPSERTed (latest state) and fills are append-only (immutable facts).

See :class:`~trading_bot.storage.sqlite_store.SqliteStore`.
"""

from __future__ import annotations

from trading_bot.storage.sqlite_store import SqliteStore

__all__ = ["SqliteStore"]
