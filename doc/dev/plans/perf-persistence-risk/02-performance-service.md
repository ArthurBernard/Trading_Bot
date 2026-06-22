---
plan: perf-persistence-risk/02-performance-service
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/performance-service
pr: ""
---

# PerformanceService — live PnL/KPI over the fill stream

## Goal

`PerformanceService`: a live performance view that subscribes to `FillEvent`s on the
`EventBus`, maintains the realised-PnL / equity curve, and exposes KPIs (Sharpe,
Sortino, max-drawdown, Calmar) by delegating to `domain.performance` (fynance-backed,
present in `.venv`). Read-side only — it never places orders.

## Files to change

- `trading_bot/application/performance_service.py` — new; `PerformanceService`.
- `trading_bot/application/__init__.py` — export it.
- `trading_bot/tests/application/test_performance_service.py` — new.

## Steps

1. Read `domain/performance.py` (`pnl`/`cum_pnl`/`equity_curve`/`equity_array` +
   `sharpe`/`sortino`/`max_drawdown`/`calmar`, all Decimal/fynance), `domain/fill.py`,
   `domain/position.py` (`Position.from_fills`), `application/events.py` (`FillEvent`).
2. `PerformanceService(*, v0=money("0"), event_bus=None)`:
   - keep an ordered fill list (per instrument and/or aggregate — pick the simplest
     coherent model; document). `apply(fill)` folds a fill; if given an `event_bus`,
     subscribe to `FillEvent` and `apply` automatically.
   - expose `realised_pnl()`, `fees_paid()`, `equity_curve()` (Decimal), and
     `position(instrument)` (via `Position.from_fills`).
   - KPI methods `sharpe()/sortino()/max_drawdown()/calmar()` delegating to
     `domain.performance` over the equity/returns series. These need fynance — it's in
     the venv, so they run; if a series is too short to compute, return a defined
     value (e.g. `0.0`/`None`) rather than raising — document.
3. Money stays `Decimal` in the PnL core; the KPI series may be float (as
   `domain.performance` already does).

## Tests (via `.venv`)

- Feed a known fill sequence → `realised_pnl()`/`fees_paid()`/`equity_curve()` match
  hand-computed Decimal values (consistent with `Position.from_fills`).
- KPIs over a known equity curve match calling `domain.performance.sharpe(...)` etc.
  directly (these run — fynance is installed).
- Subscribed to an `EventBus`: emitting `FillEvent`s updates the performance view.
- Too-short series → the documented safe value, no exception.

## Verification on real data

Run a realistic trade sequence through `OrderRouter`→`PaperBroker` with the
`PerformanceService` subscribed to the bus; assert its realised PnL + equity endpoint
match an independent computation from the same fills, and that the KPIs match
`domain.performance` over that equity curve. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.PerformanceService` — live PnL/KPI over the fill stream (fynance-backed KPIs)."
- ADR: the aggregation model + the short-series KPI policy, if non-trivial.
- Status/roadmap: deferred to leaf 03.
