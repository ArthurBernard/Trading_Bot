# 04 — Brokers (capability matrix)

trading_bot talks to exchanges through a single **`Broker` port**
(`brokers/base.py`). Adapters declare what they support; the engine rejects
operations a broker hasn't declared. Multi-exchange is designed for from day one;
two live venues (Kraken, Binance) plus the in-process `PaperBroker` are shipped.

## Port surface

A `Broker` exposes (async): `place_order`, `cancel_order`, `replace_order`,
`open_orders`, `balances`, `fills` (or a fills stream), and — where the venue is
also the price source — market-data access. Every adapter wraps the shared
`transport/` primitives (httpx, websockets, token-bucket rate limiter). Adapters
are wired by `application/service_factory.py` (per-venue dispatch — there is no
`BrokerRegistry`).

## Matrix

| Broker | REST | WS | Order types | Status |
|--------|------|-----|-------------|--------|
| **Kraken** (`brokers/kraken.py`, `kraken_ws.py`) | ✅ | public (proven against real Kraken) + private executions/fills (`KrakenPrivateWS`, live conn deferred behind opt-in) | market, limit, stop-loss, best-limit | **shipped** |
| **Binance** (`brokers/binance.py`) | ✅ spot (HMAC-SHA256; **testnet-capable**) | — (public data key-free; private path mock+vector+testnet-E2E proven) | market, limit | **shipped** |
| **PaperBroker** (`brokers/paper.py`) | n/a (in-process) | n/a | mirrors the live order types it simulates | **default** (paper-trading) |
| others (Bitfinex, …) | — | — | — | declared, raise early until implemented |

Live real-key enablement (Kraken private endpoints + venue-level idempotency
against a real-key sandbox) is the one maintainer step left — see
[`06-status.md`](06-status.md) and [`07-roadmap.md`](07-roadmap.md). **No real
order is ever sent from the repo.**

## Kraken caveats

- **Call-counter rate limiting**: Kraken meters API calls with a decaying counter;
  the `RateLimiter` models it (`KrakenCallCounter` in `transport/ratelimit.py`).
- **Asset/pair naming**: Kraken's X/Z-prefixed codes vs altnames are normalised in
  `domain/instrument.py` (dccd solved the analogous mapping; the same approach).
- **Transient venue errors** (e.g. `EService:Unavailable`) are retried with
  backoff, not treated as fatal.
- **Private WS token**: Kraken's v2 private WebSocket is not signed per-frame; the
  client first fetches a short-lived WebSocket token from the private REST
  endpoint. The token is never logged.

## Binance caveats

- **HMAC-SHA256 signing**: private endpoints sign the query string
  (`signature = hmac_sha256(secret, query)`), a different scheme from Kraken's
  vector.
- **Composite venue-id** for symbol-scoped cancel; **`newClientOrderId`** carries
  idempotency.
- **Testnet**: point the base URL at `https://testnet.binance.vision` (same
  `/api/v3/*` paths) to exercise the signed round-trip; opt-in and key-gated.

## Adding an exchange

1. Implement the adapter under `brokers/` against the `Broker` port.
2. Wire it in `application/service_factory.py`.
3. Declare its capabilities honestly; raise early for anything unimplemented.
4. Add `-m network` E2E tests against the venue's sandbox where available.
