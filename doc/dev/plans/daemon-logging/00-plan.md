---
plan: daemon-logging
kind: global
status: executing
roadmap: "3b. [ ] **Daemon observability — structured logs** (surfaced by the 2026-07-14 health audit; prereq for #4's systemd alerting + soak evidence): timestamped rotated `logs/daemon.log`, per-unit evaluation/order/skip trail, tick-duration metrics on `/api/health` (additive). Plan: `plans/daemon-logging/`."
release_on_done: true
---

# daemon-logging — structured, timestamped, rotated daemon logs

## Goal

Make the daemon **auditable from its log alone**. The 2026-07-14 health audit
had to reconstruct everything from the API and the SQLite books because
`logs/daemon.log` is a bare shell redirect of untimestamped rich-console
prints: no per-line timestamps, no per-strategy-unit lines, no order/skip
trail, no rotation, and 10 process incarnations interleaved in one file.

What the audit measured (the evidence this epic answers):

- The strings `alloc1-binance`, `order`, `submit`, `evaluat` **never appear**
  in the log; the only cadence signal is the aggregate
  `daemon tick: stepped 2 strategy(ies)` line.
- `PortfolioRunner` already logs the `prepare_leg` decisions — at `DEBUG`
  (`portfolio_runner.py:704/715`), with **no handler configured**, so they are
  invisible; only `WARNING+` escapes via logging's last-resort stderr handler
  (that is how `portfolio_feed`'s corrupted-parquet/lag warnings reached the
  redirect file).
- Realised tick cadence over 47 h was **~74 s vs the nominal 60 s** (2304
  ticks where ~2832 were expected) with a single APScheduler misfire line ever
  logged; `scheduler.add_job(_tick, trigger)` (`cli/main.py:1287`) sets no
  explicit `coalesce` / `misfire_grace_time` / `max_instances`.
- Whether alloc1-kraken's post-genesis silence is dust-skips (expected at
  capital 100) or a stall is **not answerable from the log** — after this epic
  it is one grep.

## Decomposition

1. **01-log-setup** — the logging spine: `LoggingConfig` (level, dir,
   retention), `configure_daemon_logging()` with a midnight
   `TimedRotatingFileHandler` and ISO-8601 timestamps carrying the numeric tz
   offset, daemon lifecycle lines routed through it, tracebacks captured via
   `logger.exception`.
2. **02-unit-event-trail** — per-unit INFO story: rebalance summary line,
   leg round-up/skip reasons (elevated from invisible DEBUG), fill lines,
   supervisor state transitions; pinned anti-spam rule (a no-new-bar tick adds
   zero INFO lines).
3. **03-tick-timing-metrics** — per-tick and per-unit durations measured and
   logged (WARN on overrun), explicit APScheduler semantics
   (`coalesce=True, misfire_grace_time=interval, max_instances=1`), additive
   `/api/health` + `/api/strategies` timing fields — proves or refutes the
   74 s-cadence hypothesis.

## Leaf checklist

- [x] 01 log-setup — `feat/daemon-log-setup` — medium
- [x] 02 unit-event-trail — `feat/daemon-unit-event-logs` — medium
- [ ] 03 tick-timing-metrics — `feat/tick-timing-metrics` — medium

## Dependencies

`01 → 02 → 03`, strictly serial (`parallel: false` everywhere): 02 and 03
both touch `supervisor.py`, and 03 extends the tick line 01 introduces and the
summary line 02 introduces.

## Done criteria

- The audit's unanswerable questions become greps on `logs/daemon.log`: when
  did each unit last evaluate / trade / skip and **why** (skip reasons carry
  the exact Decimals from `LegDecision.reason`).
- Every line timestamped ISO-8601 **with numeric tz offset** (the audit burned
  time on UTC-vs-CEST ambiguity).
- Rotation active: `daemon.log` + dated backups, retention 14 days; one file
  no longer accumulates incarnations.
- `/api/health` carries `last_tick_duration_ms` / `ticks_total` /
  `ticks_overrun`; `/api/strategies` rows carry `last_step_duration_ms` — all
  **additive** (public-contract rule: never change existing fields).
- Verification never touches the live daemon (port 8000) — every leaf verifies
  on a **scratch instance** (port 8099, scratch book copies, read-only dccd
  store).

## Release / restart runbook note

The production daemon keeps running the released version during the epic. At
the post-release restart (maintainer-authorized): stop the daemon, archive the
legacy unrotated file to `logs/archive/daemon-pre-rotation-20260714.log`, and
restart **without** the shell `>> logs/daemon.log` redirect (the file handler
now owns the file; a shell redirect would double-write). That restart is the
moment the old log gets cleaned — not before (the running process holds the
fd).
