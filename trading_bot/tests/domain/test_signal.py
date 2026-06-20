"""Tests for the Signal venue-neutral strategy target and its delta-to-position."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.domain.errors import SignalError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import Money, money
from trading_bot.domain.order import OrderSide
from trading_bot.domain.position import Position
from trading_bot.domain.signal import Signal, SignalMode

BTCUSD = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)


def position_with(net_qty: str, *, entry: str = "30000") -> Position:
    """Build a Position with a given signed net quantity via a single fill."""
    qty = money(net_qty)
    if qty == 0:
        # A flat position: open then fully close.
        buy = Fill("T1", "cid", BTCUSD, OrderSide.BUY, money("1"), money(entry),
                   money("0"), 1)
        sell = Fill("T2", "cid", BTCUSD, OrderSide.SELL, money("1"), money(entry),
                    money("0"), 2)
        return Position.from_fills([buy, sell])
    side = OrderSide.BUY if qty > 0 else OrderSide.SELL
    fill = Fill("T1", "cid", BTCUSD, side, abs(qty), money(entry), money("0"), 1)
    return Position.from_fills([fill])


class TestConstruction:
    def test_exposure_mode_tag(self) -> None:
        s = Signal.exposure(BTCUSD, money("1"), ts=1)
        assert s.mode is SignalMode.EXPOSURE
        assert s.target == money("1")

    def test_target_qty_mode_tag(self) -> None:
        s = Signal.target_qty(BTCUSD, money("2.5"), ts=1)
        assert s.mode is SignalMode.TARGET_QTY
        assert s.target == money("2.5")

    def test_frozen_immutable(self) -> None:
        s = Signal.exposure(BTCUSD, money("0.5"), ts=1)
        with pytest.raises(AttributeError):
            s.target = money("0.1")  # type: ignore[misc]

    def test_optional_strength_default_none(self) -> None:
        assert Signal.exposure(BTCUSD, money("0.5"), ts=1).strength is None

    def test_strength_accepted_in_range(self) -> None:
        s = Signal.exposure(BTCUSD, money("0.5"), ts=1, strength=money("0.8"))
        assert s.strength == money("0.8")

    def test_negative_ts_rejected(self) -> None:
        with pytest.raises(SignalError, match="ts"):
            Signal.target_qty(BTCUSD, money("1"), ts=-1)


class TestTargetValidation:
    def test_exposure_above_one_rejected(self) -> None:
        with pytest.raises(SignalError, match=r"\[-1, 1\]"):
            Signal.exposure(BTCUSD, money("1.5"), ts=1)

    def test_exposure_below_minus_one_rejected(self) -> None:
        with pytest.raises(SignalError, match=r"\[-1, 1\]"):
            Signal.exposure(BTCUSD, money("-1.5"), ts=1)

    def test_exposure_bounds_inclusive(self) -> None:
        # The exact bounds are valid.
        assert Signal.exposure(BTCUSD, money("1"), ts=1).target == money("1")
        assert Signal.exposure(BTCUSD, money("-1"), ts=1).target == money("-1")
        assert Signal.exposure(BTCUSD, money("0"), ts=1).target == money("0")

    def test_target_qty_allows_magnitude_above_one(self) -> None:
        # An explicit quantity is not bounded by [-1, 1].
        s = Signal.target_qty(BTCUSD, money("12.5"), ts=1)
        assert s.target == money("12.5")

    def test_strength_above_one_rejected(self) -> None:
        with pytest.raises(SignalError, match="strength"):
            Signal.exposure(BTCUSD, money("0.5"), ts=1, strength=money("1.5"))

    def test_strength_negative_rejected(self) -> None:
        with pytest.raises(SignalError, match="strength"):
            Signal.target_qty(BTCUSD, money("1"), ts=1, strength=money("-0.1"))


class TestDeltaToExplicitQty:
    def test_long_target_from_flat(self) -> None:
        s = Signal.target_qty(BTCUSD, money("2"), ts=1)
        assert s.delta_to(position_with("0")) == money("2")

    def test_increase_long(self) -> None:
        # Hold 1.5, want 4 -> buy 2.5.
        s = Signal.target_qty(BTCUSD, money("4"), ts=1)
        assert s.delta_to(position_with("1.5")) == money("2.5")

    def test_reduce_long(self) -> None:
        # Hold 4, want 1 -> sell 3.
        s = Signal.target_qty(BTCUSD, money("1"), ts=1)
        assert s.delta_to(position_with("4")) == money("-3")

    def test_flip_long_to_short(self) -> None:
        # Hold +2, want -1 -> delta -3.
        s = Signal.target_qty(BTCUSD, money("-1"), ts=1)
        assert s.delta_to(position_with("2")) == money("-3")

    def test_flat_target_from_long_full_close(self) -> None:
        s = Signal.target_qty(BTCUSD, money("0"), ts=1)
        assert s.delta_to(position_with("3")) == money("-3")

    def test_flat_target_from_short_full_close(self) -> None:
        s = Signal.target_qty(BTCUSD, money("0"), ts=1)
        assert s.delta_to(position_with("-2")) == money("2")

    def test_already_on_target_zero_delta(self) -> None:
        s = Signal.target_qty(BTCUSD, money("2"), ts=1)
        assert s.delta_to(position_with("2")) == money("0")


class TestDeltaToExposure:
    REF = money("10")  # reference_qty: max position of 10 base units.

    def test_full_long_from_flat(self) -> None:
        # exposure +1 * 10 = target +10, from flat -> buy 10.
        s = Signal.exposure(BTCUSD, money("1"), ts=1)
        assert s.delta_to(position_with("0"), self.REF) == money("10")

    def test_half_long_from_flat(self) -> None:
        s = Signal.exposure(BTCUSD, money("0.5"), ts=1)
        assert s.delta_to(position_with("0"), self.REF) == money("5")

    def test_full_short_from_flat(self) -> None:
        s = Signal.exposure(BTCUSD, money("-1"), ts=1)
        assert s.delta_to(position_with("0"), self.REF) == money("-10")

    def test_reduce_from_long(self) -> None:
        # Hold +10, want exposure 0.3 -> target +3 -> sell 7.
        s = Signal.exposure(BTCUSD, money("0.3"), ts=1)
        assert s.delta_to(position_with("10"), self.REF) == money("-7")

    def test_flat_exposure_from_long_full_close(self) -> None:
        s = Signal.exposure(BTCUSD, money("0"), ts=1)
        assert s.delta_to(position_with("6"), self.REF) == money("-6")

    def test_flip_long_to_short_via_exposure(self) -> None:
        # Hold +4, want exposure -1 -> target -10 -> delta -14.
        s = Signal.exposure(BTCUSD, money("-1"), ts=1)
        assert s.delta_to(position_with("4"), self.REF) == money("-14")

    def test_missing_reference_qty_rejected(self) -> None:
        s = Signal.exposure(BTCUSD, money("0.5"), ts=1)
        with pytest.raises(SignalError, match="reference_qty"):
            s.delta_to(position_with("0"))

    def test_non_positive_reference_qty_rejected(self) -> None:
        s = Signal.exposure(BTCUSD, money("0.5"), ts=1)
        with pytest.raises(SignalError, match="reference_qty"):
            s.delta_to(position_with("0"), money("0"))

    def test_target_qty_ignores_reference_qty(self) -> None:
        # reference_qty is irrelevant for explicit-qty signals.
        s = Signal.target_qty(BTCUSD, money("3"), ts=1)
        assert s.delta_to(position_with("1"), money("999")) == money("2")
        assert s.target_net_qty() == money("3")


class TestTargetNetQty:
    def test_exposure_scaled(self) -> None:
        s = Signal.exposure(BTCUSD, money("0.25"), ts=1)
        assert s.target_net_qty(money("8")) == money("2.00")

    def test_target_qty_direct(self) -> None:
        s = Signal.target_qty(BTCUSD, money("5"), ts=1)
        assert s.target_net_qty() == money("5")


class TestRealDataSeries:
    """A realistic signal series over a long/short/flat position series.

    Mirrors the legacy ``signal``/``delta_signal`` vocabulary: ``target_net_qty``
    is the legacy ``signal`` (target position), ``delta_to`` is ``delta_signal``
    (the change to apply).
    """

    def test_explicit_qty_series(self) -> None:
        # Position series and the target each step should drive toward.
        steps: list[tuple[str, str, str]] = [
            # (current net_qty, target_qty, expected delta)
            ("0", "5", "5"),     # open long
            ("5", "8", "3"),     # add to long
            ("8", "-4", "-12"),  # flip to short
            ("-4", "0", "4"),    # close to flat
            ("0", "-3", "-3"),   # open short
        ]
        for cur, tgt, expected in steps:
            sig = Signal.target_qty(BTCUSD, money(tgt), ts=1)
            assert sig.delta_to(position_with(cur)) == money(expected)

    def test_fractional_exposure_series(self) -> None:
        ref = money("20")
        steps: list[tuple[str, str, str]] = [
            # (current net_qty, exposure, expected delta) with ref=20
            ("0", "1", "20"),       # full long
            ("20", "0.5", "-10"),   # halve exposure
            ("10", "-1", "-30"),    # flip to full short
            ("-20", "0", "20"),     # flat
            ("0", "-0.25", "-5"),   # quarter short
        ]
        for cur, exp, expected in steps:
            sig = Signal.exposure(BTCUSD, money(exp), ts=1)
            assert sig.delta_to(position_with(cur), ref) == money(expected)

    def test_deltas_sum_to_reach_target_invariant(self) -> None:
        # Applying delta to a position lands exactly on the target (legacy
        # invariant: position[t+1] == signal[t]).
        ref: Money = money("10")
        cur: Decimal = money("0")
        for exp in ("0.5", "1", "-1", "0", "0.3"):
            sig = Signal.exposure(BTCUSD, money(exp), ts=1)
            pos = position_with(_qty_str(cur))
            delta = sig.delta_to(pos, ref)
            cur = pos.net_qty + delta
            assert cur == sig.target_net_qty(ref)


def _qty_str(d: Decimal) -> str:
    """Render a Decimal net qty back to a plain string for position_with."""
    return format(d, "f")
