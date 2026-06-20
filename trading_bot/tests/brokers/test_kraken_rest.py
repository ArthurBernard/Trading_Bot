"""Tests for the :class:`~trading_bot.brokers.kraken.KrakenBroker` REST adapter.

Posture (no API key)
--------------------
Signing is proven **deterministically** against Kraken's published test vector
(:func:`test_sign_matches_kraken_published_vector`) — no key, no network. Every
private endpoint (``place_order`` / ``cancel_order`` / ``open_orders`` /
``balances`` / ``fills``) is exercised through ``pytest-httpx`` mocks of Kraken
JSON, with the broker holding **dummy** credentials (set via ``monkeypatch`` of
the environment) so the real signing path runs end to end. Real *private*
verification against Kraken is **deferred**: the test vector is the proof of
correctness. One opt-in ``@pytest.mark.network`` test hits Kraken's real
**public** ``AssetPairs`` + ``Ticker`` for ``BTC/USD``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.brokers import BrokerError, Capability, KrakenBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    Symbol,
    money,
)
from trading_bot.transport.ratelimit import KrakenCallCounter

BTC_USD = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)

# Kraken's published API-Sign test vector (docs.kraken.com authentication
# example). Deterministic: no key, no clock, no network.
_VECTOR_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9"
    "qa99HAZtuZuj6F1huXg=="
)
_VECTOR_DATA = {
    "nonce": "1616492376594",
    "ordertype": "limit",
    "pair": "XBTUSD",
    "price": "37500",
    "type": "buy",
    "volume": "1.25",
}
_VECTOR_PATH = "/0/private/AddOrder"
_VECTOR_EXPECTED = (
    "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32b"
    "Ab0nmbRn6H8ndwLUQ=="
)


def _fast_counter() -> KrakenCallCounter:
    """A call counter whose clock/sleep never block real time in tests."""
    clock = {"t": 0.0}

    async def _no_sleep(_seconds: float) -> None:
        return None

    return KrakenCallCounter.for_tier(
        "pro", time_source=lambda: clock["t"], sleep=_no_sleep
    )


def _broker(monkeypatch: pytest.MonkeyPatch, *, creds: bool = True) -> KrakenBroker:
    """Build a broker; with ``creds`` set dummy env credentials (signing runs)."""
    if creds:
        monkeypatch.setenv("KRAKEN_API_KEY", "DUMMY-KEY")
        monkeypatch.setenv("KRAKEN_API_SECRET", _VECTOR_SECRET)
    else:
        monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
        monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    return KrakenBroker(call_counter=_fast_counter())


# --- signing: the deterministic proof ------------------------------------- #


def test_sign_matches_kraken_published_vector() -> None:
    """``_sign`` reproduces Kraken's published ``API-Sign`` exactly."""
    from trading_bot.brokers.kraken import _sign

    assert _sign(_VECTOR_PATH, _VECTOR_DATA, _VECTOR_SECRET) == _VECTOR_EXPECTED


# --- capabilities & construction ------------------------------------------ #


def test_constructible_without_credentials() -> None:
    """The broker builds with no env creds and is public-only."""
    broker = KrakenBroker(api_key="", api_secret="")
    assert broker.name == "kraken"
    assert broker.has_credentials is False


def test_capabilities_declares_six_rest_ops() -> None:
    broker = KrakenBroker(api_key="", api_secret="")
    caps = broker.capabilities()
    assert caps == {
        Capability.PLACE_ORDER,
        Capability.CANCEL,
        Capability.OPEN_ORDERS,
        Capability.BALANCES,
        Capability.FILLS,
        Capability.TICKER,
    }
    # The private WS feed belongs to the WS leaf, not this REST adapter.
    assert Capability.PRIVATE_WS not in caps


# --- order mapping (pure) -------------------------------------------------- #


def test_add_order_params_market() -> None:
    broker = KrakenBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="cid-mkt",
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("0.5"),
        type=OrderType.MARKET,
    )
    params = broker._add_order_params(order)
    assert params == {
        "pair": "XBTUSD",
        "type": "buy",
        "ordertype": "market",
        "volume": "0.5",
    }


