"""Tests for the :class:`~trading_bot.application.performance_service.PerformanceService`.

These prove the service's read-side performance contract:

* a known fill sequence (buy, add, partial close, **flip**, across one *and* two
  instruments) folds to ``realised_pnl()`` / ``fees_paid()`` that match
  hand-computed ``Decimal`` values and equal the sum of
  :meth:`~trading_bot.domain.position.Position.from_fills` per instrument;
* ``equity_curve()`` matches a hand-built ``v0 + cumulative realised PnL`` series;
* the KPI methods delegate to :mod:`trading_bot.domain.performance` over the
  equity curve and equal calling those functions directly (they **run** — fynance
  is installed in the venv);
* the short-series policy: 0 or 1 fills -> every KPI returns ``0.0``, no raise;
* ``EventBus`` subscription drives the view (other events are ignored);
* an end-to-end run where an
  :class:`~trading_bot.application.order_router.OrderRouter` submits to a
  :class:`~trading_bot.brokers.paper.PaperBroker` whose emitted fills update the
  service, with realised PnL + equity endpoint + KPIs checked against an
  independent computation from the broker's reported fills.

Async tests run un-decorated (``asyncio_mode = "auto"``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.application import (
    EventBus,
    FillEvent,
    LogEvent,
    OrderRouter,
    PerformanceService,
)
from trading_bot.brokers import PaperBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    Position,
    Symbol,
    money,
)
from trading_bot.domain import performance as perf

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _fill(
    *,
    fill_id: str,
    side: OrderSide,
    qty: str,
    price: str,
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


# --- empty / short state --------------------------------------------------- #


def test_empty_service_is_flat() -> None:
    """A fresh service has zero PnL/fees, an empty curve, and no positions."""
    svc = PerformanceService(v0=money("1000"))
    assert svc.realised_pnl() == Decimal("0")
    assert svc.fees_paid() == Decimal("0")
    assert svc.equity_curve() == ()
    assert svc.position(BTC_USD) is None


def test_short_series_kpis_return_zero_no_raise() -> None:
    """0 or 1 fills (< 2 equity points) -> every KPI is the safe value 0.0."""
    svc = PerformanceService(v0=money("1000"))
    # Zero fills.
    assert svc.sharpe() == 0.0
    assert svc.sortino() == 0.0
    assert svc.max_drawdown() == 0.0
    assert svc.calmar() == 0.0

    # One fill -> one equity point -> still no return to measure.
    svc.apply(_fill(fill_id="T1", side=OrderSide.BUY, qty="1", price="30000", fee="6"))
    assert len(svc.equity_curve()) == 1
    assert svc.sharpe() == 0.0
    assert svc.sortino() == 0.0
    assert svc.max_drawdown() == 0.0
    assert svc.calmar() == 0.0


# --- realised PnL / fees / equity over a known single-instrument sequence --- #


def test_single_instrument_pnl_fees_equity_hand_computed() -> None:
    """buy -> add -> partial close -> flip: PnL/fees/equity match by hand.

    Sequence (all BTC/USD), fee 0 to keep the gross numbers clean except where
    noted:

    * F1 BUY  2 @ 30000 fee 6   -> long 2 @ 30000, realised -6 (fee)
    * F2 BUY  1 @ 33000 fee 3   -> long 3 @ 31000, realised -9 (fees)
    * F3 SELL 1 @ 35000 fee 0   -> close 1 of the long: (35000-31000)*1 = +4000
                                   realised -9 + 4000 = 3991
    * F4 SELL 4 @ 36000 fee 0   -> flip: close remaining long 2:
                                   (36000-31000)*2 = +10000; realised 3991+10000
                                   = 13991; remainder 2 opens short @ 36000.
    """
    fills = [
        _fill(fill_id="F1", side=OrderSide.BUY, qty="2", price="30000", fee="6"),
        _fill(fill_id="F2", side=OrderSide.BUY, qty="1", price="33000", fee="3"),
        _fill(fill_id="F3", side=OrderSide.SELL, qty="1", price="35000", fee="0"),
        _fill(fill_id="F4", side=OrderSide.SELL, qty="4", price="36000", fee="0"),
    ]
    svc = PerformanceService(v0=money("100000"))
    for f in fills:
        svc.apply(f)

    # Hand-computed totals.
    assert svc.realised_pnl() == Decimal("13991")
    assert svc.fees_paid() == Decimal("9")

    # Consistent with Position.from_fills over the whole instrument sequence.
    pos = Position.from_fills(fills)
    assert svc.realised_pnl() == pos.realised_pnl
    assert svc.fees_paid() == pos.fees_paid

    # Equity curve = v0 + cumulative realised PnL, one point per fill.
    # Running realised PnL: -6, -9, 3991, 13991.
    assert svc.equity_curve() == (
        Decimal("99994"),   # 100000 - 6
        Decimal("99991"),   # 100000 - 9
        Decimal("103991"),  # 100000 + 3991
        Decimal("113991"),  # 100000 + 13991
    )

    # position() reflects the flipped short remainder.
    p = svc.position(BTC_USD)
    assert p is not None
    assert p.net_qty == Decimal("-2")
    assert p.avg_entry_price == Decimal("36000")


# --- aggregate across multiple instruments --------------------------------- #


def test_multi_instrument_aggregate_equals_sum_of_positions() -> None:
    """Two instruments interleaved: aggregate = sum of per-instrument folds."""
    btc = [
        _fill(fill_id="B1", side=OrderSide.BUY, qty="2", price="30000", fee="6",
              instrument=BTC_USD, cid="btc"),
        _fill(fill_id="B2", side=OrderSide.SELL, qty="1", price="31000", fee="3.1",
              instrument=BTC_USD, cid="btc"),
    ]
    eth = [
        _fill(fill_id="E1", side=OrderSide.BUY, qty="10", price="2000", fee="2",
              instrument=ETH_USD, cid="eth"),
        _fill(fill_id="E2", side=OrderSide.SELL, qty="10", price="2100", fee="2.1",
              instrument=ETH_USD, cid="eth"),
    ]
    svc = PerformanceService(v0=money("0"))
    # Interleave to prove arrival order per instrument is what is folded.
    svc.apply(btc[0])
    svc.apply(eth[0])
    svc.apply(btc[1])
    svc.apply(eth[1])

    btc_pos = Position.from_fills(btc)
    eth_pos = Position.from_fills(eth)
    expected_realised = btc_pos.realised_pnl + eth_pos.realised_pnl
    expected_fees = btc_pos.fees_paid + eth_pos.fees_paid

    assert svc.realised_pnl() == expected_realised
    assert svc.fees_paid() == expected_fees

    # Per-instrument positions exposed independently and exactly.
    p_btc = svc.position(BTC_USD)
    p_eth = svc.position(ETH_USD)
    assert p_btc is not None and p_btc.realised_pnl == btc_pos.realised_pnl
    assert p_eth is not None and p_eth.realised_pnl == eth_pos.realised_pnl

    # Equity curve: v0 + running aggregate realised PnL after each global fill.
    # After B1: btc -6.
    # After E1: btc -6, eth -2 -> -8.
    # After B2: btc closes 1: (31000-30000)*1=+1000, fees -6-3.1 -> 990.9; eth -2
    #           -> 988.9.
    # After E2: eth closes 10: (2100-2000)*10=+1000, fees -2-2.1 -> 995.9; btc
    #           990.9 -> 1986.8.
    assert svc.equity_curve() == (
        Decimal("-6"),
        Decimal("-8"),
        Decimal("988.9"),
        Decimal("1986.8"),
    )
    # Endpoint equals total realised PnL (v0 = 0).
    assert svc.equity_curve()[-1] == svc.realised_pnl()


# --- KPIs delegate to domain.performance over the equity curve ------------- #


def test_kpis_equal_domain_performance_over_equity_curve() -> None:
    """KPI methods equal calling domain.performance directly on the curve.

    A monotonic-up equity curve gives a finite, strictly-positive Sharpe and a
    zero max drawdown — concrete assertions that prove the wrappers ran fynance.
    """
    pytest.importorskip("fynance")  # KPI ratios delegate to fynance
    # A win, a win, a bigger win across one instrument (all closes from flat).
    fills = [
        _fill(fill_id="F1", side=OrderSide.BUY, qty="1", price="100", fee="0"),
        _fill(fill_id="F2", side=OrderSide.SELL, qty="1", price="110", fee="0"),
        _fill(fill_id="F3", side=OrderSide.BUY, qty="1", price="110", fee="0"),
        _fill(fill_id="F4", side=OrderSide.SELL, qty="1", price="125", fee="0"),
        _fill(fill_id="F5", side=OrderSide.BUY, qty="1", price="125", fee="0"),
        _fill(fill_id="F6", side=OrderSide.SELL, qty="1", price="150", fee="0"),
    ]
    svc = PerformanceService(v0=money("1000"))
    for f in fills:
        svc.apply(f)

    curve = svc.equity_curve()
    # Realised PnL steps: 0 (open), +10, 0 (open), +15, 0 (open), +25 -> equity
    # 1000, 1010, 1010, 1025, 1025, 1050 (monotonic non-decreasing).
    assert curve == (
        Decimal("1000"),
        Decimal("1010"),
        Decimal("1010"),
        Decimal("1025"),
        Decimal("1025"),
        Decimal("1050"),
    )

    # Each KPI method equals the domain function over the same curve.
    assert svc.sharpe() == perf.sharpe(curve)
    assert svc.sortino() == perf.sortino(curve)
    assert svc.max_drawdown() == perf.max_drawdown(curve)
    assert svc.calmar() == perf.calmar(curve)

    # Concrete properties of a non-decreasing curve.
    assert svc.sharpe() > 0.0
    import math

    assert math.isfinite(svc.sharpe())
    assert svc.max_drawdown() == 0.0


def test_max_drawdown_nonzero_on_dip() -> None:
    """A curve that dips has a positive fractional max drawdown matching domain."""
    pytest.importorskip("fynance")  # max_drawdown delegates to fynance
    fills = [
        # +10 (peak), then -30 (dip), then +50 (recover).
        _fill(fill_id="F1", side=OrderSide.BUY, qty="1", price="100", fee="0"),
        _fill(fill_id="F2", side=OrderSide.SELL, qty="1", price="110", fee="0"),  # +10
        _fill(fill_id="F3", side=OrderSide.BUY, qty="1", price="110", fee="0"),
        _fill(fill_id="F4", side=OrderSide.SELL, qty="1", price="80", fee="0"),   # -30
        _fill(fill_id="F5", side=OrderSide.BUY, qty="1", price="80", fee="0"),
        _fill(fill_id="F6", side=OrderSide.SELL, qty="1", price="130", fee="0"),  # +50
    ]
    svc = PerformanceService(v0=money("1000"))
    for f in fills:
        svc.apply(f)
    curve = svc.equity_curve()
    assert svc.max_drawdown() == perf.max_drawdown(curve)
    assert svc.max_drawdown() > 0.0


# --- EventBus subscription -------------------------------------------------- #


def test_event_bus_subscription_drives_view() -> None:
    """Emitting FillEvents updates the performance view; other events ignored."""
    bus = EventBus()
    svc = PerformanceService(v0=money("100000"), event_bus=bus)

    bus.emit(LogEvent(message="noise"))  # ignored
    bus.emit(FillEvent(_fill(fill_id="F1", side=OrderSide.BUY, qty="1",
                             price="30000", fee="3")))
    bus.emit(FillEvent(_fill(fill_id="F2", side=OrderSide.SELL, qty="1",
                             price="31000", fee="3.1")))

    # close 1: (31000-30000)*1 = +1000; fees -3 -3.1 -> realised 993.9.
    assert svc.realised_pnl() == Decimal("993.9")
    assert svc.fees_paid() == Decimal("6.1")
    assert svc.equity_curve() == (Decimal("99997"), Decimal("100993.9"))


# --- verification on real data: OrderRouter -> PaperBroker -> service ------- #


async def test_end_to_end_router_paperbroker_drives_performance() -> None:
    """End-to-end: router -> PaperBroker -> bus -> PerformanceService.

    A realistic buy -> add -> partial-close sequence is routed through the *real*
    ``PaperBroker`` (which emits a ``FillEvent`` per simulated fill). The service,
    subscribed to the bus, must report:

    * realised PnL == sum of ``Position.from_fills`` over the broker's reported
      fills (the source of truth);
    * an equity endpoint == ``v0`` + that realised PnL;
    * KPIs == ``domain.performance`` over the service's equity curve (they run —
      fynance present).
    """
    pytest.importorskip("fynance")  # KPI ratios delegate to fynance
    bus = EventBus()
    svc = PerformanceService(v0=money("1000000"), event_bus=bus)
    broker = PaperBroker(
        fill_model="immediate",
        starting_balances={"USD": money("5000000"), "BTC": money("0")},
        event_bus=bus,
    )
    router = OrderRouter(broker, bus)

    def _limit(cid: str, side: OrderSide, qty: str, price: str) -> Order:
        return Order(
            client_order_id=cid,
            instrument=BTC_USD,
            side=side,
            qty=money(qty),
            type=OrderType.LIMIT,
            limit_price=money(price),
        )

    await router.submit(_limit("buy-1", OrderSide.BUY, "2", "30000"))
    await router.submit(_limit("buy-2", OrderSide.BUY, "1", "31500"))
    await router.submit(_limit("sell-1", OrderSide.SELL, "1", "33000"))

    # The broker is the source of truth for the fills.
    broker_fills = await broker.fills()
    assert len(broker_fills) == 3

    # Independent computation from exactly those fills.
    expected_pos = Position.from_fills(broker_fills)
    assert svc.realised_pnl() == expected_pos.realised_pnl
    assert svc.fees_paid() == expected_pos.fees_paid

    # Equity endpoint = v0 + realised PnL.
    curve = svc.equity_curve()
    assert len(curve) == 3
    assert curve[-1] == money("1000000") + expected_pos.realised_pnl

    # KPIs equal domain.performance over the same curve (they ran).
    assert svc.sharpe() == perf.sharpe(curve)
    assert svc.max_drawdown() == perf.max_drawdown(curve)
    # max drawdown is a finite fraction in [0, 1].
    assert 0.0 <= svc.max_drawdown() <= 1.0


# --- fill-id dedup (a re-emitted execution never corrupts realised PnL) ----- #


def test_apply_dedups_by_fill_id() -> None:
    """A re-applied fill (seen ``fill_id``) leaves PnL/fees/equity unchanged."""
    svc = PerformanceService(v0=money("1000"))
    svc.apply(_fill(fill_id="F1", side=OrderSide.BUY, qty="1", price="100", fee="1"))
    svc.apply(_fill(fill_id="F2", side=OrderSide.SELL, qty="1", price="110", fee="1"))
    realised = svc.realised_pnl()
    fees = svc.fees_paid()
    points = len(svc.equity_curve())

    # The SELL execution is re-emitted (same fill_id) — must be ignored.
    svc.apply(_fill(fill_id="F2", side=OrderSide.SELL, qty="1", price="110", fee="1"))
    assert svc.realised_pnl() == realised  # not double-counted
    assert svc.fees_paid() == fees
    assert len(svc.equity_curve()) == points  # no spurious equity point


def test_subscribed_service_dedups_reemitted_fill_event() -> None:
    """A re-emitted ``FillEvent`` (same ``fill_id``) does not corrupt realised PnL."""
    bus = EventBus()
    svc = PerformanceService(v0=money("1000"), event_bus=bus)
    bus.emit(FillEvent(_fill(fill_id="F1", side=OrderSide.BUY, qty="1", price="100",
                             fee="1")))
    sell = FillEvent(_fill(fill_id="F2", side=OrderSide.SELL, qty="1", price="110",
                           fee="1"))
    bus.emit(sell)
    bus.emit(sell)  # the venue re-emits the same execution after a reconnect
    # Realised PnL == one BUY 1@100 (fee 1) then one SELL 1@110 (fee 1): +10 - 2 = 8.
    assert svc.realised_pnl() == Decimal("8")
    assert svc.fees_paid() == Decimal("2")
