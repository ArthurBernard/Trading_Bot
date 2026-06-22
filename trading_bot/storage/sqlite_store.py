"""The :class:`SqliteStore` — append-only SQLite order/fill history + state.

This is the persistence layer's single store: a stdlib-:mod:`sqlite3`,
WAL-mode database recording everything the engine has *seen* and *done* — the
**reconciliation source** (on restart the engine reconciles its local view
against the broker's truth, but this store holds what it last knew). It mirrors
dccd's ``storage/runs_sqlite.py`` pattern (WAL pragma, ``row_factory =
sqlite3.Row``, a ``_conn`` context manager opening a fresh connection per
operation, ``CREATE TABLE IF NOT EXISTS`` on init, parametrised SQL).

It speaks **domain types** at its boundary: writes accept
:class:`~trading_bot.domain.order.Order` / :class:`~trading_bot.domain.fill.Fill`
aggregates and reads rebuild them; internally it stores primitives only.

Design choices (carried into the ADR)
-------------------------------------
* **Money as TEXT, never float.** Every monetary / quantity column is ``TEXT``
  holding ``str(Decimal)``. SQLite's only numeric types are ``INTEGER`` and
  ``REAL`` (binary float) — persisting a price through ``REAL`` would bake in
  the very rounding error the :mod:`~trading_bot.domain.money` layer refuses.
  Storing the canonical ``str`` form and rebuilding with
  :func:`~trading_bot.domain.money.money` on read is exact and round-trips
  losslessly. Enums are stored by ``.value`` and rebuilt via their constructor;
  the :class:`~trading_bot.domain.instrument.Symbol` is stored as its ``BASE/QUOTE``
  string and split back on ``"/"``.

* **Orders are UPSERTed; fills are append-only.** An order is a *stateful
  aggregate*: its row is keyed by ``client_order_id`` and an
  ``INSERT ... ON CONFLICT DO UPDATE`` keeps exactly one row reflecting its
  **latest** state (status, ``filled_qty``, ``avg_fill_price``, ...). A fill is
  an *immutable fact*: its row is keyed by the venue ``fill_id`` and inserted
  with ``INSERT OR IGNORE`` so re-recording the same execution (a replayed
  event, a reconciliation re-fetch) is a silent no-op — fills never mutate and
  never duplicate.

* **Reads do not replay the state machine.** :meth:`get_order` / :meth:`orders`
  reconstruct the :class:`Order` dataclass directly and set ``status`` /
  ``filled_qty`` / ``avg_fill_price`` / ``venue_order_id`` to the stored values.
  The persisted row *is* the truth; replaying ``submit -> open -> apply_fill``
  would re-derive (and could disagree with) what the engine actually recorded.

* **A fresh connection per operation.** Like dccd, every public method opens its
  own connection through the :meth:`_conn` context manager. This keeps the store
  trivially safe for the test usage (and for being shared across threads, since
  no connection is held), at the cost of per-call connection overhead — fine for
  an order/fill history written at human/venue rates.

Optionally, :meth:`attach` subscribes the store to an
:class:`~trading_bot.application.events.EventBus` so it fills itself from the
engine's event stream (``OrderEvent -> upsert_order``,
``FillEvent -> record_fill``). The store works standalone with no bus.
"""

from __future__ import annotations

import pathlib
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

if TYPE_CHECKING:
    from trading_bot.application.events import Event, EventBus

__all__ = ["SqliteStore"]

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    venue_order_id  TEXT,
    instrument      TEXT NOT NULL,
    side            TEXT NOT NULL,
    type            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    limit_price     TEXT,
    stop_price      TEXT,
    status          TEXT NOT NULL,
    filled_qty      TEXT NOT NULL,
    avg_fill_price  TEXT,
    ts              INTEGER
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    price           TEXT NOT NULL,
    fee             TEXT NOT NULL,
    ts              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_fills_cid ON fills(client_order_id);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _instrument_to_text(instrument: Instrument) -> str:
    """Render an instrument to its ``BASE/QUOTE`` symbol string for storage."""
    return str(instrument.symbol)


def _instrument_from_text(text: str) -> Instrument:
    """Rebuild an :class:`Instrument` from a ``BASE/QUOTE`` symbol string.

    Trading metadata (``price_precision`` / ``qty_precision``) is *not*
    persisted — it belongs to the venue's instrument catalogue, not the
    order/fill history — so the rebuilt instrument carries only its symbol.
    """
    base, quote = text.split("/", 1)
    return Instrument(Symbol(base, quote))


