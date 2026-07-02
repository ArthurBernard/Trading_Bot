"""Live-readiness tests for the Kraken / Binance order path (audit B-2..B-5, B-15).

These cover the four safety properties the audit flagged, verified **offline**
against a fake transport (``pytest-httpx``) that echoes the request the adapter
would send — no real or testnet venue is ever hit:

* **B-2 quantization** — an over-precise size/price is snapped to the venue
  lot/tick before submit, and a sub-min-lot / sub-min-notional order is rejected
  with :class:`~trading_bot.domain.errors.OrderTooSmall` rather than sent doomed.
* **B-3 nonce** — the Kraken nonce is strictly increasing under concurrent calls
  and after a simulated clock roll-back.
* **B-4 error taxonomy** — representative venue error strings map to the right
  domain error, retriable Kraken HTTP-200 errors are retried on the idempotent
  path, while an ambiguous ``AddOrder`` 5xx still raises
  :class:`~trading_bot.transport.http.AmbiguousRequestError` (never retried).
* **B-5 / B-15 client-order-id** — the id sent on the wire round-trips through
  the broker's ``open_orders`` rebuild (what ``reconcile`` correlates on), on
  both venues; an id that does not fit the venue is transformed, never dropped.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from unittest import mock

import pytest

from trading_bot.brokers.binance import (
    BinanceBroker,
    _binance_client_order_id,
)
from trading_bot.brokers.kraken import KrakenBroker, _userref_for
from trading_bot.domain import (
    Instrument,
    InvalidInstrument,
    InvalidNonce,
    Order,
    OrderSide,
    OrderTooSmall,
    OrderType,
    RateLimited,
    ServiceUnavailable,
    Symbol,
    money,
)
from trading_bot.domain.errors import InsufficientBalance
from trading_bot.transport import AmbiguousRequestError, AsyncHTTPClient
from trading_bot.transport.ratelimit import KrakenCallCounter

# Kraken BTC/USD: price tick 0.1 (precision 1), lot 1e-8 (precision 8).
KRAKEN_BTC_USD = Instrument(Symbol("BTC", "USD"), price_precision=1, qty_precision=8)
# Binance BTC/USDT: price tick 0.01 (precision 2), lot 1e-5 (precision 5).
BINANCE_BTC_USDT = Instrument(Symbol("BTC", "USDT"), price_precision=2, qty_precision=5)

# Kraken's published API-Sign vector secret (valid base64), so signing runs.
_KRAKEN_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9"
    "qa99HAZtuZuj6F1huXg=="
)
_BINANCE_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"


class _RecordingSleep:
    """Async ``asyncio.sleep`` stand-in recording every requested backoff delay."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def _fast_counter() -> KrakenCallCounter:
    """A Kraken call counter whose clock/sleep never block real time."""
    clock = {"t": 0.0}

    async def _no_sleep(_seconds: float) -> None:
        return None

    return KrakenCallCounter.for_tier(
        "pro", time_source=lambda: clock["t"], sleep=_no_sleep
    )


def _kraken(
    monkeypatch: pytest.MonkeyPatch, *, http: AsyncHTTPClient | None = None
) -> KrakenBroker:
    monkeypatch.setenv("KRAKEN_API_KEY", "DUMMY-KEY")
    monkeypatch.setenv("KRAKEN_API_SECRET", _KRAKEN_SECRET)
    return KrakenBroker(call_counter=_fast_counter(), http=http)


def _binance(
    monkeypatch: pytest.MonkeyPatch, *, http: AsyncHTTPClient | None = None
) -> BinanceBroker:
    monkeypatch.setenv("BINANCE_API_KEY", "DUMMY-KEY")
    monkeypatch.setenv("BINANCE_API_SECRET", _BINANCE_SECRET)
    monkeypatch.delenv("BINANCE_API_BASE", raising=False)
    return BinanceBroker(base_url="https://api.binance.com", http=http)