def test_add_order_params_limit() -> None:
    broker = KrakenBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="cid-lim",
        instrument=BTC_USD,
        side=OrderSide.SELL,
        qty=money("1.25"),
        type=OrderType.LIMIT,
        limit_price=money("37500"),
    )
    params = broker._add_order_params(order)
    assert params == {
        "pair": "XBTUSD",
        "type": "sell",
        "ordertype": "limit",
        "volume": "1.25",
        "price": "37500",
    }


def test_add_order_params_stop_loss() -> None:
    broker = KrakenBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="cid-stop",
        instrument=BTC_USD,
        side=OrderSide.SELL,
        qty=money("2"),
        type=OrderType.STOP_LOSS,
        stop_price=money("28000"),
    )
    params = broker._add_order_params(order)
    assert params == {
        "pair": "XBTUSD",
        "type": "sell",
        "ordertype": "stop-loss",
        "volume": "2",
        "price": "28000",
    }


# --- private endpoints via mocks ------------------------------------------ #


async def test_place_order_signs_and_returns_txid(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={"error": [], "result": {"txid": ["OXXXXX-YYYYY-ZZZZZ"], "descr": {}}}
    )
    broker = _broker(monkeypatch)
    order = Order(
        client_order_id="cid-1",
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("1.25"),
        type=OrderType.LIMIT,
        limit_price=money("37500"),
    )

    txid = await broker.place_order(order)

    assert txid == "OXXXXX-YYYYY-ZZZZZ"
    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    assert str(request.url) == "https://api.kraken.com/0/private/AddOrder"
    # Signed headers present (key never logged, only sent).
    assert request.headers["API-Key"] == "DUMMY-KEY"
    assert request.headers["API-Sign"]  # non-empty signature
    body = request.content.decode()
    assert "nonce=" in body
    assert "pair=XBTUSD" in body
    assert "ordertype=limit" in body
    assert "type=buy" in body
    assert "volume=1.25" in body
    assert "price=37500" in body


async def test_cancel_order_posts_txid(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(json={"error": [], "result": {"count": 1}})
    broker = _broker(monkeypatch)

    await broker.cancel_order("OXXXXX-YYYYY-ZZZZZ")

    request = httpx_mock.get_request()
    assert request is not None
    assert str(request.url) == "https://api.kraken.com/0/private/CancelOrder"
    assert request.headers["API-Sign"]
    assert "txid=OXXXXX-YYYYY-ZZZZZ" in request.content.decode()


async def test_balances_returns_decimal_map(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {"ZUSD": "1500.5000", "XXBT": "0.50000000", "USDT": "10.0"},
        }
    )
    broker = _broker(monkeypatch)

    balances = await broker.balances()

    # Asset codes normalised to canonical; amounts are exact Decimals.
    assert balances == {
        "USD": Decimal("1500.5000"),
        "BTC": Decimal("0.50000000"),
        "USDT": Decimal("10.0"),
    }
    assert all(isinstance(v, Decimal) for v in balances.values())


async def test_open_orders_rebuilds_domain_orders(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "open": {
                    "OABC-1": {
                        "vol": "1.0",
                        "vol_exec": "0.0",
                        "descr": {
                            "pair": "XBTUSD",
                            "type": "buy",
                            "ordertype": "limit",
                            "price": "30000.0",
                        },
                    }
                }
            },
        }
    )
    broker = _broker(monkeypatch)

    orders = await broker.open_orders()

    assert len(orders) == 1
    order = orders[0]
    assert isinstance(order, Order)
    assert order.venue_order_id == "OABC-1"
    assert order.instrument.symbol == Symbol("BTC", "USD")
    assert order.side is OrderSide.BUY
    assert order.type is OrderType.LIMIT
    assert order.qty == Decimal("1.0")
    assert order.limit_price == Decimal("30000.0")


async def test_open_orders_partial_fill_reflected(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "open": {
                    "OABC-2": {
                        "vol": "2.0",
                        "vol_exec": "0.5",
                        "price": "31000.0",
                        "descr": {
                            "pair": "XBTUSD",
                            "type": "sell",
                            "ordertype": "limit",
                            "price": "31000.0",
                        },
                    }
                }
            },
        }
    )
    broker = _broker(monkeypatch)

    orders = await broker.open_orders()

    assert orders[0].filled_qty == Decimal("0.5")
    assert orders[0].avg_fill_price == Decimal("31000.0")


