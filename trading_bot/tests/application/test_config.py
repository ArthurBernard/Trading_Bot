"""Tests for :class:`AppConfig` and its sub-models.

These prove the engine's declared shape parses and validates as intended:
``mode`` defaults to ``"paper"`` (the never-trade-by-accident invariant), a
realistic dict round-trips through ``model_validate``, unknown modes and
negative risk limits are rejected, blank broker names are rejected, risk limits
land as exact :class:`~decimal.Decimal`, and :meth:`AppConfig.from_yaml`
round-trips a small YAML file (via ``tmp_path``).
"""

from __future__ import annotations

import pathlib
import textwrap
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.application import (
    AppConfig,
    BrokerConfig,
    DataSourceConfig,
    RiskConfig,
    SignalRefConfig,
    StorageConfig,
    StrategyConfig,
)

#: The shipped runnable paper config (resolved from the repo root).
EXAMPLE_CONFIG = (
    pathlib.Path(__file__).resolve().parents[3] / "examples" / "config.example.yaml"
)

# A realistic config dict reused across the round-trip assertions.
REALISTIC: dict = {
    "mode": "live",
    "brokers": [
        {"name": "kraken-main", "exchange": "kraken"},
        {"name": "kraken-backup", "exchange": "kraken"},
    ],
    "strategies": [
        {"name": "ma-cross", "symbol": "BTC/USD"},
        {"name": "mean-rev", "symbol": "ETH/USD"},
    ],
    "risk": {
        "max_position": "1.5",
        "max_order": "0.25",
        "max_daily_loss": "500",
    },
}


def test_mode_defaults_to_paper() -> None:
    """An empty config is paper — never trade real money by accident."""
    cfg = AppConfig()
    assert cfg.mode == "paper"
    assert cfg.brokers == []
    assert cfg.strategies == []
    assert isinstance(cfg.risk, RiskConfig)
    assert cfg.risk.max_position is None


def test_live_enabled_defaults_to_false() -> None:
    """``live_enabled`` is off by default — live is an explicit opt-in."""
    cfg = AppConfig()
    assert cfg.live_enabled is False


def test_live_enabled_round_trips_from_yaml(tmp_path: pathlib.Path) -> None:
    """``live_enabled: true`` survives a YAML round-trip as a bool."""
    path = tmp_path / "live.yml"
    path.write_text("mode: live\nlive_enabled: true\n")
    cfg = AppConfig.from_yaml(path)
    assert cfg.mode == "live"
    assert cfg.live_enabled is True


def test_live_enabled_omitted_in_yaml_defaults_false(
    tmp_path: pathlib.Path,
) -> None:
    """A YAML config that omits ``live_enabled`` parses it as ``False``."""
    path = tmp_path / "paper.yml"
    path.write_text("mode: live\n")
    cfg = AppConfig.from_yaml(path)
    assert cfg.live_enabled is False


def test_starting_capital_defaults_to_100000() -> None:
    """An unset ``starting_capital`` is the strictly-positive default 100000."""
    cfg = AppConfig()
    assert cfg.starting_capital == Decimal("100000")
    assert isinstance(cfg.starting_capital, Decimal)


def test_starting_capital_parses_exact_decimal_without_float_error() -> None:
    """A YAML/JSON number for ``starting_capital`` keeps its exact meaning."""
    cfg = AppConfig.model_validate({"starting_capital": 250000.5})
    assert cfg.starting_capital == Decimal("250000.5")
    assert isinstance(cfg.starting_capital, Decimal)


def test_starting_capital_string_parses_exact() -> None:
    """A string ``starting_capital`` parses to an exact Decimal."""
    cfg = AppConfig.model_validate({"starting_capital": "1000000"})
    assert cfg.starting_capital == Decimal("1000000")


def test_zero_starting_capital_raises() -> None:
    """A zero ``starting_capital`` is rejected (curve would sit at zero)."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"starting_capital": "0"})


def test_negative_starting_capital_raises() -> None:
    """A negative ``starting_capital`` is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"starting_capital": "-1"})


def test_starting_capital_round_trips_from_yaml(tmp_path) -> None:
    """``starting_capital`` survives a YAML round-trip as an exact Decimal."""
    path = tmp_path / "cap.yml"
    path.write_text("starting_capital: \"500000\"\n")
    cfg = AppConfig.from_yaml(path)
    assert cfg.starting_capital == Decimal("500000")


