---
plan: triptych-orchestration
kind: global
status: done
roadmap: "- [ ] **E8 — Orchestration of the triptych.** One `AppConfig` declaring data sources (dccd) + strategies (fynance) + brokers; a single entrypoint that wires the three. Decide library-import vs service-driving for dccd here."
release_on_done: false
---

# E8 — Orchestration of the triptych

## Goal

Make Trading_Bot the **conductor** of the triptych: a single `AppConfig` declares
its **data sources** (dccd), **strategies** (fynance signals) and **brokers** +
risk, and one entrypoint wires all three and runs the whole declared system —
`trading-bot run <config.yaml>` brings up every configured strategy at once. This
is the epic that fulfils the "execution **and** orchestration" scope.

**Resolves the deferred decision** (dccd integration depth): **library import**, not
a separate service — `dccd.Client` exposes `read` (for feeds) **and**
`backfill`/`stream` (to drive collection), all in-process. No daemon/IPC; trading_bot
imports dccd and calls it.

**Invariants**: paper-by-default; everything offline-testable (a fake dccd client +
`InMemoryFeed` + `PaperBroker`).

## Decomposition

1. **app-config-full** — extend `AppConfig`: each strategy declares its data source + signal ref + sizing; a top-level data/storage section. Backward-compatible.
2. **dccd-integration** — `application/data_provider.py`: `feed_for(strategy_config, *, client=None) -> DataFeed` via `dccd.Client` (library import).
3. **entrypoint** — one entrypoint `AppConfig → engine + per-strategy runners → Orchestrator.run`, wired into `trading-bot run <config.yaml>`.

## Leaf checklist

- [x] 01 app-config-full — feat/app-config-full — medium
- [x] 02 dccd-integration — feat/dccd-integration — high (depends on 01)
- [x] 03 entrypoint — feat/triptych-entrypoint — high (depends on 01, 02)

## Dependencies

- 02 depends on 01; 03 depends on 01 + 02. Serial in the main worktree.

## Done criteria

- `AppConfig` declares data + strategies (signal ref + sizing) + brokers + risk,
  YAML-loadable, backward-compatible; `feed_for` builds a `DataFeed` from a strategy's
  data source via dccd (library import, fake client offline); one entrypoint runs the
  whole declared multi-strategy system through the `Orchestrator`.
- `trading-bot run <config.yaml>` runs a declared (paper) system end-to-end.
- `ruff`/`mypy`/`pytest` green via `.venv` (0 unexpected skips).
- Last leaf (03) removes the E8 line from `07-roadmap.md` and updates `06-status.md`
  (incl. recording the resolved dccd-integration decision).
