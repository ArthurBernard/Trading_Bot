"""Tests for :class:`CapitalEvent` and the pure capital folds (`domain.capital`).

Proves, with money exact end to end (no float) and no I/O:

* :class:`CapitalEvent` validates like :class:`~trading_bot.domain.fill.Fill`
  (mandatory non-empty ids, a money field routed through
  :func:`~trading_bot.domain.money.money`, a range guard on the business
  invariant that ``amount`` is strictly positive);
* :func:`contributed_capital` folds ``FUNDING``/``DEPOSIT`` in and
  ``WITHDRAWAL`` out;
* :func:`value_series` interleaves events and fills by ``ts`` (event before
  fill on a tie) and reconciles, to the cent, with
  :func:`~trading_bot.application.pnl_series.equity_series` when the only
  event is a genesis ``FUNDING(v0)``.
"""

from __future__ import annotations

import pytest

from trading_bot.domain.capital import (
    CapitalEvent,
    CapitalEventType,
    contributed_capital,
    value_series,
)
from trading_bot.domain.errors import MoneyError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide

BTC = Instrument(Symbol("BTC", "USD"))


def make_event(
    *,
    event_type: CapitalEventType,
    amount: str,
    ts: int = 0,
    event_id: str = "E1",
    strategy: str = "alloc1",
    note: str = "",
) -> CapitalEvent:
    """Build a test capital event with sensible defaults."""
    return CapitalEvent(
        event_id=event_id,
        strategy=strategy,
        event_type=event_type,
        amount=money(amount),
        ts=ts,
        note=note,
    )


def _fill(
    fid: str,
    side: OrderSide,
    qty: str,
    price: str,
    *,
    fee: str = "0",
    ts: int = 0,
    inst: Instrument = BTC,
) -> Fill:
    return Fill(fid, f"c-{fid}", inst, side, money(qty), money(price), money(fee), ts)


# --- CapitalEventType -------------------------------------------------------- #


def test_event_type_is_enum_by_value() -> None:
    assert CapitalEventType.FUNDING.value == "funding"
    assert CapitalEventType.DEPOSIT.value == "deposit"
    assert CapitalEventType.WITHDRAWAL.value == "withdrawal"


# --- CapitalEvent construction ---------------------------------------------- #


class TestCapitalEventConstruction:
    def test_immutable_frozen(self) -> None:
        event = make_event(event_type=CapitalEventType.FUNDING, amount="1000")
        with pytest.raises(AttributeError):
            event.amount = money("2000")  # type: ignore[misc]

    def test_empty_event_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="event_id"):
            make_event(event_type=CapitalEventType.FUNDING, amount="1000", event_id="")

    def test_empty_strategy_rejected(self) -> None:
        with pytest.raises(ValueError, match="strategy"):
            make_event(event_type=CapitalEventType.FUNDING, amount="1000", strategy="")

    def test_zero_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="amount"):
            make_event(event_type=CapitalEventType.FUNDING, amount="0")

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="amount"):
            make_event(event_type=CapitalEventType.WITHDRAWAL, amount="-1")

    def test_negative_ts_rejected(self) -> None:
        with pytest.raises(ValueError, match="ts"):
            make_event(event_type=CapitalEventType.FUNDING, amount="1000", ts=-1)

    def test_note_defaults_to_empty(self) -> None:
        event = make_event(event_type=CapitalEventType.FUNDING, amount="1000")
        assert event.note == ""

    # --- amount routed through money(): float rejected, non-finite rejected - #
    # Mirrors Fill's actual construction discipline (domain/fill.py, proven by
    # trading_bot/tests/domain/test_fill_position.py's TestFillConstruction):
    # a raw float is rejected by money() itself with TypeError, not ValueError.

    def test_float_amount_rejected(self) -> None:
        with pytest.raises(TypeError, match="float"):
            CapitalEvent(
                event_id="E1",
                strategy="alloc1",
                event_type=CapitalEventType.FUNDING,
                amount=1000.0,  # type: ignore[arg-type]
                ts=0,
            )

    def test_non_finite_amount_rejected(self) -> None:
        with pytest.raises(MoneyError, match="finite"):
            CapitalEvent(
                event_id="E1",
                strategy="alloc1",
                event_type=CapitalEventType.FUNDING,
                amount=money("nan"),
                ts=0,
            )


