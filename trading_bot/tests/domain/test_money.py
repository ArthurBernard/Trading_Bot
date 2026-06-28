"""Tests for the exact Decimal money helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

from trading_bot.domain.money import (
    Money,
    add,
    from_float,
    money,
    mul,
    quantize,
    sub,
)


class TestExactConstruction:
    def test_decimal_addition_is_exact(self) -> None:
        # The whole reason money is Decimal: 0.1 + 0.2 == 0.3 exactly.
        assert money("0.1") + money("0.2") == money("0.3")
        assert add(money("0.1"), money("0.2")) == Decimal("0.3")

    def test_float_addition_is_not_exact(self) -> None:
        # Sanity: prove the float trap we are guarding against is real.
        assert 0.1 + 0.2 != 0.3

    def test_from_str(self) -> None:
        assert money("27123.45") == Decimal("27123.45")
        assert isinstance(money("1"), Money)

    def test_from_int(self) -> None:
        assert money(5) == Decimal("5")

    def test_from_decimal_passthrough(self) -> None:
        d = Decimal("3.14159")
        assert money(d) is d


class TestFloatGuard:
    def test_float_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="float"):
            money(0.1)  # type: ignore[arg-type]

    def test_bool_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="bool"):
            money(True)  # type: ignore[arg-type]

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(TypeError):
            money(None)  # type: ignore[arg-type]

    def test_from_float_opt_in_round_trips_shortest_decimal(self) -> None:
        # The sanctioned float entry point yields the human-meant decimal.
        assert from_float(0.1) == Decimal("0.1")
        assert from_float(27123.45) == Decimal("27123.45")

    def test_from_float_rejects_non_float(self) -> None:
        with pytest.raises(TypeError):
            from_float("0.1")  # type: ignore[arg-type]


class TestQuantize:
    def test_quantize_to_kraken_price_tick(self) -> None:
        # Kraken BTC/USD price tick is 0.1.
        result = quantize(money("27123.456789"), money("0.1"))
        assert result == Decimal("27123.4")
        assert isinstance(result, Decimal)

    def test_quantize_to_satoshi_lot(self) -> None:
        # Kraken volume precision for BTC is 8 dp (satoshi).
        result = quantize(money("0.123456789"), money("0.00000001"))
        assert result == Decimal("0.12345678")

    def test_quantize_rounds_down_by_default(self) -> None:
        # Default ROUND_DOWN must never overshoot.
        assert quantize(money("0.9999"), money("0.001")) == Decimal("0.999")

    def test_quantize_honours_rounding_mode(self) -> None:
        assert quantize(
            money("0.9999"), money("0.001"), rounding=ROUND_HALF_UP
        ) == Decimal("1.000")

    def test_quantize_result_scale_matches_tick(self) -> None:
        # Result carries the tick's exponent (0.1 -> "0.1", not "0.100").
        result = quantize(money("5"), money("0.1"))
        assert str(result) == "5.0"

    def test_quantize_rejects_non_positive_tick(self) -> None:
        with pytest.raises(ValueError):
            quantize(money("1"), money("0"))
        with pytest.raises(ValueError):
            quantize(money("1"), money("-0.1"))


class TestNoFloatLeakage:
    def test_arithmetic_helpers_return_decimal(self) -> None:
        assert isinstance(add(money("1"), money("2")), Decimal)
        assert isinstance(sub(money("3"), money("1")), Decimal)
        assert isinstance(mul(money("2"), money("3")), Decimal)
        assert sub(money("0.3"), money("0.1")) == Decimal("0.2")
        assert mul(money("0.1"), money("3")) == Decimal("0.3")
