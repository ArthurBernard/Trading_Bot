---
plan: broker-kraken/02-kraken-rest
kind: leaf
status: done
complexity: high
depends: [01]
parallel: false
branch: feat/broker-kraken-rest
pr: ""
---

# KrakenBroker — REST (signing, orders, balances, fills)

## Goal

`KrakenBroker` implementing the `Broker` port over Kraken's REST API: request
**signing** (nonce + HMAC-SHA512 `API-Sign`), the public market-data calls and the
private order/balance/fills calls, mapping domain `Order`/`Fill`/`Money`/`Instrument`
to and from Kraken payloads. Sits on `transport.AsyncHTTPClient` +
`RateLimiter`/`KrakenCallCounter`. **No API key required to build or test** (see posture).

## Files to change

- `trading_bot/brokers/kraken.py` — new; `KrakenBroker` (+ a `_sign` helper).
- `trading_bot/brokers/__init__.py` — export `KrakenBroker`.
- `trading_bot/tests/brokers/test_kraken_rest.py` — new.
- `.env.example` — new; document `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` (real `.env` is gitignored).

## Steps

1. Read `trading_bot/legacy/exchanges/API_kraken.py` (`set_sign`/`_nonce`/`query_private`)
   and dccd's `sources/kraken.py` (public calls, pair rendering). Reuse
   `domain.instrument` for pair normalisation and `transport` for I/O.
2. **Signing** (`_sign(path, data, secret)`): `post = urlencode(data)`;
   `message = path.encode() + sha256((nonce + post).encode())`;
   `sig = base64(hmac_sha512(b64decode(secret), message))`. Headers: `API-Key`,
   `API-Sign`. Credentials read from env (`KRAKEN_API_KEY`/`KRAKEN_API_SECRET`); the
   broker is constructible without them for public-only/mocked use.
3. **Public**: `AssetPairs` (tick/lot precision → `Instrument`), `Ticker` (→ `ticker()`),
   optional `OHLC`. **Private**: `Balance` (→ `balances()`), `AddOrder` (→ `place_order`,
   mapping side/type/ordertype/price/volume from domain `Order`), `CancelOrder`
   (→ `cancel_order`), `OpenOrders` (→ `open_orders()` rebuilding domain `Order`s),
   `ClosedOrders`/`TradesHistory` (→ `fills()` building domain `Fill`s). Use the rate
   limiter with the right per-endpoint cost.
4. Map errors: Kraken's `error` array → `BrokerError`/`OrderError`; transient
   `EService:Unavailable` etc. left to transport retry.

## Tests

- **Signing vs Kraken's published test vector** (deterministic, no key): secret
  `kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==`,
  nonce `1616492376594`, path `/0/private/AddOrder`, data
  `ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25` →
  `API-Sign == 4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==`.
- `place_order`/`cancel_order`/`open_orders`/`balances`/`fills` via **`pytest-httpx` mocks**
  of Kraken JSON responses → assert the request payload (signed headers present, body
  fields) and the parsed domain objects (Decimal amounts, instrument, order/fill fields).
- Domain `Order` → Kraken `AddOrder` payload mapping for market/limit/stop-loss.
- A Kraken `error` response → `BrokerError`.

## Verification on real data

Network IS reachable. Real **public** smoke (`@pytest.mark.network`, no key): call
`AssetPairs`/`Ticker` for `BTC/USD` live and assert a sane parse (price > 0,
instrument precision present). **Run it.** Private endpoints are mock-only here
(no key) — note this explicitly; signing correctness is proven by the test vector.

## Closeout

- CHANGELOG (Added): "`brokers.KrakenBroker` — REST adapter (signed orders/balances/fills, public market data)."
- ADR: signing scheme (verified vs Kraken's vector), env-based credentials, the
  mock+public-only test posture (private verification deferred).
- Status/roadmap: deferred to leaf 03.
