# 04 — Brokers (capability matrix)

trading_bot talks to exchanges through a single **`Broker` port** (target:
`brokers/base.py`). Adapters declare what they support; the engine rejects
operations a broker hasn't declared. Multi-exchange is designed for from day one;
only Kraken is implemented at MVP.

## Port surface (target)

A `Broker` exposes (async): `place_order`, `cancel_order`, `replace_order`,
`open_orders`, `balances`, `fills` (or a fills stream), and — where the venue is
also the price source — market-data access. Every adapter wraps the shared
`transport/` primitives (httpx, websockets, token-bucket rate limiter).

## Matrix

| Broker | REST | WS (private fills) | Order types | Status |
|--------|------|--------------------|-------------|--------|
| **Kraken** (`brokers/kraken.py`) | ✅ | planned | market, limit, stop-loss | **MVP target — being built** |
| **PaperBroker** (`brokers/paper.py`) | n/a (in-process) | n/a | mirrors the live order types it simulates | **default** |
| Bitfinex (`legacy/exchanges/API_bfx.py`) | legacy ref | — | — | declared, not implemented |
| others | — | — | — | declared, not implemented |

## Kraken caveats (mined from `legacy/exchanges/API_kraken.py`)

- **Call-counter rate limiting**: Kraken meters API calls with a decaying counter;
  the `RateLimiter` must model it (legacy `tools/call_counters.py` →
  `transport/ratelimit.py`).
- **Asset/pair naming**: legacy X/Z-prefixed codes vs altnames — normalise in
  `domain/instrument.py` (dccd already solved the analogous mapping; mirror it).
- **Transient venue errors** (e.g. `EService:Unavailable`) must be retried with
  backoff, not treated as fatal (a recurring legacy pain point — see git history).

## Adding an exchange

1. Implement the adapter under `brokers/` against the `Broker` port.
2. Register it in `application/service_factory.py`.
3. Declare its capabilities honestly; raise early for anything unimplemented.
4. Add `-m network` E2E tests against the venue's sandbox where available.
