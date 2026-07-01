"""Tests for the :class:`~trading_bot.brokers.binance.BinanceBroker` REST adapter.

Posture (no API key)
--------------------
Signing is proven **deterministically** against Binance's published HMAC-SHA256
test vector (:func:`test_sign_matches_binance_published_vector`) — no key, no
network. Every private endpoint (``place_order`` / ``cancel_order`` /
``open_orders`` / ``balances`` / ``fills``) is exercised through ``pytest-httpx``
mocks of Binance JSON, with the broker holding **dummy** credentials so the real
signing path (query → signature → ``X-MBX-APIKEY`` header) runs end to end. Real
*private* verification against Binance is **deferred to the testnet** (opt-in,
key-gated): the vector is the proof of signing correctness in this suite. Two
opt-in ``@pytest.mark.network`` tests hit Binance's real **public**
``ticker/price`` + ``exchangeInfo`` (no key) and its **testnet** signed
round-trip (skips without a key).
"""

from __future__ import annotations

import urllib.parse
from decimal import Decimal

import httpx
import pytest

from trading_bot.application.config import AppConfig, BrokerConfig, RiskConfig
from trading_bot.application.service_factory import build_engine
from trading_bot.brokers import BinanceBroker, BrokerError, Capability
from trading_bot.brokers.binance import TESTNET_API_BASE, _sign
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain import (
    Fill,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    Symbol,
    money,
    parse_binance_symbol,
)
from trading_bot.domain.errors import LiveTradingNotEnabled
from trading_bot.transport import AmbiguousRequestError, AsyncHTTPClient

BTC_USDT = Instrument(Symbol("BTC", "USDT"), price_precision=2, qty_precision=5)
ETH_USDT = Instrument(Symbol("ETH", "USDT"), price_precision=2, qty_precision=4)

# Binance's published HMAC-SHA256 test vector (rest-api docs, ASCII example).
# NOTE: the leaf spec's secret was truncated by one trailing char; the real
# Binance vector secret ends in ``...fATj0j``. Verified against the published
# expected signature below.
_VECTOR_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
_VECTOR_QUERY = (
    "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
    "&recvWindow=5000&timestamp=1499827319559"
)
_VECTOR_EXPECTED = (
    "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
)


def _broker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    creds: bool = True,
    base_url: str | None = "https://api.binance.com",
    symbols: list[Symbol] | None = None,
    http: AsyncHTTPClient | None = None,
) -> BinanceBroker:
    """Build a broker; with ``creds`` set dummy env credentials (signing runs)."""
    if creds:
        monkeypatch.setenv("BINANCE_API_KEY", "DUMMY-KEY")
        monkeypatch.setenv("BINANCE_API_SECRET", _VECTOR_SECRET)
    else:
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_API_BASE", raising=False)
    return BinanceBroker(base_url=base_url, symbols=symbols, http=http)


def _query_of(request: httpx.Request) -> dict[str, str]:
    """The request's query params as a flat dict."""
    return dict(urllib.parse.parse_qsl(request.url.query.decode()))


def _signature_valid(request: httpx.Request) -> bool:
    """Re-derive the signature over the request's pre-signature query and compare."""
    raw = request.url.query.decode()
    base, _, sig = raw.rpartition("&signature=")
    return _sign(base, _VECTOR_SECRET) == sig


# --- signing: the deterministic proof ------------------------------------- #


def test_sign_matches_binance_published_vector() -> None:
    """``_sign`` reproduces Binance's published HMAC-SHA256 signature exactly."""
    assert _sign(_VECTOR_QUERY, _VECTOR_SECRET) == _VECTOR_EXPECTED


# --- capabilities & construction ------------------------------------------ #


def test_constructible_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker builds with no env creds and is public-only."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    broker = BinanceBroker(api_key="", api_secret="")
    assert broker.name == "binance"
    assert broker.has_credentials is False


