---
plan: binance-adapter/02-binance-rest
kind: leaf
status: planned
complexity: high
depends: [01]
parallel: false
branch: feat/binance-rest
pr: ""
---

# BinanceBroker — REST (signing, orders, balances, fills, ticker) + testnet E2E

## Goal

`BinanceBroker` implementing the `Broker` port over Binance's spot REST API:
HMAC-**SHA256** request signing (verified vs Binance's published vector), public
market data (ticker, instrument precision) and the private order/balance/fills
calls, mapping domain `Order`/`Fill`/`Money`/`Instrument` to/from Binance
payloads. Sits on `transport.AsyncHTTPClient` + a generic `RateLimiter`.
**Testnet-capable** (configurable base URL) so an opt-in `network` E2E proves the
real round-trip. **No key required to build or run the unit suite** (mirrors the
Kraken posture); the testnet E2E skips without a key.

## Files to change

- `trading_bot/brokers/binance.py` — new; `BinanceBroker` (+ a pure `_sign` helper).
- `trading_bot/brokers/__init__.py` — export `BinanceBroker`.
- `trading_bot/application/service_factory.py` — add `"binance"` to `_LIVE_VENUES`,
  a `_build_live_venue` branch constructing `BinanceBroker()`, and the import.
- `trading_bot/tests/brokers/test_binance_rest.py` — new (mocks + signing vector +
  public smoke + testnet E2E).
- `.env.example` — add `BINANCE_API_KEY` / `BINANCE_API_SECRET` and the optional
  `BINANCE_API_BASE` (testnet toggle), documenting the testnet URL.

## Steps

1. **Read first** for parity: `trading_bot/brokers/kraken.py` (the sibling adapter
   — capability set, `_private_post`/`_public_get` shape, error mapping, money via
   `money(str(...))`) and `domain/instrument.py` (`parse_binance_symbol` from leaf
   01, `to_venue_symbol`, `normalise`).
2. **Signing** (`_sign(query: str, secret: str) -> str`, pure, no I/O/clock):
   `hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()`, where
   `query` is the **urlencoded** params including `timestamp` (ms) and
   `recvWindow`. The signed request appends `&signature=<sig>` and sends the
   `X-MBX-APIKEY: <key>` header. Credentials are env-sourced
   (`BINANCE_API_KEY` / `BINANCE_API_SECRET`), never from code, never logged; the
   broker is constructible without them (public-only / mocked).
3. **Base URL / testnet**: constructor `base_url: str | None = None` →
   defaults to `os.environ.get("BINANCE_API_BASE", "https://api.binance.com")`.
   Testnet = `https://testnet.binance.vision`. Both speak the same `/api/v3/*`
   paths. Expose `has_credentials` like Kraken.
4. **Public** (key-free): `GET /api/v3/ticker/price?symbol=` → `ticker()`
   (`money(str(price))`); `GET /api/v3/exchangeInfo?symbol=` → `instrument()`
   (read `filters`: `PRICE_FILTER.tickSize` / `LOT_SIZE.stepSize` → derive
   `price_precision` / `qty_precision` as decimal places, or fall back to
   `baseAssetPrecision` / `quoteAssetPrecision`).
5. **Private** (signed): `GET /api/v3/account` → `balances()` (parse the
   `balances: [{asset, free, locked}]` array → `{normalise(asset): money(free)}`,
   skip zero free); `POST /api/v3/order` → `place_order` (see step 6);
   `DELETE /api/v3/order` → `cancel_order` (see step 7);
   `GET /api/v3/openOrders` → `open_orders()` (account-wide; rebuild domain
   `Order`s, `venue_order_id` = composite, step 7);
   `GET /api/v3/myTrades?symbol=` → `fills()` (symbol-scoped, step 8).
6. **`place_order`** — map domain `Order` → Binance params: `symbol`
   (`to_venue_symbol("binance")`), `side` = `BUY`/`SELL` (upper-case of
   `order.side.value`), `type` = `MARKET`/`LIMIT`/`STOP_LOSS_LIMIT` from
   `OrderType`, `quantity` = `str(order.qty)`, `price` = `str(limit_price)` for
   LIMIT (+ `timeInForce=GTC`), `stopPrice` for stop. **Idempotency:** forward
   `order.client_order_id` as **`newClientOrderId`** *iff* it matches Binance's
   constraint (≤36 chars, charset `[.A-Za-z0-9:/_-]`); the runner's
   `f"{name}-{step}"` ids satisfy this. This gives **venue-level dedup** (Binance
   rejects a duplicate `newClientOrderId` with `-2010`). Send the POST with
   **`retry=False`** (at-most-once, like Kraken `AddOrder`): on an
   `AmbiguousRequestError` the caller reconciles — never blind-retries. Return the
   **composite** venue id (step 7). Raise `BrokerError` on a Binance error body
   (`{"code": -xxxx, "msg": "..."}`).
7. **Composite venue-order-id** — Binance `cancel`/`order`-status require a
   **`symbol`**, but the port's `cancel_order(venue_order_id)` carries only an id.
   Resolve this by making the adapter's venue id **`f"{SYMBOL}:{orderId}"`** (e.g.
   `"BTCUSDT:123456"`). `place_order` and `open_orders` both produce this form;
   `cancel_order` splits on the last `":"` → `DELETE /api/v3/order?symbol=&orderId=`.
   The id stays opaque to the router/reconcile/store (text), so this is
   self-contained. **ADR this.**
