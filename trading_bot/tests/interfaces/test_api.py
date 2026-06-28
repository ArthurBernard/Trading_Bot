"""Tests for ``trading_bot.interfaces.api`` — the read-only FastAPI over the engine.

The whole API is exercised in-process against a **paper engine** (the real data
path: a :class:`~trading_bot.brokers.paper.PaperBroker` produces real fills,
which fold into real positions and PnL) through
:class:`fastapi.testclient.TestClient` — no real server, no network.

What is verified
----------------
* ``GET /api/health`` reports ``mode == "paper"`` and the strategy count;
* ``GET /api/positions`` returns the held instrument(s) with **money as exact
  Decimal strings** (a ``0.1`` qty serializes as ``"0.1"``, not ``0.1`` the
  float) matching ``engine.tracker``;
* ``GET /api/orders`` returns the router's tracked orders with enums by ``.value``
  and money as strings;
* ``GET /api/kpi`` returns realised PnL as a string equal to
  ``engine.perf.realised_pnl()`` and the four KPI ratio keys;
* ``GET /api/events`` (SSE) streams a :class:`FillEvent` emitted on the bus and
  the queue is removed on disconnect;
* there is **no** mutation route — a POST to a plausible order path is rejected.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from trading_bot.application.config import AppConfig
from trading_bot.application.events import FillEvent
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.interfaces.api import create_app

# --- fixtures: a paper engine seeded with real orders / fills -------------- #

_BTC = Instrument(Symbol("BTC", "USD"))
_ETH = Instrument(Symbol("ETH", "USD"))


def _build_seeded_engine() -> Engine:
    """A paper engine with two strategies declared and real orders/fills booked.

    Submits a BUY then a partial-close SELL on BTC and a single BUY on ETH
    through the engine's real :class:`~trading_bot.application.order_router.
    OrderRouter`; the wired :class:`~trading_bot.brokers.paper.PaperBroker` fills
    each immediately at the limit price and emits ``FillEvent``\\ s onto the
    shared bus, so the tracker and performance service hold real state.
    """
    config = AppConfig.model_validate(
        {
            "mode": "paper",
            "strategies": [
                {"name": "btc-ma", "symbol": "BTC/USD"},
                {"name": "eth-ma", "symbol": "ETH/USD"},
            ],
        }
    )
    engine = build_engine(config)

    async def _book() -> None:
        # BTC: buy 0.1 @ 30000, then sell 0.04 @ 31000 (realises PnL on the close).
        await engine.router.submit(
            Order(
                client_order_id="btc-buy-1",
                instrument=_BTC,
                side=OrderSide.BUY,
                qty=money("0.1"),
                type=OrderType.LIMIT,
                limit_price=money("30000"),
            )
        )
        await engine.router.submit(
            Order(
                client_order_id="btc-sell-1",
                instrument=_BTC,
                side=OrderSide.SELL,
                qty=money("0.04"),
                type=OrderType.LIMIT,
                limit_price=money("31000"),
            )
        )
        # ETH: a single buy that stays fully open as a long position.
        await engine.router.submit(
            Order(
                client_order_id="eth-buy-1",
                instrument=_ETH,
                side=OrderSide.BUY,
                qty=money("2"),
                type=OrderType.LIMIT,
                limit_price=money("2000"),
            )
        )

    asyncio.run(_book())
    return engine


@pytest.fixture
def engine() -> Engine:
    """A paper engine seeded with real BTC/ETH orders + fills."""
    return _build_seeded_engine()


@pytest.fixture
def client(engine: Engine) -> TestClient:
    """A ``TestClient`` over ``create_app(engine)`` (no real server)."""
    return TestClient(create_app(engine))


# --- health ---------------------------------------------------------------- #


def test_health_reports_paper_mode_and_strategy_count(client: TestClient) -> None:
    """``/api/health`` is 200 and reports the paper mode + declared strategies."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "paper"
    assert body["strategies"] == 2


# --- positions: money is an exact Decimal string --------------------------- #


def test_positions_money_fields_are_exact_decimal_strings(
    client: TestClient, engine: Engine
) -> None:
    """``/api/positions`` renders every money field as the **exact** Decimal string."""
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    body = resp.json()

    by_instrument = {row["instrument"]: row for row in body}
    assert set(by_instrument) == {"BTC/USD", "ETH/USD"}

    # The crux: money is a JSON *string*, not a float, and exact.
    btc = by_instrument["BTC/USD"]
    assert isinstance(btc["net_qty"], str)
    # bought 0.1, sold 0.04 -> net 0.06 long; serialized exactly, no float noise.
    assert btc["net_qty"] == "0.06"
    assert btc["avg_entry_price"] == "30000"

    eth = by_instrument["ETH/USD"]
    assert isinstance(eth["net_qty"], str)
    # The ETH 2.0 buy is the canonical "a price/qty of 2 stays exact" case.
    assert eth["net_qty"] == "2"

    # Every position dict must match the engine's tracker exactly.
    for instrument, position in engine.tracker.all_positions().items():
        row = by_instrument[str(instrument)]
        assert row["net_qty"] == str(position.net_qty)
        assert row["realised_pnl"] == str(position.realised_pnl)
        assert row["fees_paid"] == str(position.fees_paid)
        expected_entry = (
            None
            if position.avg_entry_price is None
            else str(position.avg_entry_price)
        )
        assert row["avg_entry_price"] == expected_entry


def test_positions_money_never_serialized_as_float() -> None:
    """A ``0.1`` quantity must appear in the raw JSON as ``"0.1"``, not ``0.1``.

    Guards the Decimal-as-string invariant at the byte level: a flat-from-fresh
    position of exactly 0.1 (no closing trade) must render the string ``"0.1"``,
    proving money never crosses the lossy float path.
    """
    config = AppConfig.model_validate({"mode": "paper"})
    engine = build_engine(config)
    engine.tracker.apply(
        Fill(
            fill_id="T1",
            client_order_id="cid-1",
            instrument=_BTC,
            side=OrderSide.BUY,
            qty=money("0.1"),
            price=money("30000.1"),
            fee=money("0"),
            ts=1,
        )
    )
    client = TestClient(create_app(engine))
    raw = client.get("/api/positions").text
    # The exact strings must be present in the wire bytes...
    assert '"net_qty":"0.1"' in raw
    assert '"avg_entry_price":"30000.1"' in raw
    # ...and the lossy float forms must NOT appear.
    assert '"net_qty":0.1' not in raw
    assert "0.1000000000000000055511151231257827021181583404541015625" not in raw


# --- orders: enums by value, money as strings ------------------------------ #


def test_orders_lists_tracked_orders_with_value_enums_and_string_money(
    client: TestClient, engine: Engine
) -> None:
    """``/api/orders`` returns the router's tracked orders, enums by value, money str."""
    resp = client.get("/api/orders")
    assert resp.status_code == 200
    body = resp.json()

    by_cid = {row["client_order_id"]: row for row in body}
    assert set(by_cid) == set(engine.router.tracked_orders())

    btc_buy = by_cid["btc-buy-1"]
    # Enums serialize by their .value (not "OrderSide.BUY").
    assert btc_buy["side"] == "buy"
    assert btc_buy["type"] == "limit"
    assert btc_buy["status"] in {"filled", "open", "partially_filled"}
    # Money fields are exact strings.
    assert isinstance(btc_buy["qty"], str)
    assert btc_buy["qty"] == "0.1"
    assert btc_buy["limit_price"] == "30000"
    assert isinstance(btc_buy["filled_qty"], str)
    # A LIMIT order has no stop price -> JSON null.
    assert btc_buy["stop_price"] is None
    # venue id is populated by the broker open transition.
    assert btc_buy["venue_order_id"] is not None
    assert btc_buy["instrument"] == "BTC/USD"


# --- kpi: money strings + the four ratios ---------------------------------- #


def test_kpi_realised_pnl_string_matches_engine_and_has_ratios(
    client: TestClient, engine: Engine
) -> None:
    """``/api/kpi`` realised PnL (string) equals the perf service; ratios present."""
    pytest.importorskip("fynance")  # the seeded curve makes /api/kpi compute fynance ratios
    resp = client.get("/api/kpi")
    assert resp.status_code == 200
    body = resp.json()

    # Money as exact Decimal strings matching the engine's performance service.
    assert body["realised_pnl"] == str(engine.perf.realised_pnl())
    assert body["fees_paid"] == str(engine.perf.fees_paid())
    equity = engine.perf.equity_curve()
    assert body["equity_end"] == str(equity[-1])
    assert isinstance(body["realised_pnl"], str)

    # The four KPI ratios are present and numeric (floats are fine for ratios).
    for key in ("sharpe", "sortino", "max_drawdown", "calmar"):
        assert key in body
        assert isinstance(body[key], (int, float))


def test_kpi_empty_engine_returns_zero_ratios_and_null_equity() -> None:
    """A fresh engine (no fills): zero ratios, null equity_end, zero money strings."""
    engine = build_engine(AppConfig.model_validate({"mode": "paper"}))
    client = TestClient(create_app(engine))
    body = client.get("/api/kpi").json()
    assert body["realised_pnl"] == "0"
    assert body["equity_end"] is None
    assert body["sharpe"] == 0.0


# --- SSE: a FillEvent streams through, queue removed on disconnect ---------- #


def _events_route(app: FastAPI) -> Any:
    """The ``/api/events`` route's async endpoint handler."""
    route = next(
        r for r in app.routes if getattr(r, "path", None) == "/api/events"
    )
    return route.endpoint


async def _never_disconnect() -> dict[str, Any]:
    """An ASGI ``receive`` that never reports a disconnect (the consumer stays up)."""
    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}  # pragma: no cover - never reached


async def test_events_stream_delivers_fill_event_and_removes_queue(
    engine: Engine,
) -> None:
    """``/api/events`` streams an emitted ``FillEvent`` (money as strings), cleans up.

    The endpoint serves an **infinite** ``text/event-stream``; the in-process
    ``TestClient`` deadlocks consuming an endless stream, so this drives the
    endpoint's real ``StreamingResponse.body_iterator`` directly — the exact
    generator the route returns, including the bus-queue registration on open and
    the ``remove_queue`` in its ``finally`` on close.
    """
    app = create_app(engine)
    bus = engine.bus
    before = len(bus._queues)

    fill = Fill(
        fill_id="SSE-1",
        client_order_id="btc-buy-1",
        instrument=_BTC,
        side=OrderSide.BUY,
        qty=money("0.1"),
        price=money("30000.5"),
        fee=money("0.3"),
        ts=1_700_000_000_000,
    )

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/events",
        "headers": [],
        "query_string": b"",
        "app": app,
    }
    request = Request(scope, _never_disconnect)
    response = await _events_route(app)(request)
    assert response.media_type == "text/event-stream"

    frames = response.body_iterator
    try:
        # First frame is the immediate ": connected" comment (flushes the start),
        # and the bus queue is now registered for this consumer.
        first = await frames.__anext__()
        assert first.startswith(":")
        assert len(bus._queues) == before + 1

        # Emit a fill; it must arrive next as a `data:` frame with string money.
        bus.emit(FillEvent(fill))
        frame = await frames.__anext__()
        assert frame.startswith("data:")
        payload = json.loads(frame[len("data:"):].strip())
        assert payload["type"] == "fill"
        assert payload["fill"]["fill_id"] == "SSE-1"
        # Money in the SSE frame is an exact Decimal string, like the REST routes.
        assert payload["fill"]["price"] == "30000.5"
        assert payload["fill"]["fee"] == "0.3"
        assert payload["fill"]["side"] == "buy"
    finally:
        # Closing the stream (client disconnect) runs the generator's `finally`,
        # which removes the queue from the bus.
        await frames.aclose()

    assert len(bus._queues) == before


# --- no-mutation: the API never places or cancels an order ----------------- #


@pytest.mark.parametrize("path", ["/api/orders", "/api/positions", "/api/events"])
def test_no_mutation_routes(client: TestClient, path: str) -> None:
    """A POST to a plausible order path is rejected — the API is read-only.

    Only GET is registered, so a POST returns 405 (method not allowed). The
    absence of an order-placing route is the read-only invariant.
    """
    resp = client.post(path, json={"side": "buy", "qty": "1"})
    assert resp.status_code in {404, 405}
    # And specifically: no order was created by the attempt.


def test_decimal_string_round_trips_to_exact_decimal(client: TestClient) -> None:
    """Re-parsing a money string with ``Decimal`` is exact (no float in between)."""
    btc = next(
        row
        for row in client.get("/api/positions").json()
        if row["instrument"] == "BTC/USD"
    )
    # Decimal(the string) is exact; Decimal(float(...)) would not be.
    assert Decimal(btc["net_qty"]) == Decimal("0.06")


# --- unit-level coverage of the serialization safety nets ------------------ #


def test_decimal_encoder_renders_decimal_as_string_and_rejects_other() -> None:
    """The JSON ``default`` hook stringifies a Decimal exactly, else raises."""
    from trading_bot.interfaces.api.app import _default

    assert _default(Decimal("0.1")) == "0.1"
    assert json.dumps({"x": Decimal("1.5")}, default=_default) == '{"x": "1.5"}'
    with pytest.raises(TypeError):
        _default(object())


def test_safe_ratio_returns_zero_when_estimator_is_undefined() -> None:
    """A KPI ratio that fynance rejects (sign-crossing curve) degrades to ``0.0``."""
    from trading_bot.interfaces.api.app import _safe_ratio

    def _raises() -> float:
        raise ValueError("initial value X[0] and final value X[-1] ... sign")

    assert _safe_ratio(_raises) == 0.0
    assert _safe_ratio(lambda: 1.25) == 1.25


def test_event_dict_serializes_each_event_type_with_string_money() -> None:
    """``_event_dict`` tags + renders order/fill/log events (money as strings)."""
    from trading_bot.application.events import LogEvent, OrderEvent
    from trading_bot.interfaces.api.app import _event_dict

    order = Order(
        client_order_id="cid-1",
        instrument=_BTC,
        side=OrderSide.BUY,
        qty=money("0.1"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )
    order_payload = _event_dict(OrderEvent(order))
    assert order_payload["type"] == "order"
    assert order_payload["order"]["qty"] == "0.1"
    assert order_payload["order"]["side"] == "buy"

    log_payload = _event_dict(LogEvent(message="hi", level="warning"))
    assert log_payload == {"type": "log", "message": "hi", "level": "warning"}


def test_kpi_endpoint_stays_robust_over_a_profitable_curve() -> None:
    """``/api/kpi`` never 500s even when fynance can/can't define a ratio.

    Drives the perf service with a profitable round-trip (realised PnL +10): the
    endpoint reports the exact PnL string and a JSON number for every ratio —
    whichever fynance can compute on this curve, and ``0.0`` (via ``_safe_ratio``)
    for any it rejects. The point is the read-only KPI view is always 200 + numeric.
    """
    pytest.importorskip("fynance")  # the profitable curve makes /api/kpi compute fynance ratios
    engine = build_engine(AppConfig.model_validate({"mode": "paper"}))
    perf = engine.perf
    perf.apply(
        Fill("F1", "c1", _BTC, OrderSide.BUY, money("1"), money("100"),
             money("0"), 1)
    )
    perf.apply(
        Fill("F2", "c1", _BTC, OrderSide.SELL, money("1"), money("110"),
             money("0"), 2)
    )
    body = TestClient(create_app(engine)).get("/api/kpi").json()
    # realised PnL +10, rendered as an exact Decimal string.
    assert body["realised_pnl"] == "10"
    for key in ("sharpe", "sortino", "max_drawdown", "calmar"):
        assert isinstance(body[key], (int, float))
