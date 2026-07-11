---
plan: api-completeness/01-mark-cache
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/mark-cache
pr: ""
---

# 01 — the engine's mark cache (bar closes, published by the runner)

## Goal

The engine should always know "the last close the strategy saw, and when it
was" per symbol — without any I/O on the API path. New
`trading_bot/application/mark_cache.py`:

- `@dataclass(frozen=True, slots=True) class Mark`: `price: Money`,
  `asof_ms: int`, `source: Literal["bar_close", "last_fill"]`.
- `class MarkCache`: `update(symbol: Symbol, price: Money, asof_ms: int) ->
  None` (source is always `bar_close` on this path) and
  `get(symbol) -> Mark | None`, `all() -> dict[Symbol, Mark]`. Plain dict,
  updated from the runner's async loop, read from the API thread-context —
  scout how other runner→supervisor state is shared (e.g. `last_eval_ts`)
  and mirror the same discipline (the codebase's existing answer to this
  sharing is the authority; do not invent new locking).

Wiring:

- `Engine` (service_factory.py) gains a `mark_cache: MarkCache` field
  (default_factory, like `spec_resolver`).
- `portfolio_runner`: right after `prices = self._latest_closes(frames)`
  (portfolio_runner.py:481), publish every `(symbol, close, asof_ms)` into
  the engine's cache. The asof: the frame's last bar timestamp — read how
  `_latest_closes`/the frames carry time (the runner already computes the
  rebalance `asof_ms`; use the same value it stamps on signals).
- `StrategyRunner` (single-instrument): publish its instrument's latest
  close the same way IF the seam is equally direct; if its loop differs
  materially, note the follow-up in the module docstring instead of forcing
  it (the dashboard units are portfolio units).
- The supervisor does NOT consume it yet (leaf 02).

## Files to change

- `trading_bot/application/mark_cache.py` — **new**.
- `trading_bot/application/service_factory.py` — `Engine.mark_cache`.
- `trading_bot/application/portfolio_runner.py` — the publish call (+ the
  constructor/threading if the runner doesn't already hold the engine or the
  cache — mirror how `spec_resolver` was threaded in #204).
- `trading_bot/application/run_app.py` — threading if needed (same pattern).
- `trading_bot/tests/application/test_mark_cache.py` — **new**.
- The portfolio-runner test module — one integration test.

## Steps

1. Read the `spec_resolver` threading (#204's diff) and `_latest_closes`;
   mirror both.
2. Implement + wire + tests; all three gates green
   (`~/.pyenv/versions/trading_bot_env/bin/python -m pytest`, `-m ruff check
   trading_bot/`, `-m ruff format --check trading_bot/`).

## Tests

- `test_mark_cache_update_get_all` (Decimal-exact, latest write wins).
- Runner integration: after one rebalance tick over the existing test
  harness, the engine's cache holds every traded symbol's close with the
  tick's asof; a second tick updates in place.
- `test_engine_carries_mark_cache` (factory default).

## Verification on real data

Drive one rebalance-shaped pass over a scratch copy of
`var/dashboard/alloc1-binance.sqlite` with the REAL dccd frames (the
daemon's manifest `configs/dashboard.yaml` points at the store under
`~/data/arthurserver` — read-only): after the tick, dump the cache and
report 3 sample symbols' `(price, asof_ms→ISO, source)`; each price must
equal the last close visible in the dccd frame for that symbol and the asof
must be the frame's last bar time. Originals under `var/` and port 8000
untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "per-engine mark cache — the runner
  publishes each rebalance's last closes (`bar_close` marks with their asof)
  for the API layer to read without I/O (#XX)."
- ADR: fold into leaf 02's entry (one mark-policy decision for the epic)
  unless a sharing/locking nuance emerged.
- Tick leaf 01 in `00-plan.md`; archive per `/finish-task`.
