"""Tests for the in-process :class:`~trading_bot.brokers.paper.PaperBroker`.

The paper broker *is* the engine's "real data": there is no network, the
simulation is fully deterministic, and every amount is an exact
:class:`~decimal.Decimal`. These tests assert the fill, fee and balance models
exactly (hand-computed), exercise both fill models, cover the capability set and
ticker, and run a realistic buy -> partial -> sell-to-flat sequence end to end.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.brokers import Broker, BrokerError, Capability, PaperBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    MissingOrder,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    money,
)

BTC_USD = Instrument(Symbol("BTC", "USD"))
ETH_USD = Instrument(Symbol("ETH", "USD"))


def _limit_buy(
    qty: str = "1", price: str = "30000", cid: str = "cid-1"
) -> Order:
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(price),
    )


def _limit_sell(
    qty: str = "1", price: str = "30000", cid: str = "cid-s"
) -> Order:
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.SELL,
        qty=money(qty),
        type=OrderType.LIMIT,
        limit_price=money(price),
    )


def _market_buy(qty: str = "1", cid: str = "cid-m") -> Order:
    return Order(
        client_order_id=cid,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money(qty),
        type=OrderType.MARKET,
    )


# --- the adapter satisfies the port ---------------------------------------- #


def test_paper_satisfies_broker_protocol() -> None:
    """:class:`PaperBroker` is a :class:`Broker` structurally."""
    assert isinstance(PaperBroker(), Broker)


def test_name_is_paper() -> None:
    """The venue key is ``"paper"``."""
    assert PaperBroker().name == "paper"


def test_capabilities_declares_six_ops() -> None:
    """The declared capabilities are exactly the six in-process operations."""
    assert PaperBroker().capabilities() == {
        Capability.PLACE_ORDER,
        Capability.CANCEL,
        Capability.OPEN_ORDERS,
        Capability.BALANCES,
        Capability.FILLS,
        Capability.TICKER,
    }
    assert Capability.PRIVATE_WS not in PaperBroker().capabilities()


# --- immediate-fill LIMIT buy ---------------------------------------------- #


async def test_immediate_limit_buy_one_fill_at_limit_price() -> None:
    """A LIMIT buy fully fills in one :class:`Fill` at the limit price."""
    broker = PaperBroker(starting_balances={"USD": money("100000")})
    order = _limit_buy(qty="2", price="30000")

    venue_order_id = await broker.place_order(order)

    assert venue_order_id == "PAPER-1"
    assert order.status is OrderStatus.FILLED
    assert await broker.open_orders() == []

    fills = await broker.fills()
    assert len(fills) == 1
    fill = fills[0]
    assert isinstance(fill, Fill)
    assert fill.side is OrderSide.BUY
    assert fill.qty == Decimal("2")
    assert fill.price == Decimal("30000")
    # 30000 * 2 * 10 / 10000 = 60.
    assert fill.fee == Decimal("60")


async def test_immediate_limit_buy_moves_balances_exactly() -> None:
    """Balances move by +qty (base) and -(notional + fee) (quote), exact."""
    broker = PaperBroker(starting_balances={"USD": money("100000")})
    await broker.place_order(_limit_buy(qty="2", price="30000"))

    balances = await broker.balances()
    # USD: 100000 - (30000*2) - 60 = 39940. BTC: 0 + 2 = 2.
    assert balances["USD"] == Decimal("39940")
    assert balances["BTC"] == Decimal("2")
    assert all(isinstance(v, Decimal) for v in balances.values())


async def test_immediate_limit_sell_moves_balances_exactly() -> None:
    """A SELL credits quote (notional - fee) and debits base, exact."""
    broker = PaperBroker(
        starting_balances={"USD": money("0"), "BTC": money("5")}
    )
    await broker.place_order(_limit_sell(qty="2", price="30000"))

    balances = await broker.balances()
    # USD: 0 + 30000*2 - 60 = 59940. BTC: 5 - 2 = 3.
    assert balances["USD"] == Decimal("59940")
    assert balances["BTC"] == Decimal("3")


# --- MARKET fills at the injected mark ------------------------------------- #


async def test_market_order_fills_at_injected_mark_price() -> None:
    """A MARKET order fills at the injected mark price for its instrument."""
    broker = PaperBroker(
        prices={BTC_USD: money("31000")},
        starting_balances={"USD": money("100000")},
    )
    order = _market_buy(qty="1")

    await broker.place_order(order)

    fills = await broker.fills()
    assert len(fills) == 1
    assert fills[0].price == Decimal("31000")
    # 31000 * 1 * 10 / 10000 = 31.
    assert fills[0].fee == Decimal("31")
    assert order.status is OrderStatus.FILLED


async def test_market_order_uses_set_price_hook() -> None:
    """:meth:`set_price` drives the mark a later MARKET order fills at."""
    broker = PaperBroker(starting_balances={"USD": money("100000")})
    broker.set_price(BTC_USD, money("28000"))

    await broker.place_order(_market_buy(qty="1"))

    assert (await broker.fills())[0].price == Decimal("28000")


async def test_market_order_without_mark_raises() -> None:
    """A MARKET order with no injected mark raises :class:`BrokerError`."""
    broker = PaperBroker()
    with pytest.raises(BrokerError):
        await broker.place_order(_market_buy())


# --- partial fill model ----------------------------------------------------- #


async def test_partial_model_multiple_fills_summing_to_qty_and_closes() -> None:
    """``"partial"`` emits multiple fills summing to qty; the order closes."""
    broker = PaperBroker(
        fill_model="partial",
        partial_chunks=2,
        starting_balances={"USD": money("100000")},
    )
    order = _limit_buy(qty="2", price="30000")

    await broker.place_order(order)

    fills = await broker.fills()
    assert len(fills) == 2
    assert sum((f.qty for f in fills), Decimal("0")) == Decimal("2")
    # Equal slices of 1.0 each.
    assert [f.qty for f in fills] == [Decimal("1"), Decimal("1")]
    assert order.status is OrderStatus.FILLED
    assert await broker.open_orders() == []


async def test_partial_model_remainder_left_open() -> None:
    """A sub-unit ``partial_fill_ratio`` leaves a partially-filled order open."""
    broker = PaperBroker(
        fill_model="partial",
        partial_chunks=2,
        partial_fill_ratio=money("0.5"),
        starting_balances={"USD": money("100000")},
    )
    order = _limit_buy(qty="2", price="30000")

    venue_order_id = await broker.place_order(order)

    # Half of 2.0 = 1.0 filled, across 2 chunks of 0.5.
    fills = await broker.fills()
    assert sum((f.qty for f in fills), Decimal("0")) == Decimal("1")
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == Decimal("1")
    assert order.remaining_qty == Decimal("1")

    open_orders = await broker.open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].venue_order_id == venue_order_id


async def test_partial_slices_sum_exactly_with_remainder_chunk() -> None:
    """Slices sum *exactly* to qty even when it does not divide evenly."""
    broker = PaperBroker(
        fill_model="partial",
        partial_chunks=3,
        starting_balances={"USD": money("100000")},
    )
    order = _limit_buy(qty="1", price="30000")

    await broker.place_order(order)

    fills = await broker.fills()
    assert len(fills) == 3
    assert sum((f.qty for f in fills), Decimal("0")) == Decimal("1")
    assert order.status is OrderStatus.FILLED


# --- cancel ----------------------------------------------------------------- #


async def test_cancel_removes_open_partial_order() -> None:
    """:meth:`cancel_order` removes a live (partially-filled) open order."""
    broker = PaperBroker(
        fill_model="partial",
        partial_fill_ratio=money("0.5"),
        starting_balances={"USD": money("100000")},
    )
    venue_order_id = await broker.place_order(_limit_buy(qty="2", price="30000"))
    assert len(await broker.open_orders()) == 1

    await broker.cancel_order(venue_order_id)

    assert await broker.open_orders() == []


async def test_cancel_unknown_order_raises_missing_order() -> None:
    """Cancelling an unknown id raises :class:`MissingOrder`."""
    broker = PaperBroker()
    with pytest.raises(MissingOrder):
        await broker.cancel_order("PAPER-404")


async def test_cancel_filled_order_raises_missing_order() -> None:
    """A fully-filled order is not open, so cancelling it raises."""
    broker = PaperBroker(starting_balances={"USD": money("100000")})
    venue_order_id = await broker.place_order(_limit_buy(qty="1", price="30000"))

    with pytest.raises(MissingOrder):
        await broker.cancel_order(venue_order_id)


# --- ticker ----------------------------------------------------------------- #


async def test_ticker_returns_injected_price() -> None:
    """:meth:`ticker` returns the injected price as an exact ``Decimal``."""
    broker = PaperBroker(prices={BTC_USD: money("30100.5")})
    price = await broker.ticker(BTC_USD)
    assert price == Decimal("30100.5")
    assert isinstance(price, Decimal)


async def test_ticker_unknown_instrument_raises() -> None:
    """:meth:`ticker` for an un-priced instrument raises :class:`BrokerError`."""
    broker = PaperBroker(prices={BTC_USD: money("30000")})
    with pytest.raises(BrokerError):
        await broker.ticker(ETH_USD)


# --- fee model -------------------------------------------------------------- #


async def test_fee_is_configured_basis_points() -> None:
    """The fee is exactly ``fee_bps`` of the fill notional (e.g. 25 bps)."""
    broker = PaperBroker(
        fee_bps=money("25"), starting_balances={"USD": money("100000")}
    )
    await broker.place_order(_limit_buy(qty="1", price="40000"))

    # 40000 * 1 * 25 / 10000 = 100.
    assert (await broker.fills())[0].fee == Decimal("100")


async def test_zero_fee_model() -> None:
    """``fee_bps=0`` produces fee-free fills."""
    broker = PaperBroker(
        fee_bps=money("0"), starting_balances={"USD": money("100000")}
    )
    await broker.place_order(_limit_buy(qty="1", price="30000"))

    fills = await broker.fills()
    assert fills[0].fee == Decimal("0")
    # USD: 100000 - 30000 - 0 = 70000.
    assert (await broker.balances())["USD"] == Decimal("70000")


# --- determinism & fills filtering ----------------------------------------- #


async def test_order_ids_are_deterministic_and_monotonic() -> None:
    """Synthetic order ids are ``PAPER-1``, ``PAPER-2``, ... in placement order."""
    broker = PaperBroker(starting_balances={"USD": money("1000000")})
    id1 = await broker.place_order(_limit_buy(qty="1", price="30000", cid="a"))
    id2 = await broker.place_order(_limit_buy(qty="1", price="30000", cid="b"))
    assert (id1, id2) == ("PAPER-1", "PAPER-2")


async def test_fills_since_ms_filters() -> None:
    """``fills(since_ms=...)`` returns only fills at/after the bound."""
    broker = PaperBroker(starting_balances={"USD": money("1000000")})
    await broker.place_order(_limit_buy(qty="1", price="30000", cid="a"))
    await broker.place_order(_limit_buy(qty="1", price="30000", cid="b"))

    all_fills = await broker.fills()
    assert len(all_fills) == 2
    # The default clock advances +1ms per fill, so filtering on the 2nd fill's
    # ts drops the 1st.
    second_ts = all_fills[1].ts
    later = await broker.fills(since_ms=second_ts)
    assert [f.fill_id for f in later] == [all_fills[1].fill_id]


async def test_injected_clock_stamps_fills() -> None:
    """An injected clock supplies the fill timestamps deterministically."""
    broker = PaperBroker(
        starting_balances={"USD": money("100000")},
        clock=lambda: 1_700_000_000_000,
    )
    await broker.place_order(_limit_buy(qty="1", price="30000"))
    assert (await broker.fills())[0].ts == 1_700_000_000_000


# --- construction validation ------------------------------------------------ #


def test_unknown_fill_model_raises() -> None:
    """An unknown ``fill_model`` raises at construction."""
    with pytest.raises(BrokerError):
        PaperBroker(fill_model="instant")


def test_negative_fee_bps_raises() -> None:
    """A negative ``fee_bps`` raises at construction."""
    with pytest.raises(BrokerError):
        PaperBroker(fee_bps=money("-1"))


def test_bad_partial_fill_ratio_raises() -> None:
    """A ``partial_fill_ratio`` outside ``(0, 1]`` raises at construction."""
    with pytest.raises(BrokerError):
        PaperBroker(partial_fill_ratio=money("0"))
    with pytest.raises(BrokerError):
        PaperBroker(partial_fill_ratio=money("1.5"))


# --- verification on "real data": buy -> partial -> sell-to-flat ----------- #


async def test_realistic_sequence_matches_hand_computed_decimals() -> None:
    """A realistic order sequence reconciles to hand-computed Decimal values.

    One balance-threaded ``immediate`` broker (BTC/USD, 10 bps fee, start
    USD=100000, BTC=0). The middle leg is armed (``arm_partial``) to fill only
    half its quantity, leaving a live remainder — exactly the buy -> partial ->
    sell-to-flat shape the engine sees:

    1. BUY 1.0 @ limit 30000 (fully filled):
       notional 30000, fee 30000*1*10/10000 = 30.
       USD -> 100000 - 30000 - 30 = 69970 ; BTC -> 1.0
    2. BUY 2.0 @ limit 31000, armed partial ratio 0.5 (fills 1.0; the immediate
       model slices it as one fill): notional 31000, fee 31.
       USD -> 69970 - 31000 - 31 = 38939 ; BTC -> 2.0 ; 1.0 left open.
    3. SELL 2.0 @ limit 29000 (sell-to-flat):
       notional 58000, fee 58000*10/10000 = 58.
       USD -> 38939 + 58000 - 58 = 96881 ; BTC -> 0.0

    Final: USD = 96881, BTC = 0. Total fees = 30 + 31 + 58 = 119.
    """
    broker = PaperBroker(
        fill_model="immediate",
        starting_balances={"USD": money("100000"), "BTC": money("0")},
    )

    # Leg 1: full buy of 1.0 @ 30000.
    await broker.place_order(_limit_buy(qty="1", price="30000", cid="buy-1"))
    bal = await broker.balances()
    assert bal["USD"] == Decimal("69970")
    assert bal["BTC"] == Decimal("1")

    # Leg 2: a partial buy of 2.0 @ 31000 — armed to fill half (1.0); the other
    # 1.0 stays open.
    broker.arm_partial(money("0.5"))
    vid2 = await broker.place_order(
        _limit_buy(qty="2", price="31000", cid="buy-2")
    )
    bal = await broker.balances()
    assert bal["USD"] == Decimal("38939")
    assert bal["BTC"] == Decimal("2")
    open_orders = await broker.open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].venue_order_id == vid2
    assert open_orders[0].remaining_qty == Decimal("1")

    # Leg 3: sell 2.0 @ 29000 to flatten BTC.
    await broker.place_order(_limit_sell(qty="2", price="29000", cid="sell-1"))
    bal = await broker.balances()
    assert bal["USD"] == Decimal("96881")
    assert bal["BTC"] == Decimal("0")

    # Fills reconcile: buy-1 (1.0) + buy-2 (1.0 armed) + sell (2.0) = 3; total
    # fee 30 + 31 + 58 = 119.
    all_fills = await broker.fills()
    assert len(all_fills) == 3
    assert sum((f.fee for f in all_fills), Decimal("0")) == Decimal("119")
    assert [f.qty for f in all_fills] == [
        Decimal("1"),
        Decimal("1.0"),
        Decimal("2"),
    ]
