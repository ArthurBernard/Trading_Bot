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
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.interfaces.api import create_dashboard_app
from trading_bot.interfaces.cli.main import app as cli_app

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