def test_capabilities_declares_six_rest_ops() -> None:
    broker = BinanceBroker(api_key="", api_secret="")
    caps = broker.capabilities()
    assert caps == {
        Capability.PLACE_ORDER,
        Capability.CANCEL,
        Capability.OPEN_ORDERS,
        Capability.BALANCES,
        Capability.FILLS,
        Capability.TICKER,
    }
    # The private WS feed is deferred — not part of this REST adapter.
    assert Capability.PRIVATE_WS not in caps


def test_base_url_defaults_to_mainnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINANCE_API_BASE", raising=False)
    broker = BinanceBroker(api_key="", api_secret="")
    assert broker._base_url == "https://api.binance.com"


def test_base_url_env_toggle_testnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_API_BASE", TESTNET_API_BASE)
    broker = BinanceBroker(api_key="", api_secret="")
    assert broker._base_url == TESTNET_API_BASE


# --- order mapping (pure) -------------------------------------------------- #


def test_order_params_market() -> None:
    broker = BinanceBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="strat-mkt",
        instrument=BTC_USDT,
        side=OrderSide.BUY,
        qty=money("0.5"),
        type=OrderType.MARKET,
    )
    params = broker._order_params(order)
    assert params == {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "MARKET",
        "quantity": "0.5",
        "newClientOrderId": "strat-mkt",
    }


def test_order_params_limit() -> None:
    broker = BinanceBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="strat-lim",
        instrument=BTC_USDT,
        side=OrderSide.SELL,
        qty=money("1.25"),
        type=OrderType.LIMIT,
        limit_price=money("37500"),
    )
    params = broker._order_params(order)
    assert params == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "LIMIT",
        "quantity": "1.25",
        "price": "37500",
        "timeInForce": "GTC",
        "newClientOrderId": "strat-lim",
    }


def test_order_params_stop_loss() -> None:
    broker = BinanceBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="strat-stop",
        instrument=BTC_USDT,
        side=OrderSide.SELL,
        qty=money("2"),
        type=OrderType.STOP_LOSS,
        stop_price=money("28000"),
    )
    params = broker._order_params(order)
    assert params == {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "type": "STOP_LOSS_LIMIT",
        "quantity": "2",
        "stopPrice": "28000",
        "price": "28000",
        "timeInForce": "GTC",
        "newClientOrderId": "strat-stop",
    }


def test_order_params_omits_incompatible_client_order_id() -> None:
    """A client_order_id that breaks Binance's constraint is not forwarded."""
    broker = BinanceBroker(api_key="", api_secret="")
    order = Order(
        client_order_id="x" * 40,  # > 36 chars: incompatible
        instrument=BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )
    params = broker._order_params(order)
    assert "newClientOrderId" not in params


# --- private endpoints via mocks ------------------------------------------ #


async def test_place_order_signs_and_returns_composite_id(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={"symbol": "BTCUSDT", "orderId": 123456, "status": "NEW"}
    )
    broker = _broker(monkeypatch)
    order = Order(
        client_order_id="strat-1",
        instrument=BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1.25"),
        type=OrderType.LIMIT,
        limit_price=money("37500"),
    )

    venue_id = await broker.place_order(order)

    # Composite venue id: "<SYMBOL>:<orderId>".
    assert venue_id == "BTCUSDT:123456"

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "POST"
    assert str(request.url).startswith("https://api.binance.com/api/v3/order?")
    # Signed: key header present, signature present and correct.
    assert request.headers["X-MBX-APIKEY"] == "DUMMY-KEY"
    assert _signature_valid(request)
    q = _query_of(request)
    assert q["symbol"] == "BTCUSDT"
    assert q["side"] == "BUY"
    assert q["type"] == "LIMIT"
    assert q["quantity"] == "1.25"
    assert q["price"] == "37500"
    assert q["timeInForce"] == "GTC"
    assert q["newClientOrderId"] == "strat-1"
    assert q["recvWindow"] == "5000"
    assert "timestamp" in q
    assert "signature" in q