8. **`fills(since_ms)`** — Binance has **no account-wide trade history**;
   `myTrades` is **per-symbol**. Add a constructor arg `symbols: Iterable[Symbol] |
   None = None`; `fills()` queries `myTrades?symbol=` for each (with `startTime` =
   `since_ms`), concatenating results → domain `Fill`s (`money(str(...))` for
   qty/price/`commission`→fee; `time` ms → `ts`; `isBuyer` → side; `id`/`orderId`).
   If no symbols are configured **and** none can be derived, raise a clear
   `BrokerError` explaining `fills()` needs the symbol set. **ADR this.**
9. **Capabilities**: `{PLACE_ORDER, CANCEL, OPEN_ORDERS, BALANCES, FILLS, TICKER}`
   (omit `PRIVATE_WS` — WS deferred).
10. **Rate limit**: construct `AsyncHTTPClient(exchange="binance", limiter=
    RateLimiter())` (Binance weight-budget specifics are future work; the generic
    token bucket is enough here). Note this in the docstring.
11. **Wire `service_factory`**: `_LIVE_VENUES = ("kraken", "binance")`;
    `_build_live_venue` adds `if venue == "binance": return BinanceBroker()`;
    import `BinanceBroker`. Confirm a `binance` live config without credentials
    raises `BrokerError` (not a silent paper fallback) and that paper mode is
    untouched.

## Tests

- **Signing vs Binance's published vector** (deterministic, no key): secret
  `NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0`, query
  `symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559`
  → `signature == c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71`.
- **Mocks** (`pytest-httpx`) for every private call: assert the **request** (signed
  `signature` present and correct, `X-MBX-APIKEY` header, expected params incl.
  `newClientOrderId` forwarded) **and** the parsed domain objects (Decimal amounts,
  instrument, order/fill fields). Cover: `place_order` (market/limit/stop), the
  **composite id** returned and round-tripped through `cancel_order` (asserts the
  `DELETE` carries the right `symbol`+`orderId`), `open_orders`, `balances`,
  `fills` over a 2-symbol set.
- Domain `Order` → Binance `/order` param mapping (market/limit/stop-loss).
- A Binance error body (`{"code": -2010, "msg": "Account has insufficient
  balance."}`) → `BrokerError`.
- `newClientOrderId` is **omitted** when the id is incompatible (construct an order
  with a 40-char id) — assert it falls back gracefully (no `newClientOrderId` sent).
- `service_factory`: `mode=live`, `live_enabled=True`, `exchange=binance`, **no
  creds** → `BrokerError`; paper mode still yields `PaperBroker`.

## Verification on real data

Network IS reachable. **Two** real checks (mandatory — a green mock suite is not
enough for an execution adapter):

1. **Public smoke** (`@pytest.mark.network`, **no key**): call
   `GET /api/v3/ticker/price` and `GET /api/v3/exchangeInfo` for `BTC/USDT` live;
   assert `ticker()` parses a price > 0 and `instrument()` returns sane precision;
   assert the returned symbol round-trips `parse_binance_symbol` ↔
   `to_venue_symbol("binance")`. **Run it.**
2. **Testnet round-trip** (`@pytest.mark.network`, skips if `BINANCE_API_KEY`/
   `BINANCE_API_SECRET` absent): point `base_url` at `https://testnet.binance.vision`,
   then **place** a small LIMIT order far from market (won't fill), **read** it back
   via `open_orders()`/`balances()`, **cancel** it via the composite id, and assert
   the broker-reported state matches what was requested at every step (the order
   appears with the right symbol/side/qty/price; the cancel removes it). This is
   the **reconcile-against-broker-truth** check, on a real venue, paper money. If a
   testnet key is available in `.env`, **run it and paste the evidence**; otherwise
   report it as skipped and note the exact steps to run it.

## Closeout

- CHANGELOG (Added): "`brokers.BinanceBroker` — Binance spot REST adapter (HMAC-SHA256
  signed orders/balances/fills/ticker, `newClientOrderId` idempotency, testnet-capable),
  wired into `service_factory` as the 2nd live venue." (Changed: `service_factory`
  `_LIVE_VENUES` now includes `binance`.)
- ADR (PR #NN): (a) **composite venue-order-id** `"<SYMBOL>:<orderId>"` to satisfy
  Binance's symbol-scoped cancel under the symbol-free port; (b) **`fills()`
  symbol-scoping** via a configured symbol set (no account-wide trade history on
  Binance); (c) `newClientOrderId` venue-level idempotency (improves on Kraken) kept
  alongside the reconcile-on-ambiguous `retry=False` policy; (d) testnet-capable
  base URL + the testnet-E2E posture.
- Status: `06-status.md` — record Binance as the 2nd live venue (public key-free;
  private mock+vector+testnet-E2E; mainnet real-key still deferred behind the opt-in).
- Roadmap: **last leaf** — remove the E11 line from `07-roadmap.md`.
