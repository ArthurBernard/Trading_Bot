"""The pure accounting invariant checker — the book proves itself.

The ``paper-integrity`` epic (PRs #190-#194) fixed three silent accounting bugs
that only a manual audit caught: a swallowed fill, a frozen order row left
behind by a crash, and duplicate venue ids from a pre-#190 bug. Nothing in the
engine re-checks those invariants on its own — this module is that permanent,
callable guardrail: given a snapshot of the book (tracked positions, recorded
fills, recorded orders), :func:`check_book` recomputes each invariant from
scratch and reports every disagreement as a typed :class:`Violation`.

Why pure
--------
:func:`check_book` takes plain domain data in (a positions mapping, an iterable
of fills, an iterable of orders) and returns a plain list out. No
:class:`~trading_bot.application.events.EventBus`, no
:class:`~trading_bot.storage.sqlite_store.SqliteStore` handle, no broker — the
caller assembles the snapshot (from a live engine, a restored store, a test
fixture, a one-off audit script) and hands it in. This keeps the checker:

* **testable** without a database, a broker, or an event loop — the tests in
  ``test_accounting.py`` build fixtures by hand;
* **callable from anywhere** — the engine lifecycle (leaf 02), a health
  endpoint (leaf 03), a REPL, a cron job — with no shared mutable state between
  calls;
* **honest** — it only *reports*, it never mutates a position, a fill or an
  order. Healing (replay, reconciliation) is a separate, already-existing seam;
  this module never touches it.

The violation taxonomy, the exact rules and their severities are pinned in
``doc/dev/plans/accounting-guardrail/00-plan.md`` — this module implements
exactly the four checker kinds listed there (``position_drift``,
``order_fill_mismatch``, ``status_incoherent``, ``duplicate_venue_ids``).
``kill_switch_tripped`` is deliberately **not** here: it folds the risk
manager's live ``tripped`` flag, which is a runtime concern the health layer
(leaf 03) owns, not a recomputation this pure module can do from a static
snapshot.

All comparisons are exact :class:`~decimal.Decimal` — never ``float``.
"""

from __future__ import annotations

# Built-in
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

# Local
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import Order, OrderStatus
from trading_bot.domain.position import Position

__all__ = ["Severity", "Violation", "check_book"]

#: A violation's severity: ``"error"`` for a genuine, actionable drift
#: (``position_drift`` — the tracked book disagrees with its own fill history);
#: ``"warn"`` for everything else (transient crash-lag rows that self-heal at
#: the next restart/reconciliation, or stable legacy noise). See
#: ``00-plan.md``'s taxonomy table for the per-kind mapping.
Severity = Literal["warn", "error"]

_ZERO: Money = money("0")


@dataclass(frozen=True, slots=True)
class Violation:
    """One detected disagreement between the book's parts.

    Immutable and JSON-safe: every numeric figure is carried as the *exact*
    ``str(Decimal)`` form, never a ``float``, so a violation can be logged,
    stored or serialised over the API with no precision loss.

    Parameters
    ----------
    kind : str
        The checker kind that raised this violation — one of
        ``"position_drift"``, ``"order_fill_mismatch"``, ``"status_incoherent"``,
        ``"duplicate_venue_ids"`` (see ``00-plan.md``'s taxonomy table).
    severity : Severity
        ``"error"`` or ``"warn"`` — see :data:`Severity`.
    subject : str
        What the violation is about: an instrument symbol (``position_drift``)
        or a ``client_order_id`` (``order_fill_mismatch``,
        ``status_incoherent``) or a venue order id (``duplicate_venue_ids``).
    detail : str
        One human-readable sentence describing the disagreement, carrying the
        exact ``Decimal`` figures involved.
    measured : str
        The book's actual value, as an exact ``str(Decimal)`` (or a plain
        integer count for ``duplicate_venue_ids``) — never a float repr.
    expected : str
        The value the invariant requires, in the same exact-string form.

    """

    kind: str
    severity: Severity
    subject: str
    detail: str
    measured: str
    expected: str


