---
plan: execution-engine/01-app-kernel
kind: leaf
status: done
complexity: medium
depends: []
parallel: false
branch: feat/app-kernel
pr: "#22"
---

# Application kernel — AppConfig + EventBus

## Goal

The cross-cutting glue `application/` opens with: a pydantic **`AppConfig`** (the
engine's declared shape) and an async **`EventBus`** (pub/sub fan-out the router /
tracker / future UI consume). Mirrors dccd's `application/config.py` + `events.py`.

## Files to change

- `trading_bot/application/__init__.py` — new; export `AppConfig`, `EventBus`, event types.
- `trading_bot/application/config.py` — new.
- `trading_bot/application/events.py` — new.
- `trading_bot/tests/application/__init__.py` — new (empty).
- `trading_bot/tests/application/test_config.py`, `test_events.py` — new.

## Steps

1. Read dccd's `application/config.py` + `events.py`.
2. **config.py** (`pydantic` v2): `AppConfig` with `mode: Literal["paper","live"] = "paper"`
   (default paper — invariant), `brokers: list[BrokerConfig]` (`name`, `exchange`),
   `strategies: list[StrategyConfig]` (skeleton: `name`, `instrument`/`symbol`),
   `risk: RiskConfig` (skeleton: optional `max_position`/`max_order`/`max_daily_loss`
   as `Decimal`, non-negative). YAML-loadable (`from_yaml(path)` / `model_validate`).
   Validators: reject unknown mode; non-negative risk limits; non-empty broker names.
3. **events.py**: a small `Event` base + `OrderEvent`, `FillEvent`, `LogEvent`
   (carry domain objects / ids; money `Decimal`). `EventBus`: `subscribe(handler)`,
   `unsubscribe`, `emit(event)` (sync handlers), and **async fan-out** `add_queue()`
   /`remove_queue()` returning `asyncio.Queue` (mirror dccd) so multiple async
   consumers can read concurrently.

## Tests

- `AppConfig`: default `mode=="paper"`; load from a realistic YAML dict; reject an
  unknown mode; negative risk limit raises.
- `EventBus`: `emit` reaches every subscriber; `add_queue` consumers each receive
  the event (fan-out); `unsubscribe`/`remove_queue` stop delivery.
- Event types carry their payload (Decimal money intact).

## Verification on real data

In-process (no I/O). Build an `AppConfig` from a realistic YAML and assert the parsed
shape; emit a realistic `FillEvent` to two subscribers + two queues and assert all
four receive it. Run gates via **`.venv`**: `.venv/bin/python -m pytest trading_bot/tests/application -q`.

## Closeout

- CHANGELOG (Added): "`application` kernel — `AppConfig` (pydantic) + async `EventBus`."
- ADR: the event taxonomy + paper-default mode, if worth noting.
- Status/roadmap: deferred to leaf 05.
