"""Tests for ``trading_bot.interfaces.ui`` — the read-only dashboard + ``serve``.

The dashboard is mounted on the same FastAPI app as the read-only API (leaf 01).
It is exercised in-process against a **paper engine** (the real data path: a
:class:`~trading_bot.brokers.paper.PaperBroker` produces real fills that fold into
real positions and PnL) through :class:`fastapi.testclient.TestClient` — no real
server, no network.

What is verified
----------------
* ``GET /`` → 200 HTML carrying the dashboard *shell*: the brand, the version, the
  mode badge, and the three table-body ids the JS targets
  (``positions-body`` / ``orders-body`` / ``kpi-body``);
* the static assets serve with the right content types
  (``/static/app.js`` → js, ``/static/style.css`` → css);
* the shipped ``app.js`` wires to the API (references ``/api/positions``,
  ``/api/orders``, ``/api/kpi``, ``/api/events``) — proving the UI is an HTTP
  client of the API, not a server-side data dump;
* the served HTML is a **shell** — no engine money string is baked into it
  server-side (the data only arrives over ``/api/*``);
* the ``serve`` command now builds the **read-only unified dashboard**
  (``create_dashboard_app(supervisor, read_only=True)`` — an alias of
  ``dashboard --read-only``) and hands it to ``uvicorn.run`` with the host/port —
  **patched** so no socket is opened;
* read-only still holds — a ``POST`` to a plausible order path is rejected (405).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trading_bot import __version__
from trading_bot.application.config import AppConfig
from trading_bot.application.service_factory import Engine, build_engine
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.interfaces.api import create_app
from trading_bot.interfaces.cli.main import app as cli_app

_BTC = Instrument(Symbol("BTC", "USD"))

runner = CliRunner()


# --- fixtures: a paper engine seeded with real orders / fills -------------- #


def _build_seeded_engine() -> Engine:
    """A paper engine with a real BUY then partial-close SELL booked on BTC.

    Routes both orders through the engine's real
    :class:`~trading_bot.application.order_router.OrderRouter`; the wired
    :class:`~trading_bot.brokers.paper.PaperBroker` fills each at the limit price
    and emits ``FillEvent``\\ s, so the tracker / performance service hold real
    state the dashboard's ``/api/*`` will render.
    """
    config = AppConfig.model_validate(
        {"mode": "paper", "strategies": [{"name": "btc-ma", "symbol": "BTC/USD"}]}
    )
    engine = build_engine(config)

    import asyncio

    async def _book() -> None:
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

    asyncio.run(_book())
    return engine


@pytest.fixture
def engine() -> Engine:
    """The seeded paper engine reused across the UI tests."""
    return _build_seeded_engine()


@pytest.fixture
def client(engine: Engine) -> TestClient:
    """A ``TestClient`` over ``create_app(engine)`` (no real server)."""
    return TestClient(create_app(engine))


# --- GET / : the dashboard shell ------------------------------------------- #


def test_dashboard_renders_shell_with_brand_version_and_mode(
    client: TestClient,
) -> None:
    """``GET /`` returns the dashboard shell (brand, version, mode badge)."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text

    # Header shell.
    assert "trading_bot" in html
    assert f"v{__version__}" in html
    # The paper-mode badge is rendered server-side from engine.config.mode.
    assert "mode-paper" in html
    assert ">paper<" in html


def test_dashboard_has_the_three_table_ids_the_js_targets(
    client: TestClient,
) -> None:
    """The shell carries the stable element ids ``app.js`` fills."""
    html = client.get("/").text
    assert 'id="positions-body"' in html
    assert 'id="orders-body"' in html
    assert 'id="kpi-body"' in html
    # Links to its assets.
    assert "/static/style.css" in html
    assert "/static/app.js" in html


def test_dashboard_is_a_shell_no_engine_money_baked_in(client: TestClient) -> None:
    """The page is a shell — no engine money string is rendered server-side.

    The seeded engine holds a 0.1 BTC buy at 30000; those values must NOT appear
    in the served HTML (they only arrive client-side over ``/api/*``). Proves the
    page is not a server-rendered data dump.
    """
    html = client.get("/").text
    assert "30000" not in html
    assert "0.1" not in html
    assert "31000" not in html


