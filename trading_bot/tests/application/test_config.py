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
from trading_bot.application.config import LoggingConfig, PortfolioStrategyConfig

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


def test_paper_strict_defaults_to_true() -> None:
    """``paper_strict`` is ON by default — paper predicts the venue."""
    cfg = AppConfig()
    assert cfg.paper_strict is True


def test_paper_strict_round_trips_from_yaml(tmp_path: pathlib.Path) -> None:
    """``paper_strict: false`` survives a YAML round-trip as a bool."""
    path = tmp_path / "permissive.yml"
    path.write_text("paper_strict: false\n")
    cfg = AppConfig.from_yaml(path)
    assert cfg.paper_strict is False


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
    path.write_text('starting_capital: "500000"\n')
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
        AppConfig.model_validate({"brokers": [{"name": "  ", "exchange": "kraken"}]})


def test_blank_strategy_symbol_raises() -> None:
    """A blank strategy symbol is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"strategies": [{"name": "ma", "symbol": ""}]})


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
    assert strat.allocation is None
    assert strat.capital_policy == "fixed"
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
            {"strategies": [{"name": "s", "symbol": "BTC/USD", "signal": {"ref": ""}}]}
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
            {"strategies": [{"name": "s", "symbol": "BTC/USD", "reference_qty": "0"}]}
        )


def test_allocation_and_capital_policy_default_to_none_and_fixed() -> None:
    """``allocation`` defaults to ``None``, ``capital_policy`` to ``"fixed"``."""
    cfg = AppConfig.model_validate({"strategies": [{"name": "s", "symbol": "BTC/USD"}]})
    strat = cfg.strategies[0]
    assert strat.allocation is None
    assert strat.capital_policy == "fixed"


def test_allocation_parses_exact_decimal_without_float_error() -> None:
    """A numeric ``allocation`` keeps its exact decimal meaning."""
    cfg = AppConfig.model_validate(
        {"strategies": [{"name": "s", "symbol": "BTC/USD", "allocation": 0.1}]}
    )
    assert cfg.strategies[0].allocation == Decimal("0.1")
    assert isinstance(cfg.strategies[0].allocation, Decimal)


def test_non_positive_allocation_raises() -> None:
    """A non-positive ``allocation`` is rejected (zero too)."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"strategies": [{"name": "s", "symbol": "BTC/USD", "allocation": "0"}]}
        )
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"strategies": [{"name": "s", "symbol": "BTC/USD", "allocation": "-1"}]}
        )


