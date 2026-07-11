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

# Built-in
import asyncio
import json
import logging
import time
from decimal import Decimal

# Third-party
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trading_bot.application.accounting import Violation
from trading_bot.application.config import AppConfig
from trading_bot.application.events import FillEvent, LogEvent, OrderEvent
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import Order, OrderSide, OrderType
from trading_bot.interfaces.api import create_dashboard_app
from trading_bot.interfaces.cli.main import app as cli_app
from trading_bot.tests.application.test_supervisor import (
    _dccd_ohlc,
    _FakeDccdClient,
    _trend,
    _two_venue_client,
)

runner = CliRunner()


def _FakeStartClient() -> _FakeDccdClient:  # noqa: N802 — factory named like a class
    """An offline dccd client for the single BTC/USD strategy (start() never imports dccd)."""
    return _FakeDccdClient({"BTC/USD": _dccd_ohlc(_trend())})


#: Page routes rendered as shells over ``base.html`` — the four nav tabs (Overview
#: at ``/``) plus the two sub-pages under Strategies (the deploy form and a
#: per-strategy detail page for the declared ``btc-ma`` unit). Every one extends the
#: shared shell, so the parametrized shell tests below sweep them all.
_PAGES = (
    "/",
    "/strategies",
    "/strategies/new",
    "/strategies/btc-ma",
    "/orders",
    "/logs",
)

#: Nav labels every page carries (proves the shared shell, not a bespoke page). The
#: PnL tab retired into the per-strategy detail page — the nav is four tabs now.
_NAV_LABELS = ("Overview", "Strategies", "Orders", "Logs")


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


@pytest.mark.parametrize("path", _PAGES)
def test_every_page_references_format_js(path: str) -> None:
    """Each page's shell (base.html) loads format.js before the inline helpers.

    Leaf 02 (ui-ux-overhaul): the shared `tbFmt` display formatters (rounding,
    units, currency) must be available on every page, not just the ones that
    happen to render money — base.html is the single include point.
    """
    html = _client().get(path).text
    assert "/static/format.js" in html, path


@pytest.mark.parametrize("path", _PAGES)
def test_every_page_carries_the_cadence_helpers(path: str) -> None:
    """Every page's shell (base.html) exposes the shared `tbTime` cadence helpers.

    Leaf 04 (ui-ux-overhaul): the live next-tick/next-bar countdowns and the
    "updated at" stamps are driven from base.html's single shared 1s tick, so
    every page must carry it — not just the ones with a countdown cell today.
    """
    html = _client().get(path).text
    assert "tbTime" in html, path
    assert "data-countdown-ts" in html, path
    assert "stampUpdated" in html, path


@pytest.mark.parametrize("path", _PAGES)
def test_every_page_carries_the_status_badge_language(path: str) -> None:
    """Every page's shell (base.html) carries the order-status badge CSS + helper.

    Leaf 05 (ui-ux-overhaul): order-status badges (Orders page + Overview
    open-orders) share one `statusBadge()` JS helper and `.badge-status-*` CSS
    defined once in base.html, so every page must carry them.
    """
    html = _client().get(path).text
    assert "statusBadge" in html, path
    assert ".badge-status-ok" in html, path
    assert ".badge-status-warn" in html, path
    assert ".badge-status-err" in html, path


@pytest.mark.parametrize("path", _PAGES)
def test_every_page_carries_the_health_pill_helper(path: str) -> None:
    """Every page's shell (base.html) carries the shared health-pill helper.

    Leaf 04 (accounting-guardrail): the per-strategy health pill (roster row +
    detail header) is fed by `healthPillHtml()`, defined once in base.html and
    reusing the existing `.badge-status-warn`/`.badge-status-err` CSS pair
    (leaf 05's order-status badges) rather than inventing new pill classes.
    """
    html = _client().get(path).text
    assert "healthPillHtml" in html, path


@pytest.mark.parametrize("path", _PAGES)
def test_nav_lists_four_tabs_and_no_pnl(path: str) -> None:
    """Every page's nav lists the four surviving tabs; the retired PnL tab is gone.

    The `/pnl` tab retired into the per-strategy detail page (its equity chart
    lives on `/strategies/{name}` now), so no page shell links `/pnl` any more.
    """
    html = _client().get(path).text
    for href in ('href="/"', 'href="/strategies"', 'href="/orders"', 'href="/logs"'):
        assert href in html, (href, path)
    assert 'href="/pnl"' not in html, path


def test_active_tab_is_highlighted() -> None:
    """The nav marks the current route active (Overview on ``/``, Orders on ``/orders``)."""
    overview = _client().get("/").text
    # The Overview link is the active tab on '/'.
    assert 'href="/" class="tab active"' in overview
    orders = _client().get("/orders").text
    assert 'href="/orders" class="tab active"' in orders


# --- health ---------------------------------------------------------------- #


def test_health_shape_and_values() -> None:
    """`GET /api/health` returns the health shape; `next_tick_ts`/`tick` null by default.

    With no `schedule_info` hook (the plain `dashboard` command has no scheduler),
    the cadence fields stay `null` — a scheduler-agnostic health payload.
    """
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "mode": "paper",
        "strategies": 1,
        "read_only": False,
        "next_tick_ts": None,
        "tick": None,
        "worst": "ok",
        "unhealthy": 0,
    }


def test_health_schedule_info_hook_surfaces_cadence() -> None:
    """A `schedule_info` hook's `next_tick_ts` / `tick` surface on `/api/health`."""
    app = create_dashboard_app(
        _supervisor(),
        schedule_info=lambda: {"next_tick_ts": 1_700_000_000_000, "tick": "every 60s"},
    )
    body = TestClient(app).get("/api/health").json()
    assert body["next_tick_ts"] == 1_700_000_000_000
    assert body["tick"] == "every 60s"


def test_health_schedule_info_hook_that_raises_degrades_to_nulls() -> None:
    """A raising `schedule_info` hook never breaks health — it degrades to nulls."""

    def _boom() -> dict[str, object]:
        raise RuntimeError("scheduler unavailable")

    app = create_dashboard_app(_supervisor(), schedule_info=_boom)
    resp = TestClient(app).get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_tick_ts"] is None
    assert body["tick"] is None


async def test_api_health_worst_and_count() -> None:
    """`worst` is the worst *running* unit's health; `unhealthy` counts every non-ok unit.

    Two running units (``btc-kraken`` clean, ``eth-binance`` seeded warn) prove
    the worst-of aggregation and the count; stopping the unhealthy unit then
    proves `unhealthy` still counts it (a stopped unit keeps its last cached
    report) while `worst` drops (it only looks at running units). Every
    pre-existing `/api/health` field must still be present and correct — this
    is a public-contract regression check, not just the two new fields.
    """
    import trading_bot.application.supervisor as sup_mod

    # A frozen clock: the seeded `unit.accounting` below carries `computed_at_ms
    # == 0`, so it must stay inside the 60s TTL window for every `/api/health`
    # call below (a real wall clock would blow straight past it and recompute
    # a fresh, and here always empty, report instead of serving what was seeded).
    sup = StrategySupervisor(
        _two_venue_config(), dccd_client=_two_venue_client(), clock=lambda: 0
    )
    await sup.start_all()

    warn = Violation(
        kind="duplicate_venue_ids",
        severity="warn",
        subject="VID-1",
        detail="venue_order_id 'VID-1' is shared by 2 order rows.",
        measured="2",
        expected="1",
    )
    error = Violation(
        kind="position_drift",
        severity="error",
        subject="ETH/USDT",
        detail="ETH/USDT: tracked net qty disagrees with its fills.",
        measured="5",
        expected="3",
    )
    sup._units["btc-kraken"].accounting = sup_mod._AccountingState(  # noqa: SLF001
        violations=[], computed_at_ms=0
    )
    sup._units["eth-binance"].accounting = sup_mod._AccountingState(  # noqa: SLF001
        violations=[warn], computed_at_ms=0
    )

    client = TestClient(create_dashboard_app(sup))
    body = client.get("/api/health").json()
    # Every pre-existing field, unchanged.
    assert body["status"] == "ok"
    assert body["mode"] == "paper"
    assert body["strategies"] == 2
    assert body["read_only"] is False
    assert body["next_tick_ts"] is None
    assert body["tick"] is None
    # Both units running: worst is the warn one; one unhealthy unit.
    assert body["worst"] == "warn"
    assert body["unhealthy"] == 1

    # Escalate to error (still running) -> worst follows.
    sup._units["eth-binance"].accounting = sup_mod._AccountingState(  # noqa: SLF001
        violations=[error], computed_at_ms=0
    )
    escalated = client.get("/api/health").json()
    assert escalated["worst"] == "error"
    assert escalated["unhealthy"] == 1

    # Stop the unhealthy unit: `worst` only looks at running units (drops to
    # "ok", the one clean unit left running); `unhealthy` still counts it (its
    # cached report survives stop).
    await sup.stop("eth-binance")
    stopped = client.get("/api/health").json()
    assert stopped["worst"] == "ok"
    assert stopped["unhealthy"] == 1


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
    """A correct token + CSRF at /login mints a session cookie that authenticates."""
    client, token = _auth_client()
    assert (
        client.get("/login").status_code == 200
    )  # the form is open + sets CSRF cookie
    csrf = client.cookies.get("tb_csrf", "")
    assert csrf
    ok = client.post(
        "/login",
        data={"token": token, "next": "/", "csrf": csrf},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert client.get("/api/health").status_code == 200  # session cookie works


def test_login_survives_a_background_login_rerender_stable_csrf() -> None:
    """The CSRF cookie is stable across /login renders — the favicon race.

    A browser fetches ``/favicon.ico`` in the background right after loading the
    login page. Before the fix that fetch was auth-redirected to ``/login``,
    whose render minted a FRESH token and rotated the ``tb_csrf`` cookie under
    the form the user was already looking at — so every submit 403'd, forever.
    The token embedded in the FIRST render's form must still authenticate after
    a second (background) render.
    """
    import re as _re

    client, token = _auth_client()
    first = client.get("/login")
    m = _re.search(r'name="csrf" value="([^"]+)"', first.text)
    assert m, "login form must embed the CSRF field"
    form_csrf = m.group(1)  # what the user's visible form will submit
    # A background request re-renders /login (pre-fix: rotated the cookie).
    client.get("/login?next=/favicon.ico")
    assert client.cookies.get("tb_csrf", "") == form_csrf  # cookie is STABLE
    ok = client.post(
        "/login",
        data={"token": token, "next": "/", "csrf": form_csrf},
        follow_redirects=False,
    )
    assert ok.status_code == 303  # the form the user sees still works


def test_favicon_is_open_and_never_bounces_to_login() -> None:
    """``/favicon.ico`` needs no session and never triggers a /login re-render."""
    client, _ = _auth_client()
    r = client.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/static/favicon.svg"


def test_login_render_rejects_a_malformed_csrf_cookie() -> None:
    """A cookie not matching our minted shape is replaced, never echoed back."""
    client, _ = _auth_client()
    # Raw header (not the cookie jar) so the single malformed value is exactly
    # what the server sees.
    page = client.get("/login", headers={"cookie": "tb_csrf=<script>alert(1)</script>"})
    assert "<script>alert(1)" not in page.text
    set_cookie = page.headers.get("set-cookie", "")
    assert "tb_csrf=" in set_cookie and "<script>" not in set_cookie  # fresh mint


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
    bus.emit(
        FillEvent(
            Fill(
                f"{name}1",
                f"{name}c1",
                inst,
                OrderSide.BUY,
                money("1"),
                money("100"),
                money("1"),
                1,
            )
        )
    )
    bus.emit(
        FillEvent(
            Fill(
                f"{name}2",
                f"{name}c2",
                inst,
                OrderSide.SELL,
                money("1"),
                money("110"),
                money("1"),
                2,
            )
        )
    )


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
    assert row["quote"] == "USD"  # from the unit's own symbol (BTC/USD)
    assert isinstance(row["sharpe"], (int, float)) or row["sharpe"] is None


async def test_kpi_exchange_level_folds_and_nulls_ratios() -> None:
    """`level=exchange` folds per venue; ratios are JSON null."""
    client = await _seeded_client()
    rows = client.get("/api/kpi?level=exchange").json()
    by_venue = {r["exchange"]: r for r in rows}
    assert set(by_venue) == {"kraken", "binance"}
    assert by_venue["kraken"]["realised_pnl"] == "8"
    assert by_venue["kraken"]["sharpe"] is None  # aggregate ratio → null
    # A single-strategy venue folds to that strategy's own (non-mixed) quote.
    assert by_venue["kraken"]["quote"] == "USD"
    assert by_venue["binance"]["quote"] == "USDT"


async def test_kpi_total_level_sums() -> None:
    """`level=total` is one row summing every unit; a mixed-quote group is null."""
    client = await _seeded_client()
    [total] = client.get("/api/kpi?level=total").json()
    assert total["key"] == "total"
    assert total["realised_pnl"] == "16"
    assert total["fees_paid"] == "4"
    # btc-kraken trades USD, eth-binance USDT — the folded total mixes quote
    # currencies, so the UI must render "mixed" rather than a wrong single label.
    assert total["quote"] is None


def test_kpi_unknown_level_is_422() -> None:
    """An unknown ``level`` is rejected (422)."""
    assert _client().get("/api/kpi?level=bogus").status_code == 422


# --- PnL series endpoint (/api/pnl) ---------------------------------------- #


def _pnl_config_with_store(db_path: str) -> AppConfig:
    """A paper BTC/USD strategy whose engine persists to (restores from) ``db_path``."""
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
                }
            ],
        }
    )


