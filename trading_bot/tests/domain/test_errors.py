"""Tests for the domain error hierarchy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.domain.errors import (
    InsufficientFunds,
    MissingOrder,
    NoCapability,
    OrderError,
    OrderStatusError,
    RiskLimitBreached,
    TradingBotError,
)

ALL_ERRORS = [
    OrderError,
    OrderStatusError,
    MissingOrder,
    InsufficientFunds,
    RiskLimitBreached,
    NoCapability,
]


class TestHierarchy:
    @pytest.mark.parametrize("err", ALL_ERRORS)
    def test_all_subclass_root(self, err: type[Exception]) -> None:
        assert issubclass(err, TradingBotError)

    def test_root_is_exception(self) -> None:
        assert issubclass(TradingBotError, Exception)

    def test_order_status_error_is_order_error(self) -> None:
        assert issubclass(OrderStatusError, OrderError)


class TestMessages:
    def test_order_error_default_message(self) -> None:
        err = OrderError("OID-1")
        assert "OID-1" in str(err)
        assert err.order_id == "OID-1"

    def test_order_error_custom_message(self) -> None:
        err = OrderError("OID-2", "rejected by venue")
        assert "OID-2" in str(err)
        assert "rejected by venue" in str(err)

    def test_order_status_error_message(self) -> None:
        err = OrderStatusError("OID-3", status="filled", action="cancel")
        assert "cancel" in str(err)
        assert "filled" in str(err)
        assert err.status == "filled"
        assert err.action == "cancel"

    def test_missing_order_message(self) -> None:
        err = MissingOrder("OID-4")
        assert "OID-4" in str(err)
        assert "missing" in str(err)
        assert err.order_id == "OID-4"

    def test_insufficient_funds_message(self) -> None:
        err = InsufficientFunds("USD", Decimal("100"), Decimal("40"))
        assert "USD" in str(err)
        assert "100" in str(err)
        assert "40" in str(err)
        assert err.asset == "USD"
        assert err.required == Decimal("100")
        assert err.available == Decimal("40")

    def test_risk_limit_breached_message(self) -> None:
        err = RiskLimitBreached("max_position", Decimal("5"), Decimal("3"))
        assert "max_position" in str(err)
        assert "5" in str(err)
        assert "3" in str(err)
        assert err.limit == "max_position"

    def test_no_capability_message(self) -> None:
        err = NoCapability("kraken", "margin")
        assert "kraken" in str(err)
        assert "margin" in str(err)
        assert err.venue == "kraken"
        assert err.capability == "margin"


class TestRaisability:
    def test_can_catch_via_root(self) -> None:
        with pytest.raises(TradingBotError):
            raise InsufficientFunds("EUR", Decimal("1"), Decimal("0"))
