---
plan: domain-core/05-performance
kind: leaf
status: done
complexity: high
depends: [03]
parallel: false
branch: feat/domain-performance
pr: "#11"
---

# Pure PnL / KPI performance functions

## Goal

The modern, pure `performance.py`: PnL / cumulative PnL / equity curve from a
fill+price sequence, and KPI (Sharpe, Sortino, max drawdown, Calmar) **delegated to
fynance**. No I/O. Replaces the legacy `_PnLI`/`_PnLR`/`_FullPnL`. This is the **last
leaf** — it closes the E1 roadmap line.

## Files to change

- `trading_bot/domain/performance.py` — new.
- `trading_bot/domain/__init__.py` — export.
- `trading_bot/tests/domain/test_performance.py` — new.
- `doc/dev/07-roadmap.md` — remove the E1 line. `doc/dev/06-status.md` — mark E1 done.

## Steps

1. Port the legacy `_PnLI` column model (`['price','returns','volume',
   'exchanged_volume','position','signal','delta_signal','fee','PnL','cumPnL',
   'value']` from `legacy/performance.py`) into **typed functions** over Fills +
   a price series: `returns`, `exchanged_volume`, `position_series`, `fee_series`,
   `pnl`, `cum_pnl`, `equity_curve`. Mine `_get_PnL`/`_get_returns`/`_get_fee`/
   `_get_pos`. Numeric core deterministic; Decimal at money boundaries, float arrays
   acceptable for the KPI series.
2. KPI wrappers calling **fynance** (`sharpe`, `sortino`, `mdd`/max-drawdown,
   `calmar`). fynance is a `[triptych]` dep — guard the import.
3. **mypy strict vs untyped fynance**: `domain.*` is strict (enables
   `no-untyped-call`), but fynance is untyped. Resolve by **either** a thin typed
   wrapper module isolating the fynance calls **or** a narrow
   `[[tool.mypy.overrides]] module = ["trading_bot.domain.performance"]` relaxation.
   Pick one and record it in the ADR (the wrapper is preferred — keeps the rest of
   the module strict).

## Tests

- Known fill+price sequence → expected `cum_pnl`/`equity` (hand-computed Decimal/float).
- KPI parity: a known returns series → wrappers match calling fynance directly.
- Fee impact on PnL.

## Verification on real data

Reconstruct PnL/equity for a realistic trade sequence over a **real-ish price path**
(a `trading_bot/legacy/.../data_base/example` fixture, or a synthetic-but-realistic
path) and compare the equity endpoint + Sharpe to an **independent** computation.
`pytest` green; `mypy trading_bot/` clean (strict on domain, per the chosen
fynance-typing resolution).

## Closeout

- CHANGELOG (Added): "Pure PnL/KPI performance functions (fynance-backed KPI)."
- ADR: "KPI delegated to fynance; domain stays pure — fynance's untyped calls
  isolated behind a typed wrapper (or narrow mypy override)."
- Status/roadmap: **remove the E1 line** from `07-roadmap.md`; mark E1 done and the
  domain layer present in `06-status.md`.
