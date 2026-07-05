---
plan: ui-ux-overhaul/04-cadence
kind: leaf
status: done
complexity: medium
depends: [03]
parallel: false
branch: feat/ui-cadence
pr: ""
---

# Leaf 04 — Cadence made visible: next tick, next bar close, freshness

## Goal

An operator always knows **when the next position calculation happens**:

- the daemon's next scheduler tick (when the bot next *checks*), and
- each strategy's bar cadence + next bar close (when a check can actually
  *rebalance* — a tick over unchanged bars is an idempotent no-op).

Plus explicit freshness feedback ("updated at …") on every page.

## Files to change

- `trading_bot/interfaces/ui/templates/base.html`
- `trading_bot/interfaces/ui/templates/strategies.html`
- `trading_bot/interfaces/ui/templates/overview.html` (hook the leaf-03 chip
  to the live countdown)
- `trading_bot/tests/interfaces/test_dashboard.py` (only if a shell test
  asserts affected markup)

## Steps

1. **base.html — next-tick countdown**: `refreshHealth()` already polls
   `/api/health` every 10 s; keep the latest `next_tick_ts` in a shared var
   and render a live 1 s-interval countdown into the health chip
   (`next check 42s` / `next check 12m05s`). When `next_tick_ts` is null,
   show nothing there but set a `title` on the chip: "no scheduler — serve
   mode only steps strategies via the daemon (`start --serve`)". Expose a
   small helper (e.g. `tbCountdown(tsMs)`) other pages can reuse.
2. **strategies.html — cadence columns**: two new columns on the table:
   - **Cadence** — humanized span from leaf 01 (`86400` → `1d bars`, `3600` →
     `1h bars`, `900` → `15m bars`; generic fallback `Ns bars`);
   - **Next bar** — countdown to the next epoch-aligned bar boundary,
     `ceil(now_s / span) * span` (UTC), with the absolute local datetime in
     `title`. Muted `—` for stopped units.
   Update every second alongside the chip countdown (reuse the helper; do a
   targeted cell update, not a full table re-render).
3. **"Updated at" stamps**: each data card (Overview KPI/positions/orders,
   Strategies table, Orders page tables) gets a muted `updated HH:MM:SS`
   stamp in its header, set after every successful load (SSE-triggered or
   polled). One shared helper in base.html (e.g. `stampUpdated(elId)`).
4. Keep everything resilient: a null/absent `span` (older payload) renders
   `—`, never throws.

## Tests

Server behaviour is untouched (leaf 01 already tested the API). Extend the
template smoke tests only if they assert the strategies-table header markup.
Full suite + ruff green.

## Verification on real data

`python -m pytest`. With the TestClient over the seeded paper supervisor,
confirm `/api/strategies` rows carry the `span` the countdown consumes.
Maintainer visually confirms the countdown against a running
`start --serve` daemon (real APScheduler).

## Closeout

- CHANGELOG `[Unreleased]` → `### Added`.
- Tick 00-plan checklist; frontmatter `status: done`, fill `pr:`.
