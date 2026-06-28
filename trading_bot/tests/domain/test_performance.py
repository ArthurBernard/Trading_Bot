"""Tests for the pure PnL / KPI performance functions.

The PnL core (returns / position / fee / pnl / cum_pnl / equity) is pure
numpy/Decimal and is fully exercised here. The KPI wrappers delegate to fynance,
an optional ``[triptych]`` dependency that is not importable in every
environment; those tests are gated behind ``pytest.importorskip("fynance")`` so
they **skip** where fynance is absent and **run** in CI / a dev box where it is
installed.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from trading_bot.domain import performance as perf
from trading_bot.domain.errors import TradingBotError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide
from trading_bot.domain.performance import PerformanceDependencyError

BTCUSD = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)


def make_fill(
    *,
    side: OrderSide,
    qty: str,
    price: str,
    fee: str = "0",
    fill_id: str = "T1",
    ts: int = 1,
) -> Fill:
    """Build a test fill with sensible defaults."""
    return Fill(
        fill_id=fill_id,
        client_order_id="cid-1",
        instrument=BTCUSD,
        side=side,
        qty=money(qty),
        price=money(price),
        fee=money(fee),
        ts=ts,
    )


# --------------------------------------------------------------------------- #
# Pure PnL core
# --------------------------------------------------------------------------- #


class TestReturns:
    def test_first_difference_with_zero_head(self) -> None:
        prices = [money("100"), money("110"), money("120")]
        assert perf.returns(prices) == (money("0"), money("10"), money("10"))

    def test_negative_moves(self) -> None:
        prices = [money("100"), money("90"), money("95")]
        assert perf.returns(prices) == (money("0"), money("-10"), money("5"))

    def test_empty(self) -> None:
        assert perf.returns([]) == ()

    def test_single(self) -> None:
        assert perf.returns([money("100")]) == (money("0"),)

    def test_returns_are_exact_decimal(self) -> None:
        prices = [money("0.1"), money("0.3")]
        # 0.3 - 0.1 == 0.2 exactly (would be 0.19999... in float).
        assert perf.returns(prices)[1] == money("0.2")


class TestExchangedVolume:
    def test_unsigned_qty_per_step(self) -> None:
        fills = [
            make_fill(side=OrderSide.BUY, qty="2", price="100", fill_id="A"),
            make_fill(side=OrderSide.SELL, qty="1.5", price="110", fill_id="B"),
        ]
        assert perf.exchanged_volume(fills) == (money("2"), money("1.5"))


class TestPositionSeries:
    def test_held_position_going_into_each_step(self) -> None:
        fills = [
            make_fill(side=OrderSide.BUY, qty="2", price="100", fill_id="A"),
            make_fill(side=OrderSide.SELL, qty="1", price="110", fill_id="B"),
            make_fill(side=OrderSide.SELL, qty="1", price="120", fill_id="C"),
        ]
        # Before each fill: flat, then +2, then +1.
        assert perf.position_series(fills) == (money("0"), money("2"), money("1"))

    def test_initial_position_is_the_head(self) -> None:
        fills = [make_fill(side=OrderSide.BUY, qty="1", price="100")]
        assert perf.position_series(fills, initial=money("3")) == (money("3"),)

    def test_can_go_short(self) -> None:
        fills = [
            make_fill(side=OrderSide.SELL, qty="1", price="100", fill_id="A"),
            make_fill(side=OrderSide.SELL, qty="1", price="110", fill_id="B"),
        ]
        assert perf.position_series(fills) == (money("0"), money("-1"))


class TestFeeSeries:
    def test_passes_through_fill_fee(self) -> None:
        fills = [
            make_fill(side=OrderSide.BUY, qty="1", price="100", fee="0.5", fill_id="A"),
            make_fill(side=OrderSide.SELL, qty="1", price="110", fee="0.6", fill_id="B"),
        ]
        assert perf.fee_series(fills) == (money("0.5"), money("0.6"))


class TestPnLAndEquity:
    """The headline hand-computed scenario.

    v0 = 10000, starting flat. Three fills marked at their own price:

    ===  ====  ===  =====  ===   ====  ========  =======  ========  ======
    step side  qty  price  fee   pos   returns   pnl      cum_pnl   equity
    ===  ====  ===  =====  ===   ====  ========  =======  ========  ======
    0    BUY   2    100    1     0     0          -1       -1       9999
    1    SELL  1    110    2     2     10         18       17       10017
    2    SELL  1    120    1     1     10         9        26       10026
    ===  ====  ===  =====  ===   ====  ========  =======  ========  ======

    pnl_t = position_t * returns_t - fee_t.
    """

    fills = [
        make_fill(side=OrderSide.BUY, qty="2", price="100", fee="1", fill_id="A"),
        make_fill(side=OrderSide.SELL, qty="1", price="110", fee="2", fill_id="B"),
        make_fill(side=OrderSide.SELL, qty="1", price="120", fee="1", fill_id="C"),
    ]
    prices = [money("100"), money("110"), money("120")]

    def test_pnl(self) -> None:
        assert perf.pnl(self.fills, self.prices) == (
            money("-1"),
            money("18"),
            money("9"),
        )

    def test_cum_pnl(self) -> None:
        assert perf.cum_pnl(self.fills, self.prices) == (
            money("-1"),
            money("17"),
            money("26"),
        )

    def test_equity_curve_with_v0(self) -> None:
        assert perf.equity_curve(self.fills, self.prices, v0=money("10000")) == (
            money("9999"),
            money("10017"),
            money("10026"),
        )

    def test_equity_endpoint(self) -> None:
        equity = perf.equity_curve(self.fills, self.prices, v0=money("10000"))
        assert equity[-1] == money("10026")

    def test_equity_without_v0_is_cum_pnl(self) -> None:
        assert perf.equity_curve(self.fills, self.prices) == perf.cum_pnl(
            self.fills, self.prices
        )

    def test_equity_array_is_float64(self) -> None:
        equity = perf.equity_curve(self.fills, self.prices, v0=money("10000"))
        arr = perf.equity_array(equity)
        assert arr.dtype == np.float64
        assert arr.tolist() == [9999.0, 10017.0, 10026.0]


class TestFeeImpact:
    """A non-zero fee strictly reduces PnL by exactly the fee total."""

    fills_template = [
        (OrderSide.BUY, "2", "100"),
        (OrderSide.SELL, "1", "110"),
        (OrderSide.SELL, "1", "120"),
    ]
    prices = [money("100"), money("110"), money("120")]

    def _build(self, fee: str) -> list[Fill]:
        return [
            make_fill(side=s, qty=q, price=p, fee=fee, fill_id=f"F{i}")
            for i, (s, q, p) in enumerate(self.fills_template)
        ]

    def test_fee_reduces_each_step(self) -> None:
        no_fee = perf.pnl(self._build("0"), self.prices)
        with_fee = perf.pnl(self._build("3"), self.prices)
        for clean, charged in zip(no_fee, with_fee, strict=True):
            assert charged == clean - money("3")

    def test_fee_reduces_terminal_equity_by_total_fees(self) -> None:
        no_fee = perf.equity_curve(self._build("0"), self.prices, v0=money("10000"))
        with_fee = perf.equity_curve(self._build("3"), self.prices, v0=money("10000"))
        # Three fills, fee 3 each → 9 total off the endpoint.
        assert with_fee[-1] == no_fee[-1] - money("9")


class TestInitialPosition:
    def test_carried_position_earns_first_move(self) -> None:
        # Start already long 5; the position held into step 1 is 5 + the first
        # fill's signed qty (here SELL 5 ⇒ flat). So step-1 PnL = 5 * (+10).
        fills = [
            make_fill(side=OrderSide.SELL, qty="5", price="100", fill_id="A"),
            make_fill(side=OrderSide.BUY, qty="1", price="110", fill_id="B"),
        ]
        prices = [money("100"), money("110")]
        steps = perf.pnl(fills, prices, initial_position=money("5"))
        # pos series: [5, 0]; step 1 held 0 ⇒ pnl 0.
        assert perf.position_series(fills, initial=money("5")) == (
            money("5"),
            money("0"),
        )
        assert steps[1] == money("0")

    def test_initial_position_alone_earns_the_move(self) -> None:
        # A single fill that does not change the carried exposure before its
        # own step: the head position is the initial one, earning its move.
        fills = [
            make_fill(side=OrderSide.SELL, qty="2", price="120", fill_id="A"),
        ]
        prices = [money("120")]
        # Only one step (returns[0] == 0), so PnL is just -fee (zero here).
        assert perf.pnl(fills, prices, initial_position=money("5")) == (money("0"),)


class TestValidation:
    def test_length_mismatch_raises(self) -> None:
        fills = [make_fill(side=OrderSide.BUY, qty="1", price="100")]
        with pytest.raises(ValueError, match="equal length"):
            perf.pnl(fills, [money("100"), money("110")])

    def test_empty_inputs_are_empty(self) -> None:
        assert perf.pnl([], []) == ()
        assert perf.cum_pnl([], []) == ()
        assert perf.equity_curve([], []) == ()


class TestDeterminism:
    def test_pure_decimal_no_float_drift(self) -> None:
        # Tricky decimals that float would mangle.
        fills = [
            make_fill(side=OrderSide.BUY, qty="0.3", price="0.1", fill_id="A"),
            make_fill(side=OrderSide.SELL, qty="0.1", price="0.3", fill_id="B"),
        ]
        prices = [money("0.1"), money("0.3")]
        # pos at step 1 = 0.3; move = 0.3 - 0.1 = 0.2 → pnl 0.06 exactly.
        result = perf.pnl(fills, prices)
        assert result[1] == money("0.06")
        assert isinstance(result[1], Decimal)


# --------------------------------------------------------------------------- #
# KPI wrappers — require fynance (skip cleanly where absent)
# --------------------------------------------------------------------------- #


class TestKPIParity:
    """KPI wrappers must match calling fynance directly on the same float array.

    Gated behind ``importorskip`` so this whole class skips when fynance is not
    installed (e.g. the sandbox without numba) and runs in CI / a dev box.
    """

    # A known equity path with both gains and a drawdown.
    equity = [
        money("100"),
        money("110"),
        money("105"),
        money("120"),
        money("160"),
        money("108"),
    ]

    def test_sharpe_parity(self) -> None:
        fy = pytest.importorskip("fynance")
        x = perf.equity_array(self.equity)
        assert perf.sharpe(self.equity, period=12) == pytest.approx(
            float(fy.metrics.sharpe(x, rf=0, period=12, log=False))
        )

    def test_sortino_parity(self) -> None:
        fy = pytest.importorskip("fynance")
        x = perf.equity_array(self.equity)
        assert perf.sortino(self.equity, period=12) == pytest.approx(
            float(fy.metrics.sortino(x, rf=0, period=12, log=False))
        )

    def test_max_drawdown_parity(self) -> None:
        fy = pytest.importorskip("fynance")
        x = perf.equity_array(self.equity)
        assert perf.max_drawdown(self.equity) == pytest.approx(
            float(fy.metrics.mdd(x, raw=False))
        )

    def test_calmar_parity(self) -> None:
        fy = pytest.importorskip("fynance")
        x = perf.equity_array(self.equity)
        assert perf.calmar(self.equity, period=12) == pytest.approx(
            float(fy.metrics.calmar(x, period=12))
        )

    def test_accepts_float_array_directly(self) -> None:
        pytest.importorskip("fynance")
        arr = perf.equity_array(self.equity)
        assert perf.sharpe(arr, period=12) == perf.sharpe(self.equity, period=12)

    def test_max_drawdown_value(self) -> None:
        pytest.importorskip("fynance")
        # Peak 160 → trough 108 ⇒ mdd = 1 - 108/160 = 0.325.
        assert perf.max_drawdown(self.equity) == pytest.approx(0.325)


class TestKPIMissingDependency:
    """When fynance is absent the wrappers raise a clear domain error."""

    def test_raises_performance_dependency_error_without_fynance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the dependency being unavailable regardless of the env by
        # making ``import fynance.metrics`` fail.
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name.startswith("fynance"):
                raise ImportError("no fynance in this environment")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        equity = [money("100"), money("110"), money("120")]
        with pytest.raises(PerformanceDependencyError, match="sharpe"):
            perf.sharpe(equity)
        with pytest.raises(TradingBotError):
            perf.sortino(equity)
        with pytest.raises(PerformanceDependencyError, match="max_drawdown"):
            perf.max_drawdown(equity)
        with pytest.raises(PerformanceDependencyError, match="calmar"):
            perf.calmar(equity)