def test_pnl_endpoint_returns_the_paper_series(tmp_path) -> None:  # noqa: ANN001
    """`GET /api/pnl?strategy=btc-ma` returns the paper series with exact money.

    Seeds a paper round trip (+8 realised) to the store, starts the unit, and
    asserts the endpoint's final equity matches `v0 + realised PnL` as an exact
    Decimal string (not a float), timestamps as integer ms.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    store.record_fill(
        Fill("SF1", "sc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.record_fill(
        Fill(
            "SF2", "sc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2
        )
    )

    sup = StrategySupervisor(_pnl_config_with_store(db), dccd_client=_FakeStartClient())

    import asyncio

    asyncio.run(sup.start("btc-ma"))
    client = TestClient(create_dashboard_app(sup))

    body = client.get("/api/pnl?strategy=btc-ma").json()
    assert body["strategy"] == "btc-ma"
    assert "paper" in body["series"]
    series = body["series"]["paper"]
    assert series  # non-empty
    # ts is integer ms; money is an exact string.
    assert all(isinstance(row[0], int) for row in series)
    v0 = body["v0"]  # exact Decimal string
    # Final equity == v0 + 8 (the round trip's realised PnL), as a Decimal string.
    from decimal import Decimal

    assert Decimal(series[-1][2]) == Decimal(v0) + Decimal("8")
    assert body["current"]["paper"]["equity"] == series[-1][2]


def test_pnl_endpoint_mode_filter(tmp_path) -> None:  # noqa: ANN001
    """`?mode=testnet` returns only the testnet series (live/testnet stay separate)."""
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    store.set_context(mode="paper", venue="")
    store.record_fill(
        Fill("PF1", "pc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.set_context(mode="testnet", venue="binance")
    store.record_fill(
        Fill("TF1", "tc1", inst, OrderSide.BUY, money("1"), money("100"), money("1"), 2)
    )

    sup = StrategySupervisor(_pnl_config_with_store(db), dccd_client=_FakeStartClient())
    client = TestClient(create_dashboard_app(sup))  # unit stopped → reads the db

    body = client.get("/api/pnl?strategy=btc-ma&mode=testnet").json()
    assert set(body["series"]) == {"testnet"}
    all_modes = client.get("/api/pnl?strategy=btc-ma").json()
    assert set(all_modes["series"]) == {"paper", "testnet"}


def test_pnl_endpoint_unknown_strategy_is_404() -> None:
    """An unknown ``strategy`` is a 404."""
    assert _client().get("/api/pnl?strategy=nope").status_code == 404


def test_pnl_endpoint_no_fills_is_empty_200() -> None:
    """A strategy with no persisted fills is an empty series (200, not an error)."""
    resp = _client().get("/api/pnl?strategy=btc-ma")
    assert resp.status_code == 200
    assert resp.json()["series"] == {}


def test_pnl_endpoint_unknown_mode_is_422() -> None:
    """An unknown ``mode`` filter is rejected (422)."""
    assert _client().get("/api/pnl?strategy=btc-ma&mode=bogus").status_code == 422


# --- Per-strategy detail page (the retired /pnl chart lives here now) ------- #


def test_pnl_redirects_to_overview() -> None:
    """`GET /pnl` (the retired tab) 303-redirects to Overview so bookmarks survive."""
    r = _client().get("/pnl", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_strategy_detail_serves_the_shell_with_name_and_sections() -> None:
    """`GET /strategies/{name}` is a 200 shell naming the strategy + its sections.

    The per-strategy home merges the retired /pnl chart (uPlot equity + per-mode
    stats, pre-bound to this strategy — no selector) with the moved control surface
    (mode <select> + go-live modal) and this strategy's positions, orders and fills.
    """
    html = _client().get("/strategies/btc-ma").text
    assert "btc-ma" in html  # the name injected server-side
    assert 'id="detail-header"' in html  # the header + control block
    # The equity chart (moved from /pnl), pre-bound to this strategy (no selector).
    assert 'id="pnl-chart"' in html  # the uPlot mount
    assert 'id="pnl-strategy"' not in html  # no strategy dropdown — it's pre-bound
    assert "/static/uplot.min.js" in html  # the vendored chart library
    assert "/static/uplot.min.css" in html  # its stylesheet
    assert "/api/pnl" in html  # it fetches the per-mode series
    # The stats table's Return column + the muted starting-capital note.
    assert '<th class="num">Return</th>' in html
    assert 'id="pnl-v0-note"' in html
    # This strategy's positions / orders / fills tables.
    assert 'id="positions-table"' in html
    assert 'id="orders-table"' in html
    assert 'id="fills-table"' in html
    # The control surface + go-live modal moved here from the roster row.
    assert "mode-select" in html
    assert 'id="live-modal"' in html
    assert "I UNDERSTAND" in html


def test_strategy_detail_unknown_name_is_404() -> None:
    """`GET /strategies/{name}` for an unmanaged name is a 404 (not a blank shell)."""
    assert _client().get("/strategies/does-not-exist").status_code == 404


def test_strategy_detail_read_only_guards_the_controls() -> None:
    """A read-only detail page surfaces the flag its client-rendered controls gate on."""
    html = _client(read_only=True).get("/strategies/btc-ma").text
    assert "const TB_READ_ONLY = true" in html  # the shell flag
    # The control render is gated behind that flag (no mode switch / start-stop /
    # remove when read-only) — the guard the client obeys.
    assert "if (TB_READ_ONLY)" in html


def test_strategy_detail_has_the_capital_card_and_ledger_expander() -> None:
    """The detail page carries the CAPITAL block: equation, withdrawable, ledger, modal.

    Leaf 08 — a pure consumer of leaf 07's GET/POST .../capital + POST .../policy;
    the card sits above the equity chart (starting capital + PnL = total value).
    """
    html = _client().get("/strategies/btc-ma").text
    assert 'id="capital-card"' in html
    # The equation cells (starting capital + realised/unrealised = total value).
    assert 'id="cap-contributed"' in html
    assert 'id="cap-realised"' in html
    assert 'id="cap-unrealised"' in html
    assert 'id="cap-total-value"' in html
    assert 'id="cap-delta"' in html
    assert 'id="capital-withdrawable"' in html
    # The policy toggle + Deposit/Withdraw controls (writable dashboard).
    assert 'data-policy="compound"' in html
    assert 'data-policy="fixed"' in html
    assert 'id="capital-deposit-btn"' in html
    assert 'id="capital-withdraw-btn"' in html
    # The shared Adjust-capital modal + its op_id-minting idempotency comment.
    assert 'id="capital-modal"' in html
    assert "crypto.randomUUID()" in html
    # The ledger audit-trail expander.
    assert 'id="capital-ledger"' in html
    assert 'id="capital-ledger-summary"' in html
    assert 'id="capital-ledger-body"' in html
    # It fetches the leaf-07 endpoints — no new backend.
    assert "/capital" in html
    assert "/policy" in html


def test_strategy_detail_carries_the_health_pill_hook() -> None:
    """The detail header wires the shared health pill next to the mode badge.

    Leaf 04 (accounting-guardrail) — `#detail-health-pill` sits between
    `#detail-mode-badge` and `#detail-run-pill` (the pinned "next to the mode
    badge" placement); `renderHeader()` fills it from `healthPillHtml(s)`.
    """
    html = _client().get("/strategies/btc-ma").text
    assert 'id="detail-health-pill"' in html
    assert "healthPillHtml(s)" in html


def test_strategy_detail_read_only_hides_capital_controls() -> None:
    """A read-only detail page drops the capital mutation controls, keeps the figures."""
    html = _client(read_only=True).get("/strategies/btc-ma").text
    # The mutating controls are gone entirely (server-guarded, like the roster's
    # Deploy link) — not just disabled client-side.
    assert 'id="capital-deposit-btn"' not in html
    assert 'id="capital-withdraw-btn"' not in html
    assert 'data-policy="compound"' not in html
    assert 'data-policy="fixed"' not in html
    # The figures still render — a read-only dashboard shows the money, it just
    # cannot move it.
    assert 'id="capital-card"' in html
    assert 'id="cap-total-value"' in html
    assert 'id="capital-withdrawable"' in html
    assert 'id="capital-ledger"' in html


def test_format_js_is_served_with_the_tbfmt_namespace() -> None:
    """`GET /static/format.js` is 200 and defines the `tbFmt` display formatters.

    Leaf 02: display-only rounding/units/currency, with the exact raw value
    preserved in a `title` tooltip. String containment is enough here — the
    formatters' numeric behaviour has no server-side test surface.
    """
    resp = _client().get("/static/format.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    js = resp.text
    assert "global.tbFmt = {" in js  # the namespace object attached to `window`
    for symbol in (
        "money",
        "qty",
        "price",
        "pct",
        "ratio",
        "cell",
        "moneyCell",
        "qtyCell",
        "priceCell",
        "pctCell",
        "ratioCell",
        "splitInstrument",
    ):
        assert symbol in js, symbol


def test_vendored_uplot_assets_are_served() -> None:
    """The static mount serves the real vendored uPlot bundle + stylesheet (200)."""
    client = _client()
    js = client.get("/static/uplot.min.js")
    assert js.status_code == 200
    assert "uPlot" in js.text  # the IIFE bundle's global
    css = client.get("/static/uplot.min.css")
    assert css.status_code == 200
    assert ".uplot" in css.text


def test_branding_assets_are_served() -> None:
    """The favicon + header logo are served (not the earlier 404 the shell linked)."""
    client = _client()
    for path in ("/static/favicon.svg", "/static/logo.svg"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in r.text
    # the shell references both.
    html = client.get("/").text
    assert "/static/favicon.svg" in html
    assert "/static/logo.svg" in html


def test_self_hosted_fonts_are_served() -> None:
    """The nested self-hosted fonts are served (dccd-matching interface, no CDN)."""
    client = _client()
    for font in ("spline-sans-400.woff2", "martian-mono-700.woff2"):
        r = client.get(f"/static/fonts/{font}")
        assert r.status_code == 200, font
    # the shell declares them via @font-face + uses Spline Sans / Martian Mono.
    html = client.get("/").text
    assert "/static/fonts/spline-sans-400.woff2" in html
    assert "Martian Mono" in html and "Spline Sans" in html


# --- aggregate ratio KPIs surface non-null via /api/kpi -------------------- #


def _kpi_ratio_config(db_path: str) -> AppConfig:
    """A paper BTC/USD strategy persisting to ``db_path`` (a store to read fills)."""
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
                }
            ],
        }
    )


def test_api_kpi_total_ratios_non_null_on_a_combined_curve(tmp_path) -> None:  # noqa: ANN001
    """`GET /api/kpi?level=total` surfaces non-null ratios once a curve exists."""
    pytest.importorskip("fynance")  # the aggregate ratios need the research dep
    import asyncio
    from decimal import Decimal

    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db, mode="paper", venue="kraken")
    # A varied round-trip book so the equity curve has dispersion (a ratio exists).
    prices = [(100, 108), (108, 104), (104, 112), (112, 106), (106, 115)]
    for i, (buy_px, sell_px) in enumerate(prices):
        store.record_fill(
            Fill(
                f"B{i}",
                f"cB{i}",
                inst,
                OrderSide.BUY,
                money("1"),
                money(str(buy_px)),
                money("0"),
                2 * i + 1,
            )
        )
        store.record_fill(
            Fill(
                f"S{i}",
                f"cS{i}",
                inst,
                OrderSide.SELL,
                money("1"),
                money(str(sell_px)),
                money("0"),
                2 * i + 2,
            )
        )

    sup = StrategySupervisor(_kpi_ratio_config(db), dccd_client=_FakeStartClient())
    asyncio.run(sup.start("btc-ma"))
    client = TestClient(create_dashboard_app(sup))

    [total] = client.get("/api/kpi?level=total").json()
    # The ratios are real JSON numbers now (not null), computed on the combined curve.
    assert isinstance(total["sharpe"], (int, float))
    assert isinstance(total["sortino"], (int, float))
    assert isinstance(total["calmar"], (int, float))
    assert isinstance(total["max_drawdown"], (int, float))
    # Money stays an exact Decimal string alongside the float ratios.
    assert Decimal(total["realised_pnl"]) == Decimal("15")


async def test_positions_group_by_exchange() -> None:
    """`GET /api/positions?group_by=exchange` buckets rows per venue."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    # Net-long books (buys only) on each venue so positions are non-flat.
    for name, sym in (
        ("btc-kraken", Symbol("BTC", "USD")),
        ("eth-binance", Symbol("ETH", "USDT")),
    ):
        inst = Instrument(sym)
        sup._units[name].engine.bus.emit(  # noqa: SLF001
            FillEvent(
                Fill(
                    f"{name}b",
                    f"{name}cb",
                    inst,
                    OrderSide.BUY,
                    money("2"),
                    money("100"),
                    money("1"),
                    1,
                )
            )
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
        FillEvent(
            Fill(
                "b", "cb", inst, OrderSide.BUY, money("2"), money("100"), money("1"), 1
            )
        )
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


def test_api_positions_contract_regression(tmp_path) -> None:  # noqa: ANN001
    """`/api/positions` carries every pre-existing field, byte-identical, plus the new ones.

    A public-contract regression check (mirrors `test_api_health_worst_and_count`):
    proves the ``mark``/``mark_asof_ts``/``mark_source``/``value``/``unrealised``/
    ``fee_ccy`` additions (api-completeness leaf 02) never displaced or renamed any
    of the eight fields the endpoint already shipped.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    store.record_fill(
        Fill("SF1", "sc1", inst, OrderSide.BUY, money("2"), money("100"), money("1"), 1)
    )
    config = AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db},
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
    sup = StrategySupervisor(config, dccd_client=_FakeStartClient())
    asyncio.run(sup.start("btc-ma"))
    # Seed the mark cache directly (the bar_close path) so every new field populates.
    sup._units["btc-ma"].engine.mark_cache.update(  # noqa: SLF001
        Symbol("BTC", "USD"), money("110"), 9_000
    )

    [row] = TestClient(create_dashboard_app(sup)).get("/api/positions").json()
    # Every pre-existing field, unchanged.
    assert row["strategy"] == "btc-ma"
    assert row["exchange"] == "kraken"
    assert row["instrument"] == "BTC/USD"
    assert row["base"] == "BTC"
    assert row["net_qty"] == "2"
    assert row["avg_entry_price"] == "100"
    assert row["realised_pnl"] == "-1"  # the opening fill's fee reduces realised PnL
    assert row["fees_paid"] == "1"
    # The new fields.
    assert row["mark"] == "110"
    assert row["mark_asof_ts"] == 9_000
    assert row["mark_source"] == "bar_close"
    assert row["value"] == "220"
    assert row["unrealised"] == "20"
    assert row["fee_ccy"] == "USD"


# --- Balances (broker-reported free balances, one row per running unit) --- #


async def test_api_balances_running_unit_reports_broker_balances() -> None:
    """`/api/balances` relays the running unit's own broker-reported balances.

    Seeds the paper simulator's ledger directly (mirroring the leaf-02
    mark-cache seeding technique) so the row is proven to come from
    `Broker.balances()` itself, not the locally-tracked position — the whole
    point of this endpoint (the positions<->balances cross-check seam).
    """
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    unit = sup._units["btc-kraken"]  # noqa: SLF001 — direct broker seed, test-only
    unit.engine.broker._balances.update(  # noqa: SLF001
        {"USD": money("998"), "BTC": money("2")}
    )

    [row] = TestClient(create_dashboard_app(sup)).get("/api/balances").json()
    assert row["strategy"] == "btc-kraken"
    assert row["exchange"] == "kraken"
    assert row["mode"] == "paper"
    assert row["balances"] == {"USD": "998", "BTC": "2"}
    assert row["error"] is None


async def test_api_balances_stopped_unit_absent() -> None:
    """A stopped unit contributes no row; empty list before anything starts."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    client = TestClient(create_dashboard_app(sup))
    assert client.get("/api/balances").json() == []

    await sup.start("btc-kraken")
    sup._units["btc-kraken"].engine.broker._balances.update(  # noqa: SLF001
        {"USD": money("100")}
    )
    rows = client.get("/api/balances").json()
    assert {r["strategy"] for r in rows} == {"btc-kraken"}


async def test_api_balances_strategy_filter() -> None:
    """`?strategy=` narrows the balances rows to one unit, mirroring orders/fills."""
    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    sup._units["btc-kraken"].engine.broker._balances.update(  # noqa: SLF001
        {"USD": money("100")}
    )
    sup._units["eth-binance"].engine.broker._balances.update(  # noqa: SLF001
        {"USDT": money("200")}
    )
    client = TestClient(create_dashboard_app(sup))
    rows = client.get("/api/balances?strategy=btc-kraken").json()
    assert [r["strategy"] for r in rows] == ["btc-kraken"]


async def test_api_balances_broker_error_degrades_to_error_row() -> None:
    """A broker error is never a 500 — it degrades to an ``error`` row (poll-safe)."""
    from trading_bot.domain.errors import BrokerError

    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    unit = sup._units["btc-kraken"]  # noqa: SLF001 — direct broker patch, test-only

    async def _boom() -> dict[str, object]:
        raise BrokerError("kraken: rate limited")

    unit.engine.broker.balances = _boom  # type: ignore[method-assign]

    resp = TestClient(create_dashboard_app(sup)).get("/api/balances")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["strategy"] == "btc-kraken"
    assert row["balances"] == {}
    assert row["error"] == "kraken: rate limited"


# --- Epic-wide contract sweep (api-completeness leaf 04) ------------------- #


async def test_api_completeness_contract_sweep(tmp_path) -> None:  # noqa: ANN001
    """Every pre-existing field on the six read endpoints still holds, in one pass.

    The additive-only proof for the whole `api-completeness` epic, right before
    the API contract freeze (road-to-1.0 #5): `/api/strategies`, `/api/positions`
    and `/api/kpi` each already have a dedicated, per-field contract-regression
    test proving the leaf-02/03 additions never displaced a pre-existing field
    (`test_api_positions_contract_regression`,
    `test_api_strategies_display_fields_and_contract_regression`,
    `test_api_kpi_display_fields_and_contract_regression` — test_display_ccy.py)
    and `/api/health`'s shape is pinned by `test_health_shape_and_values` (above).
    This sweep extends that established pattern to `/api/fills` and
    `/api/orders` (untouched by any leaf, but still part of the frozen contract)
    and exercises all six together against one running unit, so the whole
    epic's additive-only guarantee is proven in a single place.
    """
    from trading_bot.domain.order import Order, OrderType
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    store.record_fill(
        Fill(
            "SWEEP-F1",
            "sweep-c1",
            inst,
            OrderSide.BUY,
            money("2"),
            money("100"),
            money("1"),
            1,
        )
    )
    store.close()
    config = AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db},
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
    sup = StrategySupervisor(config, dccd_client=_FakeStartClient())
    await sup.start("btc-ma")
    unit = sup._units["btc-ma"]  # noqa: SLF001 — direct seed, test-only
    unit.engine.mark_cache.update(Symbol("BTC", "USD"), money("110"), 9_000)
    # An open (non-terminal) order so `/api/orders` (default: open only) has a row.
    unit.engine.router.restore(
        [
            Order(
                "sweep-open-1",
                inst,
                OrderSide.BUY,
                money("1"),
                OrderType.LIMIT,
                limit_price=money("90"),
            )
        ]
    )

    client = TestClient(create_dashboard_app(sup))

    [strategy_row] = client.get("/api/strategies").json()
    assert set(strategy_row) == {
        "name",
        "kind",
        "exchange",
        "span",
        "quote",
        "mode",
        "running",
        "realised_pnl",
        "open_orders",
        "last_eval_ts",
        "last_asof_ts",
        "allocation",
        "contributed",
        "unrealised",
        "total_value",
        "capital_policy",
        "health",
        "health_detail",
        "display_currency",
        "total_value_display",
        "unrealised_display",
    }

    [position_row] = client.get("/api/positions").json()
    assert set(position_row) == {
        "strategy",
        "exchange",
        "instrument",
        "base",
        "net_qty",
        "avg_entry_price",
        "realised_pnl",
        "fees_paid",
        "mark",
        "mark_asof_ts",
        "mark_source",
        "value",
        "unrealised",
        "fee_ccy",
        "display_currency",
        "value_display",
        "unrealised_display",
    }

    [fill_row] = client.get("/api/fills").json()
    assert set(fill_row) == {
        "strategy",
        "exchange",
        "base",
        "fill_id",
        "client_order_id",
        "instrument",
        "side",
        "qty",
        "price",
        "fee",
        "ts",
    }

    [order_row] = client.get("/api/orders").json()
    assert set(order_row) == {
        "strategy",
        "exchange",
        "base",
        "ts",
        "client_order_id",
        "venue_order_id",
        "instrument",
        "side",
        "type",
        "qty",
        "limit_price",
        "stop_price",
        "status",
        "filled_qty",
        "avg_fill_price",
    }

    [kpi_row] = client.get("/api/kpi?level=strategy").json()
    assert set(kpi_row) == {
        "level",
        "key",
        "strategy",
        "exchange",
        "quote",
        "realised_pnl",
        "fees_paid",
        "sharpe",
        "sortino",
        "calmar",
        "max_drawdown",
        "display_currency",
        "realised_pnl_display",
        "fees_paid_display",
    }

    health_body = client.get("/api/health").json()
    assert set(health_body) == {
        "status",
        "mode",
        "strategies",
        "read_only",
        "next_tick_ts",
        "tick",
        "worst",
        "unhealthy",
    }


