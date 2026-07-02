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

import logging
import pathlib
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generator

from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import (
    DEFAULT_FILL_TOLERANCE,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

if TYPE_CHECKING:
    from trading_bot.application.events import Event, EventBus

__all__ = ["StoredFill", "SqliteStore"]

logger = logging.getLogger(__name__)

#: The mode stamped on a fill whose deployment mode is unknown (a pre-migration
#: row, or a store written with no mode context). ``"paper"`` is the safe default
#: — a fill with no venue could only have been the simulator's.
_DEFAULT_MODE = "paper"

#: Milliseconds a connection waits for a held lock before raising ``database is
#: locked``, set explicitly per connection (``PRAGMA busy_timeout``). WAL lets
#: readers and the single writer proceed concurrently, but a checkpoint or a
#: second writer can still briefly hold the lock; a generous, *explicit* budget
#: (rather than sqlite3's implicit 5 s default) keeps concurrent access safe.
_BUSY_TIMEOUT_MS = 30_000

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
    ts              INTEGER,
    reject_reason   TEXT,
    fill_tolerance  TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    instrument      TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    price           TEXT NOT NULL,
    fee             TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'paper',
    venue           TEXT NOT NULL DEFAULT '',
    -- Composite identity: a paper fill and a live fill can legitimately share a
    -- venue ``fill_id`` (the simulator mints ids independently of any venue), so
    -- keying on ``fill_id`` alone would let ``INSERT OR IGNORE`` silently drop the
    -- second — commingling / losing a fill (the PnL source of truth). Partitioning
    -- the key by ``(fill_id, venue, mode)`` keeps the separate-series guarantee:
    -- same execution (same id + venue + mode) still dedups to a no-op.
    PRIMARY KEY (fill_id, venue, mode)
);

CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS idx_fills_cid ON fills(client_order_id);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True, slots=True)
class StoredFill:
    """A persisted :class:`Fill` plus its storage-level deployment tags.

    The store's read-side record for :meth:`SqliteStore.stored_fills`: the exact
    domain :class:`~trading_bot.domain.fill.Fill` (money intact), tagged with the
    ``mode`` (``"paper"`` / ``"testnet"`` / ``"live"``) and ``venue`` the unit's
    engine was in when the execution was recorded. The tags are a **storage /
    deployment** concern — they live on the store row, never on the pure domain
    :class:`Fill` — so a per-mode PnL curve can keep live and testnet (fake money)
    as separate series without polluting the domain type.

    Attributes
    ----------
    fill : Fill
        The immutable execution record (the PnL source of truth), money exact.
    mode : str
        The deployment mode active when the fill was recorded (``"paper"`` for a
        pre-migration row).
    venue : str
        The venue the unit ran on (``""`` when unknown — e.g. a pre-migration row
        or a paper unit with no venue).

    """

    fill: Fill
    mode: str
    venue: str


@dataclass(frozen=True, slots=True)
class _WriteJob:
    """One append-only write queued for the off-loop writer thread.

    A tagged union carrying either an :class:`~trading_bot.domain.order.Order` to
    UPSERT or a :class:`~trading_bot.domain.fill.Fill` to append, plus the
    ``mode`` / ``venue`` context captured **at enqueue time** so a later
    :meth:`SqliteStore.set_context` cannot retag a fill already in flight (the tag
    is fixed the instant the event lands). Exactly one of ``order`` / ``fill`` is
    set. Enqueued by the bus handler (off the event loop) and drained FIFO by the
    single writer thread, which applies it via the store's synchronous write API.
    """

    order: Order | None
    fill: Fill | None
    mode: str
    venue: str


