---
plan: daemon-logging/03-tick-timing-metrics
kind: leaf
status: planned
complexity: medium
depends: [01, 02]
parallel: false
branch: feat/tick-timing-metrics
pr: ""
---

# 03 — Tick timing: durations, scheduler semantics, health metrics

## Goal

Explain and monitor the tick cadence. The audit measured a realised ~74 s
cadence against the nominal 60 s (2304 ticks in 47 h where ~2832 were
expected) with a single misfire warning ever logged and no explicit scheduler
semantics. After this leaf: every tick's duration is measured and logged
(WARN on overrun), the APScheduler job's misfire behaviour is pinned
explicitly, and the numbers are exposed as **additive** fields on
`/api/health` and `/api/strategies` — proving (or refuting) the
tick-takes-longer-than-60s hypothesis with data.

## Files to change

- `trading_bot/interfaces/cli/main.py` — `_tick` timing + counters, explicit
  `add_job` semantics, extended scheduler hook dict
- `trading_bot/application/supervisor.py` — per-unit step duration captured
  on the unit's runtime status
- `trading_bot/interfaces/api/app.py` — additive serialization of the new
  fields (health + strategies rows)
- `trading_bot/tests/` — cli daemon-tick tests, supervisor step tests, API
  contract tests (additive assertions)

## Steps

1. **Measure total tick** — in `_tick` (`cli/main.py`): wrap the
   `supervisor.step_all()` await with `time.monotonic()`; keep in the daemon
   closure state: `last_tick_duration_ms: int`, `last_tick_ts: int` (epoch
   ms), `ticks_total: int`, `ticks_overrun: int` (duration > interval).
   - The leaf-01 heartbeat line gains the duration:
     `daemon tick: stepped N strategy(ies) in <s.mmm>s`.
   - Overrun: `logger.warning("daemon tick overrun: <s.mmm>s > <interval>s …")`.
2. **Pin scheduler semantics** — `scheduler.add_job(_tick, trigger,
   coalesce=True, misfire_grace_time=<interval seconds>, max_instances=1)`
   (`cli/main.py:1287`):
   - `max_instances=1` — two overlapping ticks must never race one engine;
   - `coalesce=True` — a backlog of missed runs collapses into one;
   - `misfire_grace_time=interval` — a late tick still runs (default grace is
     1 s: a 61-s-late job is silently **skipped** — the presumed source of the
     unexplained shortfall).
   Derive `<interval seconds>` from the configured trigger, not a literal.
3. **Per-unit duration** — in `Supervisor.step` (`supervisor.py:1484`): time
   the unit's evaluation, store `last_step_duration_ms` on the unit's runtime
   state next to the existing `last_eval_ts`, and append `took=<ms>ms` to the
   leaf-02 rebalance summary when a rebalance ran.
4. **API exposure (additive only — public contract)**:
   - `/api/health`: the scheduler hook (`cli/main.py` injects it; shape
     documented at `app.py:1740`) gains `last_tick_duration_ms`,
     `last_tick_ts`, `ticks_total`, `ticks_overrun`; `app.py` serializes
     whatever extra keys the hook returns (or mirrors them explicitly —
     match the existing style). `None`s before the first tick.
   - `/api/strategies` rows: `last_step_duration_ms` (`None` before first
     eval).
   - **No existing field changes name, type, or semantics.**
5. Housekeeping: the counters live in the daemon, not the app — the plain
   `dashboard` command (no scheduler) keeps `None`s, exactly like
   `next_tick_ts` today.

## Tests

- cli daemon tick (existing fake-supervisor harness): after two ticks,
  `ticks_total == 2`, `last_tick_duration_ms >= 0`, heartbeat line carries
  `in <…>s`; a stubbed slow tick (> interval) increments `ticks_overrun` and
  emits the WARN.
- `add_job` called with `coalesce=True`, `max_instances=1`,
  `misfire_grace_time == interval` (assert via the scheduler's job object or
  a spy).
- Supervisor: after a step, the unit status carries
  `last_step_duration_ms >= 0`; unset (`None`) before any step.
- API contract tests: new fields present and `None`-safe; **all existing
  fields unchanged** (extend the additive-sweep suite from `api-completeness`
  — same pattern as PRs #209–#214).
- Full suite + `ruff check trading_bot/` green.

## Verification on real data

Scratch instance (port 8099, scratch books, read-only dccd store — never port
8000):

1. Run ≥ 10 minutes (≥ 10 ticks on the 60 s trigger).
2. `curl -s -H "Authorization: Bearer <scratch token>" 127.0.0.1:8099/api/health`
   → report the real `last_tick_duration_ms`, `ticks_total`, `ticks_overrun`
   values; `/api/strategies` → both units' `last_step_duration_ms`.
3. From the scratch log, report the observed tick-duration distribution
   (grep the heartbeat lines) — e.g. "27-pair resample takes X–Y s per tick".
   State explicitly whether durations approach/exceed 60 s (validating the
   shortfall hypothesis) or stay low (pointing at misfire-grace skips instead
   — which step 2's `misfire_grace_time` fix addresses either way).
4. Confirm the live daemon untouched.

## Closeout

- CHANGELOG `### Added`: "tick timing: durations logged (WARN on overrun),
  explicit APScheduler `coalesce`/`misfire_grace_time`/`max_instances`,
  additive `/api/health` (`last_tick_duration_ms`, `ticks_total`,
  `ticks_overrun`) and `/api/strategies` (`last_step_duration_ms`) fields (#XX)".
- ADR: pinned scheduler semantics + why (silent 1 s default grace skipped
  late ticks unlogged); rejected: raising the interval, async-gathering units
  (per-unit lock already serializes safely).
- `06-status.md`: cadence now measured; note the audit's 74 s finding as
  resolved-or-explained with the first real numbers.
- **Last leaf**: tick 03, set `00-plan.md` `status: done`, remove the `3b.`
  roadmap line, move `plans/daemon-logging/` to the archive, suggest
  `/release` — and after the release, apply the 00-plan "Release / restart
  runbook note" (archive the legacy `daemon.log` at the prod restart).
