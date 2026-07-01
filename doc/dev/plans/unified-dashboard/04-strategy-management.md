---
plan: unified-dashboard/04-strategy-management
kind: leaf
status: planned
complexity: high
depends: [03]
parallel: false
branch: feat/dashboard-strategy-mgmt
pr: ""
---

# Unified dashboard — manage strategies from the UI (persistent manifest + CRUD)

## Goal

Make the dashboard the **persistent control plane**: it owns a **manifest** it reads
on startup and **rewrites** on every change, and the UI lets you **add / enable /
disable / remove** strategies — composing a *deployment* from a signal that already
exists **in code** (the UI never authors signal code). `trading-bot dashboard` with
**no `-c`** reads a default manifest, so it is one dashboard common to all strategies
it declares. Everything is persisted, so it survives a restart.

Design decision (with the maintainer): the UI **deploys existing signals** (a
`module:function` ref or a builtin name) with venue / mode / capital / universe /
risk — it does **not** write the signal's Python. Signal code stays in
`strategies/<name>/signal.py`; the *deployment* (which strategies run, how) becomes
UI-driven, exactly as dccd's UI configures jobs (not the collector code).

## Files to change

- `.gitignore` — ignore `configs/` (the manifest is deployment/strategy content →
  **local only, never committed**), keeping an ignored `configs/dashboard.yaml`
  default.
- `trading_bot/application/config.py` — `AppConfig.to_yaml(path)` (round-trip: dump
  the model to YAML so a UI edit persists) + helpers to add/remove a strategy or
  portfolio entry by name, returning a new validated `AppConfig`.
- `trading_bot/application/supervisor.py` — dynamic membership: `add_unit(cfg_slice)`
  (build a stopped `_Unit` from a single-strategy/-portfolio slice, validate, append)
  and `remove_unit(name)` (stop if running, drop). A `manifest()` accessor returning
  the current `AppConfig` (for persistence). Adding never auto-starts (paper-safe);
  the UI start button (leaf 03) runs it.
- `trading_bot/interfaces/api/app.py` — in `create_dashboard_app` (skip all when
  `read_only`): `POST /api/strategies` (create — body: `name`, `kind`
  strategy|portfolio, `venue`, `mode`, `signal` ref, `symbol` **or** `universe`,
  `capital`, optional `risk`; validates → `supervisor.add_unit(...)` → persist the
  manifest), `DELETE /api/strategies/{name}` (stop + `remove_unit` → persist), and
  `GET /api/signals` (discoverable signal refs: the `_BUILTIN_SIGNALS` names + a scan
  of `strategies/*/signal.py` module-level callables, so the Add form offers a
  choice). A create/delete on a `read_only` dashboard → `403`.
- `trading_bot/interfaces/cli/main.py` — the `dashboard` command: when no `--config`,
  read/create a **default manifest** (`configs/dashboard.yaml`; a fresh empty-paper
  `AppConfig` if absent). Wire the supervisor so its mutations persist back to that
  path (pass the manifest path into `create_dashboard_app` / the supervisor, or a
  small `on_change` persist callback). Keep `-c` working (explicit manifest).
- `trading_bot/tests/interfaces/test_dashboard.py`, `.../application/test_supervisor.py`,
  `.../application/test_config.py` — new/extended tests. **CI note** (dccd + fynance
  absent in CI): any test that `start()`s a unit injects the offline
  `_two_venue_client` / `_FakeDccdClient`; ratio asserts `importorskip("fynance")`.

## Steps

1. Read: `AppConfig` (the `strategies` / `portfolios` lists + `from_yaml` + validators);
   `_BUILTIN_SIGNALS` + `load_portfolio_signal` / `_resolve_signal_fn` (how a ref
   resolves); the supervisor's `_Unit` construction (`__init__` splitting config →
   units) so `add_unit`/`remove_unit` mirror it; leaf-03's strategy endpoints +
   `read_only` guard; how leaf-01's `dashboard` command builds the supervisor.
2. Add `AppConfig.to_yaml` + add/remove-entry helpers (pure, validated). Round-trip
   test: `from_yaml(to_yaml(cfg)) == cfg`.
3. Add `supervisor.add_unit` / `remove_unit` / `manifest`. `add_unit` validates the
   slice like `__init__` does (a bad signal ref / no matching broker → a clear error,
   nothing added); `remove_unit` stops first.
4. Wire the CRUD + `GET /api/signals` endpoints; persist the manifest to the default
   path after each successful mutation (atomic write). `read_only` → 403.
5. Default-manifest handling in the `dashboard` command (create `configs/dashboard.yaml`
   if missing; pass the path so mutations persist).
6. `python -m pytest` + `ruff` + `mypy` green; verify the both-deps-absent simulation
   (block `dccd`+`fynance`) has no failures.

## Tests

- `to_yaml` round-trips; add/remove-entry helpers produce valid configs; a bad slice
  is rejected.
- `supervisor.add_unit` appends a stopped unit (visible in `status()`), `remove_unit`
  stops + drops it; adding a duplicate name / bad signal ref raises.
- `POST /api/strategies` creates + **persists** (re-read the manifest file → the new
  entry is there); `DELETE` removes + persists; both `403` under `read_only`;
  `GET /api/signals` lists builtins + discovered refs.
- `dashboard` with no `-c` creates/reads `configs/dashboard.yaml`.

## Verification on real data

Launch `trading-bot dashboard` (**no `-c`** → default manifest). Via `curl` (with a
session if auth): `GET /api/signals` lists `ma_crossover` + the alloc1 ref; `POST
/api/strategies` deploying **alloc1** (kind portfolio, venue binance, mode paper,
signal `strategies.alloc1.signal:alloc1_portfolio_signal`, its 14-coin universe,
capital) returns 200 and the entry **appears in `configs/dashboard.yaml` on disk**;
`GET /api/strategies` now lists alloc1; **start** it and confirm the Overview shows
its book; **restart the dashboard** and confirm alloc1 is **still there** (persisted).
`DELETE /api/strategies/alloc1` removes it + rewrites the manifest. All **paper** —
no real order. If an environment issue blocks the launch, say so + give the API/unit
evidence.

## Closeout

- CHANGELOG (Added): "Manage strategies from the dashboard — a persistent manifest
  (`configs/dashboard.yaml`, default for `trading-bot dashboard`), `POST /api/strategies`
  / `DELETE /api/strategies/{name}` / `GET /api/signals`, and dynamic supervisor
  add/remove. The UI deploys existing signals; signal code stays in `strategies/`."
- ADR: the deploy-from-existing-signals model (UI configures deployments, not signal
  code) + manifest persistence (round-trip YAML, local-only) + dynamic supervisor
  membership.
- Do NOT remove the roadmap line (deferred to the last leaf).