async def test_place_order_market_no_price(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "orderId": 7})
    broker = _broker(monkeypatch)
    order = Order(
        client_order_id="strat-mkt",
        instrument=BTC_USDT,
        side=OrderSide.BUY,
        qty=money("0.5"),
        type=OrderType.MARKET,
    )

    venue_id = await broker.place_order(order)

    assert venue_id == "BTCUSDT:7"
    q = _query_of(httpx_mock.get_request())
    assert q["type"] == "MARKET"
    assert "price" not in q
    assert "timeInForce" not in q


async def test_cancel_order_splits_composite_id(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={"symbol": "BTCUSDT", "orderId": 123456, "status": "CANCELED"}
    )
    broker = _broker(monkeypatch)

    await broker.cancel_order("BTCUSDT:123456")

    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "DELETE"
    assert str(request.url).startswith("https://api.binance.com/api/v3/order?")
    assert request.headers["X-MBX-APIKEY"] == "DUMMY-KEY"
    assert _signature_valid(request)
    q = _query_of(request)
    # The composite id is split back into the symbol + orderId Binance needs.
    assert q["symbol"] == "BTCUSDT"
    assert q["orderId"] == "123456"


async def test_place_then_cancel_round_trips_composite_id(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composite id from place_order round-trips through cancel_order."""
    httpx_mock.add_response(json={"symbol": "ETHUSDT", "orderId": 999})
    httpx_mock.add_response(json={"symbol": "ETHUSDT", "orderId": 999})
    broker = _broker(monkeypatch)
    order = Order(
        client_order_id="strat-rt",
        instrument=ETH_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.LIMIT,
        limit_price=money("1000"),
    )

    venue_id = await broker.place_order(order)
    assert venue_id == "ETHUSDT:999"
    await broker.cancel_order(venue_id)

    cancel_req = httpx_mock.get_requests()[-1]
    q = _query_of(cancel_req)
    assert q["symbol"] == "ETHUSDT"
    assert q["orderId"] == "999"


async def test_cancel_malformed_composite_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(monkeypatch)
    with pytest.raises(BrokerError, match="malformed composite"):
        await broker.cancel_order("no-colon-here")


async def test_balances_returns_decimal_map(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={
            "balances": [
                {"asset": "USDT", "free": "1500.50000000", "locked": "0.0"},
                {"asset": "BTC", "free": "0.50000000", "locked": "0.1"},
                {"asset": "ETH", "free": "0.00000000", "locked": "0.0"},
            ]
        }
    )
    broker = _broker(monkeypatch)

    balances = await broker.balances()

    # Zero-free assets skipped; amounts are exact Decimals.
    assert balances == {
        "USDT": Decimal("1500.50000000"),
        "BTC": Decimal("0.50000000"),
    }
    assert all(isinstance(v, Decimal) for v in balances.values())
    # Signed GET to /account.
    request = httpx_mock.get_request()
    assert request.method == "GET"
    assert "/api/v3/account?" in str(request.url)
    assert _signature_valid(request)


async def test_open_orders_rebuilds_domain_orders(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json=[
            {
                "symbol": "BTCUSDT",
                "orderId": 555,
                "clientOrderId": "strat-7",
                "price": "30000.00",
                "origQty": "1.00000",
                "executedQty": "0.00000",
                "type": "LIMIT",
                "side": "BUY",
                "status": "NEW",
            }
        ]
    )
    broker = _broker(monkeypatch)

    orders = await broker.open_orders()

    assert len(orders) == 1
    order = orders[0]
    assert isinstance(order, Order)
    # venue_order_id is the composite — reproduced identically for a later cancel.
    assert order.venue_order_id == "BTCUSDT:555"
    assert order.instrument.symbol == Symbol("BTC", "USDT")
    assert order.side is OrderSide.BUY
    assert order.type is OrderType.LIMIT
    assert order.qty == Decimal("1.00000")
    assert order.limit_price == Decimal("30000.00")


async def test_open_orders_partial_fill_reflected(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json=[
            {
                "symbol": "ETHUSDT",
                "orderId": 42,
                "clientOrderId": "strat-9",
                "price": "2000.00",
                "origQty": "2.0000",
                "executedQty": "0.5000",
                "type": "LIMIT",
                "side": "SELL",
                "status": "PARTIALLY_FILLED",
            }
        ]
    )
    broker = _broker(monkeypatch)

    orders = await broker.open_orders()

    assert orders[0].filled_qty == Decimal("0.5000")
    assert orders[0].avg_fill_price == Decimal("2000.00")


async def test_fills_over_two_symbol_set(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # myTrades is per-symbol: one response per configured symbol.
    httpx_mock.add_response(
        json=[
            {
                "id": 1001,
                "orderId": 555,
                "symbol": "BTCUSDT",
                "price": "30000.50",
                "qty": "0.10000000",
                "commission": "0.30000",
                "isBuyer": True,
                "time": 1616492376594,
            }
        ]
    )
    httpx_mock.add_response(
        json=[
            {
                "id": 2002,
                "orderId": 42,
                "symbol": "ETHUSDT",
                "price": "2000.00",
                "qty": "1.50000000",
                "commission": "0.10000",
                "isBuyer": False,
                "time": 1616492400000,
            }
        ]
    )
    broker = _broker(
        monkeypatch, symbols=[Symbol("BTC", "USDT"), Symbol("ETH", "USDT")]
    )

    fills = await broker.fills(since_ms=1_616_492_000_000)

    assert len(fills) == 2
    btc, eth = fills
    assert isinstance(btc, Fill)
    assert btc.fill_id == "1001"
    assert btc.client_order_id == "555"
    assert btc.instrument.symbol == Symbol("BTC", "USDT")
    assert btc.side is OrderSide.BUY
    assert btc.qty == Decimal("0.10000000")
    assert btc.price == Decimal("30000.50")
    assert btc.fee == Decimal("0.30000")
    assert btc.ts == 1616492376594

    assert eth.side is OrderSide.SELL
    assert eth.instrument.symbol == Symbol("ETH", "USDT")
    assert eth.qty == Decimal("1.50000000")

    # Each request carried the symbol + startTime cursor + a valid signature.
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    queried_symbols = {_query_of(r)["symbol"] for r in requests}
    assert queried_symbols == {"BTCUSDT", "ETHUSDT"}
    for r in requests:
        assert _query_of(r)["startTime"] == "1616492000000"
        assert _signature_valid(r)


async def test_fills_without_symbols_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured symbols → a clear error (Binance has no account-wide history)."""
    broker = _broker(monkeypatch, symbols=None)
    with pytest.raises(BrokerError, match="symbol set"):
        await broker.fills()


# --- ticker / instrument (public, mocked) --------------------------------- #


async def test_ticker_returns_last_price(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "price": "64250.10000000"})
    broker = BinanceBroker(api_key="", api_secret="")

    price = await broker.ticker(BTC_USDT)

    assert price == Decimal("64250.10000000")
    assert isinstance(price, Decimal)
    request = httpx_mock.get_request()
    assert request is not None
    assert request.method == "GET"
    assert "/api/v3/ticker/price" in str(request.url)
    assert "symbol=BTCUSDT" in str(request.url)