def test_orders_endpoint_present_and_empty() -> None:
    """`GET /api/orders` exists and is an empty list when nothing is open."""
    assert _client().get("/api/orders").json() == []


# --- Orders history + fills history + filters (Orders page data) ----------- #


def _fills_config_with_store(db_path: str) -> AppConfig:
    """Two paper strategies on different venues, each persisting to its own store.

    Both units read/write the SAME store file here (a single shared db) so the
    supervisor's cross-unit `fills()` folds them together; the store's `venue` tag
    (set per unit on start) is what distinguishes the exchanges in the rows.
    """
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db_path},
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


def _seed_store(db_path: str) -> None:
    """Write two venues' fills into the shared store (tagged with mode + venue).

    A BTC leg on kraken and an ETH leg on binance, so `/api/fills` has rows to
    filter by crypto (BTC / ETH), exchange (kraken / binance) and strategy.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    btc = Instrument(Symbol("BTC", "USD"))
    eth = Instrument(Symbol("ETH", "USDT"))
    store = SqliteStore(db_path)
    store.set_context(mode="paper", venue="kraken")
    store.record_fill(
        Fill("KF1", "kc1", btc, OrderSide.BUY, money("1"), money("100"), money("1"), 1)
    )
    store.set_context(mode="paper", venue="binance")
    store.record_fill(
        Fill("BF1", "bc1", eth, OrderSide.SELL, money("2"), money("200"), money("2"), 2)
    )


def test_fills_endpoint_lists_tagged_fills(tmp_path) -> None:  # noqa: ANN001
    """`GET /api/fills` returns the units' persisted fills, tagged strategy/exchange/base."""
    db = str(tmp_path / "book.sqlite")
    _seed_store(db)
    # Units stopped → each reads the shared store at its configured db_path.
    sup = StrategySupervisor(
        _fills_config_with_store(db), dccd_client=_two_venue_client()
    )
    client = TestClient(create_dashboard_app(sup))

    rows = client.get("/api/fills").json()
    # Both strategies read the same shared store, so each fill surfaces under BOTH
    # units; the venue tag (from the store) is what pins the exchange. Assert the
    # (exchange, base, side) tuples present, and that money is exact strings.
    seen = {(r["exchange"], r["base"], r["side"], r["qty"]) for r in rows}
    assert ("kraken", "BTC", "buy", "1") in seen
    assert ("binance", "ETH", "sell", "2") in seen
    # Money is an exact Decimal string, never a float.
    a_row = next(r for r in rows if r["base"] == "ETH")
    assert a_row["price"] == "200" and a_row["fee"] == "2"


def test_fills_endpoint_filters(tmp_path) -> None:  # noqa: ANN001
    """`/api/fills` narrows by ?crypto=, ?exchange= and ?strategy= (AND, exact)."""
    db = str(tmp_path / "book.sqlite")
    _seed_store(db)
    sup = StrategySupervisor(
        _fills_config_with_store(db), dccd_client=_two_venue_client()
    )
    client = TestClient(create_dashboard_app(sup))

    by_exchange = client.get("/api/fills?exchange=binance").json()
    assert by_exchange and all(r["exchange"] == "binance" for r in by_exchange)
    by_crypto = client.get("/api/fills?crypto=BTC").json()
    assert by_crypto and all(r["base"] == "BTC" for r in by_crypto)
    by_strategy = client.get("/api/fills?strategy=btc-kraken").json()
    assert by_strategy and all(r["strategy"] == "btc-kraken" for r in by_strategy)
    # A compound filter that matches nothing is an empty list (200).
    none = client.get("/api/fills?crypto=BTC&exchange=binance").json()
    assert none == []


def test_fills_endpoint_limit_and_group_by(tmp_path) -> None:  # noqa: ANN001
    """`/api/fills` honours ?limit= and ?group_by=."""
    db = str(tmp_path / "book.sqlite")
    _seed_store(db)
    sup = StrategySupervisor(
        _fills_config_with_store(db), dccd_client=_two_venue_client()
    )
    client = TestClient(create_dashboard_app(sup))

    all_rows = client.get("/api/fills").json()
    capped = client.get("/api/fills?limit=1").json()
    assert len(capped) == 1 and len(all_rows) > 1  # most-recent single row
    grouped = client.get("/api/fills?group_by=exchange").json()
    assert {g["group"] for g in grouped} == {"kraken", "binance"}


def test_fills_endpoint_unknown_group_by_is_422() -> None:
    """An unknown ``group_by`` on /api/fills is rejected (422)."""
    assert _client().get("/api/fills?group_by=bogus").status_code == 422


def test_orders_history_reads_stored_orders(tmp_path) -> None:  # noqa: ANN001
    """`GET /api/orders?history=true` returns stored orders (any status), tagged."""
    from trading_bot.domain.order import Order, OrderStatus, OrderType
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    btc = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    # A terminal (filled) order — history includes it; the open-orders view excludes it.
    order = Order(
        "oc1", btc, OrderSide.BUY, money("1"), OrderType.LIMIT, limit_price=money("100")
    )
    order.status = OrderStatus.FILLED
    order.filled_qty = money("1")
    store.upsert_order(order)

    sup = StrategySupervisor(
        _fills_config_with_store(db), dccd_client=_two_venue_client()
    )
    client = TestClient(create_dashboard_app(sup))

    # Default (open only) — no non-terminal orders on the stopped units.
    assert client.get("/api/orders").json() == []
    # History surfaces the filled order (both units share the store), tagged + based.
    hist = client.get("/api/orders?history=true").json()
    assert hist and all(o["status"] == "filled" for o in hist)
    assert all(o["base"] == "BTC" for o in hist)
    # Filter the history by exchange (the unit tag).
    only_kraken = client.get("/api/orders?history=true&exchange=kraken").json()
    assert only_kraken and all(o["exchange"] == "kraken" for o in only_kraken)


def test_orders_history_limit_caps_to_the_most_recent(tmp_path) -> None:  # noqa: ANN001
    """`GET /api/orders?history=true&limit=N` caps to the N most recent rows.

    The Orders page's history-cap caption (ui-ux leaf 05) keys off the
    response coming back at exactly the requested/default cap — this proves
    that shape is real: several stored orders, a ``?limit=`` narrower than the
    full history, and the response is exactly that many rows.
    """
    from trading_bot.domain.order import Order, OrderStatus, OrderType
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    btc = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    for i in range(3):
        order = Order(
            f"oc{i}",
            btc,
            OrderSide.BUY,
            money("1"),
            OrderType.LIMIT,
            limit_price=money("100"),
        )
        order.status = OrderStatus.FILLED
        order.filled_qty = money("1")
        store.upsert_order(order)

    sup = StrategySupervisor(
        _fills_config_with_store(db), dccd_client=_two_venue_client()
    )
    client = TestClient(create_dashboard_app(sup))

    all_hist = client.get("/api/orders?history=true").json()
    capped = client.get("/api/orders?history=true&limit=2").json()
    assert len(capped) == 2 and len(all_hist) > 2


