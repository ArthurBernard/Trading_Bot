---
plan: accounting-guardrail
kind: global
status: planning
roadmap: "2. [ ] **`accounting-guardrail` — accounting invariant checker.** (UI consistency & accounting integrity, 2026-07-11)"
release_on_done: false
---

# accounting-guardrail — the book proves itself, continuously

## Goal

The `paper-integrity` epic (PRs #190–#194) fixed three silent accounting bugs
that only a manual audit caught. This epic encodes those invariants as a
**permanent, visible guardrail**: a pure checker that recomputes the book's
self-consistency, wired into the engine lifecycle, surfaced as a per-strategy
health field end-to-end (supervisor → API → dashboard pill), with `LogEvent`
alerts on the existing SSE stream. The 2026-07-11 audit confirmed **no such
check exists anywhere** and the natural hook points.

## Invariants checked (the violation taxonomy — pinned here, leaves implement)

| kind | rule | severity |
|---|---|---|
| `position_drift` | per instrument: `tracker.position(ins).net_qty` ≠ Σ signed store fills (mode-filtered), exact `Decimal` | `error` |
| `order_fill_mismatch` | per order with fills: Σ fill qty ≠ `filled_qty` (beyond `fill_tolerance`) | `warn` (a crash-lagged row is transient; healed at next restart) |
| `status_incoherent` | fills cover `qty` (within tolerance) but status not `FILLED`; or `FILLED` with `filled_qty` ≠ `qty` | `warn` |
| `duplicate_venue_ids` | a `venue_order_id` shared by >1 store order rows | `warn` |
| `kill_switch_tripped` | `engine.risk.tripped` (folded in at the health layer, not the checker — the checker stays pure) | `error` |

Health mapping: any `error` → `"error"`; else any `warn` → `"warn"`; else `"ok"`.

**Legacy-data policy**: the live books hold 14 (Binance) + 13 (Kraken)
pre-#190 duplicate venue ids — a *stable* `warn` count, deliberately visible
(honest history), non-red. It disappears if the books are reset for a clean
soak. The checker never mutates anything — it only reports.

## Decomposition

1. `01-invariant-checker` — pure `application/accounting.py`: `Violation`
   (frozen dataclass) + `check_book(...)` folding positions/fills/orders. No
   I/O, no wiring.
2. `02-engine-wiring` — run the checker at startup (after
   `_replay_paper_book`) and lazily with a TTL; store the report on the unit;
   emit one `LogEvent` per **new** violation on `engine.bus` (already streamed
   over SSE — zero new plumbing).
3. `03-health-surface` — `StrategyStatus.health`/`health_detail` (checker
   report + kill-switch fold — the audit showed `risk.tripped` has **zero
   readers** today), exposed via `/api/strategies` and aggregated worst-of in
   `/api/health`.
4. `04-ui-health-pill` — health pill on the Strategies roster + strategy
   detail header (tooltip lists `health_detail`), refreshed by the existing
   SSE + 10s-poll cycle.

## Leaf checklist

- [x] 01 invariant-checker — feat/accounting-checker — medium (#197)
- [x] 02 engine-wiring — feat/accounting-wiring — medium (#199)
- [x] 03 health-surface — feat/health-surface — medium (#200)
- [ ] 04 ui-health-pill — feat/health-pill — medium

## Dependencies

Serial: 01 → 02 → 03 → 04 (each consumes the previous layer's output; no
disjoint parallel pairs — 02/03 both touch `supervisor.py`).

## Done criteria

- On copies of the live books: pre-heal snapshot → checker flags the frozen
  rows + duplicate ids; post-heal → only the stable legacy `duplicate_venue_ids`
  warns remain. Deleting one fill from a copy → `position_drift` fires
  end-to-end (checker → LogEvent → API `health:"error"` → red pill).
- The invariants the `paper-integrity` agents verified by hand are now
  recomputed by the engine itself and visible in the dashboard.
- Roadmap line removed by the last leaf.
