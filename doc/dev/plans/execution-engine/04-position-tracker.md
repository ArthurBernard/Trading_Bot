---
plan: execution-engine/04-position-tracker
kind: leaf
status: done
complexity: medium
depends: [03]
parallel: false
branch: feat/position-tracker
pr: ""
---

# PositionTracker — live positions from confirmed fills

## Goal

`PositionTracker` maintains the live net `Position` per instrument by consuming
broker-confirmed **fills** (the PnL source of truth), reusing
`domain.position.Position.from_fills`. Subscribes to the `EventBus`.

## Files to change

- `trading_bot/application/position_tracker.py` — new; `PositionTracker`.
- `trading_bot/application/__init__.py` — export `PositionTracker`.
- `trading_bot/tests/application/test_position_tracker.py` — new.

## Steps

1. Read `domain/position.py` (`Position.from_fills`), `domain/fill.py`, and
   `application/events.py` (`FillEvent`).
2. `PositionTracker(event_bus=None)`:
   - `apply(fill: Fill)` — fold the fill into the running per-instrument position
     (keep an ordered fill list per instrument and recompute via `Position.from_fills`,
     or maintain an incremental fold consistent with it).
   - `position(instrument) -> Position | None`, `all_positions() -> dict[..., Position]`.
   - If given an `EventBus`, subscribe to `FillEvent` and `apply` automatically.
3. Money `Decimal`; one instrument per `Position`; deterministic in fill order.

## Tests

- Feed fills (buy, add, partial close, **flip**) → `position(instrument)` matches
  `Position.from_fills` on the same sequence exactly (net_qty, avg, realised_pnl, fees).
- Subscribed to an `EventBus`: emitting `FillEvent`s updates the tracked positions.
- Multiple instruments tracked independently.

## Verification on real data

Drive `PaperBroker` fills (via `OrderRouter`) onto the `EventBus`; assert the
`PositionTracker`'s positions equal `Position.from_fills` over the same fills.
Gates via **`.venv`**.

## Closeout

- CHANGELOG (Added): "`application.PositionTracker` — live positions from confirmed fills."
- ADR: none (delegates to `domain.Position`) unless the incremental fold is non-trivial.
- Status/roadmap: deferred to leaf 05.