async def test_orders_history_and_open_orders_carry_ts(tmp_path) -> None:  # noqa: ANN001
    """History rows carry the store's stamped `ts`; open-order rows look it up too.

    History rows read `ts` straight off the store column (an exact int match
    to what `SqliteStore.upsert_order` stamped — the domain `Order` itself
    carries no `ts`). Open-order rows come from the live router, which also
    carries no `ts` of its own, so the supervisor looks the same
    `client_order_id` up in the unit's store: a freshly-persisted id resolves
    to its stamp, an id the store never saw is `null`.
    """
    from trading_bot.domain.order import Order, OrderStatus, OrderType
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    btc = Instrument(Symbol("BTC", "USD"))

    # A terminal, persisted order — feeds the history-row assertion.
    filled = Order(
        "hist-1",
        btc,
        OrderSide.BUY,
        money("1"),
        OrderType.LIMIT,
        limit_price=money("100"),
    )
    filled.status = OrderStatus.FILLED
    filled.filled_qty = money("1")
    store = SqliteStore(db)
    store.upsert_order(filled)
    stamped_ts = store.order_ts_map()["hist-1"]
    store.close()

    sup = StrategySupervisor(
        _fills_config_with_store(db), dccd_client=_two_venue_client()
    )
    client = TestClient(create_dashboard_app(sup))

    hist = client.get("/api/orders?history=true").json()
    hist_row = next(r for r in hist if r["client_order_id"] == "hist-1")
    assert hist_row["ts"] == stamped_ts

    # A running unit's open (non-terminal) order: persisted at submit, then
    # seeded straight into the live router (mirrors `router.restore()` on
    # startup) — `open_orders()` must resolve its `ts` from the same store.
    await sup.start("btc-kraken")
    unit = sup._units["btc-kraken"]  # noqa: SLF001 — seed the live router directly
    open_order = Order(
        "open-1",
        btc,
        OrderSide.BUY,
        money("1"),
        OrderType.LIMIT,
        limit_price=money("90"),
    )
    assert unit.engine is not None and unit.engine.store is not None
    unit.engine.store.upsert_order(open_order)
    unit.engine.router.restore([open_order])
    # An order the router tracks but the store never saw — ts must be null.
    ghost = Order(
        "ghost-1",
        btc,
        OrderSide.SELL,
        money("1"),
        OrderType.LIMIT,
        limit_price=money("90"),
    )
    unit.engine.router.restore([ghost])

    live = client.get("/api/orders").json()
    live_row = next(r for r in live if r["client_order_id"] == "open-1")
    ghost_row = next(r for r in live if r["client_order_id"] == "ghost-1")
    assert isinstance(live_row["ts"], int) and live_row["ts"] > 0
    assert ghost_row["ts"] is None


# --- Orders + Logs page markup --------------------------------------------- #


def test_orders_page_has_tables_and_filters() -> None:
    """`GET /orders` carries the orders + fills tables and the filter controls."""
    html = _client().get("/orders").text
    assert 'id="orders-table"' in html
    assert 'id="fills-table"' in html
    # Filter controls (crypto / exchange / strategy).
    assert 'id="f-crypto"' in html
    assert 'id="f-exchange"' in html
    assert 'id="f-strategy"' in html
    # It fetches both history endpoints (orders history + fills).
    assert "/api/orders" in html and "history=true" in html
    assert "/api/fills" in html
    # Freshness stamps on both tables (ui-ux leaf 04).
    assert 'id="orders-updated"' in html
    assert 'id="fills-updated"' in html
    # History-cap caption nodes, one per table (ui-ux leaf 05): shown when a
    # history read comes back at exactly the server's default ?limit= cap.
    assert 'id="orders-cap"' in html and "showing the most recent 200" in html
    assert 'id="fills-cap"' in html
    # The Orders table's Time column (order date/time, this leaf) — mirrors the
    # Fills table's own Time column.
    assert html.count("<th>Time</th>") == 2  # one in Orders, one in Fills


def test_logs_page_has_feed_and_subscribes_to_sse() -> None:
    """`GET /logs` carries the activity feed container and subscribes to /api/events."""
    html = _client().get("/logs").text
    assert 'id="logs-feed"' in html  # the feed container
    assert "/api/events" in html  # it subscribes to the merged SSE stream
    assert "connect(" in html  # via the shared connect() helper
    # Event-type filter chips + a min-level select for log events (ui-ux leaf 05).
    assert 'data-type="all"' in html
    assert 'data-type="order"' in html
    assert 'data-type="fill"' in html
    assert 'data-type="log"' in html
    assert 'id="log-min-level"' in html


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
    # The "By strategy" grouping is present and the default (ui-ux leaf 03).
    assert 'data-group="strategy"' in html
    assert 'class="btn pos-group is-active" data-group="strategy"' in html
    assert 'id="orders-table"' in html
    # The open-orders table's Time column (order date/time, this leaf).
    assert "<th>Time</th>" in html
    # The summary strip (running/total strategies, open orders, total PnL, next
    # tick) sits above the KPI card.
    assert 'id="summary-strip"' in html
    assert 'id="sum-running"' in html and 'id="sum-total"' in html
    assert 'id="sum-open-orders"' in html
    assert 'id="sum-pnl"' in html
    assert 'id="sum-next-tick"' in html
    # Freshness stamps on the KPI/positions/open-orders cards (ui-ux leaf 04).
    assert 'id="kpi-updated"' in html
    assert 'id="positions-updated"' in html
    assert 'id="overview-orders-updated"' in html
    # View preferences persist across reloads.
    assert "tb.overview.posGroup" in html
    assert "tb.overview.kpiLevel" in html
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
        "type": "http",
        "method": "GET",
        "path": "/api/events",
        "headers": [],
        "query_string": b"",
        "app": app,
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
        buses[1].emit(
            FillEvent(
                Fill(
                    "SF1",
                    "sc1",
                    inst,
                    OrderSide.BUY,
                    money("1"),
                    money("100"),
                    money("1"),
                    1,
                )
            )
        )
        frame = await frames.__anext__()
        assert frame.startswith("data:")
        payload = json.loads(frame[len("data:") :].strip())
        assert payload["type"] == "fill"
        assert payload["fill"]["fill_id"] == "SF1"
        # Tagged with the emitting unit's name + a server-side epoch-ms timestamp
        # (the Logs page's attribution + event time, not the client receive time).
        assert payload["strategy"] == "eth-binance"  # emitted on buses[1]
        assert isinstance(payload["ts"], int)
    finally:
        await frames.aclose()  # disconnect → generator finally removes every queue
    assert [len(b._queues) for b in buses] == before  # noqa: SLF001


async def test_events_stream_ends_cleanly_on_cancellation() -> None:
    """Cancelling the merged generator's task (server shutdown) ends it cleanly.

    Mirrors what uvicorn's graceful-shutdown timeout does to a connected SSE
    client: the task driving the generator is force-cancelled while it is
    suspended in ``asyncio.wait`` on the per-unit getter tasks. The generator
    must convert that ``CancelledError`` into a clean end-of-stream
    (``StopAsyncIteration``) rather than letting it propagate — which is what
    used to make uvicorn log a scary ERROR-level traceback on every shutdown
    while a dashboard client held this endpoint open — and every outstanding
    per-iteration getter task plus every bus queue must still be cleaned up.
    """
    import asyncio

    from fastapi import Request

    sup = StrategySupervisor(_two_venue_config(), dccd_client=_two_venue_client())
    await sup.start("btc-kraken")
    await sup.start("eth-binance")
    app = create_dashboard_app(sup)
    buses = [sup._units[n].engine.bus for n in ("btc-kraken", "eth-binance")]  # noqa: SLF001
    before = [len(b._queues) for b in buses]  # noqa: SLF001

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/events",
        "headers": [],
        "query_string": b"",
        "app": app,
    }
    request = Request(scope, _never_disconnect)
    response = await _events_route(app)(request)  # type: ignore[operator]
    frames = response.body_iterator

    first = await frames.__anext__()
    assert first.startswith(":")
    assert [len(b._queues) for b in buses] == [n + 1 for n in before]  # noqa: SLF001

    # Cancel the task while it is suspended in `asyncio.wait(getters, ...)` (no
    # unit has emitted anything, so it is parked there — exactly where a real
    # shutdown-time cancellation would land).
    task = asyncio.ensure_future(frames.__anext__())
    await asyncio.sleep(0)  # let the task actually start awaiting the getters
    task.cancel()

    with pytest.raises(StopAsyncIteration):
        await task
    assert not task.cancelled()  # the cancellation was converted, not propagated

    # The `finally` still ran: every bus queue is unregistered like any other
    # close, and no getter task is left dangling (no "was destroyed but it is
    # pending" warning — pytest-asyncio would otherwise surface it).
    assert [len(b._queues) for b in buses] == before  # noqa: SLF001


# --- strategy control (list + start/stop/mode, the live gate) -------------- #


def test_strategies_endpoint_lists_units_with_exchange() -> None:
    """`GET /api/strategies` lists the managed units, tagged with their exchange.

    Also carries `span` (the unit's `data.span`, seconds) and `quote` (the part
    after `/` of its `symbol`) — read from the actually-wired config, not
    hardcoded (`_config()` declares `data.span: 60` and `symbol: "BTC/USD"`).
    """
    resp = _client().get("/api/strategies")
    assert resp.status_code == 200
    [s] = resp.json()
    assert s["name"] == "btc-ma"
    assert s["exchange"] == "kraken"  # grouped/displayed by exchange
    assert s["mode"] == "paper"
    assert s["running"] is False
    assert s["span"] == 60
    assert s["quote"] == "USD"


def test_api_strategies_carries_health() -> None:
    """`GET /api/strategies` row carries `health`/`health_detail`, JSON-safe.

    A stopped unit with a cached warn-only accounting report (seeded directly
    on ``unit.accounting`` — no live engine needed to exercise the JSON shape)
    surfaces `health: "warn"` and its violation's `detail` sentence in a plain
    JSON list of strings (never a raw ``Violation`` or a ``Decimal``).
    """
    import trading_bot.application.supervisor as sup_mod

    sup = _supervisor()
    warn = Violation(
        kind="duplicate_venue_ids",
        severity="warn",
        subject="VID-1",
        detail="venue_order_id 'VID-1' is shared by 2 order rows.",
        measured="2",
        expected="1",
    )
    sup._units["btc-ma"].accounting = sup_mod._AccountingState(  # noqa: SLF001
        violations=[warn], computed_at_ms=0
    )
    client = TestClient(create_dashboard_app(sup))
    [row] = client.get("/api/strategies").json()
    assert row["health"] == "warn"
    assert row["health_detail"] == [warn.detail]
    assert isinstance(row["health_detail"], list)
    assert all(isinstance(sentence, str) for sentence in row["health_detail"])


async def test_strategies_endpoint_last_eval_and_asof_ts() -> None:
    """`GET /api/strategies` carries `last_eval_ts`/`last_asof_ts`, set after a tick.

    Both are `None` before the unit has ever ticked via its `*_latest` path
    (incl. a never-started unit — PR #168's diagnostic fields). Driving one
    tick through the supervisor's `step_all` (the daemon's own path) stamps
    both: `last_eval_ts` is the wall-clock of the attempt, `last_asof_ts` the
    as-of of the data actually evaluated.
    """
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    sup = StrategySupervisor(_config(), dccd_client=_FakeStartClient())
    client = TestClient(create_dashboard_app(sup))

    # Never started/ticked -> both null.
    [before] = client.get("/api/strategies").json()
    assert before["last_eval_ts"] is None
    assert before["last_asof_ts"] is None

    await sup.start("btc-ma")
    assert await sup.step_all() == 1  # the one running unit stepped once

    [after] = client.get("/api/strategies").json()
    assert isinstance(after["last_eval_ts"], int) and after["last_eval_ts"] > 0
    assert isinstance(after["last_asof_ts"], int) and after["last_asof_ts"] > 0


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
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"  # unchanged


def test_set_mode_live_with_confirmation_flips() -> None:
    """With the typed acknowledgement phrase, the mode flips to live."""
    client = _client()
    r = client.post(
        "/api/strategies/btc-ma/mode",
        json={"mode": "live", "confirm": True, "ack": "I UNDERSTAND"},
    )
    assert r.status_code == 200
    assert r.json()["status"]["mode"] == "live"


def test_set_mode_unknown_is_400() -> None:
    """An unknown mode is rejected (400)."""
    r = _client().post("/api/strategies/btc-ma/mode", json={"mode": "bogus"})
    assert r.status_code == 400


def test_start_unknown_strategy_is_404() -> None:
    """Starting an unknown strategy is a 404."""
    assert _client().post("/api/strategies/nope/start").status_code == 404


def test_start_then_stop_toggles_running() -> None:
    """`POST start` runs the unit in its own engine; `POST stop` tears it down."""
    pytest.importorskip("fynance")  # ma_crossover evaluates fynance.sma
    client = TestClient(
        create_dashboard_app(
            StrategySupervisor(_config(), dccd_client=_FakeStartClient())
        )
    )
    r = client.post("/api/strategies/btc-ma/start")
    assert r.status_code == 200
    assert r.json()["status"]["running"] is True
    r = client.post("/api/strategies/btc-ma/stop")
    assert r.status_code == 200
    assert r.json()["status"]["running"] is False


def test_read_only_write_routes_are_403() -> None:
    """Under `read_only`, the write routes (start/stop/mode) are 403; reads work."""
    client = _client(read_only=True)
    assert client.get("/api/strategies").status_code == 200  # a read still works
    assert client.post("/api/strategies/btc-ma/start").status_code == 403
    assert client.post("/api/strategies/btc-ma/stop").status_code == 403
    assert (
        client.post("/api/strategies/btc-ma/mode", json={"mode": "testnet"}).status_code
        == 403
    )
    # And nothing changed.
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"


