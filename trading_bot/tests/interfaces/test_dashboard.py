"""Tests for the **unified dashboard** — the single shell + stub pages + health.

Drives :func:`~trading_bot.interfaces.api.create_dashboard_app` with a
:class:`fastapi.testclient.TestClient` (no real server) over a
:class:`~trading_bot.application.supervisor.StrategySupervisor` built from a small
paper :class:`~trading_bot.application.config.AppConfig`. Proves the shared shell
(the nav across every page), the ``/api/health`` shape, the ``read_only`` flag,
and the auth path (token → the pages redirect to ``/login``, ``/api/*`` needs
auth). Page *data* lands in later leaves; this leaf ships the shell only.

A separate CLI test proves ``trading-bot dashboard`` builds the app and hands it
to a (patched) :func:`uvicorn.run` with the right host/port, and that a
non-loopback ``--host`` without a token is refused — mirroring the ``serve`` test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trading_bot.application.config import AppConfig
from trading_bot.application.events import FillEvent
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide
from trading_bot.interfaces.api import create_dashboard_app
from trading_bot.interfaces.cli.main import app as cli_app
from trading_bot.tests.application.test_supervisor import _two_venue_client

runner = CliRunner()

#: The five page routes the shared nav links to (Overview at ``/``).
_PAGES = ("/", "/strategies", "/orders", "/pnl", "/logs")

#: Nav labels every page carries (proves the shared shell, not a bespoke page).
_NAV_LABELS = ("Overview", "Strategies", "Orders", "PnL", "Logs")


def _config() -> AppConfig:
    """A small paper config with one declared strategy (no network needed)."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                }
            ],
        }
    )


def _supervisor() -> StrategySupervisor:
    """A supervisor over the paper config (no dccd client needed for the shell)."""
    return StrategySupervisor(_config())


def _client(**kwargs: object) -> TestClient:
    return TestClient(create_dashboard_app(_supervisor(), **kwargs))  # type: ignore[arg-type]


# --- shell + pages --------------------------------------------------------- #


@pytest.mark.parametrize("path", _PAGES)
def test_every_page_renders_the_shared_nav(path: str) -> None:
    """Each of the five pages returns 200 and carries the shared nav labels."""
    resp = _client().get(path)
    assert resp.status_code == 200, path
    html = resp.text
    assert "trading_bot" in html
    for label in _NAV_LABELS:
        assert label in html, f"{label} missing from {path}"


def test_active_tab_is_highlighted() -> None:
    """The nav marks the current route active (Overview on ``/``, Orders on ``/orders``)."""
    overview = _client().get("/").text
    # The Overview link is the active tab on '/'.
    assert 'href="/" class="tab active"' in overview
    orders = _client().get("/orders").text
    assert 'href="/orders" class="tab active"' in orders


# --- health ---------------------------------------------------------------- #


def test_health_shape_and_values() -> None:
    """`GET /api/health` returns ``{status, mode, strategies, read_only}``."""
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "mode": "paper",
        "strategies": 1,
        "read_only": False,
    }


def test_read_only_reflected_everywhere() -> None:
    """`read_only=True` sets ``app.state`` + the health payload + the shell footer."""
    client = _client(read_only=True)
    assert client.get("/api/health").json()["read_only"] is True
    # The shell surfaces the read-only stance in the footer.
    assert "read-only" in client.get("/").text


# --- auth (token login, for remote exposure) ------------------------------- #


def _auth_client(token: str = "secret-token") -> tuple[TestClient, str]:
    return (
        TestClient(create_dashboard_app(_supervisor(), auth_token=token)),
        token,
    )


def test_no_token_means_no_auth() -> None:
    """Default (no `auth_token`) — the dashboard is open (loopback/tunnel use)."""
    assert _client().get("/api/health").status_code == 200


def test_auth_page_redirects_to_login() -> None:
    """With auth on, an unauthenticated page request redirects to /login."""
    client, _ = _auth_client()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_auth_api_requires_a_token() -> None:
    """With auth on, an unauthenticated `/api/health` call is 401."""
    client, _ = _auth_client()
    assert client.get("/api/health").status_code == 401


