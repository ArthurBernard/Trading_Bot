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
from trading_bot.interfaces.api import create_control_app, create_dashboard_app


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

    def read(
        self, exchange, symbol, data_type="ohlc", span=None, start_ns=None, end_ns=None
    ):  # noqa: ANN001, ANN201
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
    """With the typed acknowledgement phrase, the mode flips to live."""
    client = _client()
    r = client.post(
        "/api/strategies/btc-ma/mode",
        json={"mode": "live", "confirm": True, "ack": "I UNDERSTAND"},
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


def test_control_wrapper_strategies_page_is_the_linked_roster() -> None:
    """The Strategies page is the linked roster; its rows deep-link to detail pages."""
    html = _client().get("/strategies").text
    assert 'id="strategies-body"' in html  # the table the page fills
    assert 'href="/strategies/' in html  # rows link to the per-strategy detail page


def test_control_wrapper_detail_page_has_the_control_surface() -> None:
    """The per-strategy detail page carries the control surface + go-live modal.

    The mode <select> + typed go-live confirmation moved off the roster onto the
    per-strategy detail page (`/strategies/{name}`); the wrapper serves it too.
    """
    html = _client().get("/strategies/btc-ma").text
    assert 'id="detail-header"' in html  # the header + control block
    assert 'id="live-modal"' in html  # the deliberate go-live confirmation
    assert "I UNDERSTAND" in html  # the typed-confirmation phrase


def test_control_wrapper_health_is_the_dashboard_shape() -> None:
    """The wrapper's `/api/health` is the unified dashboard's shape (mode + read_only).

    `create_control_app` wires no `schedule_info` hook, so the cadence fields stay
    `null` — same scheduler-agnostic default as the plain `dashboard` command.
    """
    body = _client().get("/api/health").json()
    assert body == {
        "status": "ok",
        "mode": "paper",
        "strategies": 1,
        "read_only": False,
        "next_tick_ts": None,
        "tick": None,
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
    assert (
        client.get(
            "/api/strategies", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )
    assert client.get(f"/api/strategies?token={token}").status_code == 200


def test_auth_page_redirects_to_login() -> None:
    """An unauthenticated page request redirects to /login."""
    client, _ = _auth_client()
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def _csrf(client: TestClient) -> str:
    """GET /login and return the double-submit CSRF token it set as a cookie (I-13)."""
    assert client.get("/login").status_code == 200  # the form is open + sets the cookie
    return client.cookies.get("tb_csrf", "")


def test_auth_login_flow_sets_session_cookie() -> None:
    """A correct token at /login mints a session cookie that authenticates; logout clears it."""
    client, token = _auth_client()
    csrf = _csrf(client)
    assert csrf  # GET /login set the CSRF cookie

    bad = client.post(
        "/login",
        data={"token": "nope", "next": "/", "csrf": csrf},
        follow_redirects=False,
    )
    assert bad.status_code == 401
    assert client.get("/api/strategies").status_code == 401  # still no session

    ok = client.post(
        "/login",
        data={"token": token, "next": "/", "csrf": _csrf(client)},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert client.get("/api/strategies").status_code == 200  # session cookie works

    client.post("/logout", follow_redirects=False)
    assert client.get("/api/strategies").status_code == 401  # cleared


def test_auth_login_without_csrf_is_403() -> None:
    """A login POST missing the CSRF token is refused (I-13 — login-CSRF guard)."""
    client, token = _auth_client()
    _csrf(client)  # the cookie is set, but the form omits the field
    r = client.post(
        "/login", data={"token": token, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 403
    assert client.get("/api/strategies").status_code == 401  # no session minted


def test_auth_login_is_rate_limited() -> None:
    """Repeated login attempts are throttled (429) — brute-force guard."""
    client, _ = _auth_client()
    csrf = _csrf(client)
    statuses = [
        client.post(
            "/login",
            data={"token": "x", "next": "/", "csrf": csrf},
            follow_redirects=False,
        ).status_code
        for _ in range(20)
    ]
    assert 429 in statuses


# --- capital control plane: deposit / withdraw / policy -------------------- #


def _capital_config(db_path: str) -> AppConfig:
    """A paper BTC/USD strategy declaring an ``allocation`` + its own store."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db_path},
            "brokers": [{"name": "kraken", "exchange": "kraken"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                    "allocation": "100",
                }
            ],
        }
    )


def _capital_supervisor(db_path: str, *, started: bool = False) -> StrategySupervisor:
    trend = [100.0 + i for i in range(20)] + [119.0 - i for i in range(1, 21)]
    sup = StrategySupervisor(
        _capital_config(db_path),
        dccd_client=_FakeDccdClient({"BTC/USD": _dccd_ohlc(trend)}),
    )
    if started:
        import asyncio

        asyncio.run(sup.start("btc-ma"))
    return sup


def _capital_client(
    db_path: str,
    *,
    started: bool = False,
    read_only: bool = False,
) -> TestClient:
    return TestClient(
        create_dashboard_app(
            _capital_supervisor(db_path, started=started), read_only=read_only
        )
    )


def test_capital_get_lists_the_breakdown_and_genesis(tmp_path) -> None:  # noqa: ANN001
    """`GET .../capital` returns the breakdown (contributed + the genesis event)."""
    pytest.importorskip("fynance")  # started unit seeds the genesis via build_runners
    client = _capital_client(str(tmp_path / "book.sqlite"), started=True)
    body = client.get("/api/strategies/btc-ma/capital").json()
    assert body["contributed"] == "100"  # exact Decimal string, not 100.0
    assert body["allocation"] == "100"
    assert body["policy"] == "fixed"
    assert "btc-ma:funding" in {e["event_id"] for e in body["events"]}


def test_capital_deposit_then_idempotent_retry(tmp_path) -> None:  # noqa: ANN001
    """`POST .../capital` deposits; the same `op_id` re-POSTed is a no-op (idempotent)."""
    client = _capital_client(str(tmp_path / "book.sqlite"))
    r1 = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": "50", "op_id": "op-1"},
    )
    assert r1.status_code == 200
    assert r1.json()["contributed"] == "150"
    # Retry the exact same op_id → 200 and still 150 (idempotency on the wire).
    r2 = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": "50", "op_id": "op-1"},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # A distinct op_id accumulates.
    r3 = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": "50", "op_id": "op-2"},
    )
    assert r3.json()["contributed"] == "200"


def test_capital_withdraw_over_limit_is_422_with_figure(tmp_path) -> None:  # noqa: ANN001
    """A withdrawal beyond withdrawable is 422 carrying the exact withdrawable figure."""
    client = _capital_client(str(tmp_path / "book.sqlite"))  # flat → withdrawable 100
    r = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "withdraw", "amount": "150", "op_id": "w1"},
    )
    assert r.status_code == 422
    assert "100" in r.json()["detail"]  # the exact withdrawable figure


def test_capital_valid_withdraw_reduces_contributed(tmp_path) -> None:  # noqa: ANN001
    """A valid withdrawal drops contributed by the amount."""
    client = _capital_client(str(tmp_path / "book.sqlite"))
    r = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "withdraw", "amount": "40", "op_id": "w1"},
    )
    assert r.status_code == 200
    assert r.json()["contributed"] == "60"


def test_capital_amount_as_json_float_is_422(tmp_path) -> None:  # noqa: ANN001
    """A float `amount` (not a string) is refused — money crosses the wire as a string."""
    client = _capital_client(str(tmp_path / "book.sqlite"))
    r = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": 12.5, "op_id": "op-1"},
    )
    assert r.status_code == 422


def test_capital_unknown_strategy_is_404(tmp_path) -> None:  # noqa: ANN001
    """A capital op on an unknown unit is 404."""
    client = _capital_client(str(tmp_path / "book.sqlite"))
    r = client.post(
        "/api/strategies/nope/capital",
        json={"action": "deposit", "amount": "1", "op_id": "op-1"},
    )
    assert r.status_code == 404
    assert client.get("/api/strategies/nope/capital").status_code == 404


def test_capital_ops_refused_when_read_only(tmp_path) -> None:  # noqa: ANN001
    """`read_only=True` refuses both POSTs (403); the GET read stays available."""
    client = _capital_client(str(tmp_path / "book.sqlite"), read_only=True)
    dep = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": "1", "op_id": "op-1"},
    )
    assert dep.status_code == 403
    pol = client.post("/api/strategies/btc-ma/policy", json={"policy": "compound"})
    assert pol.status_code == 403
    assert client.get("/api/strategies/btc-ma/capital").status_code == 200


def test_capital_op_requires_auth(tmp_path) -> None:  # noqa: ANN001
    """With auth on, an unauthenticated capital POST is 401."""
    sup = _capital_supervisor(str(tmp_path / "book.sqlite"))
    client = TestClient(create_dashboard_app(sup, auth_token="secret-token"))
    r = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": "1", "op_id": "op-1"},
    )
    assert r.status_code == 401


def test_capital_op_on_live_unit_is_409(tmp_path) -> None:  # noqa: ANN001
    """A deposit on a live-mode unit is 409 (real-money ops deferred to real keys)."""
    client = _capital_client(str(tmp_path / "book.sqlite"))
    # Flip the (stopped) unit to live over the wire (typed acknowledgement).
    flip = client.post(
        "/api/strategies/btc-ma/mode",
        json={"mode": "live", "confirm": True, "ack": "I UNDERSTAND"},
    )
    assert flip.status_code == 200
    r = client.post(
        "/api/strategies/btc-ma/capital",
        json={"action": "deposit", "amount": "50", "op_id": "op-1"},
    )
    assert r.status_code == 409


def test_capital_policy_flip_persists_to_the_manifest(tmp_path) -> None:  # noqa: ANN001
    """`POST .../policy` flips the policy, reflects it, and persists the manifest YAML."""
    import yaml

    db = str(tmp_path / "book.sqlite")
    manifest = tmp_path / "manifest.yaml"
    sup = _capital_supervisor(db)
    client = TestClient(
        create_dashboard_app(sup, on_change=lambda: sup.manifest().to_yaml(manifest))
    )
    r = client.post("/api/strategies/btc-ma/policy", json={"policy": "compound"})
    assert r.status_code == 200
    assert r.json()["policy"] == "compound"
    # The scratch manifest on disk now carries the flipped policy.
    on_disk = yaml.safe_load(manifest.read_text())
    assert on_disk["strategies"][0]["capital_policy"] == "compound"


def test_capital_policy_unknown_is_422_at_the_body(tmp_path) -> None:  # noqa: ANN001
    """An unrecognised policy is rejected by the body model (422)."""
    client = _capital_client(str(tmp_path / "book.sqlite"))
    r = client.post("/api/strategies/btc-ma/policy", json={"policy": "bogus"})
    assert r.status_code == 422
