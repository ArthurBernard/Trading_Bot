---
plan: triptych-orchestration/01-app-config-full
kind: leaf
status: done
complexity: medium
depends: []
parallel: false
branch: feat/app-config-full
pr: ""
---

# AppConfig — full declarative config (data + signal + sizing)

## Goal

Extend `AppConfig` so a single YAML fully declares a runnable system: each strategy
names its **data source** (dccd: exchange + span + optional history start), its
**signal** (a `module:function` reference or a builtin like `ma_crossover` with
params), and its **sizing** (`reference_qty`, `lookback`); plus a top-level
**data/storage** section. Backward-compatible with the current minimal `AppConfig`.

## Files to change

- `trading_bot/application/config.py` — extend `StrategyConfig` + add `DataSourceConfig`/`StorageConfig`.
- `trading_bot/tests/application/test_config.py` — extend.
- A small example config: `examples/config.example.yaml` — new (a runnable paper config).

## Steps

1. Read the current `config.py` (`AppConfig`, `BrokerConfig`, `StrategyConfig`,
   `RiskConfig`, `from_yaml`).
2. Extend `StrategyConfig` (keep `name`, `symbol`; all new fields **optional with
   sensible defaults** so existing configs still validate):
   - `data: DataSourceConfig` — `exchange: str` (e.g. "kraken"), `span: int`
     (bar seconds), `start: str | int | None` (history start), `data_type: str = "ohlc"`.
   - `signal: SignalRefConfig` — `ref: str` (a `"module:function"` **or** a builtin
     name like `"ma_crossover"`), `params: dict[str, ...]` (e.g. `{fast: 10, slow: 30}`).
   - `reference_qty: Decimal | None`, `lookback: int = 0`.
   Use nested pydantic models; validators (non-empty exchange, positive span,
   non-negative lookback, `reference_qty` positive when set).
3. `StorageConfig` (top-level, optional): `db_path: str | None = None` (sqlite),
   plus optional dccd defaults (`data_path: str | None`). Add `AppConfig.storage`.
4. Keep `from_yaml` working; ensure a **minimal** `AppConfig()` and the existing test
   configs still validate (backward-compatible — new fields optional/defaulted).

## Tests (via `.venv`)

- A full YAML (mode, storage.db_path, a strategy with data+signal+sizing) →
  `AppConfig.from_yaml` parses the nested shape exactly (Decimal sizing intact).
- A **minimal** legacy-shape config (just name+symbol) still validates (defaults applied).
- Validators: empty exchange / non-positive span / negative lookback / non-positive
  reference_qty all raise `ValidationError`.
- The `examples/config.example.yaml` loads and validates.

## Verification on real data

In-process. Load `examples/config.example.yaml` and assert the parsed `AppConfig`
exposes a strategy with its dccd data source, signal ref + params, and Decimal
sizing — the shape leaf 02/03 consume. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`AppConfig` — full declarative config (per-strategy data source + signal ref + sizing, storage section)."
- ADR: the declarative-config shape (signal-by-reference, data-source-per-strategy).
- Status/roadmap: deferred to leaf 03.
