---
plan: daemon-logging/02-unit-event-trail
kind: leaf
status: planned
complexity: medium
depends: [01]
parallel: false
branch: feat/daemon-unit-event-logs
pr: ""
---

# 02 — Per-unit event trail: evaluations, orders, skips, fills

## Goal

The log tells each unit's story. After this leaf, "why hasn't alloc1-kraken
traded since genesis?" is answered by grepping `logs/daemon.log`: every
completed rebalance logs a summary, every round-up/skip logs its
`LegDecision.reason` (the exact Decimals and the binding minimum), every
submit and fill logs identifiers and amounts. Pinned anti-spam rule: a
steady-state tick where nothing happened adds **zero** INFO lines.

## Files to change

- `trading_bot/application/portfolio_runner.py` — summary line + elevate the
  leg-decision/submit DEBUGs (`:704`, `:715`) to the pinned INFO formats
- `trading_bot/application/order_fill_sync.py` — INFO per fill applied
- `trading_bot/application/supervisor.py` — unit state-transition lines
  (started / stopped / step error)
- `trading_bot/application/service_factory.py` — thread the unit name where a
  component lacks it (see step 4)
- `trading_bot/tests/application/` — extend the runner / fill-sync /
  supervisor tests with `caplog` assertions

## Steps

Pinned line formats (key=value prefix, grep-stable; all through the module
loggers `logging.getLogger(__name__)` that already exist):

1. **Rebalance summary** — in `PortfolioRunner`, one INFO at the end of each
   *evaluated* rebalance (a new bar was consumed, whether or not orders
   resulted):
   `unit=<name> rebalance asof=<bar ts ISO-UTC> legs=<n> submitted=<n> round_up=<n> skipped=<n> on_target=<n>`
   The runner already counts these outcomes to return the submitted count —
   accumulate the four counters alongside.
2. **Leg decisions** — in `_prepare_delta` (`portfolio_runner.py:704/715`
   today): `round_up` and `skip` decisions log **INFO**
   `unit=<name> leg <symbol> <action>: <LegDecision.reason>`;
   plain `submit` passthroughs stay DEBUG (the router/summary already tell
   that story — no double INFO per order).
3. **Order lifecycle** — the runner is per-unit and awaits the router, so it
   logs (no router signature change):
   - accepted: `unit=<name> order submitted cid=<client_order_id> <side> <qty> <symbol> @ <limit px|mkt> venue_id=<id>`
     (INFO — replaces/absorbs the `:715` DEBUG);
   - refused/rejected: WARN with the same prefix + `reason=<…>`.
4. **Fills** — `OrderFillSync` applies every fill; give it an optional
   `unit_name: str | None = None` constructor param (additive, default keeps
   current signature working), threaded from `service_factory` where each
   unit's sync is built. On apply: INFO
   `unit=<name> fill cid=<cid> <signed qty> @ <price> fee=<fee> <fee_asset|quote>`.
5. **Supervisor transitions** — INFO on unit start/stop, ERROR (with
   `logger.exception`) when a unit's `step` raises. A tick that evaluates
   nothing (no new bar) logs at DEBUG only.
6. **Anti-spam check** — with INFO level and no new daily bar, one hour of
   ticks must add only the ~60 heartbeat lines from leaf 01 — nothing per
   unit. (Two units × 1440 min/day of per-unit INFO would bury the signal.)
7. Secrets: none of these lines may include tokens/keys — amounts, ids,
   symbols, reasons only (`LegDecision.reason` carries only Decimals and
   minimum names by construction).

## Tests

- Runner rebalance with a mixed outcome (≥1 submit, ≥1 skip via a venue-min
  spec): `caplog` shows the summary line with correct counters, the skip's
  INFO line containing the `LegDecision.reason` text, the submit's INFO line
  with cid; counters sum to `legs`.
- No-new-bar tick: `caplog` at INFO gains **zero** unit lines.
- `OrderFillSync` with `unit_name="u1"`: fill application logs the pinned
  format incl. `fee_asset`; with `unit_name=None`: line still valid (prefix
  `unit=?` or omitted — pick one and test it).
- Supervisor: step exception → ERROR with traceback in `caplog`.
- Full suite + `ruff check trading_bot/` green.

## Verification on real data

Scratch instance (same recipe as leaf 01 — port 8099, scratch book copies,
read-only real dccd store, **never** port 8000 / `var/` originals):

1. Point the scratch manifest at **fresh empty books** (no copies) so genesis
   funding + the first rebalance fire immediately on start — the same real
   signal path as 2026-07-12, on today's real bars.
2. Run ≥ 3 minutes; SIGINT.
3. Report verbatim from the scratch log: both units' rebalance summary lines,
   at least one real `skip` line with its full `LegDecision.reason` (expected
   in numbers: at capital 100, most kraken legs sit below the venue minimum —
   this excerpt IS the documented answer to the kraken-silence question), at
   least one submitted-order line and its fill line with matching cid.
4. Cross-check one logged fill against the scratch book
   (`sqlite3 <scratch>.sqlite 'select …'`): cid, qty, price match — the log
   never claims a fill the store does not have.
5. Confirm the live daemon untouched (port 8000 responds, pid unchanged).

## Closeout

- CHANGELOG `### Added`: "per-unit daemon log trail: rebalance summaries,
  round-up/skip reasons (venue minimums), order submits and fills with ids —
  a unit's inactivity is now explained by the log (#XX)".
- ADR if judged non-trivial: lifecycle logging lives in the runner (per-unit,
  sees router outcomes) instead of the router (shared, unit-blind) — rejected:
  threading a LoggerAdapter through the router.
- `06-status.md`: soak observability note — kraken dust-skip visibility.
- Tick leaf 02 in `00-plan.md`; archive this leaf file. Roadmap line stays
  (leaf 03 open).