# --- Strategies page markup ------------------------------------------------ #


def test_strategies_page_is_a_linked_roster() -> None:
    """`GET /strategies` is a slim roster: linked rows, columns; no mode-select/modal.

    The roster slimmed to a linked index — each name links to its detail page
    (/strategies/{name}) where the deep controls (mode switch, go-live, remove) and
    charts live. The mode <select> and the go-live modal moved off this page.
    """
    html = _client().get("/strategies").text
    assert 'id="strategies-body"' in html  # the table the page fills
    assert "/api/strategies" in html  # it wires the control endpoints
    # Rows link to the per-strategy detail page (client-rendered in the roster JS).
    assert 'href="/strategies/' in html
    assert "strat-link" in html
    # The kept roster columns (cadence / next-bar / last-eval), plus the stamp.
    assert "<th>Cadence</th>" in html
    assert "<th>Next bar</th>" in html
    assert "<th>Last eval</th>" in html
    # The condensed Total-value column (leaf 08) — replaces nothing; Realised
    # PnL stays alongside it so the same numbers read at every altitude.
    assert '<th class="num">Realised PnL</th>' in html
    assert '<th class="num">Total value</th>' in html
    assert 'id="strategies-updated"' in html
    # The go-live modal + its typed phrase MOVED to the detail page — the roster
    # no longer carries the confirmation surface (the `.mode-select` CSS class
    # lives in base.html for every page, so the modal id is the clean marker).
    assert 'id="live-modal"' not in html
    assert "I UNDERSTAND" not in html


def test_strategies_roster_carries_the_health_pill_hook() -> None:
    """The roster's row renderer calls the shared health-pill helper (leaf 04).

    Placed next to the existing run-pill status cell content, per the pinned
    display rule ("ok" -> no pill; "warn"/"error" -> the badge-status-* pill).
    """
    html = _client().get("/strategies").text
    assert "healthPillHtml(s)" in html


def test_strategies_page_read_only_note_and_no_deploy_link() -> None:
    """A read-only roster advertises disabled controls and hides the Deploy link."""
    html = _client(read_only=True).get("/strategies").text
    assert "read-only" in html.lower()
    assert "const TB_READ_ONLY = true" in html  # Start/Stop rendering guards on it
    # The Deploy link is server-guarded ({% if not read_only %}) — gone here.
    assert 'href="/strategies/new"' not in html


def test_strategies_roster_links_to_the_deploy_page_when_writable() -> None:
    """A writable roster carries the Deploy link to the relocated form."""
    html = _client().get("/strategies").text
    assert 'href="/strategies/new"' in html


def test_strategies_new_page_has_deploy_form_when_writable() -> None:
    """`GET /strategies/new` carries the relocated deploy form wired to /api/signals."""
    html = _client().get("/strategies/new").text
    assert 'id="deploy-form"' in html
    assert "/api/signals" in html  # the form fetches discoverable signal refs
    assert 'href="/strategies"' in html  # a link back to the roster
    # The form is no longer on the roster page (it moved here).
    assert 'id="deploy-form"' not in _client().get("/strategies").text


def test_strategies_new_page_hides_deploy_form_when_read_only() -> None:
    """A read-only `/strategies/new` omits the deploy form (no create affordance)."""
    html = _client(read_only=True).get("/strategies/new").text
    assert 'id="deploy-form"' not in html


def test_strategies_new_is_not_matched_as_a_strategy_name() -> None:
    """`/strategies/new` binds the deploy form, never the detail route for a unit "new".

    Route ordering: the `/strategies/new` shell is registered before the
    parameterized `/strategies/{name}` route, so the exact "new" always wins.
    """
    r = _client().get("/strategies/new")
    assert r.status_code == 200
    assert 'id="deploy-form"' in r.text  # the form, not a per-strategy detail shell


# --- restored paper book surfaces through the dashboard -------------------- #