class SqliteStore:
    """Append-only SQLite store for order/fill history and engine state.

    Construct it on a database path (created if absent, with its parent
    directories); the schema is applied on init. Then persist with
    :meth:`upsert_order`, :meth:`record_fill` and :meth:`set_state`, and read
    back exact-:class:`~decimal.Decimal` domain objects with :meth:`get_order`,
    :meth:`orders`, :meth:`fills` and :meth:`get_state`. Optionally wire it to an
    :class:`~trading_bot.application.events.EventBus` with :meth:`attach`.

    Parameters
    ----------
    db_path : str or pathlib.Path
        Path to the SQLite database file. Created if absent; parent directories
        are created too. Use ``":memory:"`` for an ephemeral in-memory store
        (note: with a fresh connection per op, an in-memory DB does not persist
        across operations — use a file path for anything real).

    Examples
    --------
    >>> import tempfile, os
    >>> from trading_bot.domain import Instrument, Symbol, Order, OrderSide, OrderType, money
    >>> path = tempfile.mktemp(suffix=".db")
    >>> store = SqliteStore(path)
    >>> o = Order("cid-1", Instrument(Symbol("BTC", "USD")), OrderSide.BUY,
    ...           money("2"), OrderType.LIMIT, limit_price=money("30000"))
    >>> store.upsert_order(o)
    >>> store.get_order("cid-1").qty
    Decimal('2')
    >>> store.close(); os.unlink(path)

    """

    def __init__(self, db_path: str | pathlib.Path) -> None:
        self._path = pathlib.Path(db_path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a fresh connection (``sqlite3.Row`` rows), commit or rollback."""
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # --- write API --------------------------------------------------------- #

    def upsert_order(self, order: Order) -> None:
        """Insert or update ``order``'s row, keyed by ``client_order_id``.

        UPSERT semantics: the first call inserts; any later call with the same
        ``client_order_id`` overwrites every mutable column so the single row
        always reflects the order's **latest** state (``status``,
        ``filled_qty``, ``avg_fill_price``, ``venue_order_id``). Money/qty are
        stored as ``str(Decimal)`` TEXT; enums by ``.value``.

        Parameters
        ----------
        order : Order
            The order aggregate to persist (its current snapshot).

        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    client_order_id, venue_order_id, instrument, side, type,
                    qty, limit_price, stop_price, status, filled_qty,
                    avg_fill_price, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    venue_order_id = excluded.venue_order_id,
                    instrument     = excluded.instrument,
                    side           = excluded.side,
                    type           = excluded.type,
                    qty            = excluded.qty,
                    limit_price    = excluded.limit_price,
                    stop_price     = excluded.stop_price,
                    status         = excluded.status,
                    filled_qty     = excluded.filled_qty,
                    avg_fill_price = excluded.avg_fill_price,
                    ts             = excluded.ts
                """,
                (
                    order.client_order_id,
                    order.venue_order_id,
                    _instrument_to_text(order.instrument),
                    order.side.value,
                    order.type.value,
                    str(order.qty),
                    None if order.limit_price is None else str(order.limit_price),
                    None if order.stop_price is None else str(order.stop_price),
                    order.status.value,
                    str(order.filled_qty),
                    None
                    if order.avg_fill_price is None
                    else str(order.avg_fill_price),
                    None,
                ),
            )

    def record_fill(self, fill: Fill) -> None:
        """Append ``fill`` to the fills table — append-only, no overwrite.

        ``INSERT OR IGNORE`` on the ``fill_id`` primary key: re-recording the
        same execution (a replayed :class:`~trading_bot.application.events.
        FillEvent`, a reconciliation re-fetch) is a silent no-op. Fills are
        immutable facts; they never mutate and never duplicate. Money/qty/fee
        are stored as ``str(Decimal)`` TEXT.

        Parameters
        ----------
        fill : Fill
            The broker-confirmed execution to persist.

        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO fills (
                    fill_id, client_order_id, instrument, side, qty, price,
                    fee, ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.fill_id,
                    fill.client_order_id,
                    _instrument_to_text(fill.instrument),
                    fill.side.value,
                    str(fill.qty),
                    str(fill.price),
                    str(fill.fee),
                    fill.ts,
                ),
            )

    def set_state(self, key: str, value: str) -> None:
        """Set the engine-state ``value`` for ``key`` (UPSERT by ``key``).

        A small string key/value scratchpad for engine state (e.g. the last
        reconcile timestamp). Both columns are TEXT.

        Parameters
        ----------
        key : str
            The state key.
        value : str
            The value to store (callers serialise non-string state themselves).

        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # --- read API ---------------------------------------------------------- #

    def get_order(self, client_order_id: str) -> Order | None:
        """Return the stored :class:`Order` for ``client_order_id``, or ``None``.

        The order is rebuilt **directly** from the stored row — the dataclass is
        constructed and ``status`` / ``filled_qty`` / ``avg_fill_price`` /
        ``venue_order_id`` are set to the persisted values (the state machine is
        *not* replayed; the row is the truth). All money is exact
        :class:`~decimal.Decimal`.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return None if row is None else _row_to_order(row)

    def orders(self) -> list[Order]:
        """Return every stored order, rebuilt as domain :class:`Order` objects.

        Returns
        -------
        list of Order
            All persisted orders (one row per ``client_order_id``), insertion
            order. Money exact.

        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM orders ORDER BY rowid").fetchall()
        return [_row_to_order(r) for r in rows]

    def fills(self, since_ms: int | None = None) -> list[Fill]:
        """Return stored fills, optionally only those at/after ``since_ms``.

        Parameters
        ----------
        since_ms : int, optional
            Lower time bound as **milliseconds since the Unix epoch (UTC)**,
            inclusive. ``None`` (default) returns every stored fill.

        Returns
        -------
        list of Fill
            The matching fills, in insertion (execution) order. Money exact.

        """
        with self._conn() as conn:
            if since_ms is None:
                rows = conn.execute(
                    "SELECT * FROM fills ORDER BY rowid"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fills WHERE ts >= ? ORDER BY rowid",
                    (since_ms,),
                ).fetchall()
        return [_row_to_fill(r) for r in rows]

    def get_state(self, key: str) -> str | None:
        """Return the stored value for ``key``, or ``None`` if the key is unknown."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    # --- bus integration --------------------------------------------------- #

    def attach(self, event_bus: EventBus) -> None:
        """Subscribe the store to ``event_bus`` so it fills from the event stream.

        A thin adapter: it subscribes one handler that routes
        :class:`~trading_bot.application.events.OrderEvent` to
        :meth:`upsert_order` and :class:`~trading_bot.application.events.
        FillEvent` to :meth:`record_fill` (other events are ignored). The store
        works standalone without a bus; this just wires the engine's order/fill
        stream straight into the history.

        Parameters
        ----------
        event_bus : EventBus
            The bus to subscribe to.

        """
        # Imported lazily so the storage module never hard-depends on the
        # application layer (it works standalone); only ``attach`` needs it.
        from trading_bot.application.events import FillEvent, OrderEvent

        def _on_event(event: Event) -> None:
            if isinstance(event, OrderEvent):
                self.upsert_order(event.order)
            elif isinstance(event, FillEvent):
                self.record_fill(event.fill)

        event_bus.subscribe(_on_event)

    # --- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        """Close the store.

        A no-op for connection state (each operation opens and closes its own
        connection), provided so callers can treat the store as a closable
        resource symmetrically with a real connection-holding store.
        """
        # Nothing to release: connections are per-operation (see ``_conn``).

    def __enter__(self) -> SqliteStore:
        """Enter the runtime context, returning the store."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit the runtime context, closing the store."""
        self.close()


