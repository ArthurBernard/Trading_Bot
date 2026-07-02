"""Tests for the Order aggregate and its lifecycle state machine."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from trading_bot.domain.errors import MoneyError, OrderError, OrderStatusError
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import (
    AVG_PRICE_PRECISION,
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


class TestMoneyFieldGuard:
    """D-1/D-7: every money field is routed through money() at construction."""

    def test_float_qty_rejected(self) -> None:
        with pytest.raises(TypeError, match="float"):
            Order(
                client_order_id="cid",
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                qty=1.5,  # type: ignore[arg-type]
                type=OrderType.MARKET,
            )

    def test_float_limit_price_rejected(self) -> None:
        with pytest.raises(TypeError, match="float"):
            Order(
                client_order_id="cid",
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                qty=money("1"),
                type=OrderType.LIMIT,
                limit_price=30000.0,  # type: ignore[arg-type]
            )

    def test_float_stop_price_rejected(self) -> None:
        with pytest.raises(TypeError, match="float"):
            Order(
                client_order_id="cid",
                instrument=INSTRUMENT,
                side=OrderSide.SELL,
                qty=money("1"),
                type=OrderType.STOP_LOSS,
                stop_price=29000.0,  # type: ignore[arg-type]
            )

    def test_non_finite_qty_rejected(self) -> None:
        with pytest.raises(MoneyError, match="finite"):
            Order(
                client_order_id="cid",
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                qty=Decimal("NaN"),
                type=OrderType.MARKET,
            )

    def test_apply_fill_float_rejected(self) -> None:
        o = make_order(qty="2", otype=OrderType.MARKET, limit_price=None)
        o.submit()
        o.open("V1")
        with pytest.raises(TypeError, match="float"):
            o.apply_fill(1.0, 30000.0)  # type: ignore[arg-type]

    def test_apply_fill_non_finite_rejected(self) -> None:
        o = make_order(qty="2", otype=OrderType.MARKET, limit_price=None)
        o.submit()
        o.open("V1")
        with pytest.raises(MoneyError, match="finite"):
            o.apply_fill(money("1"), Decimal("Infinity"))


class TestClientOrderIdIdentity:
    """A-12: the identity is set at construction; the runner rebuilds, not mutates."""

    def test_client_order_id_set_at_construction(self) -> None:
        o = make_order()
        assert o.client_order_id == "cid-1"

    def test_replace_stamps_a_new_id_at_construction(self) -> None:
        # The runner's factory path replaces (rebuilds) the Order with the
        # deterministic id set *at construction* rather than mutating the id
        # after the fact. replace re-runs validation and yields a fresh, distinct
        # aggregate that shares the factory's shape but owns the runner's id.
        from dataclasses import replace

        built = Order(
            client_order_id="pending",
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            qty=money("2"),
            type=OrderType.LIMIT,
            limit_price=money("30000"),
        )
        stamped = replace(built, client_order_id="strat-7")
        assert stamped.client_order_id == "strat-7"
        # Same shape, distinct instance, and the original is untouched.
        assert stamped.qty == built.qty
        assert stamped.limit_price == built.limit_price
        assert stamped.side is built.side
        assert stamped.type is built.type
        assert stamped is not built
        assert built.client_order_id == "pending"

    def test_replace_reruns_construction_validation(self) -> None:
        # Because the id is stamped at construction (via replace), the empty-id
        # guard fires there too — you cannot rebuild into an invalid identity.
        built = make_order()
        from dataclasses import replace

        with pytest.raises(OrderError, match="client_order_id is mandatory"):
            replace(built, client_order_id="")


class TestAvgFillPriceDeterminism:
    """D-4: the average fill price is deterministic across global contexts."""

    def test_repeating_quotient_is_context_independent(self) -> None:
        from decimal import getcontext

        def compute_avg() -> Money:
            o = make_order(qty="3", otype=OrderType.MARKET, limit_price=None)
            o.submit()
            o.open("V1")
            # A notional not divisible by the filled qty -> a repeating tail.
            o.apply_fill(money("1"), money("100"))
            o.apply_fill(money("2"), money("100.0000001"))
            assert o.avg_fill_price is not None
            return o.avg_fill_price

        original = getcontext().prec
        results = set()
        try:
            for prec in (5, 10, 28, 60, 200):
                getcontext().prec = prec
                results.add(str(compute_avg()))
        finally:
            getcontext().prec = original
        # One and only one value, regardless of the process-global precision.
        assert len(results) == 1


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


class TestOverFillTolerance:
    """D-6: a market over-delivery within tolerance closes; beyond it raises."""

    def test_over_fill_within_tolerance_closes_to_filled(self) -> None:
        # Default tol 0.1%. Fill qty * (1 + tol/2) -> 0.05% over -> within tol.
        o = make_order(qty="1000", otype=OrderType.MARKET, limit_price=None)
        o.submit()
        o.open("VID-1")
        excess = money("1000") * (DEFAULT_FILL_TOLERANCE / money("2"))
        assert (excess / money("1000")) < DEFAULT_FILL_TOLERANCE
        o.apply_fill(money("1000") + excess, money("30000"))
        # Closes rather than raising; filled_qty is clamped to the order qty.
        assert o.status is OrderStatus.FILLED
        assert o.filled_qty == money("1000")
        # remaining is exactly zero (not negative).
        assert o.remaining_qty == money("0")

    def test_over_fill_after_partial_within_tolerance_closes(self) -> None:
        # A partial fill then a slight over-delivery of the remainder.
        o = make_order(qty="2", otype=OrderType.MARKET, limit_price=None)
        o.submit()
        o.open("VID-1")
        o.apply_fill(money("1.5"), money("30000"))
        assert o.status is OrderStatus.PARTIALLY_FILLED
        # Remaining 0.5; deliver 0.5008 -> excess 0.0008 -> 0.04% of 2 < 0.1%.
        o.apply_fill(money("0.5008"), money("30000"))
        assert o.status is OrderStatus.FILLED
        assert o.filled_qty == money("2")

    def test_over_fill_records_actual_price_in_average(self) -> None:
        # The within-tolerance over-fill's real qty/price must weight the average
        # (what we actually paid), even though filled_qty is clamped to qty.
        o = make_order(qty="1000", otype=OrderType.MARKET, limit_price=None)
        o.submit()
        o.open("VID-1")
        excess = money("0.4")  # 0.04% of 1000 -> within 0.1% tolerance
        o.apply_fill(money("1000") + excess, money("30000"))
        # Single fill at a flat price -> average is that price regardless of clamp.
        assert o.avg_fill_price == money("30000")
        assert o.status is OrderStatus.FILLED

    def test_material_over_fill_still_rejected(self) -> None:
        # 1% over -> well beyond the 0.1% tolerance -> hard error, state intact.
        o = make_order(qty="1000", otype=OrderType.MARKET, limit_price=None)
        o.submit()
        o.open("VID-1")
        with pytest.raises(OrderError, match="over-fill"):
            o.apply_fill(money("1010"), money("30000"))
        assert o.filled_qty == money("0")
        assert o.status is OrderStatus.OPEN

    def test_over_fill_at_tolerance_boundary_is_rejected(self) -> None:
        # Excess fraction == tol exactly is NOT strictly below -> rejected.
        o = make_order(
            qty="1000", otype=OrderType.MARKET, limit_price=None,
            fill_tolerance="0.001",
        )
        o.submit()
        o.open("VID-1")
        # Excess 1.0 -> 0.1% of 1000 == tol exactly -> material.
        with pytest.raises(OrderError, match="over-fill"):
            o.apply_fill(money("1001"), money("30000"))

    def test_zero_tolerance_rejects_any_over_fill(self) -> None:
        o = make_order(
            qty="1000", otype=OrderType.MARKET, limit_price=None,
            fill_tolerance="0",
        )
        o.submit()
        o.open("VID-1")
        with pytest.raises(OrderError, match="over-fill"):
            o.apply_fill(money("1000.0001"), money("30000"))


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
        # Reproduce the exact quantity-weighted average the aggregate computes:
        # an *incremental* running average (re-weighted each fill), evaluated
        # under the same pinned AVG_PRICE_PRECISION context so rounding matches
        # deterministically (see D-4). A single-shot notional/total_qty would
        # differ in the last digits from the incremental fold.
        with localcontext() as ctx:
            ctx.prec = AVG_PRICE_PRECISION
            acc_qty = money("0")
            acc_avg = money("0")
            for q, p in fills:
                acc_notional = acc_avg * acc_qty + q * p
                acc_qty = acc_qty + q
                acc_avg = acc_notional / acc_qty
            expected_avg = acc_avg

        assert total_qty == money("1.5")
        assert o.filled_qty == money("1.5")
        assert o.status is OrderStatus.FILLED
        assert o.avg_fill_price == expected_avg
        assert o.remaining_qty == money("0")