def test_dashboard_shows_restored_paper_book(tmp_path) -> None:  # noqa: ANN001
    """A paper unit over a seeded store, once started, shows its book on /api/positions.

    The end-to-end win: the dashboard now starts the unit, `start` replays the
    store's persisted fills into the engine, so a freshly-launched dashboard shows
    the restored book (positions + realised PnL) rather than an empty one.
    """
    from trading_bot.storage.sqlite_store import SqliteStore

    db = str(tmp_path / "book.sqlite")
    inst = Instrument(Symbol("BTC", "USD"))
    store = SqliteStore(db)
    # A net-long book (buys only) so the position is non-flat.
    store.record_fill(
        Fill("SF1", "sc1", inst, OrderSide.BUY, money("2"), money("100"), money("1"), 1)
    )

    config = AppConfig.model_validate(
        {
            "mode": "paper",
            "storage": {"db_path": db},
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
    sup = StrategySupervisor(config, dccd_client=_FakeStartClient())

    import asyncio

    asyncio.run(sup.start("btc-ma"))
    client = TestClient(create_dashboard_app(sup))

    rows = client.get("/api/positions").json()
    [row] = rows
    assert row["instrument"] == "BTC/USD"
    assert row["net_qty"] == "2"  # the restored book, exact Decimal string
    kpi = client.get("/api/kpi?level=total").json()[0]
    assert kpi["fees_paid"] == "1"  # the fee from the seeded fill


# --- signal discovery + deployment CRUD + manifest persistence ------------- #


def test_signals_endpoint_lists_builtins_and_discovered() -> None:
    """`GET /api/signals` lists the `ma_crossover` builtin + a discovered ref.

    Real strategy signals live under the gitignored `strategies/` (absent in CI), so
    this drops a throwaway `strategies/<pkg>/signal.py` with a `*_signal` callable,
    asserts the scan finds it as a `module:function` ref, then cleans it up — proving
    discovery works without depending on any local-only strategy.
    """
    import pathlib
    import shutil
    import sys

    from trading_bot.interfaces.api import app as app_module

    pkg = "_disco_probe"
    probe_dir = (
        pathlib.Path(app_module.__file__).resolve().parents[3] / "strategies" / pkg
    )
    probe_dir.mkdir(parents=True, exist_ok=True)
    (probe_dir / "signal.py").write_text(
        "def probe_signal(asof_ms, frames):\n    return {}\n"
    )
    try:
        body = _client().get("/api/signals").json()
        assert "ma_crossover" in body["builtins"]
        assert f"strategies.{pkg}.signal:probe_signal" in body["discovered"], body[
            "discovered"
        ]
        # A re-exported helper (as_portfolio_signal) / a private closure is NOT a ref.
        assert not any(
            ref.endswith(":as_portfolio_signal") for ref in body["discovered"]
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        sys.modules.pop(f"strategies.{pkg}.signal", None)
        sys.modules.pop(f"strategies.{pkg}", None)


def _portfolio_deploy_body(name: str = "demo1") -> dict:
    """A create-deployment body deploying the demo1 portfolio (paper, binance)."""
    return {
        "name": name,
        "kind": "portfolio",
        "venue": "binance",
        "mode": "paper",
        "signal": "strategies.demo.signal:portfolio_signal",
        "universe": ["BTC/USDT", "ETH/USDT"],
        "capital": "100000",
    }


def test_create_strategy_adds_a_stopped_unit_and_persists(tmp_path) -> None:  # noqa: ANN001
    """`POST /api/strategies` deploys a stopped unit and rewrites the manifest on disk."""
    manifest = tmp_path / "dashboard.yaml"
    sup = StrategySupervisor(AppConfig())  # empty paper base

    def _persist() -> None:
        sup.manifest().to_yaml(manifest)

    client = TestClient(create_dashboard_app(sup, on_change=_persist))
    r = client.post("/api/strategies", json=_portfolio_deploy_body())
    assert r.status_code == 200, r.text
    assert r.json()["status"]["running"] is False  # never auto-started

    # It now lists via /api/strategies.
    names = [s["name"] for s in client.get("/api/strategies").json()]
    assert names == ["demo1"]

    # And it was PERSISTED to disk — re-read the manifest file.
    assert manifest.is_file()
    reloaded = AppConfig.from_yaml(manifest)
    assert [p.name for p in reloaded.portfolios] == ["demo1"]
    assert reloaded.portfolios[0].capital == money("100000")


def test_create_strategy_auto_assigns_an_isolated_db_path(tmp_path) -> None:  # noqa: ANN001
    """`POST /api/strategies` with no db_path auto-assigns a distinct, isolated store.

    A UI-deployed strategy is isolated by default: with no ``db_path`` in the body
    the endpoint derives one under ``<manifest-storage-dir>/dashboard/<name>.sqlite``,
    and it round-trips into the persisted manifest (so a restart keeps the isolation).
    Two deployments get two distinct stores.
    """
    manifest = tmp_path / "dashboard.yaml"
    # A manifest whose global store lives under a known dir; the auto-assigned path
    # is derived from that dir (../dashboard/<name>.sqlite).
    base = AppConfig.model_validate(
        {"mode": "paper", "storage": {"db_path": str(tmp_path / "global.sqlite")}}
    )
    sup = StrategySupervisor(base)
    client = TestClient(
        create_dashboard_app(sup, on_change=lambda: sup.manifest().to_yaml(manifest))
    )

    r1 = client.post("/api/strategies", json=_portfolio_deploy_body("demo-binance"))
    r2 = client.post("/api/strategies", json=_portfolio_deploy_body("demo-kraken"))
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    reloaded = AppConfig.from_yaml(manifest)
    by_name = {p.name: p for p in reloaded.portfolios}
    # Each got its own, distinct, non-null store path under dashboard/.
    a = by_name["demo-binance"].db_path
    b = by_name["demo-kraken"].db_path
    assert a is not None and b is not None and a != b
    assert a.endswith("dashboard/demo-binance.sqlite"), a
    assert b.endswith("dashboard/demo-kraken.sqlite"), b


def test_create_strategy_honours_an_explicit_db_path() -> None:
    """`POST /api/strategies` with an explicit ``db_path`` uses it verbatim (no auto-assign)."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    body = {**_portfolio_deploy_body("pf"), "db_path": "./var/custom/pf.sqlite"}
    r = client.post("/api/strategies", json=body)
    assert r.status_code == 200, r.text
    [p] = sup.manifest().portfolios
    assert p.db_path == "./var/custom/pf.sqlite"


def test_create_then_delete_persists_the_removal(tmp_path) -> None:  # noqa: ANN001
    """`DELETE /api/strategies/{name}` removes the unit and rewrites the manifest."""
    manifest = tmp_path / "dashboard.yaml"
    sup = StrategySupervisor(AppConfig())

    client = TestClient(
        create_dashboard_app(sup, on_change=lambda: sup.manifest().to_yaml(manifest))
    )
    client.post("/api/strategies", json=_portfolio_deploy_body())
    assert AppConfig.from_yaml(manifest).portfolios  # persisted on create

    r = client.delete("/api/strategies/demo1")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": "demo1"}
    assert client.get("/api/strategies").json() == []
    # The removal was persisted too — the manifest is rewritten empty.
    assert AppConfig.from_yaml(manifest).portfolios == []


def test_create_single_instrument_strategy() -> None:
    """A ``kind=strategy`` deployment builds a single-instrument unit from a builtin."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    r = client.post(
        "/api/strategies",
        json={
            "name": "btc-ma",
            "kind": "strategy",
            "venue": "kraken",
            "signal": "ma_crossover",
            "symbol": "BTC/USD",
            "params": {"fast": 3, "slow": 6},
            "reference_qty": "2",
            "lookback": 6,
            "span": 60,
        },
    )
    assert r.status_code == 200, r.text
    [s] = client.get("/api/strategies").json()
    assert s["name"] == "btc-ma" and s["exchange"] == "kraken"


def test_create_strategy_duplicate_name_is_422() -> None:
    """Deploying a name already managed is rejected (422); nothing added."""
    sup = StrategySupervisor(_config())  # already has 'btc-ma'
    client = TestClient(create_dashboard_app(sup))
    r = client.post(
        "/api/strategies",
        json={
            "name": "btc-ma",
            "kind": "strategy",
            "venue": "kraken",
            "signal": "ma_crossover",
            "symbol": "ETH/USD",
        },
    )
    assert r.status_code == 422
    assert [s["name"] for s in client.get("/api/strategies").json()] == ["btc-ma"]


def test_create_portfolio_without_universe_is_422() -> None:
    """A portfolio deployment with no universe is rejected (422)."""
    client = TestClient(create_dashboard_app(StrategySupervisor(AppConfig())))
    r = client.post(
        "/api/strategies",
        json={
            "name": "pf",
            "kind": "portfolio",
            "venue": "binance",
            # An allow-listed ref (so the 422 is the missing-universe shape check,
            # not the signal-ref import gate — which would be a 400).
            "signal": "strategies.pf.signal:sig",
            "capital": "100000",
        },
    )
    assert r.status_code == 422


def test_delete_unknown_strategy_is_404() -> None:
    """Deleting an unmanaged name is a 404."""
    client = TestClient(create_dashboard_app(StrategySupervisor(AppConfig())))
    assert client.delete("/api/strategies/nope").status_code == 404


def test_deployment_crud_is_403_under_read_only() -> None:
    """Under `read_only`, POST/DELETE are 403 and never touch the supervisor."""
    client = _client(read_only=True)
    assert (
        client.post("/api/strategies", json=_portfolio_deploy_body()).status_code == 403
    )
    assert client.delete("/api/strategies/btc-ma").status_code == 403
    # Nothing changed — the one declared unit is still there, unremoved.
    assert [s["name"] for s in client.get("/api/strategies").json()] == ["btc-ma"]


# --- server-side gate hardening (I-2 live ack / I-3 db_path / I-1 signal ref) --- #


def test_set_mode_live_with_confirm_but_no_ack_is_403() -> None:
    """I-2: `confirm:true` alone (no typed ack phrase) cannot flip to live — 403.

    The typed acknowledgement is enforced SERVER-SIDE: a raw body bypassing the
    browser modal (`{"mode":"live","confirm":true}`) is refused, nothing changes.
    """
    client = _client()
    r = client.post(
        "/api/strategies/btc-ma/mode", json={"mode": "live", "confirm": True}
    )
    assert r.status_code == 403
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"  # unchanged


def test_set_mode_live_with_blank_ack_is_403() -> None:
    """I-2: a blank / wrong ack phrase is refused (403) — the phrase must be exact."""
    client = _client()
    for ack in ("", "   ", "i understand", "I UNDERSTAND!"):
        r = client.post(
            "/api/strategies/btc-ma/mode",
            json={"mode": "live", "confirm": True, "ack": ack},
        )
        assert r.status_code == 403, ack
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"  # unchanged


def test_set_mode_live_with_correct_ack_flips() -> None:
    """I-2: the exact typed acknowledgement phrase flips paper → live (200)."""
    client = _client()
    r = client.post(
        "/api/strategies/btc-ma/mode",
        json={"mode": "live", "confirm": True, "ack": "I UNDERSTAND"},
    )
    assert r.status_code == 200
    assert r.json()["status"]["mode"] == "live"


def test_set_mode_paper_testnet_need_no_ack() -> None:
    """I-2: paper / testnet switches never require the ack (only live is gated)."""
    client = _client()
    assert (
        client.post("/api/strategies/btc-ma/mode", json={"mode": "testnet"}).status_code
        == 200
    )
    assert (
        client.post("/api/strategies/btc-ma/mode", json={"mode": "paper"}).status_code
        == 200
    )


def test_every_mutating_route_is_403_under_read_only() -> None:
    """I-2 matrix: under `read_only`, EVERY mutating route → 403; reads still 200.

    Extends the read-only stance across the full write surface (deploy / delete /
    start / stop / mode incl. the live-ack path) — a read-only dashboard can observe
    but never mutate, and the live gate is unreachable.
    """
    client = _client(read_only=True)
    # Reads still work.
    assert client.get("/api/strategies").status_code == 200
    assert client.get("/api/health").status_code == 200
    # Every mutation is refused.
    mutations = (
        client.post("/api/strategies", json=_portfolio_deploy_body()),
        client.delete("/api/strategies/btc-ma"),
        client.post("/api/strategies/btc-ma/start"),
        client.post("/api/strategies/btc-ma/stop"),
        client.post("/api/strategies/btc-ma/mode", json={"mode": "testnet"}),
        client.post(
            "/api/strategies/btc-ma/mode",
            json={"mode": "live", "confirm": True, "ack": "I UNDERSTAND"},
        ),
    )
    assert all(r.status_code == 403 for r in mutations), [
        r.status_code for r in mutations
    ]
    # Nothing changed.
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"


def test_deploy_with_traversal_db_path_is_4xx() -> None:
    """I-3: a `..` traversal db_path is rejected (4xx); nothing is deployed."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    body = {**_portfolio_deploy_body("pf"), "db_path": "../../evil.sqlite"}
    r = client.post("/api/strategies", json=body)
    assert 400 <= r.status_code < 500, r.status_code
    assert sup.manifest().portfolios == []  # nothing added


def test_deploy_with_absolute_db_path_is_4xx() -> None:
    """I-3: an absolute db_path (write-anywhere) is rejected (4xx); nothing deployed."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    for abs_path in ("/tmp/evil.sqlite", "/etc/evil.sqlite"):
        body = {**_portfolio_deploy_body("pf"), "db_path": abs_path}
        r = client.post("/api/strategies", json=body)
        assert 400 <= r.status_code < 500, (abs_path, r.status_code)
    assert sup.manifest().portfolios == []  # nothing added


def test_deploy_with_normal_db_path_lands_under_the_data_dir() -> None:
    """I-3: a normal relative db_path is accepted (200) and confined (no `..`, relative)."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    body = {**_portfolio_deploy_body("pf"), "db_path": "dashboard/pf.sqlite"}
    r = client.post("/api/strategies", json=body)
    assert r.status_code == 200, r.text
    [p] = sup.manifest().portfolios
    assert p.db_path == "dashboard/pf.sqlite"
    # The stored path is relative + traversal-free (confined to the data dir).
    assert p.db_path is not None
    assert not p.db_path.startswith("/")
    assert ".." not in p.db_path.split("/")


def test_deploy_with_signal_ref_outside_allow_list_is_4xx() -> None:
    """I-1: a signal ref whose module is outside the allow-list is rejected (4xx).

    A dotted ref is handed to `importlib.import_module` at unit start; the allow-list
    confines the import root to the trading stack's own packages. `os:system` (and
    other out-of-tree modules) never reach the import.
    """
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    for bad_ref in ("os:system", "subprocess:run", "builtins:eval", "evilpkg.x:go"):
        body = {**_portfolio_deploy_body("pf"), "signal": bad_ref}
        r = client.post("/api/strategies", json=body)
        assert 400 <= r.status_code < 500, (bad_ref, r.status_code)
    assert sup.manifest().portfolios == []  # nothing added


def test_deploy_with_malformed_signal_ref_is_4xx() -> None:
    """I-1: a malformed dotted ref (bad module shape / empty function) is 4xx."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    for bad_ref in ("strategies foo:sig", "strategies.:sig", "strategies.mod:"):
        body = {**_portfolio_deploy_body("pf"), "signal": bad_ref}
        r = client.post("/api/strategies", json=body)
        assert 400 <= r.status_code < 500, (bad_ref, r.status_code)
    assert sup.manifest().portfolios == []  # nothing added


def test_deploy_with_allow_listed_signal_ref_passes_the_gate() -> None:
    """I-1: an allow-listed ref clears the import gate (a builtin name is fine too).

    The gate only rejects *out-of-list* imports; a builtin single-instrument signal
    (`ma_crossover`, no `:`) is not an import path and deploys.
    """
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    r = client.post(
        "/api/strategies",
        json={
            "name": "btc-ma",
            "kind": "strategy",
            "venue": "kraken",
            "signal": "ma_crossover",
            "symbol": "BTC/USD",
            "params": {"fast": 3, "slow": 6},
        },
    )
    assert r.status_code == 200, r.text
    assert [s["name"] for s in client.get("/api/strategies").json()] == ["btc-ma"]


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
    # A bounded graceful-shutdown timeout so Ctrl-C quits promptly even when a
    # browser holds the /api/events SSE stream open — uvicorn's default graceful
    # shutdown is unbounded and waits for that never-ending stream forever, which
    # made the server feel unquittable on the first SIGINT.
    grace = kwargs["timeout_graceful_shutdown"]
    assert isinstance(grace, int) and grace > 0

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


def test_dashboard_ignores_a_repo_root_default_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray, secret-bearing ``configs/dashboard.yaml`` in the repo root is ignored.

    Proves the suite's hermeticity fixture (``trading_bot/tests/conftest.py``): the
    dashboard default-manifest path is CWD-relative, so a developer's real
    ``configs/dashboard.yaml`` — here declaring a strategy AND a ``ui.token`` that
    would enable auth — must NOT be picked up. The autouse fixture runs each test
    from a temp CWD, so a bare ``dashboard`` reads a fresh empty-paper manifest
    (no strategies, no auth), not the poison file. We drop the poison into the
    real repo root (derived from the CLI module's location), then rely on the
    fixture having already moved the CWD elsewhere.
    """
    import pathlib

    import uvicorn

    import trading_bot.interfaces.cli.main as cli_main

    # The repo root is three parents up from trading_bot/interfaces/cli/main.py.
    repo_root = pathlib.Path(cli_main.__file__).resolve().parents[3]
    configs = repo_root / "configs"
    poison = configs / "dashboard.yaml"
    preexisting_dir = configs.is_dir()
    preexisting_file = poison.is_file()
    if preexisting_file:  # never clobber a real developer manifest
        pytest.skip("a real configs/dashboard.yaml exists; refusing to touch it")

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))

    configs.mkdir(exist_ok=True)
    try:
        poison.write_text(
            "mode: paper\n"
            "ui:\n"
            "  token: poison-token-would-enable-auth\n"
            "strategies:\n"
            "  - name: poison-strategy\n"
            "    symbol: BTC/USD\n"
            "    data: {exchange: kraken, span: 60}\n"
            "    signal: {ref: ma_crossover, params: {fast: 3, slow: 6}}\n"
            "    reference_qty: '2'\n"
            "    lookback: 6\n"
        )
        result = runner.invoke(cli_app, ["dashboard"])
    finally:
        poison.unlink(missing_ok=True)
        if not preexisting_dir:
            configs.rmdir()

    assert result.exit_code == 0, result.output
    client = TestClient(captured["app"])
    # Auth is OFF (the poison's ui.token was never read) — /api/health is open.
    health = client.get("/api/health")
    assert health.status_code == 200
    # And the fresh empty-paper manifest declares no strategies (not the poison's).
    assert client.get("/api/strategies").json() == []


def _write_ui_manifest(tmp_path, ui: dict) -> str:  # noqa: ANN001
    """A minimal paper manifest carrying a ``ui:`` section, as a file path str."""
    from trading_bot.application.config import AppConfig

    m = tmp_path / "dash.yaml"
    AppConfig.model_validate({"mode": "paper", "ui": ui}).to_yaml(m)
    return str(m)


def test_dashboard_reads_ui_settings_from_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """A bare `dashboard -c <manifest>` binds host/port/token from the `ui:` section.

    The dccd model: set the web settings **once** in the config and no CLI flags are
    needed — a non-loopback host + token from the manifest serves remotely on its own.
    """
    import uvicorn

    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kw: captured.update(app=app, kwargs=kw)
    )
    manifest = _write_ui_manifest(
        tmp_path, {"host": "0.0.0.0", "port": 9200, "token": "cfg-tok"}
    )

    result = runner.invoke(cli_app, ["dashboard", "-c", manifest])

    assert result.exit_code == 0, result.output  # non-loopback allowed (config token)
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["kwargs"]["port"] == 9200
    # the config token enabled auth on the served app.
    resp = TestClient(captured["app"]).get("/", follow_redirects=False)
    assert resp.status_code == 303  # → /login


def test_dashboard_cli_flags_override_the_ui_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """Explicit `--host` / `--port` win over the manifest's `ui:` section."""
    import uvicorn

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kwargs=kw))
    manifest = _write_ui_manifest(
        tmp_path, {"host": "0.0.0.0", "port": 9200, "token": "cfg-tok"}
    )

    result = runner.invoke(
        cli_app, ["dashboard", "-c", manifest, "--host", "127.0.0.1", "--port", "9300"]
    )
    assert result.exit_code == 0, result.output
    assert captured["kwargs"]["host"] == "127.0.0.1"  # flag overrode config
    assert captured["kwargs"]["port"] == 9300


def test_dashboard_non_loopback_ui_config_without_token_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """A hand-edited manifest binding non-loopback with no token is refused (A-4).

    Written as raw YAML (bypassing ``model_validate``) precisely because
    ``UIConfig`` now **rejects** a non-loopback host + no token at validation — so a
    persisted / hand-edited manifest can never even load, and the CLI surfaces the
    refusal cleanly (never reaching uvicorn, never binding wide open with no auth).
    """
    import uvicorn

    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    called = {"run": False}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: called.update(run=True))
    # Raw YAML — a hand-edited wide-open-no-auth manifest the model would reject.
    manifest = tmp_path / "dash.yaml"
    manifest.write_text("mode: paper\nui:\n  host: 0.0.0.0\n")

    result = runner.invoke(cli_app, ["dashboard", "-c", str(manifest)])
    assert result.exit_code == 1
    assert called["run"] is False
    assert "token" in result.output.lower()


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

    # Behaviour, not prose: the command fails (non-zero) and never serves. The
    # refusal must point the user at the missing auth token (the load-bearing
    # remedy), asserted as a stable keyword rather than the exact sentence — which
    # is free to be reworded without breaking this test.
    assert result.exit_code != 0
    assert called["run"] is False  # never reached uvicorn
    assert "token" in result.output.lower()  # names the missing credential


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


