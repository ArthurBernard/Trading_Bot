"""Tests for the daemon's control API — now a thin alias of the unified dashboard.

Drives :func:`~trading_bot.interfaces.api.create_control_app` with a
:class:`fastapi.testclient.TestClient` (no real server) over a
:class:`~trading_bot.application.supervisor.StrategySupervisor` built from a paper
config + a fake dccd client. ``create_control_app`` is now a **backward-compat
wrapper** that delegates to :func:`~trading_bot.interfaces.api.create_dashboard_app`
(``start --serve`` and any lingering imports go through it), so these tests prove
the wrapper still exposes the read+write control plane and that **real money is
gated** (live needs an explicit confirmation → ``403`` otherwise). The full control
surface (deploy/remove, the page shells) is covered by ``test_dashboard.py``.
"""

from __future__ import annotations

import polars as pl
import pytest
from fastapi.testclient import TestClient

from trading_bot.application.config import AppConfig
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.interfaces.api import create_control_app


def _dccd_ohlc(closes: list[float]) -> pl.DataFrame:
    span_ns = 60 * 1_000_000_000
    return pl.DataFrame(
        {
            "TS": [i * span_ns for i in range(len(closes))],
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1.0] * len(closes),
            "quote_volume": list(closes),
            "trades": [1] * len(closes),
        }
    )


class _FakeDccdClient:
    def __init__(self, frames: dict[str, pl.DataFrame]) -> None:
        self._frames = frames

    def read(self, exchange, symbol, data_type="ohlc", span=None, start_ns=None, end_ns=None):  # noqa: ANN001, ANN201
        return self._frames[symbol]

    def backfill(self, *a, **k):  # noqa: ANN002, ANN003, ANN201  # pragma: no cover
        return None


def _config() -> AppConfig:
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


def _client() -> TestClient:
    trend = [100.0 + i for i in range(20)] + [119.0 - i for i in range(1, 21)]
    sup = StrategySupervisor(
        _config(), dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(trend)})
    )
    return TestClient(create_control_app(sup))


def test_list_strategies() -> None:
    """`GET /api/strategies` lists the managed units (stopped, paper to start)."""
    resp = _client().get("/api/strategies")
    assert resp.status_code == 200
    [s] = resp.json()
    assert s["name"] == "btc-ma"
    assert s["exchange"] == "kraken"  # grouped/displayed by exchange
    assert s["mode"] == "paper"
    assert s["running"] is False
    assert s["realised_pnl"] is None


def test_set_mode_testnet_then_paper() -> None:
    """Switching paper ↔ testnet needs no confirmation and updates the mode."""
    client = _client()
    r = client.post("/api/strategies/btc-ma/mode", json={"mode": "testnet"})
    assert r.status_code == 200
    assert r.json()["status"]["mode"] == "testnet"
    r = client.post("/api/strategies/btc-ma/mode", json={"mode": "paper"})
    assert r.json()["status"]["mode"] == "paper"


def test_set_mode_live_without_confirmation_is_403() -> None:
    """Switching to live (real money) without confirmation is refused — nothing changes."""
    client = _client()
    r = client.post("/api/strategies/btc-ma/mode", json={"mode": "live"})
    assert r.status_code == 403
    # Mode unchanged.
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"


def test_set_mode_live_with_confirmation_flips() -> None:
    """With the deliberate confirmation, the mode flips to live."""
    client = _client()
    r = client.post(
        "/api/strategies/btc-ma/mode", json={"mode": "live", "confirm": True}
    )
    assert r.status_code == 200
    assert r.json()["status"]["mode"] == "live"


def test_unknown_strategy_is_404() -> None:
    r = _client().post("/api/strategies/nope/start")
    assert r.status_code == 404


def test_unknown_mode_is_400() -> None:
    r = _client().post("/api/strategies/btc-ma/mode", json={"mode": "bogus"})
    assert r.status_code == 400


def test_control_wrapper_serves_the_unified_dashboard() -> None:
    """`create_control_app` now serves the unified dashboard shell (Overview at `/`)."""
    resp = _client().get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "trading_bot" in html
    # The unified shell's nav links every page — Strategies (control) among them.
    assert "Overview" in html and "Strategies" in html and "Orders" in html


def test_control_wrapper_strategies_page_has_the_control_surface() -> None:
    """The Strategies page carries the control table + the deliberate go-live modal."""
    html = _client().get("/strategies").text
    assert 'id="strategies-body"' in html  # the table the page fills
    assert 'id="live-modal"' in html  # the deliberate go-live confirmation
    assert "I UNDERSTAND" in html  # the typed-confirmation phrase


def test_control_wrapper_health_is_the_dashboard_shape() -> None:
    """The wrapper's `/api/health` is the unified dashboard's shape (mode + read_only)."""
    body = _client().get("/api/health").json()
    assert body == {
        "status": "ok",
        "mode": "paper",
        "strategies": 1,
        "read_only": False,
    }


def test_start_then_stop() -> None:
    """`POST start` runs the strategy in its own engine; `POST stop` tears it down."""
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    client = _client()

    r = client.post("/api/strategies/btc-ma/start")
    assert r.status_code == 200
    assert r.json()["status"]["running"] is True

    r = client.post("/api/strategies/btc-ma/stop")
    assert r.status_code == 200
    assert r.json()["status"]["running"] is False


# --- auth (token login, for remote exposure) ------------------------------- #


def _auth_client(token: str = "secret-token") -> tuple[TestClient, str]:
    trend = [100.0 + i for i in range(20)] + [119.0 - i for i in range(1, 21)]
    sup = StrategySupervisor(
        _config(), dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(trend)})
    )
    return TestClient(create_control_app(sup, auth_token=token)), token


def test_no_token_means_no_auth() -> None:
    """Default (no `auth_token`) — the app is open (loopback/tunnel use)."""
    assert _client().get("/api/strategies").status_code == 200


def test_auth_api_requires_a_token() -> None:
    """With auth on, an unauthenticated `/api/*` call is 401."""
    client, _ = _auth_client()
    assert client.get("/api/strategies").status_code == 401


def test_auth_bearer_and_query_token_work() -> None:
    """`/api/*` accepts a Bearer header or `?token=` (non-browser clients)."""
    client, token = _auth_client()
    assert client.get(
        "/api/strategies", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200
    assert client.get(f"/api/strategies?token={token}").status_code == 200


def test_auth_page_redirects_to_login() -> None:
    """An unauthenticated page request redirects to /login."""
    client, _ = _auth_client()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_auth_login_flow_sets_session_cookie() -> None:
    """A correct token at /login mints a session cookie that authenticates; logout clears it."""
    client, token = _auth_client()
    assert client.get("/login").status_code == 200  # the form is open

    bad = client.post(
        "/login", data={"token": "nope", "next": "/"}, follow_redirects=False
    )
    assert bad.status_code == 401
    assert client.get("/api/strategies").status_code == 401  # still no session

    ok = client.post(
        "/login", data={"token": token, "next": "/"}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert client.get("/api/strategies").status_code == 200  # session cookie works

    client.post("/logout", follow_redirects=False)
    assert client.get("/api/strategies").status_code == 401  # cleared


def test_auth_login_is_rate_limited() -> None:
    """Repeated login attempts are throttled (429) — brute-force guard."""
    client, _ = _auth_client()
    statuses = [
        client.post(
            "/login", data={"token": "x", "next": "/"}, follow_redirects=False
        ).status_code
        for _ in range(20)
    ]
    assert 429 in statuses
