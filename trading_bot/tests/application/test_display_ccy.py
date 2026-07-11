"""Tests for display-currency resolution + conversion (api-completeness leaf 03).

Two layers, mirroring :mod:`trading_bot.application.display_ccy`'s own split:

* **Pure** — :func:`~trading_bot.application.display_ccy.resolve_currency` /
  :func:`~trading_bot.application.display_ccy.convert` directly, plus the three
  new :class:`~trading_bot.application.config.AppConfig` fields' validation.
* **API shape** — a real :class:`~trading_bot.application.supervisor.
  StrategySupervisor` (two venues: Kraken ``USD``-quoted, Binance
  ``USDT``-quoted — the real book layout) driven through
  :func:`~trading_bot.interfaces.api.create_dashboard_app` with a
  :class:`fastapi.testclient.TestClient`: proves ``/api/positions``,
  ``/api/strategies`` and ``/api/kpi`` rows carry the new ``display_currency``
  / ``*_display`` fields (additive) with every pre-existing field
  byte-identical (contract regression, the leaf-02 pattern).
"""

from __future__ import annotations

# Built-in
import asyncio

# Third-party
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

# Local
from trading_bot.application.config import AppConfig
from trading_bot.application.display_ccy import convert, resolve_currency
from trading_bot.application.events import FillEvent
from trading_bot.application.supervisor import StrategySupervisor
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument, Symbol
from trading_bot.domain.money import money
from trading_bot.domain.order import OrderSide
from trading_bot.interfaces.api import create_dashboard_app
from trading_bot.tests.application.test_supervisor import (
    _dccd_ohlc,
    _FakeDccdClient,
    _trend,
)

# ---------------------------------------------------------------------------
# Pure: resolve_currency + convert
# ---------------------------------------------------------------------------


def _config(**overrides: object) -> AppConfig:
    """A minimal paper :class:`AppConfig` with the given display-currency fields."""
    return AppConfig.model_validate({"mode": "paper", **overrides})


def test_convert_identity_for_the_display_currency_itself() -> None:
    """``from_ccy == display_currency`` never needs a declared rate."""
    cfg = _config(display_currency="USD")
    assert convert(money("42.5"), "USD", cfg) == money("42.5")


def test_convert_declared_rate_is_decimal_exact() -> None:
    """A declared rate converts exactly — no binary-float error."""
    cfg = _config(display_currency="USD", conversion_rates={"EUR": "1.08"})
    assert convert(money("100"), "EUR", cfg) == money("108.00")


def test_convert_missing_rate_is_none_never_guessed() -> None:
    """No declared rate for the source currency → ``None``, not a fabricated figure."""
    cfg = _config(display_currency="USD")
    assert convert(money("100"), "EUR", cfg) is None


def test_convert_none_amount_passes_through() -> None:
    """An unmarked/unknown amount (``None``) stays ``None`` (nothing to convert)."""
    cfg = _config(display_currency="USD", conversion_rates={"EUR": "1.08"})
    assert convert(None, "EUR", cfg) is None


def test_convert_none_from_ccy_is_none_never_guessed() -> None:
    """A mixed-quote aggregate (``quote is None``) never guesses a source currency."""
    cfg = _config(display_currency="USD", conversion_rates={"USD": "1"})
    assert convert(money("100"), None, cfg) is None


def test_resolve_currency_override_for_named_exchange() -> None:
    """An overridden exchange's rows are labelled with the override, not the default."""
    cfg = _config(
        display_currency="USD", display_currency_overrides={"binance": "USDT"}
    )
    assert resolve_currency("binance", cfg) == "USDT"


def test_resolve_currency_default_for_an_unlisted_exchange() -> None:
    """An exchange with no override falls back to the global ``display_currency``."""
    cfg = _config(
        display_currency="USD", display_currency_overrides={"binance": "USDT"}
    )
    assert resolve_currency("kraken", cfg) == "USD"


def test_resolve_currency_none_exchange_falls_back_to_default() -> None:
    """No single exchange (a KPI ``level="total"`` row) resolves like an unlisted one."""
    cfg = _config(
        display_currency="USD", display_currency_overrides={"binance": "USDT"}
    )
    assert resolve_currency(None, cfg) == "USD"