def test_dashboard_calls_start_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dashboard` starts every declared unit before serving (spy the start path).

    The command now brings the declared strategies up (so they come online restored
    + controllable). Patches ``uvicorn.run`` (no socket) and spies
    ``StrategySupervisor.start`` — every declared unit must have been started before
    uvicorn was handed the app.
    """
    import uvicorn

    from trading_bot.application.supervisor import StrategySupervisor

    started: list[str] = []
    real_start = StrategySupervisor.start

    async def _spy_start(self: StrategySupervisor, name: str) -> None:
        started.append(name)
        await real_start(self, name)

    monkeypatch.setattr(StrategySupervisor, "start", _spy_start)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    # A default paper config declares no strategies; use an explicit paper config
    # with one so there is a unit to start.
    import tempfile
    from pathlib import Path

    cfg = (
        "mode: paper\n"
        "brokers:\n  - name: kraken\n    exchange: kraken\n"
        "strategies:\n"
        "  - name: btc-ma\n"
        "    symbol: BTC/USD\n"
        "    data: {exchange: kraken, span: 60}\n"
        "    signal: {ref: ma_crossover, params: {fast: 3, slow: 6}}\n"
        "    reference_qty: '2'\n"
        "    lookback: 6\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cfg.yaml"
        path.write_text(cfg)
        result = runner.invoke(cli_app, ["dashboard", "-c", str(path)])

    assert result.exit_code == 0, result.output
    assert started == ["btc-ma"]  # the declared unit was started before serving


def test_dashboard_no_config_creates_default_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """`dashboard` with no `-c` creates/reads the default `configs/dashboard.yaml`.

    Runs the command in a tmp cwd (so the relative default path lands there) with
    `uvicorn.run` patched, and asserts a fresh empty-paper manifest was written.
    """
    import os

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(cli_app, ["dashboard"])
    finally:
        os.chdir(old_cwd)

    assert result.exit_code == 0, result.output
    manifest = tmp_path / "configs" / "dashboard.yaml"
    assert manifest.is_file()  # created on first launch
    cfg = AppConfig.from_yaml(manifest)
    assert cfg.mode == "paper"
    assert cfg.strategies == [] and cfg.portfolios == []


def test_dashboard_reads_an_existing_default_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """A pre-existing `configs/dashboard.yaml` is read (not overwritten) at launch."""
    import os

    import uvicorn

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))

    configs = tmp_path / "configs"
    configs.mkdir()
    # A manifest declaring one portfolio (no start → no dccd import).
    (configs / "dashboard.yaml").write_text(
        "mode: paper\n"
        "portfolios:\n"
        "  - name: demo1\n"
        "    venue: binance\n"
        "    universe: [BTC/USDT, ETH/USDT]\n"
        "    signal: {ref: 'strategies.demo.signal:portfolio_signal'}\n"
        "    capital: '100000'\n"
        "    data: {exchange: binance, span: 86400}\n"
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = runner.invoke(cli_app, ["dashboard"])
    finally:
        os.chdir(old_cwd)

    assert result.exit_code == 0, result.output
    test_client = TestClient(captured["app"])
    names = [s["name"] for s in test_client.get("/api/strategies").json()]
    assert names == ["demo1"]  # read from the existing manifest


def test_dashboard_tolerates_a_unit_that_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A unit that fails to start is logged and skipped — the dashboard still serves."""
    import uvicorn

    from trading_bot.application.supervisor import StrategySupervisor

    async def _boom(self: StrategySupervisor, name: str) -> None:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(StrategySupervisor, "start", _boom)
    served = {"ran": False}

    def _fake_run(app: object, **kw: object) -> None:
        served["ran"] = True

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    import tempfile
    from pathlib import Path

    cfg = (
        "mode: paper\n"
        "brokers:\n  - name: kraken\n    exchange: kraken\n"
        "strategies:\n"
        "  - name: btc-ma\n"
        "    symbol: BTC/USD\n"
        "    data: {exchange: kraken, span: 60}\n"
        "    signal: {ref: ma_crossover, params: {fast: 3, slow: 6}}\n"
        "    reference_qty: '2'\n"
        "    lookback: 6\n"
    )
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cfg.yaml"
        path.write_text(cfg)
        result = runner.invoke(cli_app, ["dashboard", "-c", str(path)])

    assert result.exit_code == 0, result.output
    assert served["ran"] is True  # the dashboard still served despite the failure
    assert "skipping strategy" in result.output


# --- CLI: serve alias + start --serve fold onto the unified dashboard ------- #


def test_serve_alias_is_the_read_only_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`trading-bot serve` now brings up the unified dashboard **read-only** (an alias).

    The retired split: `serve` folds onto `create_dashboard_app(read_only=True)` over
    a supervisor. Patches `uvicorn.run`, asserts it is called with a FastAPI app and
    the requested host/port, that the built app is the unified shell (Overview +
    Orders + Logs nav), health reports `read_only: true`, and a control mutation is
    refused (403) — no separate read-only-over-one-engine app anymore.
    """
    import uvicorn
    from fastapi import FastAPI

    captured: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", _fake_run)

    result = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--port", "9151"])
    assert result.exit_code == 0, result.output
    assert isinstance(captured["app"], FastAPI)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9151

    client = TestClient(captured["app"])
    html = client.get("/").text
    assert "Overview" in html and "Orders" in html and "Logs" in html
    assert client.get("/api/health").json()["read_only"] is True
    assert client.post("/api/strategies/x/start").status_code == 403


def test_serve_default_config_is_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``--config``, ``serve`` defaults to a paper engine (never live)."""
    import uvicorn

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))

    result = runner.invoke(cli_app, ["serve"])
    assert result.exit_code == 0, result.output

    client = TestClient(captured["app"])
    assert client.get("/api/health").json()["mode"] == "paper"


def test_start_serve_folds_onto_create_dashboard_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start --serve` serves the SINGLE unified dashboard (via `create_dashboard_app`).

    Spies `create_dashboard_app` (the single code path) and stubs the serving loop so
    `_run_daemon(serve=True)` returns promptly — proving the daemon's `--serve` builds
    the unified dashboard over its supervisor, not a separate control app.
    """
    import asyncio

    import trading_bot.interfaces.api as api_pkg

    built: dict[str, object] = {}
    real_factory = api_pkg.create_dashboard_app

    def _spy(supervisor: object, **kwargs: object) -> object:
        app = real_factory(supervisor, **kwargs)  # type: ignore[arg-type]
        built["app"] = app
        built["kwargs"] = kwargs
        return app

    # `_run_daemon` does `from trading_bot.interfaces.api import create_dashboard_app`
    # (the package re-export), so patch the name on the package namespace.
    monkeypatch.setattr(api_pkg, "create_dashboard_app", _spy)

    class _FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config

        async def serve(self) -> None:
            return None  # return immediately (no socket, no blocking)

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: object())

    from trading_bot.interfaces.cli.main import _run_daemon

    # An empty paper config (no units) so start_all/shutdown are trivial + dccd-free.
    asyncio.run(_run_daemon(AppConfig(), interval=0.05, cron=None, serve=True))

    assert "app" in built  # the unified dashboard was built for --serve
    client = TestClient(built["app"])
    assert "Overview" in client.get("/").text  # the unified shell

    # The daemon wires its scheduler cadence into `/api/health` via the
    # `schedule_info` hook — unlike the plain `dashboard` command (no scheduler).
    kwargs = built["kwargs"]
    assert isinstance(kwargs, dict)
    hook = kwargs["schedule_info"]
    assert callable(hook)
    info = hook()
    assert isinstance(info["next_tick_ts"], int)  # apscheduler already scheduled it
    assert info["tick"] == "every 0.05s"
    health = client.get("/api/health").json()
    assert isinstance(health["next_tick_ts"], int)
    assert health["tick"] == "every 0.05s"


def _patch_serve_stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch the uvicorn Server/Config used by `start --serve` and spy the app factory.

    Makes `_run_daemon`'s served branch return immediately (no socket ever opens)
    and records the ``host``/``port`` handed to ``uvicorn.Config`` plus the kwargs
    handed to ``create_dashboard_app`` (notably ``auth_token``) — the two places the
    resolved web settings actually land.
    """
    import uvicorn

    import trading_bot.interfaces.api as api_pkg

    captured: dict[str, object] = {}
    real_factory = api_pkg.create_dashboard_app

    def _spy_factory(supervisor: object, **kwargs: object) -> object:
        built_app = real_factory(supervisor, **kwargs)  # type: ignore[arg-type]
        captured["app"] = built_app
        captured["dashboard_kwargs"] = kwargs
        return built_app

    def _fake_config(app: object, **kwargs: object) -> object:
        captured["config_kwargs"] = kwargs
        return object()

    class _FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config

        async def serve(self) -> None:
            return None  # return immediately (no socket, no blocking)

    monkeypatch.setattr(api_pkg, "create_dashboard_app", _spy_factory)
    monkeypatch.setattr(uvicorn, "Config", _fake_config)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    return captured


def test_start_serve_reads_ui_settings_from_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """`start --serve` with no `--serve-*` flags binds host/port/token from the manifest.

    Mirrors ``dashboard``'s manifest resolution
    (``test_dashboard_reads_ui_settings_from_the_manifest``): a manifest configured
    once serves the control dashboard the same way whether launched via ``dashboard``
    or ``start --serve``. Also proves a non-loopback manifest host is accepted when
    the manifest itself carries the token.
    """
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    captured = _patch_serve_stack(monkeypatch)
    manifest = _write_ui_manifest(
        tmp_path, {"host": "0.0.0.0", "port": 9400, "token": "cfg-tok"}
    )

    result = runner.invoke(
        cli_app, ["start", "--serve", "-c", manifest, "--interval", "0.05"]
    )

    assert result.exit_code == 0, result.output  # non-loopback allowed (config token)
    config_kwargs = captured["config_kwargs"]
    assert isinstance(config_kwargs, dict)
    assert config_kwargs["host"] == "0.0.0.0"
    assert config_kwargs["port"] == 9400
    dashboard_kwargs = captured["dashboard_kwargs"]
    assert isinstance(dashboard_kwargs, dict)
    assert dashboard_kwargs["auth_token"] == "cfg-tok"


def test_start_serve_cli_flags_override_the_ui_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # noqa: ANN001
    """Explicit `--serve-host` / `--serve-port` / `--serve-token` win over the manifest."""
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    captured = _patch_serve_stack(monkeypatch)
    manifest = _write_ui_manifest(
        tmp_path, {"host": "0.0.0.0", "port": 9400, "token": "cfg-tok"}
    )

    result = runner.invoke(
        cli_app,
        [
            "start",
            "--serve",
            "-c",
            manifest,
            "--interval",
            "0.05",
            "--serve-host",
            "127.0.0.1",
            "--serve-port",
            "9500",
            "--serve-token",
            "flag-tok",
        ],
    )

    assert result.exit_code == 0, result.output
    config_kwargs = captured["config_kwargs"]
    assert isinstance(config_kwargs, dict)
    assert config_kwargs["host"] == "127.0.0.1"  # flag overrode the manifest
    assert config_kwargs["port"] == 9500
    dashboard_kwargs = captured["dashboard_kwargs"]
    assert isinstance(dashboard_kwargs, dict)
    assert dashboard_kwargs["auth_token"] == "flag-tok"


def test_start_serve_no_config_no_flags_defaults_to_loopback_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `--config` and no `--serve-*` flags: `start --serve` stays loopback :8000, no auth.

    A bare :class:`~trading_bot.application.config.AppConfig`'s ``ui`` defaults must
    keep today's behaviour so an operator who never touches the manifest sees no
    change.
    """
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    captured = _patch_serve_stack(monkeypatch)

    result = runner.invoke(cli_app, ["start", "--serve", "--interval", "0.05"])

    assert result.exit_code == 0, result.output
    config_kwargs = captured["config_kwargs"]
    assert isinstance(config_kwargs, dict)
    assert config_kwargs["host"] == "127.0.0.1"
    assert config_kwargs["port"] == 8000
    dashboard_kwargs = captured["dashboard_kwargs"]
    assert isinstance(dashboard_kwargs, dict)
    assert dashboard_kwargs["auth_token"] is None


def test_start_defaults_to_the_dashboard_manifest_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare `start --serve` picks up ./configs/dashboard.yaml when it exists.

    The daemon runs the same manifest the dashboard manages (the persistent
    control plane), so no ``--config`` is needed once that file exists — proven
    here by the manifest's ``ui:`` settings landing on the served dashboard.
    The autouse temp-CWD fixture guarantees the file seen is the one written.
    """
    import pathlib as _pathlib

    from trading_bot.application.config import AppConfig

    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    captured = _patch_serve_stack(monkeypatch)
    manifest = _pathlib.Path("configs/dashboard.yaml")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    AppConfig.model_validate(
        {"mode": "paper", "ui": {"host": "0.0.0.0", "port": 9600, "token": "cfg-tok"}}
    ).to_yaml(manifest)

    result = runner.invoke(cli_app, ["start", "--serve", "--interval", "0.05"])

    assert result.exit_code == 0, result.output
    assert "using default manifest" in result.output
    config_kwargs = captured["config_kwargs"]
    assert isinstance(config_kwargs, dict)
    assert config_kwargs["host"] == "0.0.0.0"  # came from the default manifest
    assert config_kwargs["port"] == 9600
    dashboard_kwargs = captured["dashboard_kwargs"]
    assert isinstance(dashboard_kwargs, dict)
    assert dashboard_kwargs["auth_token"] == "cfg-tok"


def test_start_serve_non_loopback_without_token_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-loopback `--serve-host` with no token anywhere refuses (never serves).

    Mirrors ``test_dashboard_non_loopback_without_token_refuses``: the same guard
    (currently evaluated in ``_run_daemon``) still applies once the host/token have
    been resolved from flags/env/manifest.
    """
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    called = {"server": False}

    class _FakeServer:
        def __init__(self, config: object) -> None:  # pragma: no cover
            called["server"] = True

        async def serve(self) -> None:  # pragma: no cover
            return None

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: object())

    result = runner.invoke(cli_app, ["start", "--serve", "--serve-host", "0.0.0.0"])

    assert result.exit_code != 0
    assert called["server"] is False  # never reached uvicorn
    assert "token" in result.output.lower()


async def test_start_serve_disables_uvicorn_signal_capture_and_owns_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start --serve` disables uvicorn's own signal capture and owns Ctrl-C itself.

    Regression test for the "Ctrl-C loses the teardown" bug: uvicorn 0.49's
    ``Server.serve()`` unconditionally wraps itself in ``capture_signals()``,
    which restores the pre-``serve()`` signal disposition and **re-raises** the
    captured SIGINT/SIGTERM right after ``serve()`` returns — killing the
    process before ``_run_daemon``'s own ``finally`` (scheduler + supervisor
    teardown) can run. ``_run_daemon`` works around this (there is no public
    ``install_signal_handlers`` config flag on this uvicorn version) by
    overriding the server instance's ``capture_signals`` with a no-op context
    manager and installing its own loop-level SIGINT/SIGTERM handlers around
    ``await server.serve()``. This proves both halves: the override is applied,
    and the daemon's own handlers are the ones registered *while* serving —
    then removed once ``serve()`` returns. The end-to-end "the process actually
    survives a real Ctrl-C and completes its teardown" claim is verified
    separately by the pty-driver check (a unit test cannot deliver a real OS
    signal to itself safely).
    """
    import asyncio
    import contextlib
    import signal

    import trading_bot.interfaces.api as api_pkg

    monkeypatch.setattr(api_pkg, "create_dashboard_app", lambda sup, **kw: object())

    built: dict[str, object] = {}
    ready = asyncio.Event()
    release = asyncio.Event()
    installed_during_serve: set[int] = set()

    class _FakeServer:
        def __init__(self, config: object) -> None:
            self.config = config
            self.should_exit = False
            self.force_exit = False
            built["server"] = self

        async def serve(self) -> None:
            loop = asyncio.get_running_loop()
            installed_during_serve.update(loop._signal_handlers)  # noqa: SLF001
            ready.set()
            await release.wait()

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: object())

    from trading_bot.interfaces.cli.main import _run_daemon

    task = asyncio.ensure_future(
        _run_daemon(AppConfig(), interval=0.05, cron=None, serve=True)
    )
    await ready.wait()

    # The daemon's own handlers are installed WHILE serving — the exact window
    # uvicorn 0.49 would otherwise own via its own (now-disabled) capture.
    assert {signal.SIGINT, signal.SIGTERM} <= installed_during_serve
    # uvicorn's own capture/re-raise is disabled on this server instance.
    assert built["server"].capture_signals is contextlib.nullcontext  # type: ignore[attr-defined]

    release.set()
    await task

    # Removed again once `serve()` returned — nothing left registered, so a
    # later real signal has exactly one handler to reach: the OS default.
    loop = asyncio.get_running_loop()
    assert signal.SIGINT not in loop._signal_handlers  # noqa: SLF001
    assert signal.SIGTERM not in loop._signal_handlers  # noqa: SLF001


