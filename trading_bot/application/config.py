"""Application configuration — the engine's declared shape (pydantic v2).

:class:`AppConfig` is the top-level, YAML-loadable declaration of *what the
engine should run*: which brokers to wire, which strategies to drive, and the
risk limits to enforce. It mirrors dccd's ``application/config.py`` (a pydantic
``AppConfig`` with sub-models + validators, loaded from YAML) but speaks this
package's vocabulary.

Design choices (carried into the ADR):

* **Paper by default.** ``mode`` defaults to ``"paper"`` — a fresh config never
  trades real money by accident. Switching to ``"live"`` is always an explicit,
  deliberate edit. ``mode`` is a ``Literal["paper", "live"]`` so any other value
  is rejected at validation time.
* **Money stays exact.** Risk limits (:class:`RiskConfig`) and the
  ``starting_capital`` are :class:`~decimal.Decimal`, parsed from ``str``/``int``
  without ever touching ``float`` — pydantic builds a ``Decimal`` directly from
  the YAML scalar, so a value like ``0.1`` keeps its exact decimal meaning.
* **KPIs anchor to a real account value.** ``starting_capital`` seeds the
  performance service's equity curve (``equity = starting_capital + cumulative
  realised PnL``). Defaulting it to a strictly-positive ``100000`` keeps the
  curve from crossing zero, so the KPI ratios (Sharpe/Sortino/Calmar) computed
  over a real run are statistically meaningful rather than degenerate.
* **Skeletons that grow.** :class:`StrategyConfig` and :class:`RiskConfig` are
  intentionally minimal here (just enough to parse a realistic file); they gain
  fields in later leaves (E5 / E8). :class:`BrokerConfig` carries only the
  ``name`` (logical id) and ``exchange`` (venue key) needed to resolve an
  adapter.

This module is the only place the application layer reads YAML; everything
downstream consumes a validated :class:`AppConfig`.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from trading_bot.domain.money import money

__all__ = [
    "BrokerConfig",
    "DataSourceConfig",
    "SignalRefConfig",
    "StorageConfig",
    "StrategyConfig",
    "RiskConfig",
    "AppConfig",
]


class BrokerConfig(BaseModel):
    """One broker the engine should wire up.

    Parameters
    ----------
    name : str
        Logical, config-unique id for this broker instance (how strategies and
        the router refer to it). Must be non-empty.
    exchange : str
        The venue key the broker adapter is resolved by (e.g. ``"kraken"``).
        Must be non-empty.

    """

    name: str
    exchange: str

    @field_validator("name", "exchange")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Reject blank broker ``name`` / ``exchange`` (whitespace-only too)."""
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


class DataSourceConfig(BaseModel):
    """Where a strategy's bars come from — a dccd OHLC dataset.

    Declares the dccd read a :class:`~trading_bot.application.data_feed.DccdFeed`
    performs (leaf 02 consumes ``exchange`` / ``span`` / ``start``): the venue,
    the bar width, an optional history start, and the data kind.

    Parameters
    ----------
    exchange : str
        Exchange/venue key the bars are stored under (e.g. ``"kraken"``). Passed
        straight to ``dccd.Client.read``. Must be non-empty.
    span : int
        Bar width in **seconds** (dccd's ``span``; also the live close cadence).
        Must be ``> 0``.
    start : str or int or None, optional
        Optional history start — a timestamp/ISO marker the feed maps to a
        ``start_ns`` bound. ``None`` (default) reads from the dataset's start.
    data_type : str, optional
        The dccd data kind to read. Defaults to ``"ohlc"`` (the only kind the
        bars feed normalises).

    """

    exchange: str
    span: int
    start: str | int | None = None
    data_type: str = "ohlc"

    @field_validator("exchange")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Reject a blank ``exchange`` (whitespace-only too)."""
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("span")
    @classmethod
    def _positive_span(cls, v: int) -> int:
        """Reject a non-positive bar ``span`` (seconds)."""
        if v <= 0:
            raise ValueError(f"span must be positive seconds, got {v}")
        return v


class SignalRefConfig(BaseModel):
    """The signal a strategy evaluates — a reference plus its parameters.

    A declarative pointer to a :data:`~trading_bot.application.strategy.SignalFn`
    (leaf 03 resolves it): either a safe ``"module:function"`` dotted import
    reference *or* a builtin name like ``"ma_crossover"``, plus the keyword
    ``params`` the signal is built with (e.g. ``{"fast": 10, "slow": 30}``).

    Parameters
    ----------
    ref : str
        Either a ``"module:function"`` dotted reference or a builtin signal name
        (e.g. ``"ma_crossover"``). Resolution happens in leaf 03; this leaf only
        declares the shape. Must be non-empty.
    params : dict, optional
        Keyword arguments the signal is built with (e.g.
        ``{"fast": 10, "slow": 30}``). Empty by default.

    """

    ref: str
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Reject a blank signal ``ref`` (whitespace-only too)."""
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


