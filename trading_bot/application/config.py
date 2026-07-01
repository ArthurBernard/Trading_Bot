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
* **Live is off by default — a second opt-in gate.** Beyond ``mode``, going live
  also requires ``live_enabled: true`` (default ``False``). The factory raises
  :class:`~trading_bot.domain.errors.LiveTradingNotEnabled` for ``mode == "live"``
  while ``live_enabled`` is ``False``, so flipping ``mode`` alone never reaches a
  real venue. Enabling live is a documented, deliberate choice — read the go-live
  runbook (``doc/dev/09-go-live.md``) and provide credentials.
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

from trading_bot.domain.instrument import Symbol, parse_kraken_pair
from trading_bot.domain.money import money

__all__ = [
    "BrokerConfig",
    "DataSourceConfig",
    "SignalRefConfig",
    "StorageConfig",
    "StrategyConfig",
    "PortfolioStrategyConfig",
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
    testnet : bool, optional
        Route this venue to its **testnet / sandbox** (paper money on the real
        venue) instead of production. Defaults to ``False``. When ``True`` (only
        venues that *have* a testnet, e.g. ``"binance"`` →
        ``testnet.binance.vision``), the adapter is **hard-pinned** to the testnet
        endpoint — it cannot reach mainnet — so it does **not** require the
        ``live_enabled`` opt-in (it still needs testnet credentials). Ignored in
        paper mode (the simulator). A venue with no testnet (``"kraken"``) raises.

    """

    name: str
    exchange: str
    testnet: bool = False

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
    db_path : str or None, optional
        Per-strategy SQLite store path; overrides the global ``storage.db_path``
        for this strategy so its book/PnL are isolated. ``None`` → use the global
        store. Absent → the current (shared-store) behaviour, fully
        backward-compatible.

    """

    name: str
    symbol: str
    data: DataSourceConfig | None = None
    signal: SignalRefConfig | None = None
    reference_qty: Decimal | None = None
    lookback: int = 0
    db_path: str | None = None

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


class PortfolioStrategyConfig(BaseModel):
    """One **multi-asset** strategy the engine should drive — a universe + sizing.

    The portfolio analogue of :class:`StrategyConfig`. Where a
    :class:`StrategyConfig` names *one* ``symbol`` and an optional fractional
    ``reference_qty``, a portfolio names a whole ``universe`` of pairs and an
    explicit ``capital`` base its signal's weight vector is a fraction of (a
    weight ``w`` for a coin targets a position worth ``w * capital``). The signal
    is a ``"module:function"`` :class:`SignalRefConfig` resolving to a
    ``target_weights``-shaped callable (a
    :data:`~trading_bot.application.portfolio.PortfolioSignalFn`); the data is the
    dccd dataset the universe's bars come from (daily by default — ``span =
    86400``). Rebalance cadence is the data's daily ``span``.

    Parameters
    ----------
    name : str
        Logical, config-unique id for the portfolio instance. Must be non-empty.
    universe : list of str
        The canonical pairs the portfolio allocates across, ``BASE/QUOTE`` (e.g.
        ``["BTC/USDT", "ETH/USDT"]``). Must be **non-empty**, every entry must
        parse to a :class:`~trading_bot.domain.instrument.Symbol`, and the
        universe must hold **no duplicate** instruments (two spellings of the same
        pair — e.g. ``"BTC/USDT"`` and ``"XBT/USDT"`` — are caught as the same
        instrument).
    signal : SignalRefConfig
        The weight-vector signal the portfolio evaluates — a ``"module:function"``
        reference to a :data:`~trading_bot.application.portfolio.PortfolioSignalFn`
        (there is no builtin portfolio-signal registry yet, so a bare name is
        rejected at resolution time). **Required**.
    capital : Decimal
        The capital base (quote units) the signal's weights are a fraction of.
        Parsed exactly from a YAML scalar (``str``/``int``) without touching
        ``float``. Must be **strictly positive**.
    data : DataSourceConfig
        The portfolio's bar source (dccd dataset). The same source feeds every
        coin in the universe (same ``exchange`` / ``span``); ``span`` is the daily
        ``86400`` for a daily rebalance. **Required**.
    gross_cap : Decimal or None, optional
        An optional declared gross-exposure cap (``Σ|w| ≤ gross_cap``), carried
        through to the
        :class:`~trading_bot.application.portfolio.PortfolioStrategy` for a
        signal/runner to honour. The engine does **not** enforce or re-normalise
        against it. Must be positive when set. ``None`` (default) means uncapped.
    venue : str, optional
        The venue key the universe's bars are stored under / rendered for (e.g.
        ``"binance"``). Defaults to ``"binance"``. Must be non-empty.
    store_key_format : {"venue", "hyphen", "slash"}, optional
        How each universe pair (a canonical
        :class:`~trading_bot.domain.instrument.Symbol`) is rendered to the **dccd
        store key** the bars are read under. The universe is written in canonical
        ``BASE/QUOTE`` form, but a real dccd store may key its pairs differently;
        this pins the convention rather than guessing:

        * ``"venue"`` (default) — the venue's native code via
          :meth:`~trading_bot.domain.instrument.Symbol.to_venue_symbol`
          (Binance ``BTCUSDT``; Kraken ``XBTUSD``). Backward-compatible.
        * ``"hyphen"`` — ``BASE-QUOTE`` (e.g. ``BTC-USDT``), the common
          hyphen-keyed dccd layout.
        * ``"slash"`` — ``BASE/QUOTE`` (e.g. ``BTC/USDT``), the canonical form
          verbatim.

        Single-instrument strategies need no equivalent: they read under the exact
        ``symbol`` string the config gives, so there is nothing to re-render.
    db_path : str or None, optional
        Per-strategy SQLite store path; overrides the global ``storage.db_path``
        for this strategy so its book/PnL are isolated. ``None`` → use the global
        store. Absent → the current (shared-store) behaviour, fully
        backward-compatible.

    """

    name: str
    universe: list[str]
    signal: SignalRefConfig
    capital: Decimal
    data: DataSourceConfig
    gross_cap: Decimal | None = None
    venue: str = "binance"
    store_key_format: Literal["venue", "hyphen", "slash"] = "venue"
    db_path: str | None = None

    @field_validator("name", "venue")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        """Reject blank portfolio ``name`` / ``venue`` (whitespace-only too)."""
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("universe")
    @classmethod
    def _valid_universe(cls, v: list[str]) -> list[str]:
        """Reject an empty universe, an unparseable pair, or a duplicate coin.

        Each entry must parse to a :class:`~trading_bot.domain.instrument.Symbol`
        (so a malformed pair is caught at config time), and no two entries may
        resolve to the **same** normalised :class:`Symbol` — the shared
        per-instrument tracker has no attribution, so a coin cannot appear twice.
        """
        if not v:
            raise ValueError("universe must be a non-empty list of pairs")
        seen: dict[Symbol, str] = {}
        for raw in v:
            if not raw or not raw.strip():
                raise ValueError("universe entries must be non-empty pair strings")
            try:
                symbol = parse_kraken_pair(raw)
            except ValueError as exc:
                raise ValueError(
                    f"universe entry {raw!r} is not a valid pair: {exc}"
                ) from exc
            previous = seen.get(symbol)
            if previous is not None:
                raise ValueError(
                    f"universe has duplicate instrument {raw!r} "
                    f"(same as {previous!r}): a coin may appear only once "
                    "(the shared per-instrument tracker has no attribution)"
                )
            seen[symbol] = raw
        return v

    @field_validator("capital")
    @classmethod
    def _positive_capital(cls, v: Decimal) -> Decimal:
        """Reject a non-positive ``capital`` (zero too)."""
        if v <= 0:
            raise ValueError(f"capital must be positive, got {v}")
        return v

    @field_validator("gross_cap")
    @classmethod
    def _positive_gross_cap(cls, v: Decimal | None) -> Decimal | None:
        """Reject a non-positive ``gross_cap`` (``None`` is allowed)."""
        if v is not None and v <= 0:
            raise ValueError(f"gross_cap must be positive, got {v}")
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
    live_enabled : bool, optional
        The explicit, off-by-default opt-in for live trading. Defaults to
        ``False``: even with ``mode == "live"`` and credentials present,
        :func:`~trading_bot.application.service_factory.build_engine` raises
        :class:`~trading_bot.domain.errors.LiveTradingNotEnabled` until this is
        set ``True``. Set it ``True`` *and* provide credentials *and* read the
        go-live runbook (``doc/dev/09-go-live.md``) to trade real money. Paper
        mode ignores it entirely.
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
        The single-instrument strategies to drive. Empty by default.
    portfolios : list of PortfolioStrategyConfig, optional
        The multi-asset portfolio strategies to drive (each allocates across a
        whole ``universe`` via a weight-vector signal). Empty by default. A
        portfolio runs alongside the single-instrument strategies through the same
        shared engine; no instrument may be claimed by both a portfolio and a
        single-instrument strategy (or by two portfolios) — see
        :func:`~trading_bot.application.run_app.build_portfolio_runners`.
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
    live_enabled: bool = False
    starting_capital: Decimal = Field(default_factory=lambda: money("100000"))
    brokers: list[BrokerConfig] = Field(default_factory=list)
    strategies: list[StrategyConfig] = Field(default_factory=list)
    portfolios: list[PortfolioStrategyConfig] = Field(default_factory=list)
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

    def to_yaml(self, path: str | pathlib.Path) -> None:
        """Dump this config to a YAML file so a UI edit persists (round-trippable).

        The inverse of :meth:`from_yaml`: serialise the validated model to YAML
        at ``path``, such that ``AppConfig.from_yaml(path)`` reconstructs an
        equivalent config. Money fields (``starting_capital`` / risk limits /
        ``capital`` / ...) are dumped via ``model_dump(mode="json")`` so each
        :class:`~decimal.Decimal` becomes an **exact string** (e.g.
        ``"100000"``), which :meth:`from_yaml` re-parses back to the same
        ``Decimal`` — never through ``float``. The parent directory is created
        if absent (the dashboard writes a default manifest under ``configs/``).

        Parameters
        ----------
        path : str or pathlib.Path
            Destination YAML file. Its parent directory is created if missing.

        """
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # `mode="json"` renders every Decimal as an exact string, so the YAML
        # scalar `from_yaml` reads back parses to the identical Decimal.
        data = self.model_dump(mode="json")
        with open(target, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)

    def add_strategy(self, strategy: StrategyConfig) -> AppConfig:
        """Return a new, validated config with ``strategy`` appended.

        A pure helper (this config is unchanged): rejects a name already claimed
        by a strategy **or** a portfolio (managed units share one name space),
        then re-validates the whole config so a bad entry (e.g. an unparseable
        symbol) is caught here rather than at deployment.

        Raises
        ------
        ValueError
            If a strategy or portfolio with the same ``name`` already exists.

        """
        self._reject_duplicate_name(strategy.name)
        return self.model_validate(
            {
                **self.model_dump(),
                "strategies": [*self.strategies, strategy],
            }
        )

    def add_portfolio(self, portfolio: PortfolioStrategyConfig) -> AppConfig:
        """Return a new, validated config with ``portfolio`` appended.

        The portfolio analogue of :meth:`add_strategy`: rejects a name already
        claimed by any managed unit and re-validates the whole config (so an
        empty / duplicate-coin universe or a non-positive capital is caught).

        Raises
        ------
        ValueError
            If a strategy or portfolio with the same ``name`` already exists.

        """
        self._reject_duplicate_name(portfolio.name)
        return self.model_validate(
            {
                **self.model_dump(),
                "portfolios": [*self.portfolios, portfolio],
            }
        )

    def remove_entry(self, name: str) -> AppConfig:
        """Return a new, validated config with the strategy/portfolio ``name`` removed.

        Drops the single-instrument strategy **or** portfolio whose ``name``
        matches (the two share one name space, so at most one is dropped), then
        re-validates. A pure helper — this config is unchanged.

        Raises
        ------
        ValueError
            If no strategy or portfolio is named ``name``.

        """
        strategies = [s for s in self.strategies if s.name != name]
        portfolios = [p for p in self.portfolios if p.name != name]
        if (
            len(strategies) == len(self.strategies)
            and len(portfolios) == len(self.portfolios)
        ):
            raise ValueError(
                f"no strategy or portfolio named {name!r} to remove"
            )
        return self.model_validate(
            {
                **self.model_dump(),
                "strategies": strategies,
                "portfolios": portfolios,
            }
        )

    def _reject_duplicate_name(self, name: str) -> None:
        """Raise if ``name`` is already a strategy or portfolio (shared name space)."""
        existing = {s.name for s in self.strategies} | {
            p.name for p in self.portfolios
        }
        if name in existing:
            raise ValueError(
                f"duplicate name {name!r}: a strategy or portfolio with that "
                "name already exists (managed units share one name space)"
            )