def test_unknown_capital_policy_raises() -> None:
    """A ``capital_policy`` outside {fixed, compound} is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "strategies": [
                    {"name": "s", "symbol": "BTC/USD", "capital_policy": "banana"}
                ]
            }
        )


def test_compound_capital_policy_accepted() -> None:
    """``capital_policy: "compound"`` validates."""
    cfg = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "s",
                    "symbol": "BTC/USD",
                    "allocation": "100",
                    "capital_policy": "compound",
                }
            ]
        }
    )
    assert cfg.strategies[0].capital_policy == "compound"


def test_allocation_round_trips_through_yaml(tmp_path) -> None:  # noqa: ANN001
    """A strategy's ``allocation`` survives a ``to_yaml``/``from_yaml`` round-trip
    as an exact ``Decimal``, alongside ``capital_policy``."""
    cfg = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "s",
                    "symbol": "BTC/USD",
                    "allocation": "1234.56",
                    "capital_policy": "compound",
                }
            ]
        }
    )
    path = tmp_path / "cfg.yaml"
    cfg.to_yaml(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded.strategies[0].allocation == Decimal("1234.56")
    assert isinstance(reloaded.strategies[0].allocation, Decimal)
    assert reloaded.strategies[0].capital_policy == "compound"


def test_signal_params_default_empty() -> None:
    """A signal with only a ref gets an empty params dict."""
    cfg = AppConfig.model_validate(
        {"strategies": [{"name": "s", "symbol": "BTC/USD", "signal": {"ref": "m:f"}}]}
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


# --- to_yaml round-trip + add/remove-entry helpers (UI-driven persistence) ---


def _full_config() -> AppConfig:
    """A rich paper config exercising Decimals, a strategy and a portfolio."""
    return AppConfig.model_validate(
        {
            "mode": "paper",
            "starting_capital": "100000",
            "brokers": [{"name": "paper-binance", "exchange": "paper"}],
            "strategies": [
                {
                    "name": "btc-ma",
                    "symbol": "BTC/USD",
                    "data": {"exchange": "kraken", "span": 3600},
                    "signal": {
                        "ref": "ma_crossover",
                        "params": {"fast": 10, "slow": 30},
                    },
                    "reference_qty": "0.5",
                    "lookback": 30,
                }
            ],
            "portfolios": [
                {
                    "name": "demo1",
                    "venue": "binance",
                    "universe": ["BTC/USDT", "ETH/USDT"],
                    "signal": {"ref": "pkg.mod:sig"},
                    "capital": "100000",
                    "data": {"exchange": "binance", "span": 86400},
                }
            ],
            "risk": {"max_daily_loss": "5000"},
        }
    )


def test_to_yaml_round_trips(tmp_path) -> None:  # noqa: ANN001
    """`from_yaml(to_yaml(cfg))` reconstructs the same config (money exact)."""
    cfg = _full_config()
    path = tmp_path / "manifest.yaml"
    cfg.to_yaml(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded == cfg  # semantic equality: Decimals, portfolios and all
    # And the money survived as an exact Decimal, never a float.
    assert reloaded.portfolios[0].capital == Decimal("100000")
    assert isinstance(reloaded.portfolios[0].capital, Decimal)


def test_to_yaml_creates_parent_directory(tmp_path) -> None:  # noqa: ANN001
    """`to_yaml` creates a missing parent dir (the dashboard's configs/ default)."""
    path = tmp_path / "configs" / "dashboard.yaml"
    assert not path.parent.exists()
    AppConfig().to_yaml(path)
    assert path.is_file()
    assert AppConfig.from_yaml(path).mode == "paper"


def test_add_strategy_appends_and_validates() -> None:
    """`add_strategy` returns a new config with the entry appended (original intact)."""
    cfg = AppConfig()
    new = cfg.add_strategy(StrategyConfig(name="s1", symbol="BTC/USD"))
    assert [s.name for s in new.strategies] == ["s1"]
    assert cfg.strategies == []  # pure — the original is unchanged


def test_add_portfolio_appends_and_validates() -> None:
    """`add_portfolio` returns a new config with the portfolio appended."""
    cfg = AppConfig()
    entry = PortfolioStrategyConfig(
        name="pf1",
        venue="binance",
        universe=["BTC/USDT", "ETH/USDT"],
        signal=SignalRefConfig(ref="pkg.mod:sig"),
        capital=Decimal("100000"),
        data=DataSourceConfig(exchange="binance", span=86400),
    )
    new = cfg.add_portfolio(entry)
    assert [p.name for p in new.portfolios] == ["pf1"]


def test_add_strategy_with_allocation_and_capital_policy_validates() -> None:
    """`add_strategy` accepts + validates an entry carrying the new fields."""
    cfg = AppConfig()
    entry = StrategyConfig(
        name="s1",
        symbol="BTC/USD",
        allocation=Decimal("500"),
        capital_policy="compound",
    )
    new = cfg.add_strategy(entry)
    assert new.strategies[0].allocation == Decimal("500")
    assert new.strategies[0].capital_policy == "compound"


def test_add_portfolio_with_allocation_and_capital_policy_validates() -> None:
    """`add_portfolio` accepts + validates an entry carrying the new fields."""
    cfg = AppConfig()
    entry = PortfolioStrategyConfig(
        name="pf1",
        venue="binance",
        universe=["BTC/USDT", "ETH/USDT"],
        signal=SignalRefConfig(ref="pkg.mod:sig"),
        capital=Decimal("100000"),
        data=DataSourceConfig(exchange="binance", span=86400),
        allocation=Decimal("100"),
        capital_policy="compound",
    )
    new = cfg.add_portfolio(entry)
    assert new.portfolios[0].allocation == Decimal("100")
    assert new.portfolios[0].capital_policy == "compound"


def test_remove_entry_still_validates_with_new_fields_present() -> None:
    """`remove_entry` still validates a config whose entries carry the new fields."""
    cfg = AppConfig().add_strategy(
        StrategyConfig(
            name="s1",
            symbol="BTC/USD",
            allocation=Decimal("500"),
            capital_policy="compound",
        )
    )
    without = cfg.remove_entry("s1")
    assert without.strategies == []


def test_add_strategy_rejects_a_duplicate_name() -> None:
    """A name already claimed by a strategy is rejected (shared name space)."""
    cfg = AppConfig().add_strategy(StrategyConfig(name="s1", symbol="BTC/USD"))
    with pytest.raises(ValueError, match="duplicate name"):
        cfg.add_strategy(StrategyConfig(name="s1", symbol="ETH/USD"))


def test_add_portfolio_rejects_a_name_claimed_by_a_strategy() -> None:
    """Strategies and portfolios share one name space — a cross-kind clash is rejected."""
    cfg = AppConfig().add_strategy(StrategyConfig(name="dup", symbol="BTC/USD"))
    with pytest.raises(ValueError, match="duplicate name"):
        cfg.add_portfolio(
            PortfolioStrategyConfig(
                name="dup",
                venue="binance",
                universe=["BTC/USDT"],
                signal=SignalRefConfig(ref="pkg.mod:sig"),
                capital=Decimal("100000"),
                data=DataSourceConfig(exchange="binance", span=86400),
            )
        )


def test_add_portfolio_rejects_an_invalid_universe() -> None:
    """A portfolio with a duplicate-coin universe is rejected at validation."""
    with pytest.raises(ValidationError):
        AppConfig().add_portfolio(
            PortfolioStrategyConfig(
                name="pf",
                venue="binance",
                universe=["BTC/USDT", "XBT/USDT"],  # same instrument twice
                signal=SignalRefConfig(ref="pkg.mod:sig"),
                capital=Decimal("100000"),
                data=DataSourceConfig(exchange="binance", span=86400),
            )
        )


def test_remove_entry_drops_a_strategy_or_portfolio() -> None:
    """`remove_entry` drops the matching unit (strategy or portfolio)."""
    cfg = _full_config()
    without_strat = cfg.remove_entry("btc-ma")
    assert [s.name for s in without_strat.strategies] == []
    assert [p.name for p in without_strat.portfolios] == ["demo1"]
    without_pf = cfg.remove_entry("demo1")
    assert [p.name for p in without_pf.portfolios] == []
    assert [s.name for s in without_pf.strategies] == ["btc-ma"]


def test_remove_entry_rejects_an_unknown_name() -> None:
    """Removing a name that is neither a strategy nor a portfolio raises."""
    with pytest.raises(ValueError, match="no strategy or portfolio named"):
        AppConfig().remove_entry("nope")


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


# --- per-strategy store isolation: the optional db_path override ------------- #


def test_strategy_db_path_defaults_to_none() -> None:
    """A strategy with no ``db_path`` defaults to ``None`` (the global store)."""
    cfg = AppConfig.model_validate({"strategies": [{"name": "s", "symbol": "BTC/USD"}]})
    assert cfg.strategies[0].db_path is None


def test_portfolio_db_path_defaults_to_none() -> None:
    """A portfolio with no ``db_path`` defaults to ``None`` (the global store)."""
    cfg = AppConfig.model_validate(
        {
            "portfolios": [
                {
                    "name": "pf",
                    "venue": "binance",
                    "universe": ["BTC/USDT"],
                    "capital": "100000",
                    "signal": {"ref": "m:f"},
                    "data": {"exchange": "binance", "span": 86400},
                }
            ]
        }
    )
    assert cfg.portfolios[0].db_path is None


def test_strategy_db_path_round_trips_through_yaml(tmp_path) -> None:  # noqa: ANN001
    """A strategy's per-strategy ``db_path`` survives a ``to_yaml``/``from_yaml`` round-trip."""
    cfg = AppConfig.model_validate(
        {
            "strategies": [
                {
                    "name": "s",
                    "symbol": "BTC/USD",
                    "db_path": "./var/dashboard/s.sqlite",
                }
            ]
        }
    )
    assert cfg.strategies[0].db_path == "./var/dashboard/s.sqlite"
    path = tmp_path / "cfg.yaml"
    cfg.to_yaml(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded.strategies[0].db_path == "./var/dashboard/s.sqlite"


def test_portfolio_db_path_round_trips_through_yaml(tmp_path) -> None:  # noqa: ANN001
    """A portfolio's per-strategy ``db_path`` survives a ``to_yaml``/``from_yaml`` round-trip."""
    cfg = AppConfig.model_validate(
        {
            "portfolios": [
                {
                    "name": "pf",
                    "venue": "binance",
                    "universe": ["BTC/USDT"],
                    "capital": "100000",
                    "signal": {"ref": "m:f"},
                    "data": {"exchange": "binance", "span": 86400},
                    "db_path": "./var/dashboard/pf.sqlite",
                }
            ]
        }
    )
    assert cfg.portfolios[0].db_path == "./var/dashboard/pf.sqlite"
    path = tmp_path / "cfg.yaml"
    cfg.to_yaml(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded.portfolios[0].db_path == "./var/dashboard/pf.sqlite"


# --- UIConfig (config-driven dashboard web settings) ---------------------- #


def test_ui_config_defaults_to_loopback_no_auth() -> None:
    """A bare config's `ui:` is loopback + no token (never exposed by accident)."""
    cfg = AppConfig.model_validate({"mode": "paper"})
    assert cfg.ui.host == "127.0.0.1"
    assert cfg.ui.port == 8000
    assert cfg.ui.token is None
    assert cfg.ui.read_only is False


def test_ui_config_round_trips_through_yaml(tmp_path) -> None:  # noqa: ANN001
    """The `ui:` section (host/port/token) persists through `to_yaml`/`from_yaml`."""
    cfg = AppConfig.model_validate(
        {"mode": "paper", "ui": {"host": "0.0.0.0", "port": 9000, "token": "t"}}
    )
    path = tmp_path / "m.yaml"
    cfg.to_yaml(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded.ui.host == "0.0.0.0"
    assert reloaded.ui.port == 9000
    assert reloaded.ui.token == "t"


def test_ui_config_rejects_a_blank_host() -> None:
    """A blank `ui.host` is a validation error."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "paper", "ui": {"host": "  "}})


def test_ui_config_non_loopback_without_token_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-4: a non-loopback `ui.host` with no token fails validation (defence in depth).

    The control surface refuses to bind wide open with no auth **at the model
    level**, not only at the CLI — so a hand-edited / persisted manifest that binds
    ``0.0.0.0`` (or a Tailscale IP) with no token can never even construct.
    """
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "paper", "ui": {"host": "0.0.0.0"}})
    # A Tailscale-style IP is non-loopback too — same refusal.
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "paper", "ui": {"host": "100.64.0.5"}})


