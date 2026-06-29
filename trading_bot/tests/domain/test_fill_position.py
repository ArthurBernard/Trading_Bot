"""Tests for the Fill execution record and Position folded from fills."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.domain.errors import InstrumentMismatch, OrderError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide
from trading_bot.domain.position import Position

BTCUSD = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)
ETHUSD = Instrument(Symbol("ETH", "USD"), price_precision=2, qty_precision=8)


def make_fill(
    *,
    side: OrderSide,
    qty: str,
    price: str,
    fee: str = "0",
    instrument: Instrument = BTCUSD,
    fill_id: str = "T1",
    client_order_id: str = "cid-1",
    ts: int = 1_700_000_000_000,
) -> Fill:
    """Build a test fill with sensible defaults."""
    return Fill(
        fill_id=fill_id,
        client_order_id=client_order_id,
        instrument=instrument,
        side=side,
        qty=money(qty),
        price=money(price),
        fee=money(fee),
        ts=ts,
    )


class TestFillConstruction:
    def test_immutable_frozen(self) -> None:
        f = make_fill(side=OrderSide.BUY, qty="1", price="30000")
        with pytest.raises(AttributeError):
            f.qty = money("2")  # type: ignore[misc]

    def test_signed_qty_buy_is_positive(self) -> None:
        f = make_fill(side=OrderSide.BUY, qty="1.5", price="30000")
        assert f.signed_qty == money("1.5")

    def test_signed_qty_sell_is_negative(self) -> None:
        f = make_fill(side=OrderSide.SELL, qty="1.5", price="30000")
        assert f.signed_qty == money("-1.5")

    def test_empty_fill_id_rejected(self) -> None:
        with pytest.raises(OrderError, match="fill_id"):
            make_fill(side=OrderSide.BUY, qty="1", price="30000", fill_id="")

    def test_empty_client_order_id_rejected(self) -> None:
        with pytest.raises(OrderError, match="client_order_id"):
            make_fill(
                side=OrderSide.BUY, qty="1", price="30000", client_order_id=""
            )

    def test_non_positive_qty_rejected(self) -> None:
        with pytest.raises(OrderError, match="qty"):
            make_fill(side=OrderSide.BUY, qty="0", price="30000")

    def test_non_positive_price_rejected(self) -> None:
        with pytest.raises(OrderError, match="price"):
            make_fill(side=OrderSide.BUY, qty="1", price="0")

    def test_negative_fee_rejected(self) -> None:
        with pytest.raises(OrderError, match="fee"):
            make_fill(side=OrderSide.BUY, qty="1", price="30000", fee="-1")

    def test_negative_ts_rejected(self) -> None:
        with pytest.raises(OrderError, match="ts"):
            make_fill(side=OrderSide.BUY, qty="1", price="30000", ts=-1)

    def test_zero_fee_allowed(self) -> None:
        f = make_fill(side=OrderSide.BUY, qty="1", price="30000", fee="0")
        assert f.fee == money("0")


class TestPositionBasics:
    def test_empty_fills_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one fill"):
            Position.from_fills([])

    def test_single_buy(self) -> None:
        pos = Position.from_fills(
            [make_fill(side=OrderSide.BUY, qty="2", price="30000")]
        )
        assert pos.net_qty == money("2")
        assert pos.avg_entry_price == money("30000")
        assert pos.realised_pnl == money("0")
        assert pos.fees_paid == money("0")
        assert pos.is_long
        assert not pos.is_flat

    def test_single_sell_opens_short(self) -> None:
        pos = Position.from_fills(
            [make_fill(side=OrderSide.SELL, qty="2", price="30000")]
        )
        assert pos.net_qty == money("-2")
        assert pos.avg_entry_price == money("30000")
        assert pos.is_short

    def test_increase_long_weighted_average(self) -> None:
        # BUY 2 @ 30000, then BUY 1 @ 33000 -> avg = (60000+33000)/3 = 31000.
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.BUY, qty="2", price="30000"),
                make_fill(side=OrderSide.BUY, qty="1", price="33000"),
            ]
        )
        assert pos.net_qty == money("3")
        assert pos.avg_entry_price == money("31000")
        assert pos.realised_pnl == money("0")

    def test_increase_short_weighted_average(self) -> None:
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.SELL, qty="2", price="30000"),
                make_fill(side=OrderSide.SELL, qty="1", price="33000"),
            ]
        )
        assert pos.net_qty == money("-3")
        assert pos.avg_entry_price == money("31000")


class TestPositionRealisedPnl:
    def test_buy_then_partial_sell(self) -> None:
        # BUY 3 @ 30000, then SELL 1 @ 35000.
        # realised gross = (35000-30000)*1 = 5000; remainder 2 @ 30000.
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.BUY, qty="3", price="30000"),
                make_fill(side=OrderSide.SELL, qty="1", price="35000"),
            ]
        )
        assert pos.net_qty == money("2")
        assert pos.avg_entry_price == money("30000")
        assert pos.realised_pnl == money("5000")
        assert pos.fees_paid == money("0")

    def test_short_then_partial_buy_back(self) -> None:
        # SELL 3 @ 35000, then BUY 1 @ 30000 -> short PnL = (35000-30000)*1.
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.SELL, qty="3", price="35000"),
                make_fill(side=OrderSide.BUY, qty="1", price="30000"),
            ]
        )
        assert pos.net_qty == money("-2")
        assert pos.avg_entry_price == money("35000")
        assert pos.realised_pnl == money("5000")

    def test_full_close_to_flat(self) -> None:
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.BUY, qty="2", price="30000"),
                make_fill(side=OrderSide.SELL, qty="2", price="31000"),
            ]
        )
        assert pos.net_qty == money("0")
        assert pos.avg_entry_price is None
        assert pos.is_flat
        assert pos.realised_pnl == money("2000")  # (31000-30000)*2

    def test_loss_is_negative(self) -> None:
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.BUY, qty="1", price="30000"),
                make_fill(side=OrderSide.SELL, qty="1", price="28000"),
            ]
        )
        assert pos.realised_pnl == money("-2000")


class TestPositionFlip:
    def test_long_to_short_flip(self) -> None:
        # BUY 2 @ 30000 (long 2), then SELL 5 @ 36000.
        # Close 2 @ entry 30000: gross = (36000-30000)*2 = 12000.
        # Remainder 3 opens short at the flipping fill's price 36000.
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.BUY, qty="2", price="30000"),
                make_fill(side=OrderSide.SELL, qty="5", price="36000"),
            ]
        )
        assert pos.net_qty == money("-3")
        assert pos.avg_entry_price == money("36000")  # flipping fill's price
        assert pos.realised_pnl == money("12000")
        assert pos.is_short

    def test_short_to_long_flip(self) -> None:
        # SELL 2 @ 36000 (short 2), then BUY 5 @ 30000.
        # Close 2 short @ entry 36000: gross = (36000-30000)*2 = 12000.
        # Remainder 3 opens long at 30000.
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.SELL, qty="2", price="36000"),
                make_fill(side=OrderSide.BUY, qty="5", price="30000"),
            ]
        )
        assert pos.net_qty == money("3")
        assert pos.avg_entry_price == money("30000")
        assert pos.realised_pnl == money("12000")
        assert pos.is_long


class TestPositionFees:
    def test_fees_accrue_and_reduce_pnl(self) -> None:
        # BUY 1 @ 30000 fee 12; SELL 1 @ 31000 fee 6.
        # gross close = 1000; realised = 1000 - 12 - 6 = 982; fees = 18.
        pos = Position.from_fills(
            [
                make_fill(side=OrderSide.BUY, qty="1", price="30000", fee="12"),
                make_fill(side=OrderSide.SELL, qty="1", price="31000", fee="6"),
            ]
        )
        assert pos.fees_paid == money("18")
        assert pos.realised_pnl == money("982")

    def test_fee_on_opening_fill_only(self) -> None:
        # A single opening BUY with a fee: no gross PnL yet, but the fee still
        # accrues and reduces realised PnL.
        pos = Position.from_fills(
            [make_fill(side=OrderSide.BUY, qty="1", price="30000", fee="12")]
        )
        assert pos.fees_paid == money("12")
        assert pos.realised_pnl == money("-12")
        assert pos.net_qty == money("1")


class TestPositionInstrument:
    def test_mixed_instrument_raises(self) -> None:
        with pytest.raises(InstrumentMismatch, match="BTC/USD"):
            Position.from_fills(
                [
                    make_fill(side=OrderSide.BUY, qty="1", price="30000"),
                    make_fill(
                        side=OrderSide.BUY,
                        qty="1",
                        price="2000",
                        instrument=ETHUSD,
                    ),
                ]
            )

    def test_position_carries_instrument(self) -> None:
        pos = Position.from_fills(
            [make_fill(side=OrderSide.BUY, qty="1", price="30000")]
        )
        assert pos.instrument == BTCUSD


class TestRealDataFlipSequence:
    """A realistic multi-fill BTC/USD sequence with an increase, partial close
    and a flip, asserted against hand-computed Decimal expectations.

    Sequence (in order):
      F1 BUY  2 @ 30000 fee 12.0  -> long 2 @ 30000
      F2 BUY  1 @ 33000 fee 6.6   -> long 3 @ 31000  (avg (60000+33000)/3)
      F3 SELL 1 @ 35000 fee 7.0   -> long 2 @ 31000, close 1 -> gross +4000
      F4 SELL 5 @ 36000 fee 36.0  -> FLIP: close 2 @ 31000 -> gross +10000,
                                     open short 3 @ 36000

    Hand-computed final:
      net_qty         = 2 + 1 - 1 - 5 = -3
      avg_entry_price = 36000 (the flipping fill's price)
      fees_paid       = 12.0 + 6.6 + 7.0 + 36.0 = 61.6
      gross realised  = 4000 + 10000 = 14000
      realised_pnl    = 14000 - 61.6 = 13938.4
    """

    def test_realistic_flip_sequence(self) -> None:
        fills = [
            make_fill(
                side=OrderSide.BUY, qty="2", price="30000", fee="12.0",
                fill_id="T1", ts=1_700_000_000_000,
            ),
            make_fill(
                side=OrderSide.BUY, qty="1", price="33000", fee="6.6",
                fill_id="T2", ts=1_700_000_060_000,
            ),
            make_fill(
                side=OrderSide.SELL, qty="1", price="35000", fee="7.0",
                fill_id="T3", ts=1_700_000_120_000,
            ),
            make_fill(
                side=OrderSide.SELL, qty="5", price="36000", fee="36.0",
                fill_id="T4", ts=1_700_000_180_000,
            ),
        ]
        pos = Position.from_fills(fills)

        assert pos.net_qty == money("-3")
        assert pos.avg_entry_price == money("36000")
        assert pos.realised_pnl == money("13938.4")
        assert pos.fees_paid == money("61.6")
        # Exact-Decimal guarantee: no binary float contamination.
        assert isinstance(pos.realised_pnl, Decimal)
        assert pos.realised_pnl == Decimal("13938.4")


class TestIncrementalFold:
    """`Position.flat` + `with_fill` — the O(1)-per-fill incremental fold."""

    def test_flat_is_zero_exposure(self) -> None:
        """`flat` is a no-exposure position (the fold identity)."""
        pos = Position.flat(BTCUSD)
        assert pos.instrument == BTCUSD
        assert pos.net_qty == money("0")
        assert pos.avg_entry_price is None
        assert pos.realised_pnl == money("0")
        assert pos.fees_paid == money("0")
        assert pos.is_flat

    def test_with_fill_matches_from_fills_at_every_prefix(self) -> None:
        """Folding `with_fill` from `flat` == `from_fills` over every prefix.

        Drives a realistic BTC/USD sequence — open, increase (weighted avg),
        partial close (realise PnL), full close, then a flip — and asserts the
        running incremental position equals `Position.from_fills` over the same
        prefix at *every* step. This is the exactness guarantee the tracker and
        performance service rely on for their O(n) drain.
        """
        fills = [
            make_fill(side=OrderSide.BUY, qty="2", price="30000", fee="1", fill_id="F1"),
            make_fill(side=OrderSide.BUY, qty="1", price="33000", fee="1", fill_id="F2"),
            make_fill(side=OrderSide.SELL, qty="1", price="35000", fee="1", fill_id="F3"),
            make_fill(side=OrderSide.SELL, qty="2", price="31000", fee="1", fill_id="F4"),
            make_fill(side=OrderSide.SELL, qty="2", price="29000", fee="1", fill_id="F5"),
        ]
        running = Position.flat(BTCUSD)
        for i, fill in enumerate(fills, start=1):
            running = running.with_fill(fill)
            expected = Position.from_fills(fills[:i])
            assert running.net_qty == expected.net_qty
            assert running.avg_entry_price == expected.avg_entry_price
            assert running.realised_pnl == expected.realised_pnl
            assert running.fees_paid == expected.fees_paid

    def test_with_fill_rejects_instrument_mismatch(self) -> None:
        """`with_fill` of a different instrument raises `InstrumentMismatch`."""
        pos = Position.from_fills(
            [make_fill(side=OrderSide.BUY, qty="1", price="30000")]
        )
        with pytest.raises(InstrumentMismatch, match="BTC/USD"):
            pos.with_fill(
                make_fill(
                    side=OrderSide.BUY, qty="1", price="2000", instrument=ETHUSD,
                    fill_id="F2",
                )
            )
