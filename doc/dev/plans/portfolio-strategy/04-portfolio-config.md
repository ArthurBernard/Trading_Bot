---
plan: portfolio-strategy/04-portfolio-config
kind: leaf
status: done
complexity: medium
depends: [02, 03]
parallel: false
branch: feat/portfolio-config
pr: "#66"
---

# Portfolio config + wiring into run_app; fynance-research extra

## Goal

Make a portfolio strategy **declarable** in `AppConfig` and **runnable** through
`run_app`, alongside the existing single-instrument strategies, and add
`fynance-research` as an optional editable dependency so LS1's signal can be
imported by reference.

## Files to change

- `trading_bot/application/config.py` — add `PortfolioStrategyConfig` and hang it
  off `AppConfig` (e.g. `portfolios: list[PortfolioStrategyConfig] = []`).
- `trading_bot/application/run_app.py` — build a `PortfolioRunner` per declared
  portfolio and add it to the orchestrator alongside the single-instrument runners;
  extend `RunReport`/`StrategyReport` (or add a `PortfolioReport`) for per-coin
  outcomes.
- `pyproject.toml` — add `fynance-research` to the `[triptych]` extra (editable
  from `../fynance-research`, kept out of the hard deps like `dccd`); document it
  in the extra's comment.
- `trading_bot/tests/application/test_portfolio_config.py` — new.

## Steps

1. `PortfolioStrategyConfig` (pydantic v2, mirror `StrategyConfig` validators):
   - `name: str` (non-empty);
   - `universe: list[str]` (canonical pairs, e.g. `["BTC/USDT", ...]`; non-empty,
     each parsed to a `Symbol`; reject duplicates within the universe);
   - `signal: SignalRefConfig` (a `module:function` ref → `target_weights`-shaped
     callable; reuse `SignalRefConfig`);
   - `capital: Decimal` (positive; exact from YAML scalar);
   - `data: DataSourceConfig` (the dccd Binance source; `span` = 86400 for daily);
   - `gross_cap: Decimal | None = None`; `cadence`/rebalance is daily (driven by the
     daily `span`); `venue: str = "binance"`.
2. In `run_app`/`build_runners` (or a sibling `build_portfolio_runners`):
   - resolve the signal via `load_portfolio_signal(signal.ref)` (leaf 01);
   - build a `PortfolioFeed` (leaf 02) over the universe from `data`;
   - build a `PortfolioStrategy` + `PortfolioRunner` (leaf 03) over the **shared**
     engine; add to the orchestrator.
   - **Commingling:** a portfolio **owns** its N instruments, so it is exempt from
     the single-instrument `_reject_commingled`, but assert **no overlap** between
     any portfolio's universe and any other strategy/portfolio's instruments
     (same shared per-instrument tracker → same attribution problem). Raise a clear
     `ConfigError` on overlap.
3. Keep **paper the default**; the portfolio path goes through the same factory /
   live opt-in gate (live still off by default).

## Tests

- A YAML/dict config with one `portfolios:` entry (fake signal ref, 3-coin
  universe, capital) validates and `run_app` (offline, injected fake dccd client +
  fake signal) produces the expected per-coin orders/positions.
- Overlap detection: a portfolio universe sharing a coin with a single-instrument
  strategy (or another portfolio) → `ConfigError`.
- Validators: empty universe, duplicate coin in a universe, non-positive capital →
  `ValidationError`/`ConfigError`.
- Backward-compat: a config with **no** `portfolios:` still validates and runs
  exactly as before.

## Verification on real data

**Mandatory.** Offline end-to-end through `run_app` with a fake signal + fake dccd
client over a small universe: assert the per-coin routed orders and final tracker
positions match the intended `weight × capital / price` targets read back from the
engine (broker-confirmed). The **real** LS1 + real dccd Binance run is leaf 05.

## Closeout

- CHANGELOG (Added): "`AppConfig.portfolios` + `run_app` — declare and run a native
  multi-asset portfolio strategy (universe + weight-vector signal + capital)."
  (Changed: `[triptych]` extra now includes `fynance-research`.)
- ADR: the `PortfolioStrategyConfig` shape; the no-instrument-overlap rule (vs
  single-instrument `_reject_commingled`); `fynance-research` as an optional extra
  (engine stays generic; LS1 is config).
- Status/roadmap: deferred to leaf 05.
