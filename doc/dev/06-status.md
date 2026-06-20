# 06 — Status

_Last updated: 2026-06-20_

## Where things stand

**Phase 0 (bootstrap) — done / in this branch.** The repo now has the dccd/fynance
developer standard: `pyproject.toml`, ruff/mypy/pytest/interrogate, pre-commit,
GitHub Actions CI (3.11–3.13), Git Flow (`develop`/`master`), `CLAUDE.md`,
`.claude/` workflow + hooks, and this `doc/dev/` pack. The package imports and a
smoke test passes.

**The rewrite has begun.** `domain/` exists with the primitives — `money`
(Decimal, float-guarded), `instrument` (Kraken normalisation), `errors` (E1 leaf
01 landed). The remaining domain leaves (order, fill/position, signal, performance)
and the later layers (`transport`, `brokers`, `storage`, `application`,
`interfaces`) are pending — see `07-roadmap.md` / `08-program-plan.md`.

## Done

- Legacy implementation parked under `trading_bot/legacy/` (excluded from tooling).
- Modern packaging + tooling + CI + Git Flow.
- Claude Code workflow wired (`/pick-task` … `/release` resolve against this repo).
- Developer brief (`doc/dev/`) and rewrite roadmap.

## Pending

Everything in [`07-roadmap.md`](07-roadmap.md): the domain core, transport, the
Kraken broker + paper broker, the order router, the strategy runner, performance/
risk, the CLI, the orchestration layer, and (later) the UI and go-live hardening.

## Known gaps / deferred

- **Final project name** — kept as `trading_bot` for now (deferred decision).
- **Default paper-vs-live beyond MVP** — paper-first for now; revisit at go-live.
- **dccd↔trading_bot orchestration depth** (library import vs driving a service) —
  decided when the orchestration epic (E8) is planned.
- **`trading-bot` console script** — not declared until the CLI module exists (E7).
