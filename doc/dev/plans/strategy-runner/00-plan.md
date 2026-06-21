---
plan: strategy-runner
kind: global
status: planning
roadmap: "- [ ] **E5 — Strategy runner.** Load a strategy (config + fynance signal), feed it data from dccd, emit target positions/orders; the live loop. Replaces `legacy/StrategyBot`."
release_on_done: false
---

# E5 — Strategy runner

## Goal

Wire the triptych into a live loop: a **Strategy** (config + a signal callable,
typically fynance-backed) is fed market **data** (a `DataFeed`, dccd-backed) and
produces a domain `Signal`; the runner turns that into a target `Position` and
routes the resulting orders through the E4 `OrderRouter`. Replaces the legacy
`StrategyBot` iterator + importlib `get_signal({-1,0,1})` loading
(`trading_bot/legacy/strategy_manager.py`). The first epic that consumes **both**
sibling repos (dccd data, fynance signals).

**Hard invariant — causality / no lookahead:** a strategy at bar *t* may only see
bars `≤ t`; the feed must never hand future data to the signal. (Mirrors fynance's
walk-forward discipline.)

## Decomposition

1. **strategy-spec** — `application/strategy.py`: how a strategy is declared/loaded (config + signal callable → domain `Signal`).
2. **data-feed** — `application/data_feed.py`: `DataFeed` abstraction; in-memory feed (offline) + dccd-backed feed (`Client.read`); causal.
3. **strategy-runner** — `application/strategy_runner.py`: the loop feed→signal→target position→orders via the router.

## Leaf checklist

- [ ] 01 strategy-spec — feat/strategy-spec — medium
- [ ] 02 data-feed — feat/data-feed — high
- [ ] 03 strategy-runner — feat/strategy-runner — high (depends on 01, 02)

## Dependencies

- 01 and 02 are independent (01 needs E1/E4-01; 02 needs E2 + dccd) — run serially.
- 03 depends on 01 + 02 (and E4-03 OrderRouter + E4-04 PositionTracker).

## Done criteria

- `application/` exposes `Strategy`, `DataFeed` (+ in-memory & dccd-backed), and
  `StrategyRunner`. `ruff`/`mypy`/`pytest` green via `.venv` (0 skipped).
- A strategy run is verifiable **fully offline**: in-memory bars → fynance-backed
  signal → target position → orders on `PaperBroker` → positions match expectation;
  **causality asserted** (signal at t sees only bars ≤ t).
- A dccd-read smoke (`-m network`) reads real bars where inventory has data.
- Last leaf (03) removes the E5 line from `07-roadmap.md` and updates `06-status.md`.
