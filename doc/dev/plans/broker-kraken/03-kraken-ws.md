---
plan: broker-kraken/03-kraken-ws
kind: leaf
status: planned
complexity: high
depends: [02]
parallel: false
branch: feat/broker-kraken-ws
pr: ""
---

# KrakenBroker — private WebSocket (fills / order updates)

## Goal

Stream Kraken's **private** WebSocket channels (`executions` / own-trades and order
updates) on top of `transport.WebSocketBase`, parsing frames into domain `Fill`s and
order-status updates — the live feed the order router/position tracker (E4) consume.
This is the **last E3 leaf** — it closes the E3 roadmap line. **No API key here**:
the auth-token step and frame parsing are tested via **mocks**; real private WS is
deferred until a key is provided.

## Files to change

- `trading_bot/brokers/kraken_ws.py` — new (or extend `kraken.py`); `KrakenPrivateWS`.
- `trading_bot/brokers/__init__.py` — export it.
- `trading_bot/tests/brokers/test_kraken_ws.py` — new.
- `doc/dev/07-roadmap.md` — remove the E3 line. `doc/dev/06-status.md` — mark E3 done.

## Steps

1. Read dccd's `_KrakenWS` (`sources/kraken.py`) and the local `transport/ws.py`
   (`on_connect`, `stream_raw`, `send`). The private feed needs a **WebSocket token**
   from the REST `GetWebSocketsToken` (private → uses leaf 02's signing); fetch it in
   `on_connect`/setup. Token retrieval is **mocked** here (no key).
2. `KrakenPrivateWS(WebSocketBase)`:
   - `on_connect`: authenticate + subscribe to the executions / own-trades + open-orders
     channels (Kraken v2 private subscribe with the token).
   - `async fills() -> AsyncIterator[Fill]` (or `events()` yielding parsed fills + order
     updates) — parse frames into domain `Fill`s (Decimal qty/price/fee, `client_order_id`
     / `venue_order_id` linkage) and order-status changes.
   - Handle snapshot vs update frames; ignore heartbeats/status.
3. Inject the token-fetch and the `connect` seam so the whole path is testable offline.

## Tests (mocks; no real private connection)

- A canned sequence of Kraken private frames (snapshot + an own-trade update) → parsed
  into the expected domain `Fill`s (exact Decimal fields, correct instrument/side).
- An order-status update frame → the expected order-update event.
- `on_connect` performs auth+subscribe (assert the subscribe message includes the token,
  fed by the mocked token-fetch); reconnect re-subscribes (self-heal).
- Heartbeat/status frames are ignored.

## Verification on real data

The **public** WS path is already proven (E2 ws leaf, real Kraken frame). The private
path needs a key → **deferred**: verify here against **realistic canned private frames**
(copy the shape from Kraken's v2 private docs) that parse to correct domain `Fill`s, and
state clearly that the live private connection is gated on credentials. **Run** the
mock-based suite.

## Closeout

- CHANGELOG (Added): "`brokers.KrakenPrivateWS` — private WS fills/order-update parsing (mock-verified; live gated on a key)."
- ADR: the private-WS auth-token flow + the deferred real-private-verification posture.
- Status/roadmap: **remove the E3 line** from `07-roadmap.md`; mark E3 done in `06-status.md`.
