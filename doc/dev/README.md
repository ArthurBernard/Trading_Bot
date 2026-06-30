# trading_bot — developer brief (for Claude Code)

This folder is an **orientation pack written for Claude Code** (not end users). Its
only job is to give an agent a fast, faithful overview of the repository: what
exists, how it fits together, why it was built that way, and what is and isn't
done. The authoritative working rules live in the repo-root `CLAUDE.md`.

> **Relationship to `CLAUDE.md`**: `CLAUDE.md` is the source of truth for
> *commands, the layer map, and the hard invariants you must not regress*. This
> folder is the *narrative and depth* around it. When the two disagree, trust
> `CLAUDE.md` and fix this folder.

> **Rewrite in progress.** Much of the architecture here is the **target**, not
> the current reality. The pre-2026 code is parked under `trading_bot/legacy/`.
> [`06-status.md`](06-status.md) and [`07-roadmap.md`](07-roadmap.md) are the
> honest record of what has actually landed.

## Read in this order

1. [`01-overview.md`](01-overview.md) — what trading_bot is, its place in the
   triptych, the current state, the repo map.
2. [`02-architecture.md`](02-architecture.md) — the target hexagonal layers and
   where each responsibility lives.
3. [`03-decisions.md`](03-decisions.md) — the design choices and *why* (rewrite,
   hexagonal, multi-exchange-ready/Kraken-first, paper-first, Decimal money).
4. [`04-brokers.md`](04-brokers.md) — the per-broker capability matrix (Kraken
   implemented; others declared).
5. [`05-testing.md`](05-testing.md) — testing layers and the "test the chain on
   real data" discipline for an execution engine.
6. [`06-status.md`](06-status.md) — what's done, what's pending, known gaps.
7. [`09-go-live.md`](09-go-live.md) — the go-live runbook (opt-in gates, pre-trade
   checklist, proven-vs-pending) — read before trading real money.
8. [`10-deploy.md`](10-deploy.md) — running the daemon under systemd (pyenv) + the
   control dashboard.

## Tools kept here

- [`plans/`](plans/) — **active plan trees** (durable, hierarchical task plans,
  tracked in git). Each roadmap item being worked on expands into a
  `plans/<epic>/` tree of a global map + precise leaf specs that drive
  `/plan` → `/execute-leaf` → `/finish-task`. Finished trees move to
  `_archive/plans/` (gitignored). See [`plans/README.md`](plans/README.md).

## Conventions for keeping this current

- This is descriptive, not aspirational: write what the repo **is**, not what it
  should become. Open work goes in [`07-roadmap.md`](07-roadmap.md) — the single
  source *index* — its **full decomposition** (every leaf + branch + deps) in
  [`08-program-plan.md`](08-program-plan.md), and its executable expansion (while
  being worked) in [`plans/`](plans/); history stays in git/CHANGELOG.
