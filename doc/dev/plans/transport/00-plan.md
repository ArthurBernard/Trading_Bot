---
plan: transport
kind: global
status: planning
roadmap: "- [ ] **E2 — Transport.** `AsyncHTTPClient` (httpx + retry/backoff), `WebSocketBase` (reconnect), `RateLimiter` (token-bucket / Kraken call-counter). Mirror dccd's transport."
release_on_done: false
---

# E2 — Transport

## Goal

Build `trading_bot/transport/` — the async I/O primitives every broker adapter
sits on, mirroring dccd's `transport/` layer
(`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/transport/`): an
`AsyncHTTPClient` (httpx wrapper with retry/backoff, GET **and POST** for order
placement), a `WebSocketBase` with `stream_raw()` + exponential reconnect (for
private order/fill streams), and a `RateLimiter` (token-bucket + a Kraken
**call-counter** model). Async, fully type-annotated. No domain coupling beyond
small transport-local errors. Auth/signing stays in the broker layer (E3) — transport
is venue-neutral plumbing.

## Decomposition

1. **http** — `AsyncHTTPClient` (httpx): async CM, `get`/`post`, retry/backoff, timeouts, optional `RateLimiter`.
2. **ws** — `WebSocketBase`: `stream_raw()` + exponential reconnect, `on_connect` hook, `send()`.
3. **ratelimit** — `TokenBucket` + `RateLimiter` (per-exchange) + Kraken decaying call-counter.

## Leaf checklist

- [ ] 01 http — feat/transport-http — medium
- [ ] 02 ws — feat/transport-ws — medium
- [ ] 03 ratelimit — feat/transport-ratelimit — medium

## Dependencies

- None between the three leaves (independent; run serially in the main worktree —
  the safe default). All assume E1 is merged (typing only).

## Done criteria

- `trading_bot/transport/` exposes `http`, `ws`, `ratelimit` with a clean `__init__`.
- `ruff` + `mypy` + `pytest` green; transport tests run locally (httpx + websockets
  installed, network reachable) and include a real public-endpoint smoke for http & ws.
- `pytest-httpx` added to the `[dev]` extra in `pyproject.toml`.
- Last leaf (03) removes the E2 line from `07-roadmap.md` and updates `06-status.md`.