def test_model_validate_realistic_dict() -> None:
    """A realistic dict parses into fully-typed sub-models."""
    cfg = AppConfig.model_validate(REALISTIC)

    assert cfg.mode == "live"

    assert [b.name for b in cfg.brokers] == ["kraken-main", "kraken-backup"]
    assert all(isinstance(b, BrokerConfig) for b in cfg.brokers)
    assert cfg.brokers[0].exchange == "kraken"

    assert [s.name for s in cfg.strategies] == ["ma-cross", "mean-rev"]
    assert all(isinstance(s, StrategyConfig) for s in cfg.strategies)
    assert cfg.strategies[0].symbol == "BTC/USD"


def test_risk_limits_are_exact_decimals() -> None:
    """Risk limits parse to exact ``Decimal`` (no float error)."""
    cfg = AppConfig.model_validate(REALISTIC)
    assert cfg.risk.max_position == Decimal("1.5")
    assert cfg.risk.max_order == Decimal("0.25")
    assert cfg.risk.max_daily_loss == Decimal("500")
    assert all(
        isinstance(v, Decimal)
        for v in (
            cfg.risk.max_position,
            cfg.risk.max_order,
            cfg.risk.max_daily_loss,
        )
    )


def test_decimal_parses_from_number_without_float_error() -> None:
    """A YAML/JSON number for a risk limit keeps its exact decimal meaning."""
    cfg = AppConfig.model_validate({"risk": {"max_order": 0.1}})
    assert cfg.risk.max_order == Decimal("0.1")


def test_unknown_mode_raises() -> None:
    """A mode outside {paper, live} is rejected by the Literal."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "shadow"})


def test_negative_risk_limit_raises() -> None:
    """A negative risk limit is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"max_position": "-1"}})


def test_zero_risk_limit_allowed() -> None:
    """Zero is a valid (fully-constraining) risk limit."""
    cfg = AppConfig.model_validate({"risk": {"max_daily_loss": "0"}})
    assert cfg.risk.max_daily_loss == Decimal("0")


def test_blank_broker_name_raises() -> None:
    """A blank / whitespace-only broker name is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"brokers": [{"name": "  ", "exchange": "kraken"}]}
        )


def test_blank_strategy_symbol_raises() -> None:
    """A blank strategy symbol is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"strategies": [{"name": "ma", "symbol": ""}]}
        )


def test_from_yaml_round_trips(tmp_path) -> None:
    """``from_yaml`` parses a small YAML file into the expected shape."""
    yaml_text = textwrap.dedent(
        """\
        mode: paper
        brokers:
          - name: kraken-main
            exchange: kraken
        strategies:
          - name: ma-cross
            symbol: BTC/USD
        risk:
          max_position: "2.0"
          max_order: "0.5"
          max_daily_loss: "1000"
        """
    )
    path = tmp_path / "config.yml"
    path.write_text(yaml_text)

    cfg = AppConfig.from_yaml(path)

    assert cfg.mode == "paper"
    assert cfg.brokers[0].name == "kraken-main"
    assert cfg.brokers[0].exchange == "kraken"
    assert cfg.strategies[0].symbol == "BTC/USD"
    assert cfg.risk.max_position == Decimal("2.0")
    assert cfg.risk.max_order == Decimal("0.5")
    assert cfg.risk.max_daily_loss == Decimal("1000")


def test_from_yaml_empty_file_is_all_defaults(tmp_path) -> None:
    """An empty YAML file yields an all-defaults (paper) config."""
    path = tmp_path / "empty.yml"
    path.write_text("")
    cfg = AppConfig.from_yaml(path)
    assert cfg.mode == "paper"
    assert cfg.brokers == []


# --- full declarative config: data + signal + sizing + storage --------------

# A fully-declared strategy (data + signal + sizing) + storage, as a YAML doc.
FULL_YAML = textwrap.dedent(
    """\
    mode: paper
    storage:
      db_path: /tmp/tb.sqlite
      data_path: /tmp/dccd
    brokers:
      - name: paper-main
        exchange: kraken
    strategies:
      - name: btc-ma-cross
        symbol: BTC/USD
        data:
          exchange: kraken
          span: 3600
          start: "2024-01-01"
          data_type: ohlc
        signal:
          ref: ma_crossover
          params:
            fast: 10
            slow: 30
        reference_qty: "0.5"
        lookback: 30
    risk:
      max_order: "0.25"
    """
)


