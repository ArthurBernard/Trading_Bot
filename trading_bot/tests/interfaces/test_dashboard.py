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
        Fill("SF2", "sc2", inst, OrderSide.SELL, money("1"), money("110"), money("1"), 2)
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


# --- PnL page markup + vendored uPlot assets ------------------------------- #


def test_pnl_page_has_chart_container_and_selector() -> None:
    """`GET /pnl` carries the chart container, strategy selector + uPlot reference."""
    html = _client().get("/pnl").text
    assert 'id="pnl-chart"' in html  # the uPlot mount
    assert 'id="pnl-strategy"' in html  # the strategy selector
    assert "/static/uplot.min.js" in html  # the vendored chart library
    assert "/static/uplot.min.css" in html  # its stylesheet
    assert "/api/pnl" in html  # it fetches the per-mode series
    assert "/api/strategies" in html  # it populates the selector


def test_vendored_uplot_assets_are_served() -> None:
    """The static mount serves the real vendored uPlot bundle + stylesheet (200)."""
    client = _client()
    js = client.get("/static/uplot.min.js")
    assert js.status_code == 200
    assert "uPlot" in js.text  # the IIFE bundle's global
    css = client.get("/static/uplot.min.css")
    assert css.status_code == 200
    assert ".uplot" in css.text


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
            Fill(f"B{i}", f"cB{i}", inst, OrderSide.BUY,
                 money("1"), money(str(buy_px)), money("0"), 2 * i + 1)
        )
        store.record_fill(
            Fill(f"S{i}", f"cS{i}", inst, OrderSide.SELL,
                 money("1"), money(str(sell_px)), money("0"), 2 * i + 2)
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
    sup = StrategySupervisor(_fills_config_with_store(db), dccd_client=_two_venue_client())
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
    sup = StrategySupervisor(_fills_config_with_store(db), dccd_client=_two_venue_client())
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
    sup = StrategySupervisor(_fills_config_with_store(db), dccd_client=_two_venue_client())
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
    order = Order("oc1", btc, OrderSide.BUY, money("1"), OrderType.LIMIT,
                  limit_price=money("100"))
    order.status = OrderStatus.FILLED
    order.filled_qty = money("1")
    store.upsert_order(order)

    sup = StrategySupervisor(_fills_config_with_store(db), dccd_client=_two_venue_client())
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


def test_logs_page_has_feed_and_subscribes_to_sse() -> None:
    """`GET /logs` carries the activity feed container and subscribes to /api/events."""
    html = _client().get("/logs").text
    assert 'id="logs-feed"' in html  # the feed container
    assert "/api/events" in html  # it subscribes to the merged SSE stream
    assert "connect(" in html  # via the shared connect() helper


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


# --- strategy control (list + start/stop/mode, the live gate) -------------- #


def test_strategies_endpoint_lists_units_with_exchange() -> None:
    """`GET /api/strategies` lists the managed units, tagged with their exchange."""
    resp = _client().get("/api/strategies")
    assert resp.status_code == 200
    [s] = resp.json()
    assert s["name"] == "btc-ma"
    assert s["exchange"] == "kraken"  # grouped/displayed by exchange
    assert s["mode"] == "paper"
    assert s["running"] is False


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
    """With the deliberate confirmation, the mode flips to live."""
    client = _client()
    r = client.post(
        "/api/strategies/btc-ma/mode", json={"mode": "live", "confirm": True}
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
            StrategySupervisor(
                _config(), dccd_client=_FakeStartClient()
            )
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


def test_strategies_page_has_table_and_live_modal() -> None:
    """`GET /strategies` carries the grouped table + mode select + the live modal."""
    html = _client().get("/strategies").text
    assert 'id="strategies-body"' in html  # the table the page fills
    assert "mode-select" in html  # the paper/testnet/live select
    assert 'id="live-modal"' in html  # the deliberate go-live confirmation
    assert "I UNDERSTAND" in html  # the typed-confirmation phrase
    assert "/api/strategies" in html  # it wires the control endpoints


def test_strategies_page_read_only_note() -> None:
    """A read-only dashboard's Strategies page advertises disabled controls."""
    html = _client(read_only=True).get("/strategies").text
    assert "read-only" in html.lower()


def test_strategies_page_has_deploy_form_when_writable() -> None:
    """A writable Strategies page carries the deploy form wired to /api/signals."""
    html = _client().get("/strategies").text
    assert 'id="deploy-form"' in html
    assert "/api/signals" in html  # the form fetches discoverable signal refs


def test_strategies_page_hides_deploy_form_when_read_only() -> None:
    """A read-only Strategies page omits the deploy form (no create affordance)."""
    html = _client(read_only=True).get("/strategies").text
    assert 'id="deploy-form"' not in html


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
        assert (
            f"strategies.{pkg}.signal:probe_signal" in body["discovered"]
        ), body["discovered"]
        # A re-exported helper (as_portfolio_signal) / a private closure is NOT a ref.
        assert not any(
            ref.endswith(":as_portfolio_signal") for ref in body["discovered"]
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        sys.modules.pop(f"strategies.{pkg}.signal", None)
        sys.modules.pop(f"strategies.{pkg}", None)


def _portfolio_deploy_body(name: str = "alloc1") -> dict:
    """A create-deployment body deploying the alloc1 portfolio (paper, binance)."""
    return {
        "name": name,
        "kind": "portfolio",
        "venue": "binance",
        "mode": "paper",
        "signal": "strategies.alloc1.signal:alloc1_portfolio_signal",
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
    assert names == ["alloc1"]

    # And it was PERSISTED to disk — re-read the manifest file.
    assert manifest.is_file()
    reloaded = AppConfig.from_yaml(manifest)
    assert [p.name for p in reloaded.portfolios] == ["alloc1"]
    assert reloaded.portfolios[0].capital == money("100000")


def test_create_then_delete_persists_the_removal(tmp_path) -> None:  # noqa: ANN001
    """`DELETE /api/strategies/{name}` removes the unit and rewrites the manifest."""
    manifest = tmp_path / "dashboard.yaml"
    sup = StrategySupervisor(AppConfig())

    client = TestClient(
        create_dashboard_app(sup, on_change=lambda: sup.manifest().to_yaml(manifest))
    )
    client.post("/api/strategies", json=_portfolio_deploy_body())
    assert AppConfig.from_yaml(manifest).portfolios  # persisted on create

    r = client.delete("/api/strategies/alloc1")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": "alloc1"}
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
            "signal": "pkg.mod:sig",
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
        client.post("/api/strategies", json=_portfolio_deploy_body()).status_code
        == 403
    )
    assert client.delete("/api/strategies/btc-ma").status_code == 403
    # Nothing changed — the one declared unit is still there, unremoved.
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
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kw: captured.update(app=app)
    )

    configs = tmp_path / "configs"
    configs.mkdir()
    # A manifest declaring one portfolio (no start → no dccd import).
    (configs / "dashboard.yaml").write_text(
        "mode: paper\n"
        "portfolios:\n"
        "  - name: alloc1\n"
        "    venue: binance\n"
        "    universe: [BTC/USDT, ETH/USDT]\n"
        "    signal: {ref: 'strategies.alloc1.signal:alloc1_portfolio_signal'}\n"
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
    assert names == ["alloc1"]  # read from the existing manifest


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
    a supervisor. Patches `uvicorn.run`, asserts the built app is the unified shell
    (Overview + Orders + Logs nav), health reports `read_only: true`, and a control
    mutation is refused (403) — no separate read-only-over-one-engine app anymore.
    """
    import uvicorn

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(app=app))

    result = runner.invoke(cli_app, ["serve", "--port", "9151"])
    assert result.exit_code == 0, result.output

    client = TestClient(captured["app"])
    html = client.get("/").text
    assert "Overview" in html and "Orders" in html and "Logs" in html
    assert client.get("/api/health").json()["read_only"] is True
    assert client.post("/api/strategies/x/start").status_code == 403


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
    asyncio.run(
        _run_daemon(AppConfig(), interval=0.05, cron=None, serve=True)
    )

    assert "app" in built  # the unified dashboard was built for --serve
    client = TestClient(built["app"])
    assert "Overview" in client.get("/").text  # the unified shell
