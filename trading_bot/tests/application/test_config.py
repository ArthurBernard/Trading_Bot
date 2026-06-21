"""Tests for :class:`AppConfig` and its sub-models.

These prove the engine's declared shape parses and validates as intended:
``mode`` defaults to ``"paper"`` (the never-trade-by-accident invariant), a
realistic dict round-trips through ``model_validate``, unknown modes and
negative risk limits are rejected, blank broker names are rejected, risk limits
land as exact :class:`~decimal.Decimal`, and :meth:`AppConfig.from_yaml`
round-trips a small YAML file (via ``tmp_path``).
"""

from __future__ import annotations

import textwrap
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_bot.application import (
    AppConfig,
    BrokerConfig,
    RiskConfig,
    StrategyConfig,
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