def test_display_currency_defaults_to_usd_with_empty_overrides_and_rates() -> None:
    """A fresh config never fails to resolve/convert — everything defaults empty."""
    cfg = AppConfig.model_validate({})
    assert cfg.display_currency == "USD"
    assert cfg.display_currency_overrides == {}
    assert cfg.conversion_rates == {}


def test_zero_conversion_rate_rejected_at_config_parse() -> None:
    """A non-positive rate is rejected — the money-field pattern."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"conversion_rates": {"EUR": "0"}})


def test_negative_conversion_rate_rejected_at_config_parse() -> None:
    """A negative rate is rejected — the money-field pattern."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"conversion_rates": {"EUR": "-1.08"}})


def test_blank_display_currency_rejected_at_config_parse() -> None:
    """An empty/whitespace ``display_currency`` is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"display_currency": "   "})


# ---------------------------------------------------------------------------
# API shape: /api/positions, /api/strategies, /api/kpi
# ---------------------------------------------------------------------------


#: The real book layout (see ``configs/dashboard.yaml``): Kraken quotes in USD,
#: Binance quotes in USDT. Each strategy declares an ``allocation`` so
#: ``contributed``/``unrealised``/``total_value`` populate (non-``None``),
#: exercising the strategy-row conversion pair.
def _two_venue_config(
    *,
    display_currency: str = "USD",
    display_currency_overrides: dict[str, str] | None = None,
    conversion_rates: dict[str, str] | None = None,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "display_currency": display_currency,
            "display_currency_overrides": display_currency_overrides or {},
            "conversion_rates": conversion_rates or {},
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
                    "allocation": "1000",
                },
                {
                    "name": "eth-binance",
                    "symbol": "ETH/USDT",
                    "data": {"exchange": "binance", "span": 60},
                    "signal": {"ref": "ma_crossover", "params": {"fast": 3, "slow": 6}},
                    "reference_qty": "2",
                    "lookback": 6,
                    "allocation": "1000",
                },
            ],
        }
    )


def _two_venue_client() -> _FakeDccdClient:
    """Offline dccd client — `start()` never imports the real dccd (absent in CI)."""
    return _FakeDccdClient(
        {"BTC/USD": _dccd_ohlc(_trend()), "ETH/USDT": _dccd_ohlc(_trend())}
    )


async def _seeded_supervisor(**config_kwargs: object) -> StrategySupervisor:
    """Two running units (Kraken USD, Binance USDT), each with an open, marked position.

    A single opening buy per unit (position stays open, unlike a closing round
    trip) plus a directly-seeded :class:`~trading_bot.application.mark_cache.
    MarkCache` entry (the leaf-02 contract-test technique) so ``value`` /
    ``unrealised`` / ``total_value`` are non-``None`` on every row — otherwise
    the ``_display`` conversion would be trivially ``None`` regardless of
    config, and prove nothing.
    """
    sup = StrategySupervisor(
        _two_venue_config(**config_kwargs), dccd_client=_two_venue_client()
    )
    await sup.start("btc-kraken")
    await sup.start("eth-binance")

    kraken_unit = sup._units["btc-kraken"]  # noqa: SLF001 — direct seed, test-only
    kraken_unit.engine.bus.emit(
        FillEvent(
            Fill(
                "K-F1",
                "K-c1",
                Instrument(Symbol("BTC", "USD")),
                OrderSide.BUY,
                money("2"),
                money("100"),
                money("1"),
                1,
            )
        )
    )
    kraken_unit.engine.mark_cache.update(Symbol("BTC", "USD"), money("110"), 9_000)

    binance_unit = sup._units["eth-binance"]  # noqa: SLF001 — direct seed, test-only
    binance_unit.engine.bus.emit(
        FillEvent(
            Fill(
                "B-F1",
                "B-c1",
                Instrument(Symbol("ETH", "USDT")),
                OrderSide.BUY,
                money("2"),
                money("200"),
                money("1"),
                1,
            )
        )
    )
    binance_unit.engine.mark_cache.update(Symbol("ETH", "USDT"), money("220"), 9_000)
    return sup


def test_api_positions_display_fields_default_config() -> None:
    """No override: Kraken's own USD quote matches the global default (identity,
    non-``null``); Binance's USDT quote has no declared rate (``null``), native
    ``value``/``unrealised`` untouched.
    """
    sup = asyncio.run(_seeded_supervisor())
    rows = TestClient(create_dashboard_app(sup)).get("/api/positions").json()
    by_exchange = {r["exchange"]: r for r in rows}
    kraken_row = by_exchange["kraken"]
    binance_row = by_exchange["binance"]

    assert kraken_row["fee_ccy"] == "USD"
    assert kraken_row["display_currency"] == "USD"
    assert kraken_row["value_display"] == kraken_row["value"]
    assert kraken_row["unrealised_display"] == kraken_row["unrealised"]

    assert binance_row["fee_ccy"] == "USDT"
    assert binance_row["display_currency"] == "USD"
    assert binance_row["value"] is not None  # native untouched
    assert binance_row["value_display"] is None
    assert binance_row["unrealised_display"] is None


def test_api_positions_display_fields_with_override_and_identity_rate() -> None:
    """``overrides={"binance":"USDT"}`` + ``rates={"USDT":"1"}``: Binance rows
    label + display in USDT (identity vs native); Kraken rows stay USD.
    """
    sup = asyncio.run(
        _seeded_supervisor(
            display_currency_overrides={"binance": "USDT"},
            conversion_rates={"USDT": "1"},
        )
    )
    rows = TestClient(create_dashboard_app(sup)).get("/api/positions").json()
    by_exchange = {r["exchange"]: r for r in rows}
    kraken_row = by_exchange["kraken"]
    binance_row = by_exchange["binance"]

    assert binance_row["display_currency"] == "USDT"
    assert binance_row["value_display"] == binance_row["value"]
    assert binance_row["unrealised_display"] == binance_row["unrealised"]

    assert kraken_row["display_currency"] == "USD"
    assert kraken_row["value_display"] == kraken_row["value"]
    assert kraken_row["unrealised_display"] == kraken_row["unrealised"]


def test_api_positions_display_fields_empty_rates_goes_null() -> None:
    """Declaring ``conversion_rates={}`` (no rate for USDT): Binance's displays
    go ``null`` while its native ``value``/``unrealised`` stay untouched;
    Kraken's own quote (``USD``) still identity-matches the global default (a
    same-currency conversion never needs a declared rate).
    """
    sup = asyncio.run(
        _seeded_supervisor(
            display_currency_overrides={"binance": "USDT"}, conversion_rates={}
        )
    )
    rows = TestClient(create_dashboard_app(sup)).get("/api/positions").json()
    by_exchange = {r["exchange"]: r for r in rows}
    kraken_row = by_exchange["kraken"]
    binance_row = by_exchange["binance"]

    assert binance_row["value_display"] is None
    assert binance_row["unrealised_display"] is None
    assert binance_row["value"] is not None
    assert binance_row["unrealised"] is not None

    assert kraken_row["value_display"] == kraken_row["value"]
    assert kraken_row["unrealised_display"] == kraken_row["unrealised"]


def test_api_positions_contract_regression() -> None:
    """`/api/positions` carries every pre-existing field, byte-identical, plus the
    new ``display_currency`` / ``value_display`` / ``unrealised_display`` (leaf 03).
    """
    sup = asyncio.run(_seeded_supervisor())
    [row] = [
        r
        for r in TestClient(create_dashboard_app(sup)).get("/api/positions").json()
        if r["exchange"] == "kraken"
    ]
    assert set(row) == {
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
    assert row["strategy"] == "btc-kraken"
    assert row["exchange"] == "kraken"
    assert row["instrument"] == "BTC/USD"
    assert row["base"] == "BTC"
    assert row["net_qty"] == "2"
    assert row["avg_entry_price"] == "100"
    assert row["realised_pnl"] == "-1"  # the opening fill's fee reduces realised PnL
    assert row["fees_paid"] == "1"
    assert row["mark"] == "110"
    assert row["mark_asof_ts"] == 9_000
    assert row["mark_source"] == "bar_close"
    assert row["value"] == "220"
    assert row["unrealised"] == "20"
    assert row["fee_ccy"] == "USD"


def test_api_strategies_display_fields_and_contract_regression() -> None:
    """`/api/strategies` rows carry ``display_currency`` / ``total_value_display`` /
    ``unrealised_display`` (mirroring the position-row pair) with every
    pre-existing field untouched.
    """
    sup = asyncio.run(
        _seeded_supervisor(
            display_currency_overrides={"binance": "USDT"},
            conversion_rates={"USDT": "1"},
        )
    )
    rows = TestClient(create_dashboard_app(sup)).get("/api/strategies").json()
    by_name = {r["name"]: r for r in rows}
    kraken_row = by_name["btc-kraken"]
    binance_row = by_name["eth-binance"]

    assert set(kraken_row) == {
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
    # Pre-existing fields, unchanged.
    assert kraken_row["quote"] == "USD"
    assert kraken_row["allocation"] == "1000"
    assert kraken_row["contributed"] == "1000"
    assert kraken_row["realised_pnl"] == "-1"
    assert kraken_row["unrealised"] == "20"
    assert kraken_row["total_value"] == "1019"  # 1000 - 1 + 20
    # New fields: Kraken's own USD quote identity-matches the global default.
    assert kraken_row["display_currency"] == "USD"
    assert kraken_row["total_value_display"] == kraken_row["total_value"]
    assert kraken_row["unrealised_display"] == kraken_row["unrealised"]

    assert binance_row["quote"] == "USDT"
    assert binance_row["total_value"] == "1039"  # 1000 - 1 + 40
    # New fields: Binance's override + identity rate -> displayed in USDT.
    assert binance_row["display_currency"] == "USDT"
    assert binance_row["total_value_display"] == binance_row["total_value"]
    assert binance_row["unrealised_display"] == binance_row["unrealised"]


def test_api_kpi_display_fields_and_contract_regression() -> None:
    """`/api/kpi` (``level="strategy"``) rows carry ``display_currency`` /
    ``realised_pnl_display`` / ``fees_paid_display`` with every pre-existing
    field untouched.
    """
    sup = asyncio.run(
        _seeded_supervisor(
            display_currency_overrides={"binance": "USDT"},
            conversion_rates={"USDT": "1"},
        )
    )
    rows = TestClient(create_dashboard_app(sup)).get("/api/kpi?level=strategy").json()
    by_strategy = {r["strategy"]: r for r in rows}
    kraken_row = by_strategy["btc-kraken"]
    binance_row = by_strategy["eth-binance"]

    assert set(kraken_row) == {
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
    assert kraken_row["level"] == "strategy"
    assert kraken_row["quote"] == "USD"
    assert kraken_row["realised_pnl"] == "-1"
    assert kraken_row["fees_paid"] == "1"
    assert kraken_row["display_currency"] == "USD"
    assert kraken_row["realised_pnl_display"] == kraken_row["realised_pnl"]
    assert kraken_row["fees_paid_display"] == kraken_row["fees_paid"]

    assert binance_row["quote"] == "USDT"
    assert binance_row["display_currency"] == "USDT"
    assert binance_row["realised_pnl_display"] == binance_row["realised_pnl"]
    assert binance_row["fees_paid_display"] == binance_row["fees_paid"]


def test_api_kpi_total_level_mixed_quote_and_no_single_exchange_go_null() -> None:
    """``level="total"`` folds both venues: ``quote`` is ``None`` (mixed currencies)
    and ``exchange`` is ``None`` (no single venue) — the ``_display`` fields
    never guess a conversion, but ``display_currency`` still resolves to the
    global default (an unlisted/absent exchange falls back, per
    :func:`~trading_bot.application.display_ccy.resolve_currency`).
    """
    sup = asyncio.run(
        _seeded_supervisor(
            display_currency_overrides={"binance": "USDT"},
            conversion_rates={"USDT": "1"},
        )
    )
    [row] = TestClient(create_dashboard_app(sup)).get("/api/kpi?level=total").json()
    assert row["quote"] is None
    assert row["exchange"] is None
    assert row["display_currency"] == "USD"
    assert row["realised_pnl_display"] is None
    assert row["fees_paid_display"] is None
    # Native fields stay untouched even though the display pair is null.
    assert row["realised_pnl"] == "-2"
    assert row["fees_paid"] == "2"