# --- web hardening (audit wave 3: I-4, I-6, I-7, I-9, I-10, I-11, I-13) ----- #


def test_security_headers_on_authed_json() -> None:
    """I-11: `/api/*` JSON carries nosniff + anti-clickjacking + no-store headers."""
    resp = _client().get("/api/health")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["referrer-policy"] == "no-referrer"
    # The live book must never be cached by a browser / intermediary.
    assert resp.headers["cache-control"] == "no-store"


def test_security_headers_on_html_pages() -> None:
    """I-11: the HTML shell carries the security headers too (no no-store — no book)."""
    resp = _client().get("/")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    # A page is not under /api/, so no forced no-store (it carries no engine data).
    assert resp.headers.get("cache-control") != "no-store"


def test_oversized_deploy_body_is_rejected() -> None:
    """I-6: a request body over the size cap is refused 413 (memory/disk-amp guard)."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    # A >64 KiB body via a giant `params` blob — over the middleware cap.
    body = {**_portfolio_deploy_body("big"), "params": {"x": "A" * 70_000}}
    r = client.post("/api/strategies", json=body)
    assert r.status_code == 413, r.status_code
    # Nothing was deployed.
    assert client.get("/api/strategies").json() == []


def test_oversized_name_field_is_rejected() -> None:
    """I-6: a giant `name` field is rejected (per-field max_length, defence in depth)."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    body = {**_portfolio_deploy_body(), "name": "n" * 1000}  # over max_length=128
    r = client.post("/api/strategies", json=body)
    assert r.status_code == 422, r.text  # pydantic rejects before any add
    assert client.get("/api/strategies").json() == []


def test_deploy_mode_mismatch_is_rejected() -> None:
    """I-9: a deploy `mode` that the manifest would not seed is refused 422 (honest API)."""
    sup = StrategySupervisor(AppConfig())  # paper manifest → seeds paper
    client = TestClient(create_dashboard_app(sup))
    body = {**_portfolio_deploy_body("wants-live"), "mode": "live"}
    r = client.post("/api/strategies", json=body)
    assert r.status_code == 422, r.text
    assert "paper" in r.json()["detail"]
    # Rolled back — nothing left added.
    assert client.get("/api/strategies").json() == []


def test_deploy_mode_matching_manifest_is_accepted() -> None:
    """I-9: a deploy `mode` equal to the manifest seed (paper) is accepted."""
    sup = StrategySupervisor(AppConfig())
    client = TestClient(create_dashboard_app(sup))
    r = client.post("/api/strategies", json=_portfolio_deploy_body("ok"))  # mode=paper
    assert r.status_code == 200, r.text
    assert client.get("/api/strategies").json()[0]["mode"] == "paper"


def test_session_and_rate_maps_are_pruned() -> None:
    """I-7: expired sessions AND idle rate-buckets are swept — no unbounded growth."""
    import trading_bot.interfaces.api.app as appmod

    token = "secret-token"
    app = create_dashboard_app(_supervisor(), auth_token=token)
    client = TestClient(app)

    # A login to seed both maps: a rate-bucket (this peer) + a session.
    csrf = (client.get("/login"), client.cookies.get("tb_csrf", ""))[1]
    client.post(
        "/login",
        data={"token": token, "next": "/", "csrf": csrf},
        follow_redirects=False,
    )
    assert len(app.state.sessions) == 1
    assert len(app.state.login_buckets) >= 1

    # Force both entries "old": backdate the session timestamp beyond its TTL and the
    # rate bucket beyond its idle window, then a fresh valid_session call prunes.
    old_ns = 0  # epoch — far past any TTL cutoff
    app.state.sessions = {sid: old_ns for sid in app.state.sessions}
    stale_key = next(iter(app.state.login_buckets))
    # `time.monotonic()` counts machine uptime, not wall clock — backdate relative
    # to "now" (not to epoch 0) so the bucket is stale even on a freshly booted VM.
    stale_last_seen = time.monotonic() - appmod._RATE_BUCKET_TTL_SECONDS - 1
    app.state.login_buckets[stale_key] = (
        float(appmod._LOGIN_RATE_PER_MIN),
        stale_last_seen,
    )

    # Any auth check runs the prune sweep (session gone → 401; bucket swept).
    assert client.get("/api/health").status_code == 401
    assert app.state.sessions == {}  # expired session pruned
    assert stale_key not in app.state.login_buckets  # idle bucket pruned


def test_session_map_is_capped() -> None:
    """I-7: the session map never exceeds the hard cap (oldest evicted on overflow)."""
    import trading_bot.interfaces.api.app as appmod

    app = create_dashboard_app(_supervisor(), auth_token="t")
    # Fill the map to the cap with dummy sessions, then a new login evicts the oldest.
    for i in range(appmod._MAX_SESSIONS):
        app.state.sessions[f"sid-{i}"] = i  # ascending timestamps
    client = TestClient(app)
    csrf = (client.get("/login"), client.cookies.get("tb_csrf", ""))[1]
    client.post(
        "/login",
        data={"token": "t", "next": "/", "csrf": csrf},
        follow_redirects=False,
    )
    assert len(app.state.sessions) <= appmod._MAX_SESSIONS
    assert "sid-0" not in app.state.sessions  # the oldest was evicted


def test_login_without_csrf_is_403_and_mints_no_session() -> None:
    """I-13: a login POST with the correct token but no CSRF field is refused 403."""
    client, token = _auth_client()
    client.get("/login")  # sets the CSRF cookie
    r = client.post(
        "/login", data={"token": token, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 403
    assert client.get("/api/health").status_code == 401  # no session minted


def test_login_session_cookie_is_samesite_strict() -> None:
    """I-13: the session cookie is SameSite=strict (blocks cross-site cookie carry)."""
    client, token = _auth_client()
    client.get("/login")
    csrf = client.cookies.get("tb_csrf", "")
    ok = client.post(
        "/login",
        data={"token": token, "next": "/", "csrf": csrf},
        follow_redirects=False,
    )
    set_cookie = ok.headers.get("set-cookie", "")
    assert "tb_session=" in set_cookie
    assert "samesite=strict" in set_cookie.lower()


def test_secure_cookie_not_forced_by_x_forwarded_proto() -> None:
    """I-4: a client `X-Forwarded-Proto: https` does NOT force the Secure cookie flag.

    On the plain-HTTP tailnet the header is client-controlled; forcing Secure would
    self-break the cookie. Only a real https scheme sets Secure (trust is off by
    default — `_TRUST_FORWARDED_PROTO`).
    """
    client, token = _auth_client()
    client.get("/login", headers={"X-Forwarded-Proto": "https"})
    csrf = client.cookies.get("tb_csrf", "")
    ok = client.post(
        "/login",
        data={"token": token, "next": "/", "csrf": csrf},
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    set_cookie = ok.headers.get("set-cookie", "").lower()
    assert "tb_session=" in set_cookie
    assert "secure" not in set_cookie  # not forced by the spoofed header


# --- shared serialization helpers (module-level, both apps build on) -------- #
#
# Ported from the retired ``test_api.py`` (leaf 01, single-engine dashboard):
# these exercise ``trading_bot.interfaces.api.app``'s helper functions directly
# — no engine/app fixture needed — so they survive the legacy ``create_app``'s
# removal unchanged. The high-level positions/orders/health/kpi/SSE coverage
# ``test_api.py`` also carried is superseded by this file's supervisor-level
# equivalents (e.g. ``test_events_stream_merges_and_yields_a_fill``, the
# ``/api/kpi`` level tests above) and was not re-ported.


def test_decimal_json_response_renders_exact_string_not_lossy_float() -> None:
    """The Decimal-as-string JSON response renders exact strings, never lossy floats.

    Guards the Decimal-as-string invariant at the byte level: ``Decimal("0.1")``
    must render as the JSON string ``"0.1"``, never the float ``0.1`` (whose true
    binary value is ``0.1000000000000000055511151231257827021181583404541015625``).
    """
    from trading_bot.interfaces.api.app import _DecimalJSONResponse

    resp = _DecimalJSONResponse(
        {"net_qty": Decimal("0.1"), "avg_entry_price": Decimal("30000.1")}
    )
    raw = resp.body.decode()
    assert '"net_qty":"0.1"' in raw
    assert '"avg_entry_price":"30000.1"' in raw
    assert '"net_qty":0.1' not in raw
    assert "0.1000000000000000055511151231257827021181583404541015625" not in raw


def test_decimal_encoder_renders_decimal_as_string_and_rejects_other() -> None:
    """The JSON ``default`` hook stringifies a Decimal exactly, else raises."""
    from trading_bot.interfaces.api.app import _default

    assert _default(Decimal("0.1")) == "0.1"
    assert json.dumps({"x": Decimal("1.5")}, default=_default) == '{"x": "1.5"}'
    with pytest.raises(TypeError):
        _default(object())


def test_finite_or_none_maps_non_finite_and_none_to_null() -> None:
    """A KPI ratio that is ``inf``/``nan``/``None`` degrades to JSON ``null``.

    A *monotonically rising* equity curve has zero drawdown, so Calmar
    (return / max-drawdown) is ``inf`` on an otherwise valid, winning curve — a
    bare ``inf``/``nan`` is not valid JSON, so it must map to ``null`` rather
    than raising or serializing lossily. A finite value passes through unchanged.
    """
    import math

    from trading_bot.interfaces.api.app import _finite_or_none

    assert _finite_or_none(None) is None
    assert _finite_or_none(math.inf) is None
    assert _finite_or_none(math.nan) is None
    assert _finite_or_none(1.25) == 1.25


def test_event_dict_serializes_each_event_type_with_string_money() -> None:
    """``_event_dict`` tags + renders order/fill/log events (money as strings)."""
    from trading_bot.interfaces.api.app import _event_dict

    btc = Instrument(Symbol("BTC", "USD"))
    order = Order(
        client_order_id="cid-1",
        instrument=btc,
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


def _log_record(*, msg: str, exc: BaseException | None) -> logging.LogRecord:
    """Build a bare ``LogRecord`` carrying ``exc`` as its ``exc_info`` (or none)."""
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def test_graceful_shutdown_cancellation_filter_drops_only_the_expected_shape() -> None:
    """The uvicorn.error filter drops a shutdown CancelledError, keeps real errors.

    Regression test for the Ctrl-C quiet-shutdown fix: uvicorn 0.49 logs *any*
    exception escaping the ASGI app as an ERROR "Exception in ASGI application"
    record — including the deliberate ``CancelledError`` it raises itself when
    force-cancelling a still-open SSE connection past the graceful-shutdown
    timeout. The filter must drop *that* shape (regardless of the exception's
    message — see the class docstring on why nested ``BaseHTTPMiddleware``
    task groups can strip it) while leaving a genuine application error, or a
    ``CancelledError`` logged under an unrelated message, alone.
    """
    from trading_bot.interfaces.api.app import _SuppressGracefulShutdownCancellation

    carveout = _SuppressGracefulShutdownCancellation()

    # Dropped: a CancelledError (message-bearing or not) under uvicorn's own
    # "Exception in ASGI application" message.
    assert (
        carveout.filter(
            _log_record(
                msg="Exception in ASGI application",
                exc=asyncio.CancelledError(
                    "Task cancelled, timeout graceful shutdown exceeded"
                ),
            )
        )
        is False
    )
    assert (
        carveout.filter(
            _log_record(
                msg="Exception in ASGI application",
                exc=asyncio.CancelledError(),
            )
        )
        is False
    )

    # Kept: a real bug under the same message.
    assert (
        carveout.filter(
            _log_record(msg="Exception in ASGI application", exc=ValueError("boom"))
        )
        is True
    )
    # Kept: a CancelledError logged under a different, unrelated message.
    assert (
        carveout.filter(
            _log_record(msg="some other message", exc=asyncio.CancelledError())
        )
        is True
    )
    # Kept: no exc_info at all.
    assert carveout.filter(_log_record(msg="plain info line", exc=None)) is True


def test_graceful_shutdown_cancellation_filter_installs_once_per_process() -> None:
    """Building an app repeatedly never stacks up duplicate filter instances.

    ``create_control_app``/``create_dashboard_app`` install the filter on the
    shared, process-wide ``uvicorn.error`` logger every time they build an app
    (tests build many); the installer must stay idempotent.
    """
    from trading_bot.interfaces.api.app import (
        _suppress_graceful_shutdown_cancellation_logs,
        _SuppressGracefulShutdownCancellation,
    )

    target = logging.getLogger("uvicorn.error")
    before = sum(
        isinstance(f, _SuppressGracefulShutdownCancellation) for f in target.filters
    )
    _suppress_graceful_shutdown_cancellation_logs()
    _suppress_graceful_shutdown_cancellation_logs()
    after = sum(
        isinstance(f, _SuppressGracefulShutdownCancellation) for f in target.filters
    )
    assert after == max(before, 1)