def _now_ms() -> int:
    """Current wall-clock time as **ms since the Unix epoch (UTC)**.

    Stamped on an order's ``ts`` column at first persist. :func:`time.time` is
    UTC-anchored epoch seconds, matching the millisecond convention the domain
    :class:`~trading_bot.domain.fill.Fill` uses for its own ``ts``.
    """
    return int(time.time() * 1000)


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

    def __init__(
        self,
        db_path: str | pathlib.Path,
        *,
        mode: str = _DEFAULT_MODE,
        venue: str = "",
    ) -> None:
        self._path = pathlib.Path(db_path)
        # The deployment tags a fresh fill is stamped with (the unit's engine mode
        # + venue). A storage/deployment concern, kept off the pure domain Fill.
        self._mode = mode
        self._venue = venue
        # The off-loop writer: a single dedicated thread draining a FIFO queue of
        # append-only writes, started lazily by ``attach`` (only the bus/event hot
        # path needs it; direct method calls stay synchronous for read-after-write).
        # ``None`` until attached. See ``_ensure_writer`` / ``_writer_loop``.
        self._writer: threading.Thread | None = None
        self._write_queue: queue.Queue[_WriteJob | None] = queue.Queue()
        self._writer_lock = threading.Lock()
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            _migrate_fills_tags(conn)
            _migrate_fills_pk(conn)
            _migrate_orders_columns(conn)

    def set_context(self, *, mode: str, venue: str) -> None:
        """Set the ``mode`` / ``venue`` stamped on subsequently-recorded fills.

        The seam the supervisor uses to tag a unit's fills with the deployment
        mode (``paper`` / ``testnet`` / ``live``) and venue its engine is running
        under, so a per-mode PnL curve can keep live and testnet (fake money) as
        separate series. Affects only fills recorded **after** the call; already
        persisted rows keep their stamp (fills are append-only, immutable facts).

        Parameters
        ----------
        mode : str
            The deployment mode to stamp (``"paper"`` / ``"testnet"`` / ``"live"``).
        venue : str
            The venue the unit runs on (``""`` for a paper unit with no venue).

        """
        self._mode = mode
        self._venue = venue

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield a fresh connection (``sqlite3.Row`` rows), commit or rollback.

        Every connection is opened in WAL journal mode with an **explicit**
        ``busy_timeout`` (:data:`_BUSY_TIMEOUT_MS`) rather than relying on
        sqlite3's implicit 5 s default, so concurrent access (the writer thread
        plus reconciliation reads on the loop) waits on a briefly-held lock
        instead of raising ``database is locked``. ``journal_mode`` is a
        persistent database property but is (re)asserted here so a connection to a
        DB created elsewhere still lands in WAL.
        """
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
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
                    avg_fill_price, ts, reject_reason, fill_tolerance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    reject_reason  = excluded.reject_reason,
                    fill_tolerance = excluded.fill_tolerance,
                    -- Keep the first-seen write timestamp: this is the
                    -- append-only reconciliation source, so the row records when
                    -- the order was first persisted, not last touched.
                    ts             = COALESCE(orders.ts, excluded.ts)
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
                    None if order.avg_fill_price is None else str(order.avg_fill_price),
                    _now_ms(),
                    order.reject_reason,
                    str(order.fill_tolerance),
                ),
            )

    def record_fill(self, fill: Fill) -> None:
        """Append ``fill`` to the fills table — append-only, no overwrite.

        ``INSERT OR IGNORE`` on the composite ``(fill_id, venue, mode)`` primary
        key: re-recording the *same* execution (a replayed
        :class:`~trading_bot.application.events.FillEvent`, a reconciliation
        re-fetch) is a silent no-op, while a paper fill and a live fill that
        happen to share a venue ``fill_id`` are **kept as distinct rows** (the
        simulator mints ids independently of any venue — keying on ``fill_id``
        alone would silently drop the second and lose a fill). Fills are immutable
        facts; they never mutate and never duplicate. Money/qty/fee are stored as
        ``str(Decimal)`` TEXT.

        The row is tagged with the store's current ``mode`` / ``venue`` (see
        :meth:`set_context`) — a **storage / deployment** concern kept off the
        pure domain :class:`Fill`, so a per-mode PnL curve can keep live and
        testnet (fake money) as separate series. The tags are part of the fill's
        storage identity, so the separate series never collide.

        Parameters
        ----------
        fill : Fill
            The broker-confirmed execution to persist.

        """
        self._record_fill_tagged(fill, self._mode, self._venue)

    def _record_fill_tagged(self, fill: Fill, mode: str, venue: str) -> None:
        """Append ``fill`` with *explicit* ``mode`` / ``venue`` storage tags.

        The tag-bearing core of :meth:`record_fill`. The public method reads the
        store's *current* context; the off-loop writer replays a job with the tags
        captured **at enqueue time** so a fill in flight is never retagged by a
        later :meth:`set_context`. Idempotent (``INSERT OR IGNORE`` on the
        composite key).
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO fills (
                    fill_id, client_order_id, instrument, side, qty, price,
                    fee, ts, mode, venue
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    mode,
                    venue,
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
                rows = conn.execute("SELECT * FROM fills ORDER BY rowid").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fills WHERE ts >= ? ORDER BY rowid",
                    (since_ms,),
                ).fetchall()
        return [_row_to_fill(r) for r in rows]

    def stored_fills(self, since_ms: int | None = None) -> list[StoredFill]:
        """Return stored fills with their ``mode`` / ``venue`` deployment tags.

        The tagged counterpart of :meth:`fills`: each :class:`StoredFill` carries
        the exact domain :class:`Fill` (money intact) plus the ``mode``
        (``paper`` / ``testnet`` / ``live``) and ``venue`` the unit's engine was
        in when the execution was recorded. This is what a per-mode PnL curve
        folds — live and testnet (fake money) are kept as separate series. A
        pre-migration row reads back as ``mode="paper"`` / ``venue=""`` (the
        column defaults).

        Parameters
        ----------
        since_ms : int, optional
            Lower time bound as **milliseconds since the Unix epoch (UTC)**,
            inclusive. ``None`` (default) returns every stored fill.

        Returns
        -------
        list of StoredFill
            The matching fills with their tags, in insertion (execution) order.
            Money exact.

        """
        with self._conn() as conn:
            if since_ms is None:
                rows = conn.execute("SELECT * FROM fills ORDER BY rowid").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fills WHERE ts >= ? ORDER BY rowid",
                    (since_ms,),
                ).fetchall()
        return [_row_to_stored_fill(r) for r in rows]

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
        :class:`~trading_bot.application.events.OrderEvent` to an UPSERT and
        :class:`~trading_bot.application.events.FillEvent` to an append (other
        events are ignored). The store works standalone without a bus; this wires
        the engine's order/fill stream straight into the history.

        **Off the event loop.** :meth:`~trading_bot.application.events.EventBus.
        emit` calls handlers synchronously, on whatever coroutine emitted (the
        broker / router, on the event loop). A synchronous SQLite open → write →
        commit → close inside that handler would block *every* runner on the loop
        on the order/fill hot path. So the handler only **enqueues** a
        :class:`_WriteJob` (a non-blocking, in-memory hand-off) and a single
        dedicated writer thread drains the FIFO queue and does the actual I/O.
        This keeps the loop free while preserving:

        * **ordering** — one FIFO queue, one consumer, so fills/orders are applied
          in arrival order;
        * **append-only / idempotency** — the writer calls the same UPSERT /
          ``INSERT OR IGNORE`` API, so a retried write never duplicates;
        * **no loss / clean drain** — :meth:`close` (and :meth:`flush`) drain the
          queue and join the thread before returning, so a shutdown loses nothing.

        The ``mode`` / ``venue`` tags are captured **at enqueue time** so a later
        :meth:`set_context` never retags a fill already in flight.

        Parameters
        ----------
        event_bus : EventBus
            The bus to subscribe to.

        """
        # Imported lazily so the storage module never hard-depends on the
        # application layer (it works standalone); only ``attach`` needs it.
        from trading_bot.application.events import FillEvent, OrderEvent

        self._ensure_writer()

        def _on_event(event: Event) -> None:
            # Enqueue only — no I/O on the event loop. Tags are snapshotted now so
            # a concurrent set_context cannot retag this in-flight fill.
            if isinstance(event, OrderEvent):
                self._write_queue.put(
                    _WriteJob(
                        order=event.order,
                        fill=None,
                        mode=self._mode,
                        venue=self._venue,
                    )
                )
            elif isinstance(event, FillEvent):
                self._write_queue.put(
                    _WriteJob(
                        order=None,
                        fill=event.fill,
                        mode=self._mode,
                        venue=self._venue,
                    )
                )

        event_bus.subscribe(_on_event)

    # --- off-loop writer --------------------------------------------------- #

    def _ensure_writer(self) -> None:
        """Start the dedicated writer thread once (idempotent).

        Called by :meth:`attach`: the writer only exists to drain bus-driven
        writes, so a store used purely through its synchronous API never spawns a
        thread. Guarded by a lock so a double :meth:`attach` (or attach from two
        threads) starts exactly one writer.
        """
        with self._writer_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(
                target=self._writer_loop,
                name=f"sqlite-store-writer-{id(self):x}",
                daemon=True,
            )
            self._writer.start()

    def _writer_loop(self) -> None:
        """Drain the write queue FIFO, applying each job, until the sentinel.

        The single consumer: it blocks on the queue, applies each
        :class:`_WriteJob` through the synchronous write API (preserving arrival
        order, append-only semantics and idempotency), and marks it done so
        :meth:`flush` / :meth:`close` can join. A ``None`` sentinel stops the loop
        (drain-then-exit — every job enqueued before it is applied first). A write
        that raises is logged and swallowed so one bad row cannot wedge the writer
        and silently stall every later write; the job is still marked done.
        """
        while True:
            job = self._write_queue.get()
            try:
                if job is None:
                    return
                if job.order is not None:
                    self.upsert_order(job.order)
                elif job.fill is not None:
                    self._record_fill_tagged(job.fill, job.mode, job.venue)
            except Exception:
                logger.exception("SqliteStore writer failed to apply a job")
            finally:
                self._write_queue.task_done()

    def flush(self) -> None:
        """Block until every queued write has been applied.

        The barrier the read side / tests use after driving events through an
        attached bus: because writes are applied on a background thread,
        ``emit(...)`` returns before the row lands. :meth:`flush` waits on the
        queue to drain so a subsequent read (or reconciliation) sees every write.
        A no-op when no writer is running (the synchronous API writes inline).
        """
        if self._writer is None:
            return
        self._write_queue.join()

    # --- lifecycle --------------------------------------------------------- #

    def close(self) -> None:
        """Close the store, draining any pending off-loop writes first.

        Drains the writer queue and joins the writer thread so **no queued write
        is lost** on shutdown: every order/fill already enqueued is persisted
        before ``close`` returns. Idempotent — a second call (or a call on a store
        that was never attached, hence has no writer) is a harmless no-op. Each
        connection is per-operation (see :meth:`_conn`), so there is no long-lived
        connection to release beyond the writer.
        """
        with self._writer_lock:
            writer = self._writer
            self._writer = None
        if writer is None:
            return
        # Drain outstanding jobs, then signal the writer to stop and join it.
        self._write_queue.join()
        self._write_queue.put(None)  # sentinel: drain-then-exit
        writer.join()

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
    keys = row.keys()
    # ``fill_tolerance`` is passed at construction (it is a plain field, not a
    # post-init lifecycle field), so read it first and default to the domain
    # default when absent (a pre-migration row has no such column).
    tol_raw = row["fill_tolerance"] if "fill_tolerance" in keys else None
    order = Order(
        client_order_id=str(row["client_order_id"]),
        instrument=_instrument_from_text(str(row["instrument"])),
        side=OrderSide(row["side"]),
        qty=money(str(row["qty"])),
        type=OrderType(row["type"]),
        limit_price=None if limit_raw is None else money(str(limit_raw)),
        stop_price=None if stop_raw is None else money(str(stop_raw)),
        fill_tolerance=(
            DEFAULT_FILL_TOLERANCE if tol_raw is None else money(str(tol_raw))
        ),
    )
    order.filled_qty = money(str(row["filled_qty"]))
    order.avg_fill_price = None if avg_raw is None else money(str(avg_raw))
    order.status = OrderStatus(row["status"])
    venue = row["venue_order_id"]
    order.venue_order_id = None if venue is None else str(venue)
    # Restore the rejection reason so a reloaded REJECTED order keeps its cause
    # (a pre-migration row has no column: it reads back None, the field default).
    reject_raw = row["reject_reason"] if "reject_reason" in keys else None
    order.reject_reason = None if reject_raw is None else str(reject_raw)
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


def _row_to_stored_fill(row: sqlite3.Row) -> StoredFill:
    """Rebuild a :class:`StoredFill` (fill + mode/venue tags) from a stored row.

    A pre-migration row has no ``mode`` / ``venue`` column value; the migration's
    ``ADD COLUMN ... DEFAULT`` backfills every existing row, so the read always
    finds a value (``"paper"`` / ``""`` for the backfilled rows).
    """
    mode = row["mode"]
    venue = row["venue"]
    return StoredFill(
        fill=_row_to_fill(row),
        mode=_DEFAULT_MODE if mode is None else str(mode),
        venue="" if venue is None else str(venue),
    )


def _migrate_fills_tags(conn: sqlite3.Connection) -> None:
    """Add the ``mode`` / ``venue`` columns to a pre-existing ``fills`` table.

    A lightweight, idempotent forward migration: ``CREATE TABLE IF NOT EXISTS``
    (in :data:`_SCHEMA`) already gives a *fresh* database the tagged columns, but
    a database created before this leaf has the old ``fills`` shape. This inspects
    the live columns and ``ALTER TABLE ... ADD COLUMN`` for whichever tag is
    missing — SQLite backfills every existing row with the column ``DEFAULT``
    (``mode="paper"`` / ``venue=""``), so no row is lost or corrupted and the
    money columns are untouched. A no-op once both columns exist (a fresh DB, or a
    second open of a migrated one).
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(fills)")}
    if "mode" not in columns:
        conn.execute(
            f"ALTER TABLE fills ADD COLUMN mode TEXT NOT NULL DEFAULT '{_DEFAULT_MODE}'"
        )
    if "venue" not in columns:
        conn.execute("ALTER TABLE fills ADD COLUMN venue TEXT NOT NULL DEFAULT ''")