def test_full_yaml_parses_nested_shape(tmp_path) -> None:
    """A full YAML round-trips into the exact data+signal+sizing+storage shape."""
    path = tmp_path / "full.yml"
    path.write_text(FULL_YAML)
    cfg = AppConfig.from_yaml(path)

    # storage section
    assert isinstance(cfg.storage, StorageConfig)
    assert cfg.storage.db_path == "/tmp/tb.sqlite"
    assert cfg.storage.data_path == "/tmp/dccd"

    strat = cfg.strategies[0]
    assert strat.name == "btc-ma-cross"
    assert strat.symbol == "BTC/USD"

    # data source — exactly what DccdFeed consumes (leaf 02)
    assert isinstance(strat.data, DataSourceConfig)
    assert strat.data.exchange == "kraken"
    assert strat.data.span == 3600
    assert strat.data.start == "2024-01-01"
    assert strat.data.data_type == "ohlc"

    # signal ref + params — leaf 03 resolves these; ints survive intact
    assert isinstance(strat.signal, SignalRefConfig)
    assert strat.signal.ref == "ma_crossover"
    assert strat.signal.params == {"fast": 10, "slow": 30}
    assert all(isinstance(v, int) for v in strat.signal.params.values())

    # sizing — reference_qty is an exact Decimal, never float
    assert strat.reference_qty == Decimal("0.5")
    assert isinstance(strat.reference_qty, Decimal)
    assert strat.lookback == 30


def test_minimal_legacy_strategy_still_validates() -> None:
    """A {name, symbol}-only strategy validates — all new fields defaulted."""
    cfg = AppConfig.model_validate(
        {"strategies": [{"name": "legacy", "symbol": "BTC/USD"}]}
    )
    strat = cfg.strategies[0]
    assert strat.data is None
    assert strat.signal is None
    assert strat.reference_qty is None
    assert strat.lookback == 0
    # storage defaults to an all-unset StorageConfig
    assert isinstance(cfg.storage, StorageConfig)
    assert cfg.storage.db_path is None
    assert cfg.storage.data_path is None


def test_reference_qty_parses_decimal_without_float_error() -> None:
    """A numeric reference_qty keeps its exact decimal meaning."""
    cfg = AppConfig.model_validate(
        {"strategies": [{"name": "s", "symbol": "BTC/USD", "reference_qty": 0.1}]}
    )
    assert cfg.strategies[0].reference_qty == Decimal("0.1")


def test_empty_data_exchange_raises() -> None:
    """A blank data-source exchange is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "strategies": [
                    {
                        "name": "s",
                        "symbol": "BTC/USD",
                        "data": {"exchange": "  ", "span": 60},
                    }
                ]
            }
        )


def test_non_positive_span_raises() -> None:
    """A non-positive bar span is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "strategies": [
                    {
                        "name": "s",
                        "symbol": "BTC/USD",
                        "data": {"exchange": "kraken", "span": 0},
                    }
                ]
            }
        )


def test_empty_signal_ref_raises() -> None:
    """A blank signal ref is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "strategies": [
                    {"name": "s", "symbol": "BTC/USD", "signal": {"ref": ""}}
                ]
            }
        )


def test_negative_lookback_raises() -> None:
    """A negative lookback is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"strategies": [{"name": "s", "symbol": "BTC/USD", "lookback": -1}]}
        )


def test_non_positive_reference_qty_raises() -> None:
    """A non-positive reference_qty is rejected (zero too)."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "strategies": [
                    {"name": "s", "symbol": "BTC/USD", "reference_qty": "0"}
                ]
            }
        )


def test_signal_params_default_empty() -> None:
    """A signal with only a ref gets an empty params dict."""
    cfg = AppConfig.model_validate(
        {
            "strategies": [
                {"name": "s", "symbol": "BTC/USD", "signal": {"ref": "m:f"}}
            ]
        }
    )
    assert cfg.strategies[0].signal is not None
    assert cfg.strategies[0].signal.params == {}


def test_data_source_data_type_defaults_to_ohlc() -> None:
    """A data source without data_type defaults to ``ohlc``."""
    cfg = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "s",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 60},
                }
            ]
        }
    )
    assert cfg.strategies[0].data is not None
    assert cfg.strategies[0].data.data_type == "ohlc"
    assert cfg.strategies[0].data.start is None


def test_example_config_loads_and_validates() -> None:
    """The shipped ``examples/config.example.yaml`` loads + validates.

    Verification on real data: the parsed config exposes the exact shape that
    leaves 02/03 consume — a dccd data source (exchange/span), a signal ref +
    params, and Decimal sizing.
    """
    assert EXAMPLE_CONFIG.is_file()
    cfg = AppConfig.from_yaml(EXAMPLE_CONFIG)

    assert cfg.mode == "paper"
    assert cfg.storage.db_path is not None

    strat = cfg.strategies[0]
    assert strat.data is not None
    assert strat.data.exchange == "kraken"
    assert strat.data.span == 3600
    assert strat.signal is not None
    assert strat.signal.ref == "ma_crossover"
    assert strat.signal.params == {"fast": 10, "slow": 30}
    assert isinstance(strat.reference_qty, Decimal)
    assert strat.reference_qty == Decimal("0.5")
    assert strat.lookback == 30