def check_book(
    positions: Mapping[Instrument, Position],
    fills: Iterable[Fill],
    orders: Iterable[Order],
) -> list[Violation]:
    """Recompute the book's self-consistency and report every violation.

    Runs the four pinned checkers, in order, and concatenates their results:

    1. ``position_drift`` (error) — per instrument, the signed fold of
       ``fills`` must equal the tracked position's ``net_qty`` exactly.
    2. ``order_fill_mismatch`` (warn) — per ``client_order_id`` with fills, the
       summed fill quantity must match the order's ``filled_qty`` within its
       ``fill_tolerance``.
    3. ``status_incoherent`` (warn) — an order's ``status`` must agree with
       what its fills (and its own ``filled_qty``) say about how filled it is.
    4. ``duplicate_venue_ids`` (warn) — no two order rows may share a
       ``venue_order_id``.

    Parameters
    ----------
    positions : Mapping[Instrument, Position]
        The tracker's live positions (e.g.
        :meth:`~trading_bot.application.position_tracker.PositionTracker.
        all_positions`), one entry per instrument with any exposure ever seen.
    fills : Iterable[Fill]
        The store's recorded fills for this book, **already mode-filtered** by
        the caller (a paper book must only see its own paper fills — see
        :meth:`~trading_bot.application.supervisor.StrategySupervisor.
        _replay_paper_book`). Order within an instrument does not matter here
        (the fold is commutative — pure addition of signed quantities).
    orders : Iterable[Order]
        The store's recorded orders for this book (e.g.
        :meth:`~trading_bot.storage.sqlite_store.SqliteStore.orders`).

    Returns
    -------
    list of Violation
        Every detected disagreement, in the checker order above. Empty when
        the book is fully self-consistent.

    """
    ordered_fills = list(fills)
    ordered_orders = list(orders)

    violations: list[Violation] = []
    violations.extend(_check_position_drift(positions, ordered_fills))
    violations.extend(_check_order_fill_mismatch(ordered_fills, ordered_orders))
    violations.extend(_check_status_incoherent(ordered_fills, ordered_orders))
    violations.extend(_check_duplicate_venue_ids(ordered_orders))
    return violations


# --- shared folds ----------------------------------------------------------- #


def _group_fills_by_cid(fills: Iterable[Fill]) -> dict[str, list[Fill]]:
    """Bucket ``fills`` by their ``client_order_id``, preserving arrival order."""
    groups: dict[str, list[Fill]] = {}
    for fill in fills:
        groups.setdefault(fill.client_order_id, []).append(fill)
    return groups


def _folded_net_qty(fills: Iterable[Fill]) -> dict[Instrument, Money]:
    """Fold ``fills`` into a per-instrument signed net quantity total.

    Mirrors :meth:`~trading_bot.domain.position.Position.with_fill`'s
    ``net_qty`` update exactly: whichever branch that method takes (opening,
    same-direction increase, partial close, exact close, or flip), the
    resulting ``net_qty`` is always the *running sum* of
    :attr:`~trading_bot.domain.fill.Fill.signed_qty` (``+qty`` for BUY,
    ``-qty`` for SELL) — so this direct sum over the whole fill history equals
    ``Position.from_fills(fills).net_qty`` without needing to fold through
    :class:`~trading_bot.domain.position.Position` itself.
    """
    totals: dict[Instrument, Money] = {}
    for fill in fills:
        totals[fill.instrument] = totals.get(fill.instrument, _ZERO) + fill.signed_qty
    return totals


def _within_fill_tolerance(filled: Money, qty: Money, tolerance: Money) -> bool:
    """Whether ``filled`` covers ``qty`` within ``tolerance`` (unfilled side).

    Mirrors :meth:`~trading_bot.domain.order.Order._is_filled_within_tolerance`:
    an exact or over- fill always counts; a residual unfilled fraction
    ``(qty - filled) / qty`` strictly below ``tolerance`` is treated as a full
    fill (the venue left sub-tick dust outstanding).
    """
    if filled >= qty:
        return True
    if qty <= 0:
        return False
    unfilled_fraction = (qty - filled) / qty
    return unfilled_fraction < tolerance


