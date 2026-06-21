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
* **Money stays exact.** Risk limits (:class:`RiskConfig`) are
  :class:`~decimal.Decimal`, parsed from ``str``/``int`` without ever touching
  ``float`` — pydantic builds a ``Decimal`` directly from the YAML scalar, so a
  value like ``0.1`` keeps its exact decimal meaning.
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
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

__all__ = [
    "BrokerConfig",
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


class StrategyConfig(BaseModel):
    """One strategy the engine should drive (skeleton — grows in E5/E8).

    Parameters
    ----------
    name : str
        Logical, config-unique id for the strategy instance. Must be non-empty.
    symbol : str
        The canonical pair the strategy trades, ``BASE/QUOTE`` (e.g.
        ``"BTC/USD"``). Kept as a plain string here; later leaves resolve it to
        a :class:`~trading_bot.domain.instrument.Symbol`. Must be non-empty.

    """

    name: str
    symbol: str

    @field_validator("name", "symbol")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Reject blank strategy ``name`` / ``symbol``."""
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


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
    brokers : list of BrokerConfig, optional
        The brokers to wire up. Empty by default.
    strategies : list of StrategyConfig, optional
        The strategies to drive. Empty by default.
    risk : RiskConfig, optional
        Engine-wide risk limits. Defaults to all-unconstrained.

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
    brokers: list[BrokerConfig] = Field(default_factory=list)
    strategies: list[StrategyConfig] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)

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
