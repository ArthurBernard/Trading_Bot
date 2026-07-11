"""Tests for :func:`~trading_bot.application.accounting.check_book`.

Pure-domain fixtures only — no store, no broker, no event bus — proving the
checker's contract in isolation: a self-consistent book reports nothing, and
each of the four pinned violation kinds (see
``doc/dev/plans/accounting-guardrail/00-plan.md``) fires exactly when its rule
is broken, with every reported figure an exact ``str(Decimal)``.
"""

from __future__ import annotations

from decimal import Decimal

from trading_bot.application.accounting import Violation, check_book
from trading_bot.domain import (
    DEFAULT_FILL_TOLERANCE,
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _fill(
    *,
    fill_id: str,
    side: OrderSide,
    qty: str,
    price: str = "30000",
    fee: str = "0",
    ts: int = 1,
    instrument: Instrument = BTC_USD,
    cid: str = "cid-1",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        client_order_id=cid,
        instrument=instrument,
        side=side,
        qty=money(qty),
        price=money(price),
        fee=money(fee),
        ts=ts,
    )


def _order(
    *,
    cid: str = "cid-1",
    side: OrderSide = OrderSide.BUY,
    qty: str = "1",
    status: OrderStatus = OrderStatus.FILLED,
    filled_qty: str | None = None,
    venue_order_id: str | None = "VID-1",
    instrument: Instrument = BTC_USD,
    fill_tolerance: Decimal = DEFAULT_FILL_TOLERANCE,
) -> Order:
    """A stored order row, its lifecycle fields set directly (mirrors how the
    store rebuilds a row — the state machine is not replayed, see
    ``SqliteStore._row_to_order``).
    """
    order = Order(
        client_order_id=cid,
        instrument=instrument,
        side=side,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
        fill_tolerance=fill_tolerance,
    )
    order.status = status
    order.filled_qty = money(qty) if filled_qty is None else money(filled_qty)
    order.venue_order_id = venue_order_id
    return order


def _positions(*fills: Fill) -> dict[Instrument, Position]:
    """Fold ``fills`` into a per-instrument :class:`Position` mapping.

    Groups by instrument first since :meth:`Position.from_fills` rejects a
    mixed-instrument sequence.
    """
    by_instrument: dict[Instrument, list[Fill]] = {}
    for fill in fills:
        by_instrument.setdefault(fill.instrument, []).append(fill)
    return {
        instrument: Position.from_fills(fills_for)
        for instrument, fills_for in by_instrument.items()
    }


# --- clean book --------------------------------------------------------------- #


def test_clean_book_no_violations() -> None:
    """A fully self-consistent book (positions, fills, orders agree) reports []."""
    fill = _fill(fill_id="T1", side=OrderSide.BUY, qty="1")
    order = _order(cid="cid-1", qty="1", status=OrderStatus.FILLED, filled_qty="1")

    violations = check_book(_positions(fill), [fill], [order])

    assert violations == []


# --- 1. position_drift ---------------------------------------------------------- #


def test_position_drift_detected() -> None:
    """Dropping one fill from the fold desyncs the tracked position by exactly it."""
    fill1 = _fill(fill_id="T1", side=OrderSide.BUY, qty="2")
    fill2 = _fill(fill_id="T2", side=OrderSide.BUY, qty="1", ts=2)
    # The tracked position saw both fills; the checker is handed only the first.
    tracked_position = Position.from_fills([fill1, fill2])
    positions = {BTC_USD: tracked_position}

    violations = check_book(positions, [fill1], [])

    drift = [v for v in violations if v.kind == "position_drift"]
    assert len(drift) == 1
    v = drift[0]
    assert v.severity == "error"
    assert v.subject == str(BTC_USD)
    assert v.measured == "3"  # the tracked position's net_qty (both fills)
    assert v.expected == "2"  # the fold the checker was handed (fill1 only)


def test_drift_from_zero_and_orphan_position() -> None:
    """Fills with no tracked position, and a non-flat position with no fills."""
    orphan_fill = _fill(
        fill_id="T1", side=OrderSide.BUY, qty="1", instrument=ETH_USD, cid="cid-eth"
    )
    untracked_position = Position.from_fills(
        [_fill(fill_id="T2", side=OrderSide.BUY, qty="5", instrument=BTC_USD)]
    )
    # The checker sees a BTC position with no matching fills, and an ETH fill
    # with no matching tracked position.
    positions = {BTC_USD: untracked_position}

    violations = check_book(positions, [orphan_fill], [])

    drift_by_subject = {v.subject: v for v in violations if v.kind == "position_drift"}
    assert set(drift_by_subject) == {str(BTC_USD), str(ETH_USD)}

    btc = drift_by_subject[str(BTC_USD)]
    assert btc.severity == "error"
    assert btc.measured == "5"
    assert btc.expected == "0"

    eth = drift_by_subject[str(ETH_USD)]
    assert eth.severity == "error"
    assert eth.measured == "0"
    assert eth.expected == "1"


# --- 2. order_fill_mismatch ------------------------------------------------------ #


def test_order_fill_mismatch() -> None:
    """A frozen ``filled_qty=0`` row with a full fill warns; rounding dust does not."""
    fill = _fill(fill_id="T1", side=OrderSide.BUY, qty="1", cid="cid-frozen")
    frozen_order = _order(cid="cid-frozen", qty="1", filled_qty="0")

    positions = {BTC_USD: Position.from_fills([fill])}
    violations = check_book(positions, [fill], [frozen_order])

    mismatches = [v for v in violations if v.kind == "order_fill_mismatch"]
    assert len(mismatches) == 1
    v = mismatches[0]
    assert v.severity == "warn"
    assert v.subject == "cid-frozen"
    assert v.measured == "1"
    assert v.expected == "0"

    # Within-tolerance rounding dust: fills sum to 1, filled_qty is 0.9995 —
    # the 0.05% unfilled fraction is strictly below the 0.1% default tolerance,
    # so this is NOT a violation (mirrors Order.apply_fill's dust handling).
    dust_order = _order(cid="cid-dust", qty="1", filled_qty="0.9995")
    dust_fill = _fill(fill_id="T2", side=OrderSide.BUY, qty="1", cid="cid-dust")
    positions_dust = {BTC_USD: Position.from_fills([dust_fill])}
    violations_dust = check_book(positions_dust, [dust_fill], [dust_order])
    assert [v for v in violations_dust if v.kind == "order_fill_mismatch"] == []


def test_order_fill_mismatch_fills_without_order_row() -> None:
    """A fill whose client_order_id has no order row is also a mismatch."""
    fill = _fill(fill_id="T1", side=OrderSide.BUY, qty="1", cid="cid-orphan")
    positions = {BTC_USD: Position.from_fills([fill])}

    violations = check_book(positions, [fill], [])

    mismatches = [v for v in violations if v.kind == "order_fill_mismatch"]
    assert len(mismatches) == 1
    assert mismatches[0].subject == "cid-orphan"
    assert mismatches[0].severity == "warn"
    assert mismatches[0].measured == "1"


# --- 3. status_incoherent --------------------------------------------------------- #


def test_status_incoherent_both_directions() -> None:
    """Covered-but-not-FILLED, and FILLED-but-short, both warn."""
    # Covered but open: the fills cover the order's qty within tolerance, but
    # the stored row is still OPEN (a crash-lagged status update).
    covered_fill = _fill(fill_id="T1", side=OrderSide.BUY, qty="1", cid="cid-open")
    open_order = _order(
        cid="cid-open",
        qty="1",
        status=OrderStatus.OPEN,
        filled_qty="0",
        venue_order_id="VID-OPEN",
    )

    # FILLED but short: the row claims FILLED, but filled_qty is materially
    # below qty — independent of what fills say (this order has none at all).
    short_order = _order(
        cid="cid-short",
        qty="1",
        status=OrderStatus.FILLED,
        filled_qty="0.5",
        venue_order_id="VID-SHORT",
    )

    positions = {
        BTC_USD: Position.from_fills([covered_fill]),
    }
    violations = check_book(positions, [covered_fill], [open_order, short_order])

    incoherent = {v.subject: v for v in violations if v.kind == "status_incoherent"}
    assert set(incoherent) == {"cid-open", "cid-short"}

    covered_but_open = incoherent["cid-open"]
    assert covered_but_open.severity == "warn"
    assert covered_but_open.measured == "1"  # fills sum, covering qty
    assert covered_but_open.expected == "1"  # the order's qty
    assert "open" in covered_but_open.detail
    assert "filled" in covered_but_open.detail

    filled_but_short = incoherent["cid-short"]
    assert filled_but_short.severity == "warn"
    assert filled_but_short.measured == "0.5"
    assert filled_but_short.expected == "1"


def test_status_incoherent_absent_for_consistent_order() -> None:
    """A FILLED order whose filled_qty matches qty within tolerance is silent."""
    fill = _fill(fill_id="T1", side=OrderSide.BUY, qty="1", cid="cid-1")
    order = _order(cid="cid-1", qty="1", status=OrderStatus.FILLED, filled_qty="1")
    positions = {BTC_USD: Position.from_fills([fill])}

    violations = check_book(positions, [fill], [order])

    assert [v for v in violations if v.kind == "status_incoherent"] == []


# --- 4. duplicate_venue_ids -------------------------------------------------------- #


def test_duplicate_venue_ids() -> None:
    """Two orders sharing a venue id warn once, naming both; unique ids are silent."""
    shared_a = _order(cid="cid-a", venue_order_id="PAPER-1", qty="1")
    shared_b = _order(cid="cid-b", venue_order_id="PAPER-1", qty="1")
    unique = _order(cid="cid-c", venue_order_id="PAPER-2", qty="1")
    no_venue_id = _order(cid="cid-d", venue_order_id=None, qty="1")
    empty_venue_id = _order(cid="cid-e", venue_order_id="", qty="1")

    violations = check_book(
        {}, [], [shared_a, shared_b, unique, no_venue_id, empty_venue_id]
    )

    duplicates = [v for v in violations if v.kind == "duplicate_venue_ids"]
    assert len(duplicates) == 1
    v = duplicates[0]
    assert v.severity == "warn"
    assert v.subject == "PAPER-1"
    assert "cid-a" in v.detail
    assert "cid-b" in v.detail
    assert v.measured == "2"
    assert v.expected == "1"


def test_duplicate_venue_ids_none_and_unique_produce_no_violation() -> None:
    """No shared venue ids, and orders with no venue id at all, are both silent."""
    unique_a = _order(cid="cid-a", venue_order_id="PAPER-1", qty="1")
    unique_b = _order(cid="cid-b", venue_order_id="PAPER-2", qty="1")
    unsubmitted = _order(cid="cid-c", venue_order_id=None, qty="1")

    violations = check_book({}, [], [unique_a, unique_b, unsubmitted])

    assert [v for v in violations if v.kind == "duplicate_venue_ids"] == []


# --- exact figures ------------------------------------------------------------------ #


def test_all_figures_are_exact_strings() -> None:
    """Every violation's measured/expected parses as an exact Decimal, no float noise."""
    fill1 = _fill(fill_id="T1", side=OrderSide.BUY, qty="0.1", cid="cid-drift")
    fill2 = _fill(fill_id="T2", side=OrderSide.BUY, qty="0.2", ts=2, cid="cid-drift")
    tracked_position = Position.from_fills([fill1, fill2])
    positions = {BTC_USD: tracked_position}

    # Handing the checker only fill1 forces a position_drift (0.3 vs 0.1).
    frozen_order = _order(cid="cid-drift", qty="0.3", filled_qty="0")
    shared_a = _order(
        cid="dup-a", venue_order_id="PAPER-9", qty="1", instrument=ETH_USD
    )
    shared_b = _order(
        cid="dup-b", venue_order_id="PAPER-9", qty="1", instrument=ETH_USD
    )

    violations = check_book(positions, [fill1], [frozen_order, shared_a, shared_b])

    assert violations  # sanity: this fixture does produce violations
    for v in violations:
        assert isinstance(v, Violation)
        # Decimal(str) round-trips exactly with no float artifacts (e.g. no
        # "0.30000000000000004"-style tails); a bare int count (duplicate ids)
        # parses just as cleanly.
        assert str(Decimal(v.measured)) == v.measured
        assert str(Decimal(v.expected)) == v.expected
        # No violation ever carries a float repr artifact (scientific notation
        # or a long near-epsilon tail).
        assert "e" not in v.measured.lower()
        assert "e" not in v.expected.lower()
        assert "0000000" not in v.measured
        assert "0000000" not in v.expected