# --- contributed_capital ------------------------------------------------------ #


class TestContributedCapital:
    def test_empty_is_zero(self) -> None:
        assert contributed_capital([]) == money("0")

    def test_genesis_only_is_the_allocation(self) -> None:
        events = [make_event(event_type=CapitalEventType.FUNDING, amount="1000")]
        assert contributed_capital(events) == money("1000")

    def test_deposit_adds(self) -> None:
        events = [
            make_event(event_type=CapitalEventType.FUNDING, amount="1000", ts=0),
            make_event(event_type=CapitalEventType.DEPOSIT, amount="500", ts=1),
        ]
        assert contributed_capital(events) == money("1500")

    def test_withdrawal_subtracts(self) -> None:
        events = [
            make_event(event_type=CapitalEventType.FUNDING, amount="1000", ts=0),
            make_event(event_type=CapitalEventType.WITHDRAWAL, amount="200", ts=1),
        ]
        assert contributed_capital(events) == money("800")

    def test_deposit_and_withdraw_sequence(self) -> None:
        events = [
            make_event(event_type=CapitalEventType.FUNDING, amount="1000", ts=0),
            make_event(event_type=CapitalEventType.DEPOSIT, amount="300", ts=1),
            make_event(event_type=CapitalEventType.WITHDRAWAL, amount="100", ts=2),
            make_event(event_type=CapitalEventType.WITHDRAWAL, amount="50", ts=3),
        ]
        assert contributed_capital(events) == money("1150")


# --- value_series ------------------------------------------------------------- #