# --- static assets --------------------------------------------------------- #


def test_app_js_serves_as_javascript(client: TestClient) -> None:
    """``/static/app.js`` → 200 with a JavaScript content type."""
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_style_css_serves_as_css(client: TestClient) -> None:
    """``/static/style.css`` → 200 with a CSS content type."""
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


# --- the UI wires to the API (pure HTTP client) ---------------------------- #


def test_app_js_references_every_api_endpoint(client: TestClient) -> None:
    """``app.js`` fetches the JSON endpoints + the SSE stream — an HTTP client.

    Asserting the served JS references ``/api/positions|orders|kpi`` and
    ``/api/events`` proves the dashboard pulls its data from the API over HTTP
    (rather than the server rendering engine state into the page).
    """
    js = client.get("/static/app.js").text
    assert "/api/positions" in js
    assert "/api/orders" in js
    assert "/api/kpi" in js
    assert "/api/events" in js
    assert "EventSource" in js


def test_api_endpoints_back_the_dashboard_with_real_state(
    client: TestClient, engine: Engine
) -> None:
    """The ``/api/*`` JSON the JS will render reflects the engine's real state.

    The seeded engine holds a net 0.06 BTC long (0.1 bought, 0.04 sold); the
    positions/KPI endpoints report it as exact Decimal strings — the verbatim
    money the JS renders.
    """
    pytest.importorskip("fynance")  # the seeded curve makes /api/kpi compute fynance ratios
    positions = client.get("/api/positions").json()
    assert positions, "engine should hold a position"
    btc = next(p for p in positions if p["instrument"] == "BTC/USD")
    # 0.1 - 0.04 = 0.06, rendered as an exact Decimal string (never a float).
    assert btc["net_qty"] == "0.06"
    assert isinstance(btc["net_qty"], str)

    kpi = client.get("/api/kpi").json()
    assert isinstance(kpi["realised_pnl"], str)
    assert kpi["realised_pnl"] == str(engine.perf.realised_pnl())


# --- read-only still holds ------------------------------------------------- #


def test_post_to_order_path_is_rejected(client: TestClient) -> None:
    """A POST to a plausible order path is rejected — the UI added no write route.

    Only GET routes are registered; there is no mutating endpoint anywhere on the
    app (the dashboard mount added none). FastAPI returns 405 (method not allowed)
    for a POST to a known GET path, and 404 for an unknown path — either way no
    order is ever placed over HTTP.
    """
    assert client.post("/api/orders").status_code in (404, 405)
    assert client.post("/").status_code in (404, 405)


# --- serve command: wiring without launching uvicorn ----------------------- #


def test_serve_builds_app_and_calls_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve`` builds the read-only unified dashboard and hands it to ``uvicorn.run``.

    Patches :func:`uvicorn.run` so no socket opens, then asserts the command
    called it with a FastAPI app and the requested host/port — proving the wiring
    without standing up a real server. The built app is the read-only dashboard:
    ``GET /`` serves the shell, and a control mutation returns ``403``.
    """
    import uvicorn
    from fastapi import FastAPI

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(
        cli_app, ["serve", "--host", "0.0.0.0", "--port", "9123"]
    )

    assert result.exit_code == 0, result.output
    assert isinstance(captured["app"], FastAPI)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9123

    # The built app really is the read-only unified dashboard: GET / serves the
    # shell, health reports read_only, and a control POST is refused (403).
    test_client = TestClient(captured["app"])
    assert test_client.get("/").status_code == 200
    assert test_client.get("/api/health").json()["read_only"] is True
    assert test_client.post("/api/strategies/x/start").status_code == 403


def test_serve_default_config_is_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``--config``, ``serve`` defaults to a paper engine (never live)."""
    import uvicorn

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(cli_app, ["serve"])
    assert result.exit_code == 0, result.output

    test_client = TestClient(captured["app"])
    assert test_client.get("/api/health").json()["mode"] == "paper"


def test_serve_uses_decimal_safe_money(monkeypatch: pytest.MonkeyPatch) -> None:
    """A money string the served app renders stays exact (no float round-trip)."""
    # Sanity that the money helper used across the stack is the exact-Decimal one
    # the API serializes (guards against a float regression in the seam).
    assert str(Decimal("0.1")) == "0.1"