#: The composite primary key the current ``fills`` table carries — a fill's
#: storage identity is its venue id *plus* the deployment tags, so a paper fill
#: and a live fill sharing a ``fill_id`` never collide (see :func:`_migrate_fills_pk`).
_FILLS_PK: frozenset[str] = frozenset({"fill_id", "venue", "mode"})


def _migrate_fills_pk(conn: sqlite3.Connection) -> None:
    """Widen the ``fills`` primary key to composite ``(fill_id, venue, mode)``.

    A pre-existing ``fills`` table keys on ``fill_id`` alone, so a paper fill and a
    live fill that share a venue ``fill_id`` **collide**: ``INSERT OR IGNORE``
    keeps the first and silently drops the second — losing a fill (the PnL source
    of truth) and breaking the separate-series / no-double-count guarantee. This
    rebuilds the table under the composite key so the two coexist, mode-partitioned.

    SQLite cannot alter a primary key in place, so the migration follows the
    supported table-rebuild recipe **inside the caller's transaction**: create a
    new table with the composite key, copy every row across (``INSERT OR IGNORE``
    so any pre-existing exact-duplicate id — only possible under the old single-id
    key, hence already unique — copies cleanly), drop the old table, rename the new
    one, and rebuild the indexes. No money column is touched and no row is lost;
    the tag columns are assumed present (:func:`_migrate_fills_tags` runs first).

    Idempotent and safe on fresh / partial / old DBs: it inspects the live PK via
    ``PRAGMA table_info`` and returns immediately when the composite key is already
    in place (a fresh DB — :data:`_SCHEMA` builds it composite — or a second open
    of a migrated one). Must run *before* any composite-key write.
    """
    pk_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(fills)") if row["pk"]
    }
    if pk_columns == set(_FILLS_PK):
        return  # already composite — fresh DB or already migrated
    # Rebuild under the composite key. ``synchronous`` / ``journal_mode`` are
    # connection-level and untouched; this is pure DDL + a copy.
    conn.executescript(
        """
        CREATE TABLE fills_new (
            fill_id         TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            instrument      TEXT NOT NULL,
            side            TEXT NOT NULL,
            qty             TEXT NOT NULL,
            price           TEXT NOT NULL,
            fee             TEXT NOT NULL,
            ts              INTEGER NOT NULL,
            mode            TEXT NOT NULL DEFAULT 'paper',
            venue           TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (fill_id, venue, mode)
        );
        INSERT OR IGNORE INTO fills_new (
            fill_id, client_order_id, instrument, side, qty, price,
            fee, ts, mode, venue
        )
        SELECT fill_id, client_order_id, instrument, side, qty, price,
               fee, ts, mode, venue
        FROM fills
        ORDER BY rowid;
        DROP TABLE fills;
        ALTER TABLE fills_new RENAME TO fills;
        CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
        CREATE INDEX IF NOT EXISTS idx_fills_cid ON fills(client_order_id);
        """
    )