class TestValueSeries:
    def test_empty_streams_yield_empty_series(self) -> None:
        assert value_series([], []) == []

    def test_worked_example_fund_profit_withdraw(self) -> None:
        """fund 1000 -> +100 realised -> withdraw 100 => C=900, R=100, V(last)=1000."""
        genesis = make_event(
            event_type=CapitalEventType.FUNDING, amount="1000", ts=0, event_id="E1"
        )
        withdrawal = make_event(
            event_type=CapitalEventType.WITHDRAWAL, amount="100", ts=10, event_id="E2"
        )
        events = [genesis, withdrawal]
        fills = [
            _fill("F1", OrderSide.BUY, "1", "100", ts=1),
            _fill("F2", OrderSide.SELL, "1", "200", ts=2),
        ]
        assert contributed_capital(events) == money("900")

        points = value_series(fills, events)
        last = points[-1]
        assert last.contributed == money("900")
        assert last.realised_pnl == money("100")
        assert last.value == money("1000")

    def test_reconciles_with_equity_series_given_genesis_funding(self) -> None:
        """With only a genesis FUNDING(v0), value_series reconciles to equity_series.

        Importing application code in the test (not in domain source) is fine
        per the leaf plan: this is the load-bearing reconciliation invariant —
        value_series's realised-PnL fold must never diverge from
        ``application.pnl_series.equity_series``'s.
        """
        from trading_bot.application.pnl_series import equity_series

        v0 = money("1000")
        genesis = make_event(
            event_type=CapitalEventType.FUNDING, amount="1000", ts=0, event_id="E1"
        )
        fills = [
            _fill("F1", OrderSide.BUY, "2", "100", fee="1", ts=1),
            _fill("F2", OrderSide.SELL, "1", "120", fee="1", ts=2),
            _fill("F3", OrderSide.SELL, "1", "90", fee="1", ts=3),
        ]

        points = value_series(fills, [genesis])
        expected = equity_series(fills, v0=v0)

        # value_series has one extra leading point (the genesis event itself);
        # every subsequent point must line up one-for-one with equity_series.
        assert len(points) == len(expected) + 1
        assert points[0].ts_ms == genesis.ts
        assert points[0].contributed == v0
        assert points[0].realised_pnl == money("0")
        assert points[0].value == v0
        for point, exp in zip(points[1:], expected, strict=True):
            assert point.ts_ms == exp.ts_ms
            assert point.realised_pnl == exp.realised_pnl
            assert point.value == exp.equity

    def test_mid_stream_deposit_shifts_value_not_realised(self) -> None:
        """A mid-stream DEPOSIT shifts ``value`` by its amount; realised_pnl is untouched."""
        genesis = make_event(
            event_type=CapitalEventType.FUNDING, amount="1000", ts=0, event_id="E1"
        )
        deposit = make_event(
            event_type=CapitalEventType.DEPOSIT, amount="200", ts=2, event_id="E2"
        )
        fills = [
            _fill("F1", OrderSide.BUY, "1", "100", ts=1),
            _fill("F2", OrderSide.SELL, "1", "150", ts=3),
        ]

        without_deposit = value_series(fills, [genesis])
        with_deposit = value_series(fills, [genesis, deposit])

        assert without_deposit[-1].realised_pnl == with_deposit[-1].realised_pnl
        assert with_deposit[-1].value == without_deposit[-1].value + money("200")

    def test_event_applies_before_fill_on_tie(self) -> None:
        """An event and a fill sharing the same ts: the event's effect lands first.

        Built so a wrong tie-break is observable: at ts=5 a WITHDRAWAL and a
        closing SELL land together. If the fill were folded first, the point
        right after the withdrawal would carry the fill's realised PnL too
        (contributed=950, realised=30, value=980 for *both* points) instead of
        the event-first shape asserted below.
        """
        genesis = make_event(
            event_type=CapitalEventType.FUNDING, amount="1000", ts=0, event_id="E1"
        )
        withdrawal = make_event(
            event_type=CapitalEventType.WITHDRAWAL, amount="50", ts=5, event_id="E2"
        )
        fills = [
            _fill("F1", OrderSide.BUY, "1", "100", ts=1),
            _fill("F2", OrderSide.SELL, "1", "130", ts=5),
        ]

        points = value_series(fills, [genesis, withdrawal])

        assert len(points) == 4
        # ts=0: genesis funding.
        assert points[0].ts_ms == 0
        assert points[0].contributed == money("1000")
        assert points[0].realised_pnl == money("0")
        # ts=1: opening buy, no realisation yet.
        assert points[1].ts_ms == 1
        assert points[1].contributed == money("1000")
        assert points[1].realised_pnl == money("0")
        # ts=5, event first: contributed drops, realised_pnl not yet from the fill.
        assert points[2].ts_ms == 5
        assert points[2].contributed == money("950")
        assert points[2].realised_pnl == money("0")
        assert points[2].value == money("950")
        # ts=5, fill second: contributed unchanged, realised_pnl now reflects the close.
        assert points[3].ts_ms == 5
        assert points[3].contributed == money("950")
        assert points[3].realised_pnl == money("30")
        assert points[3].value == money("980")

    def test_deterministic_regardless_of_input_order(self) -> None:
        """Shuffling the input lists yields the identical output (internal sort)."""
        genesis = make_event(
            event_type=CapitalEventType.FUNDING, amount="1000", ts=0, event_id="E1"
        )
        deposit = make_event(
            event_type=CapitalEventType.DEPOSIT, amount="100", ts=3, event_id="E2"
        )
        f1 = _fill("F1", OrderSide.BUY, "1", "100", ts=1)
        f2 = _fill("F2", OrderSide.SELL, "1", "150", ts=2)

        a = value_series([f1, f2], [genesis, deposit])
        b = value_series([f2, f1], [deposit, genesis])

        assert a == b