def test_ui_config_non_loopback_with_token_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-4: a non-loopback host **with** a token validates (auth present → allowed)."""
    monkeypatch.delenv("TRADING_BOT_UI_TOKEN", raising=False)
    cfg = AppConfig.model_validate(
        {"mode": "paper", "ui": {"host": "0.0.0.0", "token": "tok"}}
    )
    assert cfg.ui.host == "0.0.0.0"
    assert cfg.ui.token == "tok"


def test_ui_config_non_loopback_env_token_satisfies_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-4: TRADING_BOT_UI_TOKEN in the env satisfies the token requirement (CLI parity)."""
    monkeypatch.setenv("TRADING_BOT_UI_TOKEN", "from-env")
    cfg = AppConfig.model_validate({"mode": "paper", "ui": {"host": "0.0.0.0"}})
    assert cfg.ui.host == "0.0.0.0"  # env token satisfied the non-loopback guard


def test_ui_config_loopback_without_token_is_allowed() -> None:
    """A-4: the loopback default (no token) is fine — local-only, never exposed."""
    for host in ("127.0.0.1", "localhost", "::1"):
        cfg = AppConfig.model_validate({"mode": "paper", "ui": {"host": host}})
        assert cfg.ui.host == host
        assert cfg.ui.token is None


# --- LoggingConfig (daemon logging spine — level/dir/retention) ------------ #