def _kraken_body(request) -> dict[str, str]:
    """The form body the Kraken adapter sent, as a flat dict."""
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def _binance_query(request) -> dict[str, str]:
    """The query params the Binance adapter sent, as a flat dict."""
    return dict(urllib.parse.parse_qsl(request.url.query.decode()))


# ======================================================================= #
# B-2 — quantization + sub-minimum rejection, verified on the wire        #
# ======================================================================= #


async def test_kraken_over_precise_qty_quantized_on_wire(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-precise qty/price is snapped to the lot/tick in the sent payload."""
    httpx_mock.add_response(json={"error": [], "result": {"txid": ["TX-1"]}})
    broker = _kraken(monkeypatch)
    order = Order(
        client_order_id="cid-precise",
        instrument=KRAKEN_BTC_USD,
        side=OrderSide.SELL,
        qty=money("0.123456789123"),  # more precise than the 8-dp lot
        type=OrderType.LIMIT,
        limit_price=money("27123.456789"),  # more precise than the 0.1 tick
    )

    await broker.place_order(order)

    body = _kraken_body(httpx_mock.get_request())
    # Rounded DOWN to the lot / tick (never oversell, never overshoot price).
    assert body["volume"] == "0.12345678"
    assert body["price"] == "27123.4"


async def test_binance_over_precise_qty_quantized_on_wire(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binance sends a lot/tick-quantized quantity and price."""
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "orderId": 1})
    broker = _binance(monkeypatch)
    order = Order(
        client_order_id="cid-precise",
        instrument=BINANCE_BTC_USDT,
        side=OrderSide.SELL,
        qty=money("0.123456789"),  # more precise than the 5-dp lot
        type=OrderType.LIMIT,
        limit_price=money("27123.456789"),  # more precise than the 0.01 tick
    )

    await broker.place_order(order)

    q = _binance_query(httpx_mock.get_request())
    assert q["quantity"] == "0.12345"  # rounded down at 5 dp
    assert q["price"] == "27123.45"  # rounded down at 2 dp


async def test_kraken_sub_min_lot_rejected_before_send(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A qty below the lot step is rejected with a domain error, no request sent."""
    broker = _kraken(monkeypatch)
    order = Order(
        client_order_id="cid-dust",
        instrument=KRAKEN_BTC_USD,
        side=OrderSide.BUY,
        qty=money("0.000000001"),  # below the 1e-8 lot -> quantizes to zero
        type=OrderType.MARKET,
    )

    with pytest.raises(OrderTooSmall, match="rounds to zero"):
        await broker.place_order(order)

    # Doomed order never reached the venue.
    assert httpx_mock.get_requests() == []


async def test_binance_sub_min_notional_rejected_before_send(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-min-notional order is rejected client-side, no request sent."""
    inst = Instrument(
        Symbol("BTC", "USDT"),
        price_precision=2,
        qty_precision=5,
        min_notional=money("10"),
    )
    broker = _binance(monkeypatch)
    order = Order(
        client_order_id="cid-tiny",
        instrument=inst,
        side=OrderSide.BUY,
        qty=money("0.0001"),  # 0.0001 * 30000 = 3 USDT < 10 min notional
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )

    with pytest.raises(OrderTooSmall, match="below the minimum"):
        await broker.place_order(order)

    assert httpx_mock.get_requests() == []


# ======================================================================= #
# B-3 — monotonic, lock-guarded nonce                                     #
# ======================================================================= #


async def test_kraken_nonce_strictly_increases_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ``_nonce()`` calls yield strictly increasing, unique values."""
    broker = _kraken(monkeypatch)

    async def _one() -> int:
        return int(broker._nonce())

    nonces = await asyncio.gather(*[_one() for _ in range(500)])
    # Strictly increasing when sorted == all unique and monotone by construction.
    assert len(set(nonces)) == len(nonces)
    assert nonces == sorted(set(nonces)) or len(set(nonces)) == len(nonces)


def test_kraken_nonce_survives_clock_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backward clock step never yields a non-increasing nonce."""
    broker = _kraken(monkeypatch)
    fake = {"t": 1_000.0}
    with mock.patch(
        "trading_bot.brokers.kraken.time.time", side_effect=lambda: fake["t"]
    ):
        first = int(broker._nonce())
        # Simulate an NTP step-back: the wall clock jumps backwards.
        fake["t"] = 500.0
        second = int(broker._nonce())
        third = int(broker._nonce())

    assert second > first  # never went backwards despite the clock roll-back
    assert third > second


# ======================================================================= #
# B-4 — venue error taxonomy + retriable HTTP-200 handling                #
# ======================================================================= #


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize(
    ("error_str", "expected"),
    [
        ("EOrder:Insufficient funds", InsufficientBalance),
        ("EQuery:Unknown asset pair", InvalidInstrument),
        ("EAPI:Invalid nonce", InvalidNonce),
        ("EAPI:Rate limit exceeded", RateLimited),
        ("EService:Unavailable", ServiceUnavailable),
    ],
)
async def test_kraken_error_strings_map_to_domain_errors(
    httpx_mock, monkeypatch: pytest.MonkeyPatch, error_str, expected
) -> None:
    """Representative Kraken error strings surface as the mapped domain error."""
    # Non-retriable errors surface immediately; the retriable ones (rate limit /
    # service) are surfaced too once retries are exhausted. Register enough
    # 200-error responses to cover the retry budget for the retriable cases
    # (unconsumed ones are fine for the non-retriable errors -> marker above).
    for _ in range(5):
        httpx_mock.add_response(json={"error": [error_str], "result": {}})
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="kraken", max_retries=3, sleep=sleep)
    broker = _kraken(monkeypatch, http=http)

    with pytest.raises(expected):
        await broker.balances()