def _row_to_order(row: sqlite3.Row) -> Order:
    """Rebuild an :class:`Order` from a stored ``orders`` row (exact Decimal).

    Constructs the dataclass from the immutable fields, then sets the mutable
    lifecycle fields (``status`` / ``filled_qty`` / ``avg_fill_price`` /
    ``venue_order_id``) directly to the stored values — the persisted row is the
    truth, so the state machine is not replayed.
    """
    limit_raw = row["limit_price"]
    stop_raw = row["stop_price"]
    avg_raw = row["avg_fill_price"]
    order = Order(
        client_order_id=str(row["client_order_id"]),
        instrument=_instrument_from_text(str(row["instrument"])),
        side=OrderSide(row["side"]),
        qty=money(str(row["qty"])),
        type=OrderType(row["type"]),
        limit_price=None if limit_raw is None else money(str(limit_raw)),
        stop_price=None if stop_raw is None else money(str(stop_raw)),
    )
    order.filled_qty = money(str(row["filled_qty"]))
    order.avg_fill_price = None if avg_raw is None else money(str(avg_raw))
    order.status = OrderStatus(row["status"])
    venue = row["venue_order_id"]
    order.venue_order_id = None if venue is None else str(venue)
    return order


def _row_to_fill(row: sqlite3.Row) -> Fill:
    """Rebuild a :class:`Fill` from a stored ``fills`` row (exact Decimal)."""
    return Fill(
        fill_id=str(row["fill_id"]),
        client_order_id=str(row["client_order_id"]),
        instrument=_instrument_from_text(str(row["instrument"])),
        side=OrderSide(row["side"]),
        qty=money(str(row["qty"])),
        price=money(str(row["price"])),
        fee=money(str(row["fee"])),
        ts=int(row["ts"]),
    )