def test_logging_config_defaults() -> None:
    """A bare ``LoggingConfig`` is INFO / ``logs`` / 14-day retention."""
    cfg = LoggingConfig()
    assert cfg.level == "INFO"
    assert cfg.dir == pathlib.Path("logs")
    assert cfg.retention_days == 14


def test_app_config_logging_defaults_when_section_absent() -> None:
    """A manifest with no ``logging:`` section keeps the defaults (additive)."""
    cfg = AppConfig.model_validate({"mode": "paper"})
    assert isinstance(cfg.logging, LoggingConfig)
    assert cfg.logging.level == "INFO"
    assert cfg.logging.dir == pathlib.Path("logs")
    assert cfg.logging.retention_days == 14


def test_logging_level_is_upper_cased_case_insensitively() -> None:
    """A lower-case ``level`` validates and is stored upper-case."""
    cfg = AppConfig.model_validate({"logging": {"level": "debug"}})
    assert cfg.logging.level == "DEBUG"


def test_logging_invalid_level_rejected() -> None:
    """A ``level`` that is not a standard log-level name is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"logging": {"level": "verbose"}})


def test_logging_retention_days_zero_rejected() -> None:
    """``retention_days`` must be ``>= 1`` — zero is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"logging": {"retention_days": 0}})


def test_logging_retention_days_negative_rejected() -> None:
    """A negative ``retention_days`` is rejected."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"logging": {"retention_days": -1}})


def test_logging_section_parses_from_yaml(tmp_path) -> None:  # noqa: ANN001
    """A ``logging:`` section parses (level upper-cased, dir/retention set)."""
    path = tmp_path / "with-logging.yml"
    path.write_text(
        textwrap.dedent(
            """\
            mode: paper
            logging:
              level: debug
              dir: /var/log/trading_bot
              retention_days: 30
            """
        )
    )
    cfg = AppConfig.from_yaml(path)
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.dir == pathlib.Path("/var/log/trading_bot")
    assert cfg.logging.retention_days == 30


def test_logging_absent_yaml_section_yields_defaults(tmp_path) -> None:  # noqa: ANN001
    """A YAML manifest without ``logging:`` yields the default LoggingConfig."""
    path = tmp_path / "no-logging.yml"
    path.write_text("mode: paper\n")
    cfg = AppConfig.from_yaml(path)
    assert cfg.logging.level == "INFO"
    assert cfg.logging.dir == pathlib.Path("logs")
    assert cfg.logging.retention_days == 14


def test_logging_section_round_trips_through_yaml(tmp_path) -> None:  # noqa: ANN001
    """The ``logging:`` section survives a ``to_yaml``/``from_yaml`` round-trip."""
    cfg = AppConfig.model_validate(
        {"mode": "paper", "logging": {"level": "warning", "retention_days": 7}}
    )
    path = tmp_path / "m.yaml"
    cfg.to_yaml(path)
    reloaded = AppConfig.from_yaml(path)
    assert reloaded.logging.level == "WARNING"
    assert reloaded.logging.retention_days == 7
