---
plan: transport/03-ratelimit
kind: leaf
status: planned
complexity: medium
depends: []
parallel: false
branch: feat/transport-ratelimit
pr: ""
---

# RateLimiter — token-bucket + Kraken call-counter

## Goal

`trading_bot/transport/ratelimit.py`: a per-exchange `RateLimiter` over
`TokenBucket`s (mirroring dccd's
`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/transport/ratelimit.py`),
**plus** a Kraken-style **decaying call-counter** model (mined from
`trading_bot/legacy/tools/call_counters.py`). This is the last E2 leaf — it closes
the E2 roadmap line.

## Files to change

- `trading_bot/transport/ratelimit.py` — new.
- `trading_bot/transport/__init__.py` — export `RateLimiter`, `TokenBucket`, `KrakenCallCounter`.
- `trading_bot/tests/transport/test_ratelimit.py` — new.
- `doc/dev/07-roadmap.md` — remove the E2 line. `doc/dev/06-status.md` — mark E2 done.

## Steps

1. Read dccd's `transport/ratelimit.py` and `trading_bot/legacy/tools/call_counters.py`.
2. `TokenBucket(rate, *, time_source=monotonic, sleep=asyncio.sleep)`: `async acquire()`
   refills by elapsed×rate, waits when below one token. **time/sleep are seams** for
   deterministic tests.
3. `RateLimiter(rates=None, *, time_source, sleep)`: one bucket per exchange,
   `async acquire(exchange)`; conservative default rates; fallback rate for unknown
   venues.
4. `KrakenCallCounter`: model Kraken's counter — each call adds a per-endpoint cost,
   the counter **decays** at a fixed rate per second, and calls block when the cost
   would exceed the tier limit. Port the constants/logic from
   `legacy/tools/call_counters.py`. Expose `async acquire(cost)` (and a sync
   `would_exceed`/inspection helper). Seam the clock.

## Tests

- `TokenBucket`: with a fake clock, N rapid `acquire()` calls are spaced to the
  configured rate (assert the sleep seam's total wait); a slow caller never waits.
- `RateLimiter`: distinct exchanges use independent buckets; unknown exchange uses
  the fallback rate.
- `KrakenCallCounter`: counter increments per call, decays over (faked) time, and
  blocks/waits when the next call would exceed the limit; matches the legacy numbers.

## Verification on real data

Pure/deterministic timing layer — verified with a **fake clock** (no real sleeps):
drive a realistic call pattern and assert the spacing/decay matches the configured
rate and Kraken's counter limit. Demonstrate by running it.

## Closeout

- CHANGELOG (Added): "`transport.RateLimiter` — token-bucket + Kraken decaying call-counter."
- ADR: note the Kraken call-counter model (decay rate, per-endpoint cost, tier limit) and where the constants came from.
- Status/roadmap: **remove the E2 line** from `07-roadmap.md`; mark E2 done in `06-status.md`.
