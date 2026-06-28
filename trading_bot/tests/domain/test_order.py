"""Tests for the Order aggregate and its lifecycle state machine."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.domain.errors import OrderError, OrderStatusError
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import (
    DEFAULT_FILL_TOLERANCE,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

INSTRUMENT = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)


def make_order(
    *,
    qty: str = "2",
    otype: OrderType = OrderType.LIMIT,
    limit_price: str | None = "30000",
    stop_price: str | None = None,
    fill_tolerance: str | None = None,
) -> Order:
    """Build a test order with sensible defaults."""
    kwargs: dict[str, object] = {
        "client_order_id": "cid-1",
        "instrument": INSTRUMENT,
        "side": OrderSide.BUY,
        "qty": money(qty),
        "type": otype,
        "limit_price": money(limit_price) if limit_price is not None else None,
        "stop_price": money(stop_price) if stop_price is not None else None,
    }
    if fill_tolerance is not None:
        kwargs["fill_tolerance"] = money(fill_tolerance)
    return Order(**kwargs)  # type: ignore[arg-type]


class TestConstructionValidation:
    def test_market_forbids_prices(self) -> None:
        # No prices: fine.
        Order(
            client_order_id="cid",
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            qty=money("1"),
            type=OrderType.MARKET,
        )
        with pytest.raises(OrderError, match="MARKET"):
            make_order(otype=OrderType.MARKET, limit_price="30000")
        with pytest.raises(OrderError, match="MARKET"):
            make_order(
                otype=OrderType.MARKET, limit_price=None, stop_price="29000"
            )

    def test_limit_requires_limit_price(self) -> None:
        make_order(otype=OrderType.LIMIT, limit_price="30000")
        with pytest.raises(OrderError, match="LIMIT order requires limit_price"):
            make_order(otype=OrderType.LIMIT, limit_price=None)

    def test_limit_forbids_stop_price(self) -> None:
        with pytest.raises(OrderError, match="LIMIT order forbids stop_price"):
            make_order(
                otype=OrderType.LIMIT, limit_price="30000", stop_price="29000"
            )

    def test_stop_loss_requires_stop_price(self) -> None:
        make_order(otype=OrderType.STOP_LOSS, limit_price=None, stop_price="29000")
        with pytest.raises(OrderError, match="STOP_LOSS order requires stop_price"):
            make_order(otype=OrderType.STOP_LOSS, limit_price=None, stop_price=None)

    def test_stop_loss_forbids_limit_price(self) -> None:
        with pytest.raises(OrderError, match="STOP_LOSS order forbids limit_price"):
            make_order(
                otype=OrderType.STOP_LOSS,
                limit_price="30000",
                stop_price="29000",
            )

    def test_best_limit_price_optional(self) -> None:
        # BEST_LIMIT discovers its price at runtime: no limit_price is allowed.
        make_order(otype=OrderType.BEST_LIMIT, limit_price=None)
        # But it may carry an initial limit price.
        make_order(otype=OrderType.BEST_LIMIT, limit_price="30000")

    def test_best_limit_forbids_stop_price(self) -> None:
        with pytest.raises(OrderError, match="BEST_LIMIT order forbids stop_price"):
            make_order(
                otype=OrderType.BEST_LIMIT,
                limit_price=None,
                stop_price="29000",
            )

    def test_qty_must_be_positive(self) -> None:
        with pytest.raises(OrderError, match="qty must be positive"):
            make_order(qty="0")
        with pytest.raises(OrderError, match="qty must be positive"):
            make_order(qty="-1")

    def test_fill_tolerance_must_be_non_negative(self) -> None:
        with pytest.raises(OrderError, match="fill_tolerance must be non-negative"):
            make_order(fill_tolerance="-0.001")

    def test_client_order_id_mandatory(self) -> None:
        with pytest.raises(OrderError, match="client_order_id is mandatory"):
            Order(
                client_order_id="",
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                qty=money("1"),
                type=OrderType.MARKET,
            )

    def test_initial_state(self) -> None:
        o = make_order()
        assert o.status is OrderStatus.NEW
        assert o.filled_qty == money("0")
        assert o.avg_fill_price is None
        assert o.venue_order_id is None
        assert o.reject_reason is None
        assert o.fill_tolerance == DEFAULT_FILL_TOLERANCE


class TestLegalLifecycle:
    def test_full_legal_path(self) -> None:
        # NEW -> SUBMITTED -> OPEN -> PARTIALLY_FILLED -> FILLED
        o = make_order(qty="2")
        assert o.status is OrderStatus.NEW

        o.submit()
        assert o.status is OrderStatus.SUBMITTED

        o.open("VID-42")
        assert o.status is OrderStatus.OPEN
        assert o.venue_order_id == "VID-42"

        o.apply_fill(money("1"), money("30000"))
        assert o.status is OrderStatus.PARTIALLY_FILLED
        assert o.filled_qty == money("1")
        assert o.remaining_qty == money("1")

        o.apply_fill(money("1"), money("30100"))
        assert o.status is OrderStatus.FILLED
        assert o.filled_qty == money("2")
        assert o.remaining_qty == money("0")
        assert o.is_terminal

    def test_cancel_from_open(self) -> None:
        o = make_order()
        o.submit()
        o.open("VID-1")
        o.cancel()
        assert o.status is OrderStatus.CANCELLED
        assert o.is_terminal

    def test_cancel_from_submitted(self) -> None:
        o = make_order()
        o.submit()
        o.cancel()
        assert o.status is OrderStatus.CANCELLED

    def test_cancel_from_partially_filled(self) -> None:
        o = make_order(qty="2")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1"), money("30000"))
        o.cancel()
        assert o.status is OrderStatus.CANCELLED
        # Partial fill is preserved on cancellation.
        assert o.filled_qty == money("1")

    def test_reject_from_submitted(self) -> None:
        o = make_order()
        o.submit()
        o.reject("insufficient funds")
        assert o.status is OrderStatus.REJECTED
        assert o.reject_reason == "insufficient funds"
        assert o.is_terminal


class TestIllegalTransitions:
    def test_open_before_submit(self) -> None:
        o = make_order()
        with pytest.raises(OrderStatusError, match="cannot open"):
            o.open("VID-1")

    def test_double_submit(self) -> None:
        o = make_order()
        o.submit()
        with pytest.raises(OrderStatusError, match="cannot submit"):
            o.submit()

    def test_apply_fill_on_new(self) -> None:
        o = make_order()
        with pytest.raises(OrderStatusError, match="cannot apply_fill"):
            o.apply_fill(money("1"), money("30000"))

    def test_apply_fill_on_cancelled(self) -> None:
        o = make_order()
        o.submit()
        o.open("VID-1")
        o.cancel()
        with pytest.raises(OrderStatusError, match="cannot apply_fill"):
            o.apply_fill(money("1"), money("30000"))

    def test_apply_fill_on_filled(self) -> None:
        o = make_order(qty="1")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1"), money("30000"))
        assert o.status is OrderStatus.FILLED
        with pytest.raises(OrderStatusError, match="cannot apply_fill"):
            o.apply_fill(money("0.1"), money("30000"))

    def test_cancel_from_new(self) -> None:
        o = make_order()
        with pytest.raises(OrderStatusError, match="cannot cancel"):
            o.cancel()

    def test_cancel_terminal(self) -> None:
        o = make_order(qty="1")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1"), money("30000"))
        with pytest.raises(OrderStatusError, match="cannot cancel"):
            o.cancel()

    def test_reject_from_open(self) -> None:
        o = make_order()
        o.submit()
        o.open("VID-1")
        with pytest.raises(OrderStatusError, match="cannot reject"):
            o.reject("too late")

    def test_open_with_empty_venue_id(self) -> None:
        o = make_order()
        o.submit()
        with pytest.raises(OrderError, match="venue_order_id must be non-empty"):
            o.open("")
        # Status unchanged after the failed open.
        assert o.status is OrderStatus.SUBMITTED


class TestApplyFillAccounting:
    def test_weighted_average_is_exact(self) -> None:
        # Two equal-qty fills -> arithmetic mean.
        o = make_order(qty="2")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1"), money("30000"))
        o.apply_fill(money("1"), money("30100"))
        assert o.avg_fill_price == Decimal("30050")

    def test_weighted_average_unequal_quantities(self) -> None:
        # 3 @ 100 then 1 @ 200 -> (300 + 200) / 4 = 125, exactly.
        o = make_order(qty="4", limit_price="150")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("3"), money("100"))
        assert o.avg_fill_price == Decimal("100")
        o.apply_fill(money("1"), money("200"))
        assert o.avg_fill_price == Decimal("125")
        assert o.status is OrderStatus.FILLED

    def test_avg_price_is_decimal_not_float(self) -> None:
        # A third that would lose precision as a float stays exact as Decimal.
        o = make_order(qty="3", limit_price="10")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1"), money("10"))
        o.apply_fill(money("2"), money("10"))
        assert isinstance(o.avg_fill_price, Decimal)
        assert o.avg_fill_price == Decimal("10")

    def test_over_fill_rejected(self) -> None:
        o = make_order(qty="2")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1.5"), money("30000"))
        with pytest.raises(OrderError, match="over-fill"):
            o.apply_fill(money("1"), money("30000"))
        # State unchanged after the rejected over-fill.
        assert o.filled_qty == money("1.5")
        assert o.status is OrderStatus.PARTIALLY_FILLED

    def test_non_positive_fill_qty_rejected(self) -> None:
        o = make_order(qty="2")
        o.submit()
        o.open("VID-1")
        with pytest.raises(OrderError, match="fill qty must be positive"):
            o.apply_fill(money("0"), money("30000"))

    def test_non_positive_fill_price_rejected(self) -> None:
        o = make_order(qty="2")
        o.submit()
        o.open("VID-1")
        with pytest.raises(OrderError, match="fill price must be positive"):
            o.apply_fill(money("1"), money("0"))


class TestFillTolerance:
    def test_exact_fill_closes(self) -> None:
        o = make_order(qty="1")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1"), money("30000"))
        assert o.status is OrderStatus.FILLED

    def test_dust_within_tolerance_closes_to_filled(self) -> None:
        # Default tol is 0.1%. Leave 0.05% unfilled -> treated as FILLED.
        o = make_order(qty="1000")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("999.5"), money("30000"))
        unfilled_fraction = (money("1000") - money("999.5")) / money("1000")
        assert unfilled_fraction < DEFAULT_FILL_TOLERANCE
        assert o.status is OrderStatus.FILLED
        # filled_qty reflects what actually executed, not the rounded-up qty.
        assert o.filled_qty == money("999.5")

    def test_unfilled_above_tolerance_stays_partial(self) -> None:
        # Leave 1% unfilled -> well above the 0.1% tolerance.
        o = make_order(qty="1000")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("990"), money("30000"))
        assert o.status is OrderStatus.PARTIALLY_FILLED

    def test_tolerance_boundary_is_strict(self) -> None:
        # Exactly at tolerance (unfilled == tol) is NOT within tolerance.
        o = make_order(qty="1000", fill_tolerance="0.001")
        o.submit()
        o.open("VID-1")
        # Unfilled fraction == 0.001 exactly -> not strictly below tol.
        o.apply_fill(money("999"), money("30000"))
        assert o.status is OrderStatus.PARTIALLY_FILLED

    def test_zero_tolerance_requires_exact_fill(self) -> None:
        o = make_order(qty="1000", fill_tolerance="0")
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("999.99"), money("30000"))
        assert o.status is OrderStatus.PARTIALLY_FILLED


class TestRealisticPartialFillReplay:
    def test_partial_fills_sum_to_qty_exact_weighted_average(self) -> None:
        # A realistic ladder of partial fills at different prices summing to the
        # order quantity. Assert final status FILLED and the avg price equal to
        # the exact Decimal quantity-weighted average.
        o = Order(
            client_order_id="replay-1",
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            qty=money("1.5"),
            type=OrderType.LIMIT,
            limit_price=money("30100"),
        )
        o.submit()
        o.open("KRAKEN-OXXXX")

        fills = [
            (money("0.3"), money("30000.0")),
            (money("0.45"), money("30025.5")),
            (money("0.25"), money("30050.0")),
            (money("0.5"), money("30099.9")),
        ]
        for q, p in fills:
            o.apply_fill(q, p)

        total_qty = sum((q for q, _ in fills), money("0"))
        notional = sum((q * p for q, p in fills), money("0"))
        expected_avg = notional / total_qty

        assert total_qty == money("1.5")
        assert o.filled_qty == money("1.5")
        assert o.status is OrderStatus.FILLED
        assert o.avg_fill_price == expected_avg
        assert o.remaining_qty == money("0")