def _exceeds_fill_tolerance(measured: Money, expected: Money, tolerance: Money) -> bool:
    """Whether ``measured`` differs from ``expected`` beyond a ``tolerance`` fraction.

    The symmetric, either-direction counterpart of :func:`_within_fill_tolerance`:
    ``|measured - expected| / expected`` at or beyond ``tolerance`` is a
    *material* mismatch, mirroring the same fractional-of-``qty`` tolerance
    semantics :meth:`~trading_bot.domain.order.Order.apply_fill` applies on both
    the under- and over-fill sides.
    """
    diff = abs(measured - expected)
    if expected <= 0:
        return diff > 0
    fraction = diff / expected
    return fraction >= tolerance


# --- 1. position_drift -------------------------------------------------------- #


def _check_position_drift(
    positions: Mapping[Instrument, Position], fills: list[Fill]
) -> list[Violation]:
    """Compare each instrument's tracked ``net_qty`` to its signed fill fold.

    Flags three shapes of drift, all under the same ``position_drift`` kind:
    a tracked position whose ``net_qty`` disagrees with the fold of its fills;
    fills recorded for an instrument with no tracked position at all (drift
    from zero); and a non-flat tracked position with no fills recorded for it.
    """
    folded = _folded_net_qty(fills)
    instruments = sorted(set(positions) | set(folded), key=str)

    violations: list[Violation] = []
    for instrument in instruments:
        expected_qty = folded.get(instrument, _ZERO)
        position = positions.get(instrument)
        measured_qty = position.net_qty if position is not None else _ZERO
        if measured_qty == expected_qty:
            continue

        if position is None:
            detail = (
                f"{instrument}: fills sum to a net quantity of {expected_qty} but "
                "no position is tracked for this instrument (drift from zero)."
            )
        elif instrument not in folded:
            detail = (
                f"{instrument}: the tracked position holds a non-flat net "
                f"quantity of {measured_qty} but no fills are recorded for it."
            )
        else:
            detail = (
                f"{instrument}: the tracked position's net quantity "
                f"{measured_qty} does not match the signed fold of its fills, "
                f"{expected_qty}."
            )
        violations.append(
            Violation(
                kind="position_drift",
                severity="error",
                subject=str(instrument),
                detail=detail,
                measured=str(measured_qty),
                expected=str(expected_qty),
            )
        )
    return violations


# --- 2. order_fill_mismatch --------------------------------------------------- #


def _check_order_fill_mismatch(
    fills: list[Fill], orders: list[Order]
) -> list[Violation]:
    """Compare, per ``client_order_id`` with fills, the fill sum vs ``filled_qty``.

    A ``client_order_id`` present in ``fills`` but absent from ``orders`` is
    also a mismatch (fills without an order row). Tolerance mirrors
    :meth:`~trading_bot.domain.order.Order.apply_fill`'s fractional-of-``qty``
    semantics, since a within-tolerance over-fill legitimately clamps
    ``filled_qty`` below the raw fill sum.
    """
    orders_by_cid = {order.client_order_id: order for order in orders}
    fills_by_cid = _group_fills_by_cid(fills)

    violations: list[Violation] = []
    for cid in sorted(fills_by_cid):
        cid_fills = fills_by_cid[cid]
        total_fill_qty = sum((fill.qty for fill in cid_fills), _ZERO)
        order = orders_by_cid.get(cid)

        if order is None:
            violations.append(
                Violation(
                    kind="order_fill_mismatch",
                    severity="warn",
                    subject=cid,
                    detail=(
                        f"{cid}: {len(cid_fills)} fill(s) totalling "
                        f"{total_fill_qty} are recorded, but no order row "
                        "exists for this client_order_id (fills without an "
                        "order row)."
                    ),
                    measured=str(total_fill_qty),
                    expected="0",
                )
            )
            continue

        if _exceeds_fill_tolerance(
            total_fill_qty, order.filled_qty, order.fill_tolerance
        ):
            violations.append(
                Violation(
                    kind="order_fill_mismatch",
                    severity="warn",
                    subject=cid,
                    detail=(
                        f"{cid}: fills sum to {total_fill_qty} but the order "
                        f"row records filled_qty {order.filled_qty} (order qty "
                        f"{order.qty}, tolerance {order.fill_tolerance})."
                    ),
                    measured=str(total_fill_qty),
                    expected=str(order.filled_qty),
                )
            )
    return violations


