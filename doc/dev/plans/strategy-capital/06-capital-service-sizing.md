---
plan: strategy-capital/06-capital-service-sizing
kind: leaf
status: planned
complexity: high
depends: [02, 03, 04]
parallel: false
branch: feat/capital-service-sizing
pr: ""
---

# 06 — Application: `CapitalService`, lazy sizing provider, `v0` repoint

## Goal

Make the ledger drive the engine: a `CapitalService` that seeds the genesis
event and folds the ledger, a **lazy `capital_provider` read by both runners on
every tick** (deposits/policy flips are hot — no engine rebuild), and the KPI
anchor `v0` repointed to the genesis funding. Feature-inert when `allocation`
is unset (and, for portfolios, `capital` keeps seeding the genesis). **KPI
ratios stay on the fill-only curve** — this is the leaf's hardest invariant.

## Files to change

- `trading_bot/application/capital_service.py` — **new**:
  - `CapitalService(store, strategy, mode)`; `ensure_genesis(amount)` records
    `FUNDING` with the **deterministic** `event_id = f"{strategy}:funding"`
    (INSERT OR IGNORE ⇒ re-deploys never double-fund); `contributed() -> Money`
    (fold via `domain.capital.contributed_capital`);
    `sizing_base(policy, realised_pnl) -> Money` — `fixed → C`,
    `compound → C + R` (realised only, never unrealised); floor the compound
    base at 0 (a drawdown below zero must not emit negative sizing).
- `trading_bot/application/portfolio_runner.py` — `__init__` (≈ line 249) gains
  `capital_provider: Callable[[], Money] | None = None`; `rebalance`
  (≈ line 409-414) passes `capital=self._capital_provider() if set else
  self._strategy.capital`. Provider is called **once per rebalance** (single
  consistent base per tick — this, not a lock, is the concurrency guarantee).
- `trading_bot/application/strategy_runner.py` — same optional provider;
  `step` (≈ line 334): when the provider is set, derive
  `reference_qty = sizing_base / last_close` (Decimal division via
  `domain.money`, quantized like existing qty handling; `last_close` = the
  close of the bar being stepped, already in hand). Unset → legacy static
  `reference_qty` untouched.
- `trading_bot/application/run_app.py` — `build_runners` (≈ line 507) /
  `build_portfolio_runners` (≈ line 604): construct the per-unit
  `CapitalService`, call `ensure_genesis(allocation or capital)` for units that
  declare money, wire the provider closure
  `lambda: cap.sizing_base(cfg.capital_policy, perf.realised_pnl())`.
- `trading_bot/application/service_factory.py` — `PerformanceService(v0=...)`
  (≈ line 198): `v0` = the unit's genesis amount (`allocation` or portfolio
  `capital`), falling back to `config.starting_capital` when the unit declares
  no money (legacy manifests unchanged).
- `trading_bot/application/supervisor.py`:
  - `_v0_of` (≈ line 1381): genesis amount per unit, same fallback.
  - `_status_dict`/`StrategyStatus` (≈ `supervisor.py:145`, serialized at
    `app.py:1221`): add `allocation` (genesis, str | null), `contributed`
    (ledger fold, str | null), `unrealised` (reuse `_unrealised_of`, str |
    null), `total_value` (`contributed + realised + unrealised`, null-safe:
    missing `U` ⇒ `contributed + realised`), `capital_policy`. All money as
    `str(Decimal)`.
  - `pnl_series` `current[mode]` (≈ line 1319): add the same fields
    (additive, backward-compatible keys).
- Tests: `test_portfolio_runner.py`, `test_strategy_runner.py`,
  `test_run_app.py`, `test_supervisor.py`, new `test_capital_service.py`.

## Steps

1. `CapitalService` + tests against an in-memory `SqliteStore`.
2. Runner providers + sizing tests (below) — keep every existing runner test
   passing unmodified (proves back-compat).
3. Wiring in `run_app`/`service_factory` + `v0` repoint + status fields.
4. Full `python -m pytest` + `ruff check trading_bot/` + `mypy trading_bot/`.

## Tests

- Genesis idempotency: two `ensure_genesis` calls → one event; restart
  (new service over the same store) → no re-fund.
- Sizing: portfolio with `capital_policy=fixed` sizes on `C` even after
  realised profit; `compound` sizes on `C+R`; a recorded `DEPOSIT` grows the
  **next** `rebalance`'s target quantities with **no runner rebuild** (assert on
  the same runner instance); losses in compound shrink the base, floored at 0.
- Single-instrument: `allocation` set ⇒ `reference_qty ≈ B/close` per step,
  updating as `B` changes; unset ⇒ byte-identical behaviour to today.
- KPI isolation: record a mid-run `DEPOSIT`; `equity_curve()`/`sharpe()` and
  `pnl_series`'s per-mode `series` are **unchanged** (fill-only), while
  `total_value` moves. This is the guardrail test — a deposit must never read
  as a return.
- `v0`: unit with `allocation` → KPI anchor = allocation; legacy unit →
  `starting_capital` (existing supervisor KPI tests stay green).

## Verification on real data

Run the daemon on the real manifest with `allocation: "100"` +
`capital_policy: "fixed"` on both portfolios (paper, real dccd bars): after the
first rebalance, order notionals sum ≈ weights × 100 (read back the actual
PaperBroker fills — compare requested vs broker-reported); `/api/strategies`
shows `contributed: "100"`, coherent `total_value`; then flip the manifest copy
to `compound` and confirm the next rebalance sizes on `C+R`. Record
requested-vs-filled figures in the PR.

## Closeout

- CHANGELOG (Added/Changed): capital service + policy-driven sizing; KPI anchor
  = genesis funding.
- ADR entry (the epic's core decision): append-only capital ledger; derived
  C/R/U/V; fixed|compound sizing base; KPIs stay fill-only.
- Tick leaf 06 in `00-plan.md`.