#: Columns the current ``orders`` schema carries that a pre-migration table may
#: lack, each with the ``ALTER TABLE ... ADD COLUMN`` type used to backfill it.
#: All are nullable (no ``NOT NULL``), so existing rows backfill to ``NULL`` — a
#: valid "unknown, pre-migration" value the read side already tolerates.
_ORDERS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ts", "INTEGER"),
    ("reject_reason", "TEXT"),
    ("fill_tolerance", "TEXT"),
)


def _migrate_orders_columns(conn: sqlite3.Connection) -> None:
    """Add any missing later-added columns to a pre-existing ``orders`` table.

    The ``orders`` counterpart of :func:`_migrate_fills_tags`. ``CREATE TABLE IF
    NOT EXISTS`` (in :data:`_SCHEMA`) already gives a *fresh* database the full
    column set, but a database created before a column was added has the old
    ``orders`` shape — and :meth:`SqliteStore.upsert_order` writes every current
    column, so without this an ``upsert_order`` on an old DB would hard-fail with
    an ``OperationalError`` (unknown column). This inspects the live columns and
    ``ALTER TABLE ... ADD COLUMN`` for each of :data:`_ORDERS_ADDED_COLUMNS` that
    is missing. Every added column is nullable, so SQLite backfills existing rows
    with ``NULL`` — no row is lost or corrupted and the money columns are
    untouched. Idempotent and safe on a partially-migrated table: a no-op once
    all columns exist (a fresh DB, a second open, or a DB missing only some).
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    for name, coltype in _ORDERS_ADDED_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {coltype}")
