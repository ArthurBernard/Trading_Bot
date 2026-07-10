---
plan: strategy-capital
kind: global
status: executing
roadmap: "- [ ] **Per-strategy capital & dashboard IA reorg** (`strategy-capital`): append-only capital ledger per strategy (fund/refund/cashout, fixed|compound policy, capital/PnL/value split) + IA reorg (retire the legacy dashboard, per-strategy detail page). Plan: `doc/dev/plans/strategy-capital/`."
release_on_done: true
---

# strategy-capital — per-strategy capital + dashboard information architecture

## Goal

Deliver the five points of the 2026-07-10 dashboard feedback, end-to-end in paper
mode (the only reachable mode today):

1. a findable UI — retire the legacy orphan dashboard, give every strategy its own
   deep-linkable detail page;
2. choose the amount allocated to a strategy (paper: unconstrained; the live funds
   gate is **deferred** to real-key enablement);
3. distinguish starting capital / PnL (realised + unrealised) / total value as
   first-class, reconciling figures;
4. per-strategy reinvest-vs-cashout policy (`compound` | `fixed`);
5. fund (deposit / "refund") and cashout (withdraw) operations, idempotent and
   effective without an engine restart.

**The design (validated by adversarial review — do not relitigate in leaves):**

- One new source of truth: an **append-only `capital_events` ledger** per strategy
  (`FUNDING` genesis / `DEPOSIT` / `WITHDRAWAL`; no auto-sweep), PK
  `(event_id, strategy, mode)` + `INSERT OR IGNORE`, mirroring the fills
  discipline. Config `allocation` only **seeds** a deterministic genesis event,
  then goes inert — never two sources for the base.
- Derived quantities (recomputed, never stored):
  `C = Σdeposits − Σwithdrawals` (contributed), `R` = realised PnL (fills,
  **unchanged** — still the source of truth), `U` = existing best-effort MTM
  (`_unrealised_of`), `V = C + R + U` (total value),
  `withdrawable = max(0, V − committed_to_positions − reserved_by_open_orders)`
  — **never** `C + R` (strands invested capital).
- Sizing base `B = C` (fixed) or `C + R` (compound; realised only, never `U`),
  read by the runners through a **lazy `capital_provider` called every tick** —
  deposits/withdrawals/policy flips are hot (next tick), no engine rebuild, no
  crash window (the ledger write is the only durable write).
- KPI anchor `v0` repoints to the **genesis** funding; KPI ratios stay on the
  **fill-only** equity curve. The value curve (fills + capital events interleaved)
  is display-only — a deposit must never read as a return.
- Money is `Decimal` end to end (`str(Decimal)` over the wire); every mutation
  carries a caller-assigned `op_id` (the `client_order_id` analogue).

## Decomposition

Two tracks; the UX track (01, 05) and the model track (02, 03, 04, 06, 07) are
file-disjoint until they join at 08.

1. **01 retire-legacy-dashboard** — one dashboard code path; `run --serve` retires
   its legacy read-only app.
2. **02 domain-capital-event** — pure `CapitalEvent` + folds (`contributed_capital`,
   `value_series`).
3. **03 storage-capital-events** — append-only ledger table + read/write, dormant.
4. **04 config-allocation-policy** — `allocation` + `capital_policy` config fields,
   YAML round-trip, back-compat.
5. **05 strategy-detail-page** — `/strategies/{name}` detail page; roster slims;
   deploy moves to `/strategies/new`; `/pnl` tab retires into the detail page.
6. **06 capital-service-sizing** — `CapitalService` (genesis seeding, folds), lazy
   `capital_provider` into both runners, `v0` repoint, status fields.
7. **07 capital-control-plane** — supervisor `deposit`/`withdraw`/`set_policy` +
   paper API routes (idempotent, guarded).
8. **08 ui-capital-block** — the capital block + adjust-capital modal + policy
   toggle on the detail page; value column on the roster.

## Leaf checklist

- [x] 01 retire-legacy-dashboard — chore/retire-legacy-dashboard — medium
- [ ] 02 domain-capital-event — feat/domain-capital-event — medium
- [ ] 03 storage-capital-events — feat/storage-capital-events — medium
- [ ] 04 config-allocation-policy — feat/config-allocation-policy — low
- [ ] 05 strategy-detail-page — feat/strategy-detail-page — high
- [ ] 06 capital-service-sizing — feat/capital-service-sizing — high
- [ ] 07 capital-control-plane — feat/capital-control-plane — high
- [ ] 08 ui-capital-block — feat/ui-capital-block — medium

## Dependencies

```
01 ──────────────► 05 ─────────┐
02 ──► 03 ──┐                  ├──► 08
04 ─────────┴──► 06 ──► 07 ────┘
```

Serial execution in the main worktree (the safe default). If parallelism is ever
wanted: 01, 02 and 04 have disjoint file sets and could run concurrently.

## Out of scope — deferred follow-ups (recorded on the roadmap at closeout)

- **Live funds gate** — supervisor-level admission (Σ live allocations ≤ venue
  balance) + an **async** pre-check in `OrderRouter._do_submit` (a sync
  `funds_provider` inside `RiskManager.check` is impossible: `broker.balances()`
  is async). Lands **with real-key live enablement**, its roadmap companion.
- Overview portfolio money band + allocation breakdown (read-only add).
- `close-and-refund` teardown (flatten + drain) — with its **own** cancel path,
  never overloading the kill-switch semantics; plus the `remove_unit`
  flat-and-zero guard.
- State-communication polish (error/stale pills, live red banner, unified
  SSE+poll refresh).
- API quick wins outside the epic: order-cancel endpoint, kill-switch
  trip/reset + status, `last_error` on `/api/strategies`.
- Auto profit-sweep: **dropped**, not deferred (`fixed` + manual withdraw covers it).

## Done criteria

- All 8 leaves `done`; each shipped as its own PR onto `develop`, tests + lint
  green, real-data verification recorded in the leaf.
- The dashboard manifest run (paper, real dccd data) shows: detail page per
  strategy; capital block where start + PnL = total reconciles; deposit/withdraw/
  policy effective next tick with no restart; ledger visible as an event list.
- KPI ratios unchanged by any deposit/withdraw (fill-only curve verified).
- Roadmap line replaced by the deferred follow-up entries; ADR records the
  capital-ledger decision; CHANGELOG carries one line per leaf.
