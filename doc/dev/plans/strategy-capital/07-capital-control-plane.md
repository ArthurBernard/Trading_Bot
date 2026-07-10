---
plan: strategy-capital/07-capital-control-plane
kind: leaf
status: planned
complexity: high
depends: [06]
parallel: false
branch: feat/capital-control-plane
pr: ""
---

# 07 — Control plane + API: deposit / withdraw / policy (paper)

## Goal

The mutation surface: supervisor-level `deposit`/`withdraw`/`set_policy`
(symmetric to `set_mode`), exposed as authenticated, read-only-guarded,
**idempotent** API routes. Paper/testnet are unconstrained by design (the
user's explicit ask); the **live funds gate is out of scope** (deferred to
real-key enablement — see `00-plan.md`). A live-mode deposit/withdraw is
refused with 409 ("live capital ops land with real-key enablement") rather than
silently allowed unchecked.

## Files to change

- `trading_bot/application/supervisor.py` — new methods, each `async with
  unit.lock` (the `set_mode` idiom, ≈ line 721), each persisting the manifest
  when config changes:
  - `deposit(name, amount, *, op_id, note="") -> dict` — validate amount > 0,
    unit exists; record `CapitalEvent(DEPOSIT, event_id=op_id)` via the unit's
    store (INSERT OR IGNORE ⇒ retried op is a no-op; return the current
    breakdown either way).
  - `withdraw(name, amount, *, op_id, note="") -> dict` — compute
    `withdrawable = max(0, total_value − committed − reserved)` where
    `committed = Σ |net_qty| × mark` over the unit tracker's positions (marks
    from the `_unrealised_of` price map, ≈ line 1422 — shorts caveat documented)
    and `reserved = Σ remaining qty × limit/ref price` over
    `router.tracked_orders()` non-terminal orders (usage precedent ≈ line 840);
    `amount > withdrawable` → raise a domain error mapped to HTTP 422 carrying
    the exact figure; else record `WITHDRAWAL`.
  - `set_policy(name, policy) -> dict` — update the unit's config field
    (`fixed|compound`), persist via `manifest()` + `to_yaml` (the `set_mode`
    persistence path); hot: the provider reads the new policy next tick.
  - `capital_breakdown(name) -> dict` — `{allocation, contributed, realised,
    unrealised, total_value, withdrawable, policy, events: [...]}` (events =
    the ledger list for the UI's audit trail).
- `trading_bot/interfaces/api/app.py` — routes on the dashboard app (auth +
  `read_only` 403 inherited; mirror the `create_strategy` guard ≈ line 2121):
  - `GET  /api/strategies/{name}/capital` → breakdown (+ ledger events).
  - `POST /api/strategies/{name}/capital` body
    `{action: "deposit"|"withdraw", amount: str, op_id: str, note?: str}` —
    422 on bad amount/insufficient withdrawable; 404 unknown unit; 409 live.
  - `POST /api/strategies/{name}/policy` body `{policy: "fixed"|"compound"}`.
  - Pydantic bodies parse `amount` to Decimal from **string** (never float),
    like `_CreateStrategyBody`'s money fields.
- Tests: `test_supervisor.py`, `test_control_api.py`.

## Steps

1. Supervisor methods + unit tests (in-memory stores, fake units).
2. API routes + guard tests.
3. Full `python -m pytest` + `ruff check trading_bot/`.

## Tests

- Idempotency: same `op_id` POSTed twice → one ledger event, both responses
  200 with identical breakdown; distinct op_ids accumulate.
- Withdraw guard: `amount > withdrawable` → 422 with the figure; fully-invested
  unit (open position consuming the cash) → withdrawable ≈ 0; withdrawable
  never negative.
- Policy: flip persists to the manifest YAML and shows in the breakdown; next
  `sizing_base` call reflects it (no restart — assert on the live unit).
- Guards: no auth → 401/redirect; `read_only=True` → 403 on both POSTs;
  unknown strategy → 404; live-mode unit → 409; `amount: 12.5` as JSON float →
  422 (string required).
- Money precision: deposit `"0.1"` three times → contributed exactly `"0.3"`.

## Verification on real data

Against the running daemon on the real manifest (paper, real dccd data):
`POST .../capital {deposit, "50", op_id}` → breakdown shows `contributed:
"150"`; **retry the same op_id** → still `"150"` (idempotency on the wire);
after the next daily rebalance the PaperBroker fills size on the new base
(read fills back and compare notionals); withdraw above withdrawable → 422
carrying the exact figure; policy flip visible in `configs/dashboard.yaml`.
Record the request/response pairs in the PR.

## Closeout

- CHANGELOG (Added): capital control plane — deposit/withdraw/policy endpoints,
  idempotent by op-id; paper unconstrained, live deferred (409).
- ADR note: capital mutations are ledger events under the unit lock; withdrawable
  subtracts committed + reserved.
- Tick leaf 07 in `00-plan.md`.