async def test_instrument_builds_precision_from_filters(httpx_mock) -> None:
    httpx_mock.add_response(
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAssetPrecision": 8,
                    "quoteAssetPrecision": 8,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
                    ],
                }
            ]
        }
    )
    broker = BinanceBroker(api_key="", api_secret="")

    inst = await broker.instrument(Symbol("BTC", "USDT"))

    assert inst.symbol == Symbol("BTC", "USDT")
    assert inst.price_precision == 2  # from tickSize 0.01
    assert inst.qty_precision == 5  # from stepSize 0.00001


async def test_instrument_falls_back_to_asset_precision(httpx_mock) -> None:
    """No PRICE_FILTER/LOT_SIZE → fall back to base/quote asset precision."""
    httpx_mock.add_response(
        json={
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAssetPrecision": 6,
                    "quoteAssetPrecision": 4,
                    "filters": [],
                }
            ]
        }
    )
    broker = BinanceBroker(api_key="", api_secret="")

    inst = await broker.instrument(Symbol("BTC", "USDT"))

    assert inst.price_precision == 4
    assert inst.qty_precision == 6


# --- venue-idempotency: order POST is sent at most once ------------------- #


class _RecordingSleep:
    """Async ``asyncio.sleep`` stand-in recording every requested backoff delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


async def test_place_order_does_not_retry_on_ambiguous_5xx(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5xx on the order POST is attempted once and raises an ambiguous error."""
    httpx_mock.add_response(status_code=503, text="upstream down")
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="binance", max_retries=3, sleep=sleep)
    broker = _broker(monkeypatch, http=http)
    order = Order(
        client_order_id="strat-amb",
        instrument=BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    with pytest.raises(AmbiguousRequestError, match="reconcile"):
        await broker.place_order(order)

    # The order POST is attempted at most once — no duplicate, no backoff.
    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []


async def test_place_order_does_not_retry_on_transport_error(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped connection on the order POST raises ambiguous, sent once."""
    httpx_mock.add_exception(httpx.ConnectError("dropped"))
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="binance", max_retries=3, sleep=sleep)
    broker = _broker(monkeypatch, http=http)
    order = Order(
        client_order_id="strat-drop",
        instrument=BTC_USDT,
        side=OrderSide.SELL,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    with pytest.raises(AmbiguousRequestError, match="reconcile"):
        await broker.place_order(order)

    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []


async def test_balances_still_retries_idempotent_read(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idempotent signed read (``/account``) still retries a transient 5xx."""
    httpx_mock.add_response(status_code=503, text="blip")
    httpx_mock.add_response(
        json={"balances": [{"asset": "USDT", "free": "100.0", "locked": "0"}]}
    )
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="binance", max_retries=3, sleep=sleep)
    broker = _broker(monkeypatch, http=http)

    balances = await broker.balances()

    assert balances == {"USDT": Decimal("100.0")}
    # Two attempts (one retried 5xx) → one backoff sleep: the read path is intact.
    assert len(httpx_mock.get_requests()) == 2
    assert len(sleep.calls) == 1


# --- error mapping & credential handling ---------------------------------- #


async def test_binance_error_body_raises_broker_error(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(
        json={"code": -2010, "msg": "Account has insufficient balance."}
    )
    broker = _broker(monkeypatch)
    order = Order(
        client_order_id="strat-x",
        instrument=BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    with pytest.raises(BrokerError, match="insufficient balance"):
        await broker.place_order(order)


async def test_private_call_without_credentials_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(monkeypatch, creds=False)
    with pytest.raises(BrokerError, match="requires credentials"):
        await broker.balances()


async def test_public_call_needs_no_credentials(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public call works with no credentials present."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "price": "100.0"})
    broker = BinanceBroker(api_key="", api_secret="")
    assert await broker.ticker(BTC_USDT) == Decimal("100.0")


# --- service_factory wiring ----------------------------------------------- #


def test_factory_live_binance_no_creds_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live + opted-in + binance + no creds → BrokerError (no paper fallback)."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="binance-main", exchange="binance")],
    )
    assert not BinanceBroker(api_key="", api_secret="").has_credentials
    with pytest.raises(BrokerError):
        build_engine(cfg)


def test_factory_live_binance_not_enabled_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """binance live but not opted in → LiveTradingNotEnabled (off by default)."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    cfg = AppConfig(
        mode="live",
        live_enabled=False,
        brokers=[BrokerConfig(name="binance-main", exchange="binance")],
    )
    with pytest.raises(LiveTradingNotEnabled):
        build_engine(cfg)


def test_factory_live_binance_with_creds_builds_adapter_no_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in + creds → the live BinanceBroker is constructed (no order sent)."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.delenv("BINANCE_API_BASE", raising=False)
    cfg = AppConfig(
        mode="live",
        live_enabled=True,
        brokers=[BrokerConfig(name="binance-main", exchange="binance")],
        # A real-money live config must carry explicit risk limits.
        risk=RiskConfig(
            max_order=money("1"),
            max_position=money("5"),
            max_daily_loss=money("1000"),
        ),
    )

    engine = build_engine(cfg)

    assert isinstance(engine.broker, BinanceBroker)
    assert engine.broker.has_credentials


def test_factory_paper_mode_untouched_by_binance() -> None:
    """Paper mode still yields a PaperBroker (the default invariant holds)."""
    engine = build_engine(AppConfig())
    assert isinstance(engine.broker, PaperBroker)


# --- real public smoke (opt-in: ``-m network``) --------------------------- #


@pytest.mark.network
async def test_real_binance_public_btc_usdt() -> None:
    """Live Binance ``ticker/price`` + ``exchangeInfo`` for BTC/USDT (no key)."""
    broker = BinanceBroker()  # no creds: public-only is enough here

    inst = await broker.instrument(Symbol("BTC", "USDT"))
    assert inst.symbol == Symbol("BTC", "USDT")
    assert inst.price_precision is not None and inst.price_precision >= 0
    assert inst.qty_precision is not None and inst.qty_precision >= 0

    # The venue symbol round-trips parse_binance_symbol <-> to_venue_symbol.
    venue_symbol = inst.symbol.to_venue_symbol("binance")
    assert venue_symbol == "BTCUSDT"
    assert parse_binance_symbol(venue_symbol) == Symbol("BTC", "USDT")

    price = await broker.ticker(inst)
    assert isinstance(price, Decimal)
    assert price > 0


# --- testnet signed round-trip (opt-in: ``-m network``, key-gated) -------- #


@pytest.mark.network
async def test_real_binance_testnet_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Place → read back → cancel a far-from-market LIMIT on the Binance testnet.

    Skips unless testnet credentials are present — ``BINANCE_TESTNET_API_KEY`` /
    ``BINANCE_TESTNET_API_SECRET`` (falling back to ``BINANCE_API_KEY`` /
    ``BINANCE_API_SECRET`` for the older single-key setup), from
    https://testnet.binance.vision. Points ``base_url`` at the testnet, places a
    tiny LIMIT order far below market (so it never fills), reads it back via
    ``open_orders`` (asserting the broker-reported state matches what was
    requested), then cancels it via the composite id.
    """
    import os

    key = os.environ.get("BINANCE_TESTNET_API_KEY") or os.environ.get(
        "BINANCE_API_KEY"
    )
    secret = os.environ.get("BINANCE_TESTNET_API_SECRET") or os.environ.get(
        "BINANCE_API_SECRET"
    )
    if not key or not secret:
        pytest.skip("no Binance testnet credentials in the environment")

    symbol = Symbol("BTC", "USDT")
    broker = BinanceBroker(
        api_key=key, api_secret=secret, base_url=TESTNET_API_BASE, symbols=[symbol]
    )
    inst = await broker.instrument(symbol)
    mark = await broker.ticker(inst)

    # A LIMIT buy far below market never fills; a small qty keeps it cheap.
    far_price = (mark * money("0.5")).quantize(money("0.01"))
    order = Order(
        client_order_id="tb-testnet-rt",
        instrument=inst,
        side=OrderSide.BUY,
        qty=money("0.001"),
        type=OrderType.LIMIT,
        limit_price=far_price,
    )
    venue_id = await broker.place_order(order)
    try:
        assert venue_id.startswith("BTCUSDT:")
        # Read it back: the broker-reported open order matches the request.
        opens = await broker.open_orders()
        match = [o for o in opens if o.venue_order_id == venue_id]
        assert match, f"placed order {venue_id} not found in open_orders()"
        ro = match[0]
        assert ro.side is OrderSide.BUY
        assert ro.qty == money("0.001")
        assert ro.limit_price == far_price
        # Balances are readable (locked some quote for the resting order).
        balances = await broker.balances()
        assert isinstance(balances, dict)
    finally:
        # Always cancel via the composite id, even if an assertion failed.
        await broker.cancel_order(venue_id)