# --- 3. status_incoherent ----------------------------------------------------- #


def _check_status_incoherent(fills: list[Fill], orders: list[Order]) -> list[Violation]:
    """Compare each order's ``status`` to what its fills / ``filled_qty`` imply.

    Two independent conditions, both ``status_incoherent``: (a) the raw fill
    sum for the order's ``client_order_id`` covers ``qty`` within tolerance but
    ``status`` is not ``FILLED``; (b) ``status`` is ``FILLED`` but the stored
    ``filled_qty`` does not match ``qty`` within tolerance. (b) is a pure
    order-row check — it fires even with no fills at all. ``measured``/
    ``expected`` are always the relevant Decimal quantities (never the status
    label itself), per :class:`Violation`'s exact-figures contract; the status
    disagreement is spelled out in ``detail``.
    """
    fills_by_cid = _group_fills_by_cid(fills)

    violations: list[Violation] = []
    for order in orders:
        cid = order.client_order_id
        cid_fills = fills_by_cid.get(cid, [])
        total_fill_qty = sum((fill.qty for fill in cid_fills), _ZERO)

        if (
            _within_fill_tolerance(total_fill_qty, order.qty, order.fill_tolerance)
            and order.status is not OrderStatus.FILLED
        ):
            violations.append(
                Violation(
                    kind="status_incoherent",
                    severity="warn",
                    subject=cid,
                    detail=(
                        f"{cid}: fills sum to {total_fill_qty}, covering the "
                        f"order qty {order.qty} within tolerance "
                        f"{order.fill_tolerance}, but status is "
                        f"{order.status.value!r}, not 'filled'."
                    ),
                    measured=str(total_fill_qty),
                    expected=str(order.qty),
                )
            )

        if order.status is OrderStatus.FILLED and _exceeds_fill_tolerance(
            order.filled_qty, order.qty, order.fill_tolerance
        ):
            violations.append(
                Violation(
                    kind="status_incoherent",
                    severity="warn",
                    subject=cid,
                    detail=(
                        f"{cid}: status is 'filled' but filled_qty "
                        f"{order.filled_qty} does not match the order qty "
                        f"{order.qty} within tolerance {order.fill_tolerance}."
                    ),
                    measured=str(order.filled_qty),
                    expected=str(order.qty),
                )
            )
    return violations


# --- 4. duplicate_venue_ids --------------------------------------------------- #


def _check_duplicate_venue_ids(orders: list[Order]) -> list[Violation]:
    """Flag any ``venue_order_id`` shared by more than one order row.

    ``None`` and empty-string venue ids are skipped (an order that never
    reached the venue, or a pre-migration row, carries no venue id to collide
    on).
    """
    client_ids_by_venue_id: dict[str, list[str]] = {}
    for order in orders:
        venue_id = order.venue_order_id
        if not venue_id:
            continue
        client_ids_by_venue_id.setdefault(venue_id, []).append(order.client_order_id)

    violations: list[Violation] = []
    for venue_id in sorted(client_ids_by_venue_id):
        client_ids = client_ids_by_venue_id[venue_id]
        if len(client_ids) <= 1:
            continue
        violations.append(
            Violation(
                kind="duplicate_venue_ids",
                severity="warn",
                subject=venue_id,
                detail=(
                    f"venue_order_id {venue_id!r} is shared by "
                    f"{len(client_ids)} order rows: {', '.join(sorted(client_ids))}."
                ),
                measured=str(len(client_ids)),
                expected="1",
            )
        )
    return violations
