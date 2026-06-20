---
plan: broker-kraken
kind: global
status: planning
roadmap: "- [ ] **E3 — Broker port + Kraken adapter.** `Broker` protocol (place/cancel/replace, open orders, balances, fills, market data) + registry; `KrakenBroker` (REST first, WS fills next). Other venues declared, not implemented."
release_on_done: false
---

# E3 — Broker port + Kraken adapter

## Goal

Define the venue-neutral **`Broker` port** (+ registry + capability model) that the
execution engine talks to, and implement the **Kraken** adapter over it — REST first
(orders/balances/fills), then the private WebSocket (own-trades / order updates).
Mirrors dccd's `sources/` shape (base protocol + registry + per-exchange adapter:
`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/sources/`). Sits on the E2
transport (`AsyncHTTPClient`, `WebSocketBase`, `RateLimiter`/`KrakenCallCounter`) and
speaks domain types (`Order`, `Fill`, `Instrument`, `Money`).

**Posture (decided):** mocks + **real public** endpoint smokes only — **no API key**.
Authenticated (private) endpoints are implemented and tested via **mocks**; the
**signing** is verified against **Kraken's published signature test vector**
(deterministic, no key). Real private verification (balances, live orders) is
**deferred** until a key is provided. Secrets live in `.env` (gitignored), are never
logged or committed.

## Decomposition

1. **broker-port** — `brokers/base.py` `Broker` protocol + capabilities; `brokers/registry.py`.
2. **kraken-rest** — `KrakenBroker` REST: signing (vs Kraken test vector), orders/balances/fills, domain mapping.
3. **kraken-ws** — Kraken private WS (own-trades/openOrders) parsed into domain `Fill`/order updates.

## Leaf checklist

- [ ] 01 broker-port — feat/broker-port — high
- [ ] 02 kraken-rest — feat/broker-kraken-rest — high (depends on 01)
- [ ] 03 kraken-ws — feat/broker-kraken-ws — high (depends on 02)

## Dependencies

- 02 depends on 01; 03 depends on 02. (Serial.)

## Done criteria

- `trading_bot/brokers/` exposes the `Broker` port, a `BrokerRegistry`, and `KrakenBroker`.
- `ruff` + `mypy` + `pytest` green; signing matches Kraken's documented test vector;
  private endpoints covered by mocks; a real **public** Kraken smoke (`-m network`) passes.
- Secrets only via env; no key material in the repo or logs.
- Last leaf (03) removes the E3 line from `07-roadmap.md` and updates `06-status.md`.
