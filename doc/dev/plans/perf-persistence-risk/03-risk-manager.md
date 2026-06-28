---
plan: perf-persistence-risk/03-risk-manager
kind: leaf
status: done
complexity: high
depends: []
parallel: false
branch: feat/risk-manager
pr: "#37"
---

# RiskManager — pre-trade limits + kill-switch (gates every order)

## Goal

`RiskManager`: a **pre-trade gate** enforcing `AppConfig.RiskConfig` limits
(`max_order`, `max_position`, `max_daily_loss`) plus a **kill-switch** (cancels open
orders + halts new ones). Integrated into `OrderRouter.submit` so **every order is
gated** — a breaching order (or a tripped switch) raises `RiskLimitBreached` and is
**never placed**. The last E6 leaf — closes the E6 roadmap line.

## Files to change

- `trading_bot/application/risk.py` — new; `RiskManager`.
- `trading_bot/application/order_router.py` — integrate the gate (optional
  `risk_manager` param; checked before `broker.place_order`).
- `trading_bot/application/__init__.py` — export `RiskManager`.
- `trading_bot/tests/application/test_risk.py` — new.
- `doc/dev/07-roadmap.md` — remove the E6 line. `doc/dev/06-status.md` — mark E6 done.

## Steps

1. Read `application/config.py` (`RiskConfig`), `application/order_router.py`
   (`submit` — the hook point, before `place_order`), `application/position_tracker.py`
   (`position(instrument)` for current exposure), `domain/order.py`,
   `domain/errors.py` (`RiskLimitBreached`).
2. `RiskManager(config: RiskConfig, *, position_tracker=None)`:
   - `check(order)` — raise `RiskLimitBreached` if:
     - `max_order` set and `order.qty > max_order`;
     - `max_position` set and the resulting net position
       (`|current_net + signed(order)|`, via `position_tracker`) `> max_position`;
     - `max_daily_loss` set and the day's realised loss already `>= max_daily_loss`
       (feed it realised PnL — via a `PerformanceService`/tracker hook or a setter;
       keep the coupling thin, document how daily loss is sourced/reset).
   - **Kill-switch**: `trip(reason)` sets a tripped flag (any subsequent `check`
     raises `RiskLimitBreached`); `reset()` clears it. `async kill(router, broker)`
     (or a documented entry) cancels all open orders and trips the switch.
   - `None` limits = unconstrained.
3. Integrate in `OrderRouter`: accept an optional `risk_manager`; in `submit`, call
   `risk_manager.check(order)` **before** `broker.place_order` — a raise means no
   venue call, the order is rejected/raised cleanly (document whether the order goes
   `REJECTED` or the error simply propagates without tracking). Keep idempotency intact.

## Tests (via `.venv`)

- `max_order`: an order above the cap → `RiskLimitBreached`, broker `place_order`
  **not** called (spy/PaperBroker count).
- `max_position`: an order that would push net exposure past the cap → blocked;
  one within → allowed.
- `max_daily_loss`: once the day's realised loss ≥ cap, new orders are blocked.
- **Kill-switch**: `trip()` → every subsequent `submit` raises and places nothing;
  `kill(router, broker)` cancels open orders; `reset()` re-enables.
- `None` limits → everything passes.
- Integration: `OrderRouter(..., risk_manager=rm)` blocks a breaching order end-to-end
  (no paper order created) and allows a compliant one.

## Verification on real data

Through `OrderRouter`→`PaperBroker`: submit a compliant order (fills, position moves),
then a breaching one (assert `RiskLimitBreached` + **no** new paper order + position
unchanged); trip the kill-switch and assert open orders are cancelled and further
submits are halted. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "`application.RiskManager` — pre-trade limits + kill-switch gating every order."
- ADR: the gate placement (in `OrderRouter.submit`, pre-`place_order`), the limit
  semantics (max order/position/daily-loss), and the kill-switch behaviour + how
  daily-loss is sourced.
- Status/roadmap: **remove the E6 line** from `07-roadmap.md`; mark E6 done in `06-status.md`.
