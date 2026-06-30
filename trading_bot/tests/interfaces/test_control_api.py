"""Tests for the daemon's **control** API — start/stop/mode over a supervisor.

Drives :func:`~trading_bot.interfaces.api.create_control_app` with a
:class:`fastapi.testclient.TestClient` (no real server) over a
:class:`~trading_bot.application.supervisor.StrategySupervisor` built from a paper
config + a fake dccd client. Proves the read+write control plane, and that **real
money is gated** (live needs an explicit confirmation → ``403`` otherwise).
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


def test_control_dashboard_renders() -> None:
    """`GET /` returns the control dashboard shell (brand + table + live modal)."""
    resp = _client().get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "trading_bot" in html
    assert 'id="strategies-body"' in html  # the table control.js fills
    assert 'id="live-modal"' in html  # the deliberate go-live confirmation


def test_control_js_is_served() -> None:
    """The control dashboard's script is served and carries the confirm phrase."""
    resp = _client().get("/static/control.js")
    assert resp.status_code == 200
    assert "I UNDERSTAND" in resp.text


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