def test_auth_login_flow_authenticates() -> None:
    """A correct token at /login mints a session cookie that authenticates."""
    client, token = _auth_client()
    assert client.get("/login").status_code == 200  # the form is open
    ok = client.post(
        "/login", data={"token": token, "next": "/"}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert client.get("/api/health").status_code == 200  # session cookie works


# --- aggregate read endpoints (Overview data) ------------------------------ #


def _two_venue_config() -> AppConfig:
    """A paper config with two strategies on different venues (Kraken + Binance)."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "brokers": [
                {"name": "kraken", "exchange": "kraken"},
                {"name": "binance", "exchange": "binance"},
            ],
            "strategies": [
                {
                    "name": "btc-kraken",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
                {
                    "name": "eth-binance",
                    "symbol": "ETH/USDT",
                    "data": {"exchange": "binance", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                },
            ],
        }
    )


def _seed(sup: StrategySupervisor, name: str, sym: Symbol) -> None:
    """Emit a buy→sell round trip on the running unit's engine bus (+8 realised)."""
    inst = Instrument(sym)
    bus = sup._units[name].engine.bus  # noqa: SLF001 — seed the wired bus
    bus.emit(FillEvent(Fill(f"{name}1", f"{name}c1", inst, OrderSide.BUY,
                            money("1"), money("100"), money("1"), 1)))
    bus.emit(FillEvent(Fill(f"{name}2", f"{name}c2", inst, OrderSide.SELL,
                            money("1"), money("110"), money("1"), 2)))


async def _seeded_client() -> TestClient:
    """A dashboard over two running, seeded paper units on different venues."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    _seed(sup, "btc-kraken", Symbol("BTC", "USD"))
    _seed(sup, "eth-binance", Symbol("ETH", "USDT"))
    return TestClient(create_dashboard_app(sup))


async def test_kpi_strategy_level_shape() -> None:
    """`GET /api/kpi?level=strategy` returns a row per unit with money + ratios."""
    client = await _seeded_client()
    rows = client.get("/api/kpi?level=strategy").json()
    assert {r["strategy"] for r in rows} == {"btc-kraken", "eth-binance"}
    row = next(r for r in rows if r["strategy"] == "btc-kraken")
    assert row["realised_pnl"] == "8"  # exact Decimal string, not 8.0
    assert row["fees_paid"] == "2"
    assert row["exchange"] == "kraken"
    assert isinstance(row["sharpe"], (int, float)) or row["sharpe"] is None


async def test_kpi_exchange_level_folds_and_nulls_ratios() -> None:
    """`level=exchange` folds per venue; ratios are JSON null."""
    client = await _seeded_client()
    rows = client.get("/api/kpi?level=exchange").json()
    by_venue = {r["exchange"]: r for r in rows}
    assert set(by_venue) == {"kraken", "binance"}
    assert by_venue["kraken"]["realised_pnl"] == "8"
    assert by_venue["kraken"]["sharpe"] is None  # aggregate ratio → null


async def test_kpi_total_level_sums() -> None:
    """`level=total` is one row summing every unit."""
    client = await _seeded_client()
    [total] = client.get("/api/kpi?level=total").json()
    assert total["key"] == "total"
    assert total["realised_pnl"] == "16"
    assert total["fees_paid"] == "4"


def test_kpi_unknown_level_is_422() -> None:
    """An unknown ``level`` is rejected (422)."""
    assert _client().get("/api/kpi?level=bogus").status_code == 422


async def test_positions_group_by_exchange() -> None:
    """`GET /api/positions?group_by=exchange` buckets rows per venue."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    # Net-long books (buys only) on each venue so positions are non-flat.
    for name, sym in (("btc-kraken", Symbol("BTC", "USD")),
                      ("eth-binance", Symbol("ETH", "USDT"))):
        inst = Instrument(sym)
        sup._units[name].engine.bus.emit(  # noqa: SLF001
            FillEvent(Fill(f"{name}b", f"{name}cb", inst, OrderSide.BUY,
                           money("2"), money("100"), money("1"), 1))
        )
    client = TestClient(create_dashboard_app(sup))
    groups = client.get("/api/positions?group_by=exchange").json()
    keys = {g["group"] for g in groups}
    assert keys == {"kraken", "binance"}
    for g in groups:
        assert all(r["exchange"] == g["group"] for r in g["rows"])


async def test_positions_group_by_crypto() -> None:
    """`group_by=crypto` buckets on the base asset."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    inst = Instrument(Symbol("BTC", "USD"))
    sup._units["btc-kraken"].engine.bus.emit(  # noqa: SLF001
        FillEvent(Fill("b", "cb", inst, OrderSide.BUY,
                       money("2"), money("100"), money("1"), 1))
    )
    client = TestClient(create_dashboard_app(sup))
    groups = client.get("/api/positions?group_by=crypto").json()
    assert [g["group"] for g in groups] == ["BTC"]


def test_positions_ungrouped_is_a_flat_list() -> None:
    """Without ``group_by`` the endpoint returns a flat list (empty when idle)."""
    body = _client().get("/api/positions").json()
    assert body == []


def test_positions_unknown_group_by_is_422() -> None:
    """An unknown ``group_by`` is rejected (422)."""
    assert _client().get("/api/positions?group_by=bogus").status_code == 422


def test_orders_endpoint_present_and_empty() -> None:
    """`GET /api/orders` exists and is an empty list when nothing is open."""
    assert _client().get("/api/orders").json() == []


# --- Overview page markup + live SSE --------------------------------------- #


def test_overview_page_has_kpi_strip_and_tables() -> None:
    """`GET /` carries the KPI strip (level toggle), positions + orders tables."""
    html = _client().get("/").text
    assert 'id="kpi-table"' in html
    assert "kpi-level" in html  # the strategy/exchange/total toggle buttons
    assert 'data-level="strategy"' in html and 'data-level="total"' in html
    assert 'id="positions-table"' in html
    assert "pos-group" in html  # the group-by control
    assert 'data-group="crypto"' in html and 'data-group="exchange"' in html
    assert 'id="orders-table"' in html
    # It wires the merged SSE stream + polling fallback.
    assert "/api/events" in html
    assert "/api/positions" in html
    assert "/api/kpi" in html


async def _never_disconnect() -> dict[str, object]:
    """An ASGI ``receive`` that never reports a disconnect (the consumer stays up)."""
    import asyncio

    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}  # pragma: no cover — never reached


def _events_route(app: object) -> object:
    """The dashboard's ``/api/events`` route handler (drives the merged generator)."""
    return next(
        r.endpoint  # type: ignore[attr-defined]
        for r in app.routes  # type: ignore[attr-defined]
        if getattr(r, "path", None) == "/api/events"
    )


async def test_events_stream_merges_and_yields_a_fill() -> None:
    """`/api/events` fans two units' buses onto one feed; a fill streams through.

    The endpoint serves an infinite ``text/event-stream``; the in-process
    ``TestClient`` deadlocks consuming it, so this drives the endpoint's real
    ``StreamingResponse.body_iterator`` directly — proving it subscribes to
    **every** running unit's bus (a queue on each) and streams a fill emitted on
    one of them, then cleans up all queues on close.
    """
    import json

    from fastapi import Request

    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    app = create_dashboard_app(sup)
    buses = [sup._units[n].engine.bus for n in ("btc-kraken", "eth-binance")]  # noqa: SLF001
    before = [len(b._queues) for b in buses]  # noqa: SLF001

    scope = {
        "type": "http", "method": "GET", "path": "/api/events",
        "headers": [], "query_string": b"", "app": app,
    }
    request = Request(scope, _never_disconnect)
    response = await _events_route(app)(request)  # type: ignore[operator]
    assert response.media_type == "text/event-stream"

    frames = response.body_iterator
    inst = Instrument(Symbol("ETH", "USDT"))
    try:
        first = await frames.__anext__()
        assert first.startswith(":")  # the priming ": connected" comment
        # A queue is registered on EACH running unit's bus (the merge).
        assert [len(b._queues) for b in buses] == [n + 1 for n in before]  # noqa: SLF001
        # Emit a fill on the second unit's bus; it must arrive as a data frame.
        buses[1].emit(FillEvent(Fill("SF1", "sc1", inst, OrderSide.BUY,
                                     money("1"), money("100"), money("1"), 1)))
        frame = await frames.__anext__()
        assert frame.startswith("data:")
        payload = json.loads(frame[len("data:"):].strip())
        assert payload["type"] == "fill"
        assert payload["fill"]["fill_id"] == "SF1"
    finally:
        await frames.aclose()  # disconnect → generator finally removes every queue
    assert [len(b._queues) for b in buses] == before  # noqa: SLF001


# --- CLI: dashboard command ------------------------------------------------ #


def test_dashboard_builds_app_and_calls_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dashboard` builds the app and hands it to ``uvicorn.run`` (patched).

    Patches :func:`uvicorn.run` so no socket opens, then asserts the command
    called it with a FastAPI app and the requested host/port — and that the built
    app really is the dashboard (GET / serves the shell).
    """
    import uvicorn
    from fastapi import FastAPI

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(cli_app, ["dashboard", "--port", "9137"])

    assert result.exit_code == 0, result.output
    assert isinstance(captured["app"], FastAPI)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9137

    test_client = TestClient(captured["app"])
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert "Overview" in resp.text  # the shared shell


def test_dashboard_default_config_is_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``--config``, ``dashboard`` defaults to a paper supervisor."""
    import uvicorn

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(cli_app, ["dashboard"])
    assert result.exit_code == 0, result.output

    test_client = TestClient(captured["app"])
    assert test_client.get("/api/health").json()["mode"] == "paper"


def test_dashboard_non_loopback_without_token_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-loopback ``--host`` with no ``--token`` is refused (never serves)."""
    import uvicorn

    # No TRADING_BOT_UI_TOKEN in the environment, so --host 0.0.0.0 must refuse.
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    called = {"run": False}

    def _fake_run(app: object, **kwargs: object) -> None:  # pragma: no cover
        called["run"] = True

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(cli_app, ["dashboard", "--host", "0.0.0.0"])

    assert result.exit_code != 0
    assert "refusing to bind" in result.output
    assert called["run"] is False  # never reached uvicorn


def test_dashboard_read_only_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--read-only` builds a dashboard whose health reports ``read_only: true``."""
    import uvicorn

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(cli_app, ["dashboard", "--read-only"])
    assert result.exit_code == 0, result.output

    test_client = TestClient(captured["app"])
    assert test_client.get("/api/health").json()["read_only"] is True