async def test_fills_builds_domain_fills(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "trades": {
                    "TRADE-1": {
                        "ordertxid": "OABC-1",
                        "pair": "XXBTZUSD",
                        "type": "buy",
                        "price": "30000.5",
                        "vol": "0.10000000",
                        "fee": "7.80",
                        "time": 1616492376.594,
                    }
                },
                "count": 1,
            },
        }
    )
    broker = _broker(monkeypatch)

    fills = await broker.fills()

    assert len(fills) == 1
    fill = fills[0]
    assert isinstance(fill, Fill)
    assert fill.fill_id == "TRADE-1"
    assert fill.client_order_id == "OABC-1"
    assert fill.instrument.symbol == Symbol("BTC", "USD")
    assert fill.side is OrderSide.BUY
    assert fill.qty == Decimal("0.10000000")
    assert fill.price == Decimal("30000.5")
    assert fill.fee == Decimal("7.80")
    assert fill.ts == 1616492376594


async def test_fills_since_ms_passes_start_seconds(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(json={"error": [], "result": {"trades": {}}})
    broker = _broker(monkeypatch)

    await broker.fills(since_ms=1_616_492_376_594)

    body = httpx_mock.get_request().content.decode()
    # ms -> seconds for Kraken's ``start`` cursor.
    assert "start=1616492376.594" in body


# --- ticker (public, mocked) ---------------------------------------------- #


async def test_ticker_returns_last_price(httpx_mock) -> None:
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "c": ["64250.10000", "0.01526374"],
                    "a": ["64250.20000", "1", "1.000"],
                }
            },
        }
    )
    broker = KrakenBroker(api_key="", api_secret="")

    price = await broker.ticker(BTC_USD)

    assert price == Decimal("64250.10000")
    assert isinstance(price, Decimal)
    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "GET"
    assert "pair=XBTUSD" in str(request.url)


async def test_instrument_builds_precision(httpx_mock) -> None:
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "XXBTZUSD": {
                    "altname": "XBTUSD",
                    "wsname": "XBT/USD",
                    "pair_decimals": 1,
                    "lot_decimals": 8,
                }
            },
        }
    )
    broker = KrakenBroker(api_key="", api_secret="")

    inst = await broker.instrument(Symbol("BTC", "USD"))

    assert inst.symbol == Symbol("BTC", "USD")
    assert inst.price_precision == 1
    assert inst.qty_precision == 8


# --- error mapping & credential handling ---------------------------------- #


async def test_kraken_error_raises_broker_error(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={"error": ["EOrder:Insufficient funds"], "result": {}}
    )
    broker = _broker(monkeypatch)
    order = Order(
        client_order_id="cid-x",
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    with pytest.raises(BrokerError, match="Insufficient funds"):
        await broker.place_order(order)


async def test_private_call_without_credentials_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(monkeypatch, creds=False)
    with pytest.raises(BrokerError, match="requires credentials"):
        await broker.balances()


async def test_public_call_needs_no_credentials(httpx_mock) -> None:
    """A public call works with no credentials present."""
    httpx_mock.add_response(
        json={"error": [], "result": {"XXBTZUSD": {"c": ["100.0", "1"]}}}
    )
    broker = KrakenBroker(api_key="", api_secret="")
    assert await broker.ticker(BTC_USD) == Decimal("100.0")


# --- real public smoke (opt-in: ``-m network``) --------------------------- #


@pytest.mark.network
async def test_real_kraken_public_btc_usd() -> None:
    """Live Kraken ``AssetPairs`` + ``Ticker`` for BTC/USD (no key)."""
    broker = KrakenBroker()  # no creds: public-only is enough here

    inst = await broker.instrument(Symbol("BTC", "USD"))
    assert inst.symbol == Symbol("BTC", "USD")
    assert inst.price_precision is not None
    assert inst.price_precision >= 0
    assert inst.qty_precision is not None and inst.qty_precision >= 0

    price = await broker.ticker(inst)
    assert isinstance(price, Decimal)
    assert price > 0
