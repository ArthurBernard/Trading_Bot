# Plan trees — durable, hierarchical task plans

This directory holds **active plan trees**: the durable, file-based expansion of a
roadmap item into an executable plan. It is **tracked in git** (committed via the
"plan PR"). Finished trees move to `../_archive/plans/`, which is **gitignored**
(local reference only; git log + `CHANGELOG.md` stay authoritative for what
shipped). This file is the canonical reference for the format — the `/plan`,
`/execute-leaf` and `/finish-task` skills read and write it.

## Why this exists

`plan mode` writes to `~/.claude/plans/*.md` (outside the repo), so a plan is lost
on `/compact`. Plan trees fix that: plans are **committed to the repo** (durable,
reviewable, visible to every later branch) and **hierarchical** (a global map +
precise leaves).

## Layout

```
doc/dev/plans/<epic-slug>/
  00-plan.md            # global: goal, decomposition, leaf checklist, deps
  01-<leaf-slug>.md     # leaf: precise, agent-executable spec
  02-<leaf-slug>.md
  ...
```

**Depth is adaptive — never forced.**

- Trivial task → a *single* leaf file, **no global** `00-plan.md`.
- Normal task → a global `00-plan.md` + N leaves.
- A leaf still too big → its own sub-directory with its own `00-plan.md` and
  sub-leaves (recursion). The **deepest level must be precise enough that an agent
  executes it without re-deciding anything.**

## Lifecycle

```
/pick-task → /plan (build tree + open "plan PR" → develop)
  → merge plan PR
  → /execute-leaf <epic> next   (model from `complexity`, spawn agent, verify)
  → /finish-task                (tests, ADR, PR, archive leaf, tick global)
  → … repeat per leaf (deps respected; `parallel` leaves may run concurrently)
  → last leaf → roadmap line removed, global done → /release
```

The **plan PR lands the tree on `develop` first**, so every leaf branch cut later
already contains `doc/dev/plans/<epic>/`.

## Frontmatter

### Global `00-plan.md`
```yaml
---
plan: <epic-slug>
kind: global
status: planning | executing | done
roadmap: "<verbatim roadmap line this expands>"
release_on_done: true
---
```
Body: **Goal**, **Decomposition** (numbered leaf list), **Leaf checklist**
(`- [ ] 01 <slug> — <branch> — <complexity>`), **Dependencies**, **Done criteria**.

### Leaf `NN-<slug>.md`
```yaml
---
plan: <epic-slug>/NN-<slug>
kind: leaf
status: planned | executing | done | abandoned
complexity: low | medium | high   # → haiku | sonnet | opus
depends: [01]
parallel: false
branch: <type>/<topic>
pr: ""
---
```
Mandatory body: **Goal**, **Files to change**, **Steps**, **Tests**,
**Verification on real data** (run the real op through `PaperBroker`/sandbox; read
what the broker reports; compare to requested — a green unit suite is not enough;
see [`../05-testing.md`](../05-testing.md)), **Closeout** (CHANGELOG line; ADR note
if a non-trivial decision was made; status/roadmap edits).

## Complexity → execution model

**This repo runs every leaf on `opus`** (maintainer preference — see `CLAUDE.md`).
The `complexity` tag below records **effort/risk** and orders the queue, but the
execution model is `opus` regardless — the model column is the upstream default,
overridden here:

| `complexity` | effort/risk | model |
|--------------|-------------|-------|
| `low` | mechanical fan-out — doc scans, checklists, trivial edits | ~~haiku~~ → **opus** |
| `medium` | straightforward implementation against a precise spec | ~~sonnet~~ → **opus** |
| `high` | judgement, design, cross-cutting changes | **opus** |

## Dependencies & parallelism

- `depends:` lists leaf numbers that must reach `status: done` first.
- `/execute-leaf <epic> next` picks the lowest-numbered `planned` leaf whose
  `depends` are all satisfied.
- Leaves marked `parallel: true` (deps met) may run **concurrently** in isolated
  worktrees; everything else runs **serially in the main worktree** (the safe
  default).