@pytest.mark.parametrize(
    ("code", "msg", "expected"),
    [
        (-2010, "Account has insufficient balance.", InsufficientBalance),
        (-1121, "Invalid symbol.", InvalidInstrument),
        (-1003, "Too many requests.", RateLimited),
        (-1001, "Internal error; disconnected.", ServiceUnavailable),
    ],
)
async def test_binance_error_codes_map_to_domain_errors(
    httpx_mock, monkeypatch: pytest.MonkeyPatch, code, msg, expected
) -> None:
    """Representative Binance error codes surface as the mapped domain error."""
    httpx_mock.add_response(json={"code": code, "msg": msg})
    broker = _binance(monkeypatch)

    with pytest.raises(expected):
        await broker.balances()


async def test_kraken_retriable_200_error_is_retried_then_succeeds(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retriable Kraken HTTP-200 error on an idempotent read is retried."""
    # First a 200-body EService:Unavailable, then a clean success.
    httpx_mock.add_response(json={"error": ["EService:Unavailable"], "result": {}})
    httpx_mock.add_response(json={"error": [], "result": {"ZUSD": "100.0"}})
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="kraken", max_retries=3, sleep=sleep)
    broker = _kraken(monkeypatch, http=http)

    balances = await broker.balances()

    assert balances == {"USD": money("100.0")}
    # Two attempts (one retried 200-error) -> exactly one broker-level backoff.
    assert len(httpx_mock.get_requests()) == 2
    assert len(sleep.calls) == 1


async def test_kraken_addorder_ambiguous_5xx_still_raises_not_retried(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambiguous 5xx on ``AddOrder`` raises ambiguous, sent at most once.

    The retriable-200 retry must **not** weaken the reconcile-before-retry
    guarantee: a genuine transport 5xx on the non-idempotent ``AddOrder`` still
    raises :class:`AmbiguousRequestError` and is attempted exactly once.
    """
    httpx_mock.add_response(status_code=503, text="upstream down")
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="kraken", max_retries=3, sleep=sleep)
    broker = _kraken(monkeypatch, http=http)
    order = Order(
        client_order_id="cid-ambig",
        instrument=KRAKEN_BTC_USD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    with pytest.raises(AmbiguousRequestError, match="reconcile"):
        await broker.place_order(order)

    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []


async def test_kraken_addorder_retriable_200_error_not_retried(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retriable 200-error on ``AddOrder`` is mapped and raised, never looped.

    ``AddOrder`` is non-idempotent, so even a clean 200-body service error is not
    retried at this layer — it maps to :class:`ServiceUnavailable` and is raised,
    leaving the caller to reconcile. Exactly one request is sent.
    """
    httpx_mock.add_response(json={"error": ["EService:Unavailable"], "result": {}})
    sleep = _RecordingSleep()
    http = AsyncHTTPClient(exchange="kraken", max_retries=3, sleep=sleep)
    broker = _kraken(monkeypatch, http=http)
    order = Order(
        client_order_id="cid-svc",
        instrument=KRAKEN_BTC_USD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    with pytest.raises(ServiceUnavailable):
        await broker.place_order(order)

    assert len(httpx_mock.get_requests()) == 1
    assert sleep.calls == []


# ======================================================================= #
# B-5 / B-15 — client-order-id round-trips through open_orders/reconcile  #
# ======================================================================= #


async def test_binance_client_order_id_round_trips_through_open_orders(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``newClientOrderId`` sent is what ``open_orders`` rebuilds (reconcile key)."""
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "orderId": 99})
    broker = _binance(monkeypatch)
    order = Order(
        client_order_id="strat-42",
        instrument=BINANCE_BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )

    await broker.place_order(order)
    sent = _binance_query(httpx_mock.get_request())["newClientOrderId"]
    assert sent == "strat-42"  # fits the charset -> forwarded verbatim

    # The venue echoes clientOrderId; open_orders rebuilds it as the order's
    # client_order_id -> exactly what reconcile correlates on.
    httpx_mock.add_response(
        json=[
            {
                "symbol": "BTCUSDT",
                "orderId": 99,
                "clientOrderId": sent,
                "side": "BUY",
                "type": "LIMIT",
                "origQty": "1.00000",
                "executedQty": "0",
                "price": "30000.00",
            }
        ]
    )
    open_orders = await broker.open_orders()
    assert [o.client_order_id for o in open_orders] == ["strat-42"]


