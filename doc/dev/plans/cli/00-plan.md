---
plan: cli
kind: global
status: planning
roadmap: "- [ ] **E7 — CLI.** Typer CLI (start/stop strategies, status, KPI table) + async orchestration replacing the legacy multiprocessing server. Declare the `trading-bot` console script. Delete superseded legacy modules."
release_on_done: false
---

# E7 — CLI & async orchestration (MVP "first light")

## Goal

Make the engine **runnable from a command line** — the MVP. A single
`service_factory` wires the whole application (config → brokers → router + risk +
tracker + perf + store), a **Typer** `trading-bot` CLI drives it (run a strategy,
show status/KPI), and an **async orchestration** entrypoint runs one+ strategy loops
concurrently with graceful shutdown — replacing the legacy multiprocessing
server/clients. Finally, the now-superseded legacy modules are deleted.

**Invariants**: paper-trading by default (live requires explicit opt-in + creds +
confirmation); everything offline-testable (Typer `CliRunner` + `PaperBroker`).

## Decomposition

1. **cli-skeleton** — `interfaces/cli/` Typer app + `application/service_factory.py` (single wiring point) + the `trading-bot` console script.
2. **cli-commands** — `run` / `status` / `kpi` / `version` commands (rich tables), replacing the legacy blessed CLI.
3. **async-orchestration** — async engine lifecycle: run StrategyRunner loops concurrently + graceful shutdown.
4. **legacy-removal** — delete the superseded `trading_bot/legacy/` modules; tidy packaging/docs/README.

## Leaf checklist

- [x] 01 cli-skeleton — feat/cli-skeleton — high
- [x] 02 cli-commands — feat/cli-commands — medium (depends on 01)
- [ ] 03 async-orchestration — feat/async-orchestration — high (depends on 01)
- [ ] 04 legacy-removal — chore/remove-legacy — low (depends on 02, 03)

## Dependencies

- 02 and 03 both depend on 01; 04 depends on 02 + 03. Serial in the main worktree.

## Done criteria

- `trading-bot --help` works (console script installed); `trading-bot run <config>`
  runs a strategy on the `PaperBroker` to completion and `status`/`kpi` report it.
- The async orchestration runs a strategy loop with clean Ctrl-C shutdown; no
  multiprocessing server.
- The superseded `legacy/` modules are gone; `ruff`/`mypy`/`pytest` green via `.venv`.
- Last leaf (04) removes the E7 line from `07-roadmap.md` and updates `06-status.md`.