class StrategyConfig(BaseModel):
    """One strategy the engine should drive — data, signal and sizing.

    The full declarative shape of a strategy: the pair it trades, where its bars
    come from (:class:`DataSourceConfig`), the signal it evaluates
    (:class:`SignalRefConfig`) and its sizing (``reference_qty`` / ``lookback``).
    Every field beyond ``name`` / ``symbol`` is **optional with a default**, so a
    legacy ``{name, symbol}``-only config still validates unchanged.

    Parameters
    ----------
    name : str
        Logical, config-unique id for the strategy instance. Must be non-empty.
    symbol : str
        The canonical pair the strategy trades, ``BASE/QUOTE`` (e.g.
        ``"BTC/USD"``). Kept as a plain string here; later leaves resolve it to
        a :class:`~trading_bot.domain.instrument.Symbol`. Must be non-empty.
    data : DataSourceConfig or None, optional
        The strategy's bar source (dccd dataset). ``None`` (default) when the
        runner supplies a feed by other means.
    signal : SignalRefConfig or None, optional
        The signal the strategy evaluates. ``None`` (default) when the signal is
        injected programmatically rather than declared.
    reference_qty : Decimal, optional
        The max position size (base units) a fractional-exposure signal is a
        fraction of (see :class:`~trading_bot.application.strategy.Strategy`).
        Parsed exactly from a YAML scalar (``str``/``int``) without touching
        ``float``. Must be positive when set. ``None`` (default) when the signal
        emits explicit-quantity signals.
    lookback : int, optional
        Warmup: minimum number of bars before the signal is meaningful. Must be
        ``>= 0``. Default ``0`` (no warmup).

    """

    name: str
    symbol: str
    data: DataSourceConfig | None = None
    signal: SignalRefConfig | None = None
    reference_qty: Decimal | None = None
    lookback: int = 0

    @field_validator("name", "symbol")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Reject blank strategy ``name`` / ``symbol``."""
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("reference_qty")
    @classmethod
    def _positive_qty(cls, v: Decimal | None) -> Decimal | None:
        """Reject a non-positive ``reference_qty`` (``None`` is allowed)."""
        if v is not None and v <= 0:
            raise ValueError(f"reference_qty must be positive, got {v}")
        return v

    @field_validator("lookback")
    @classmethod
    def _non_negative_lookback(cls, v: int) -> int:
        """Reject a negative ``lookback``."""
        if v < 0:
            raise ValueError(f"lookback must be non-negative, got {v}")
        return v


class StorageConfig(BaseModel):
    """Where the engine persists state and finds market data on disk.

    Both paths are optional (``None`` = use the layer's default): ``db_path`` is
    the append-only SQLite store of order/fill history + engine state (the
    reconciliation source), and ``data_path`` is the dccd data directory the
    bars feed reads from.

    Parameters
    ----------
    db_path : str or None, optional
        Path to the engine's SQLite database. ``None`` (default) defers to the
        storage layer's default location.
    data_path : str or None, optional
        Path to the dccd on-disk data directory. ``None`` (default) defers to
        dccd's own default.

    """

    db_path: str | None = None
    data_path: str | None = None


class RiskConfig(BaseModel):
    """Engine-wide risk limits (skeleton — grows in E8).

    Every limit is optional (``None`` = unconstrained) and, when set, a
    non-negative :class:`~decimal.Decimal`. Values parse exactly from a YAML
    scalar (``str``/``int``) without going through ``float``.

    Parameters
    ----------
    max_position : Decimal, optional
        Largest absolute net position (base units) any instrument may hold.
    max_order : Decimal, optional
        Largest size (base units) a single order may request.
    max_daily_loss : Decimal, optional
        Loss (quote units) at which trading halts for the day.

    """

    max_position: Decimal | None = None
    max_order: Decimal | None = None
    max_daily_loss: Decimal | None = None

    @field_validator("max_position", "max_order", "max_daily_loss")
    @classmethod
    def _non_negative(cls, v: Decimal | None) -> Decimal | None:
        """Reject a negative risk limit (``None`` and ``0`` are allowed)."""
        if v is not None and v < 0:
            raise ValueError(f"risk limit must be non-negative, got {v}")
        return v


class AppConfig(BaseModel):
    """Top-level engine configuration — brokers, strategies and risk.

    The declared shape of an engine run. Build one from a dict with
    :meth:`pydantic.BaseModel.model_validate` or from a YAML file with
    :meth:`from_yaml`; both validate every field and sub-model.

    Parameters
    ----------
    mode : {"paper", "live"}, optional
        Execution mode. Defaults to ``"paper"`` so a fresh config never trades
        real money by accident; ``"live"`` must be set deliberately. Any other
        value is rejected.
    starting_capital : Decimal, optional
        Initial account capital (quote units) that anchors the equity curve the
        KPI ratios are computed over (``equity = starting_capital + cumulative
        realised PnL``). Parsed exactly from a YAML scalar (``str``/``int``)
        without touching ``float``. Must be **strictly positive** so the curve
        stays above zero and the ratio math is well-defined. Defaults to
        ``Decimal("100000")``. Wired into the engine's
        :class:`~trading_bot.application.performance_service.PerformanceService`
        (``v0``) by :func:`~trading_bot.application.service_factory.build_engine`.
    brokers : list of BrokerConfig, optional
        The brokers to wire up. Empty by default.
    strategies : list of StrategyConfig, optional
        The strategies to drive. Empty by default.
    risk : RiskConfig, optional
        Engine-wide risk limits. Defaults to all-unconstrained.
    storage : StorageConfig, optional
        Where state is persisted (SQLite) and where the bars feed reads data
        from (dccd dir). Defaults to all-unset (each layer's own default).

    Examples
    --------
    >>> cfg = AppConfig.model_validate({
    ...     "mode": "paper",
    ...     "brokers": [{"name": "kraken-main", "exchange": "kraken"}],
    ...     "strategies": [{"name": "ma-cross", "symbol": "BTC/USD"}],
    ...     "risk": {"max_order": "0.5"},
    ... })
    >>> cfg.mode
    'paper'
    >>> cfg.risk.max_order
    Decimal('0.5')

    """

    mode: Literal["paper", "live"] = "paper"
    starting_capital: Decimal = Field(default_factory=lambda: money("100000"))
    brokers: list[BrokerConfig] = Field(default_factory=list)
    strategies: list[StrategyConfig] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @field_validator("starting_capital")
    @classmethod
    def _positive_capital(cls, v: Decimal) -> Decimal:
        """Reject a non-positive ``starting_capital`` (zero too).

        The equity curve the KPI ratios are computed over is
        ``starting_capital + cumulative realised PnL``; a non-positive anchor
        would let the curve sit at / cross zero, where the ratio estimators are
        undefined.
        """
        if v <= 0:
            raise ValueError(f"starting_capital must be positive, got {v}")
        return v

    @classmethod
    def from_yaml(cls, path: str | pathlib.Path) -> AppConfig:
        """Load and validate a YAML file into an :class:`AppConfig`.

        Parameters
        ----------
        path : str or pathlib.Path
            Path to the YAML config file. An empty file is treated as an empty
            mapping (all defaults).

        Returns
        -------
        AppConfig
            The validated configuration.

        Raises
        ------
        pydantic.ValidationError
            If the parsed document violates any field or sub-model invariant.

        """
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)