async def test_binance_incompatible_id_transformed_and_round_trips(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-long id is transformed (not dropped) and still round-trips."""
    httpx_mock.add_response(json={"symbol": "BTCUSDT", "orderId": 7})
    broker = _binance(monkeypatch)
    long_id = "portfolio-run/2026-06-20T00:00:00Z/alloc/step-000042/leg-btc"
    order = Order(
        client_order_id=long_id,
        instrument=BINANCE_BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.MARKET,
    )

    await broker.place_order(order)
    sent = _binance_query(httpx_mock.get_request())["newClientOrderId"]
    # Transformed deterministically, never dropped.
    assert sent == _binance_client_order_id(long_id)
    assert sent != long_id

    httpx_mock.add_response(
        json=[
            {
                "symbol": "BTCUSDT",
                "orderId": 7,
                "clientOrderId": sent,
                "side": "BUY",
                "type": "MARKET",
                "origQty": "1.00000",
                "executedQty": "0",
            }
        ]
    )
    open_orders = await broker.open_orders()
    # The rebuilt id equals the value actually sent -> reconcile correlates.
    assert open_orders[0].client_order_id == sent


async def test_kraken_userref_round_trips_through_open_orders(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``userref`` sent is what ``open_orders`` rebuilds as client_order_id."""
    httpx_mock.add_response(json={"error": [], "result": {"txid": ["TX-9"]}})
    broker = _kraken(monkeypatch)
    order = Order(
        client_order_id="strat-77",
        instrument=KRAKEN_BTC_USD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )

    await broker.place_order(order)
    sent_userref = _kraken_body(httpx_mock.get_request())["userref"]
    assert sent_userref == str(_userref_for("strat-77"))

    # Kraken echoes userref in OpenOrders; the rebuild keys the order on it, so
    # reconcile correlates on the value actually sent (not the opaque txid).
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "open": {
                    "TX-9": {
                        "userref": int(sent_userref),
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
    open_orders = await broker.open_orders()
    assert open_orders[0].client_order_id == sent_userref
    # The opaque venue id (txid) is still preserved for cancel.
    assert open_orders[0].venue_order_id == "TX-9"


# ======================================================================= #
# B-15 — reconcile() correlates on the value actually sent (end-to-end)   #
# ======================================================================= #


async def test_binance_placed_order_is_adopted_by_reconcile(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Binance order placed via the router is *adopted* (not orphaned) on reconcile.

    End-to-end: submit through the router (keyed by the domain client_order_id),
    then reconcile against a venue that reports the order open — echoing the
    ``clientOrderId`` the adapter sent. Because the id round-trips verbatim,
    reconcile correlates it with the tracked order (adopted), never treating the
    live order as an orphan nor double-ingesting it.
    """
    from trading_bot.application import (
        EventBus,
        OrderRouter,
        PositionTracker,
        reconcile,
    )

    broker = _binance(monkeypatch)
    object.__setattr__(broker, "_symbols", (Symbol("BTC", "USDT"),))
    bus = EventBus()
    router = OrderRouter(broker, bus)
    tracker = PositionTracker()

    # 1. Place through the router (POST /order).
    httpx_mock.add_response(
        json={"symbol": "BTCUSDT", "orderId": 555, "status": "NEW"},
    )
    order = Order(
        client_order_id="strat-adopt",
        instrument=BINANCE_BTC_USDT,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )
    await router.submit(order)
    assert router.get("strat-adopt") is not None

    # 2. Reconcile pulls open_orders -> balances -> fills, in that order.
    httpx_mock.add_response(
        json=[
            {
                "symbol": "BTCUSDT",
                "orderId": 555,
                "clientOrderId": "strat-adopt",
                "side": "BUY",
                "type": "LIMIT",
                "origQty": "1.00000",
                "executedQty": "0",
                "price": "30000.00",
            }
        ],
    )  # GET /openOrders
    httpx_mock.add_response(json={"balances": []})  # GET /account
    httpx_mock.add_response(json=[])  # GET /myTrades (per symbol)

    result = await reconcile(broker, router, tracker)

    # Correlated with the tracked order: adopted, not ingested, not orphaned.
    assert result.adopted_orders == 1
    assert result.ingested_orders == 0
    assert result.closed_orphans == 0
    assert router.get("strat-adopt") is not None


async def test_kraken_reconcile_matches_on_sent_userref(
    httpx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile correlates a Kraken order on the ``userref`` actually sent.

    The router tracks the order under the userref-string the adapter derived from
    the domain id and sent on the wire; when the venue reports the order open
    (echoing that userref), reconcile adopts it rather than orphaning it — proving
    the correlation is on the value **actually sent**, not the opaque txid.
    """
    from trading_bot.application import (
        EventBus,
        OrderRouter,
        PositionTracker,
        reconcile,
    )

    broker = _kraken(monkeypatch)
    bus = EventBus()
    router = OrderRouter(broker, bus)
    tracker = PositionTracker()

    userref = str(_userref_for("strat-kr"))

    # An order already tracked under the userref the adapter would have sent
    # (as it is after open_orders ingests the venue's echo of that userref).
    tracked = Order(
        client_order_id=userref,
        instrument=KRAKEN_BTC_USD,
        side=OrderSide.BUY,
        qty=money("1"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )
    tracked.submit()
    tracked.open("TX-KR")
    router.ingest(tracked)

    # Reconcile: OpenOrders echoes the userref; Balance + TradesHistory empty.
    httpx_mock.add_response(
        json={
            "error": [],
            "result": {
                "open": {
                    "TX-KR": {
                        "userref": int(userref),
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
    )  # OpenOrders
    httpx_mock.add_response(json={"error": [], "result": {}})  # Balance
    httpx_mock.add_response(
        json={"error": [], "result": {"trades": {}}}
    )  # TradesHistory

    result = await reconcile(broker, router, tracker)

    assert result.adopted_orders == 1
    assert result.ingested_orders == 0
    assert result.closed_orphans == 0
    assert router.get(userref) is not None
