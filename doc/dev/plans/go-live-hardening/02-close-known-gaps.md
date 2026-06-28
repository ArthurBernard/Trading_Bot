---
plan: go-live-hardening/02-close-known-gaps
kind: leaf
status: planned
complexity: high
depends: []
parallel: false
branch: feat/close-known-gaps
pr: ""
---

# Close the safe-to-fix known gaps

## Goal

Fix the recorded known gaps that are safe to close offline:
(a) **KPI v0** — config-driven starting capital so the KPI ratios are meaningful;
(b) **same-instrument commingling** — detect & reject (or clearly flag) two strategies
on the same symbol; (c) **venue-idempotency** — turn the documented live-submit policy
(don't blindly retry a non-idempotent `AddOrder`; reconcile on ambiguous failure) into
a guarded, documented code path (live still off — no real order sent).

## Files to change

- `trading_bot/application/config.py` — add a starting-capital field (e.g.
  `AppConfig.starting_capital: Decimal = money("100000")` or on `StorageConfig`/a new
  section — pick the cleanest, document).
- `trading_bot/application/service_factory.py` — wire `PerformanceService(v0=config.starting_capital)`.
- `trading_bot/application/run_app.py` — reject/flag duplicate-symbol strategies in `build_runners`.
- `trading_bot/interfaces/cli/main.py` — the `kpi --capital` default can fall back to the config's starting capital (keep the flag, document precedence).
- `trading_bot/brokers/kraken.py` — guard/document the non-idempotent `AddOrder` retry policy (the live-submit path: don't blindly retry; reconcile-on-ambiguous-failure). A clear docstring + a guard that surfaces the risk if a retry would re-place a non-idempotent order. Live stays disabled — no real call.
- Tests: `tests/application/test_config.py`, `test_service_factory.py`, `test_run_app.py`, `tests/brokers/test_kraken_rest.py` (extend), as needed.

## Steps

1. **KPI v0**: add `starting_capital` to the config (default `money("100000")`, positive
   validator). `build_engine` passes it to `PerformanceService(v0=...)`. Now Sharpe/
   Sortino/Calmar over a real run are meaningful (curve doesn't sign-cross). The CLI
   `kpi --capital` overrides the config value; document the precedence. The API/UI KPI
   inherit it automatically (they read `engine.perf`).
2. **Same-instrument detection**: in `build_runners`/`run_app`, if two `StrategyConfig`s
   declare the same `symbol`, raise a clear `ConfigError` (per-strategy attribution
   isn't available — the shared per-instrument tracker would commingle them). Document
   why; a future per-strategy book is the real fix.
3. **Venue-idempotency policy**: in `KrakenBroker`, make the non-idempotent-`AddOrder`
   retry behaviour an explicit, documented decision: the AddOrder path should NOT be
   blindly retried by the transport on an ambiguous failure (a lost response after the
   order landed). Implement the guard (e.g. mark order-placing POSTs non-retryable, or
   surface an "ambiguous — reconcile before retrying" error) + a docstring pointing at
   `reconcile`. Live is still off, so this is a guarded code path, not a live call.

## Tests (via `.venv`)

- `starting_capital`: a config value flows to `engine.perf` so a real run's KPIs are
  non-trivial (Sharpe finite/non-zero on a winning curve); the CLI `--capital` override
  wins over the config; default is `100000`.
- duplicate-symbol config → `build_runners`/`run_app` raises `ConfigError` (clear message);
  distinct symbols still build fine.
- AddOrder retry guard: a simulated ambiguous failure on `AddOrder` does not silently
  retry-and-duplicate (assert the guard fires / the error is the documented one), with
  the broker mocked (no real venue).

## Verification on real data

In-process. Run a winning paper strategy with a config `starting_capital` and assert
the API `/api/kpi` now returns a meaningful Sharpe (not `0.0`); assert a 2-strategy
same-symbol config is rejected; assert the AddOrder ambiguous-failure guard via mocks.
Gates via `.venv`.

## Closeout

- CHANGELOG (Added/Changed/Fixed): "config `starting_capital` → meaningful KPIs; reject same-symbol strategies; guard non-idempotent AddOrder retries."
- ADR: the three gap closures + their rationale; update `06-status` known-gaps (resolve/scope them).
- Status/roadmap: deferred to leaf 03.
