# Audit — brokers & transport (2026-07-02)

## Scope & method

Deep read-only audit of the layers that talk to real exchanges, on branch
`develop` (clean tree). Every file below was read in full:

- `trading_bot/brokers/`: `base.py`, `binance.py`, `kraken.py`, `kraken_ws.py`,
  `paper.py`, `__init__.py`
- `trading_bot/transport/`: `http.py`, `ratelimit.py`, `ws.py`, `__init__.py`
- their tests: `trading_bot/tests/brokers/{test_base,test_binance_rest,test_kraken_rest,test_kraken_ws,test_paper}.py`,
  `trading_bot/tests/transport/{test_http,test_ratelimit,test_ws}.py`
- supporting domain/application read for cross-checks: `domain/money.py`,
  `domain/errors.py`, `domain/instrument.py`, `application/order_router.py`,
  `application/reconcile.py`.

Checks were read-only: full reads, `grep` over call sites, and one offline test
run (`python -m pytest trading_bot/tests/brokers trading_bot/tests/transport -q`
— **145 passed, 5 deselected**, network tests never run). No source was
modified, nothing committed, no credentials accessed, no live API touched.

Audited against the repo invariants: idempotent submit (client-order-id +
retry=False), per-exchange rate limiting, retry/backoff taxonomy, secrets never
logged, all-money-Decimal, error-code mapping, signature/auth correctness, the
private-WS reconnect/resubscribe/reconcile path, PaperBroker fidelity, port
completeness, and HTTP transport hygiene (TLS, timeouts, pooling, JSON floats).

## Summary

The layer is well-structured and the *transport-level* idempotency guard is
correct and well-tested: `place_order` on both venues posts with `retry=False`,
so an ambiguous 5xx/429/dropped-connection raises `AmbiguousRequestError`
(reconcile, never blind-retry) and is provably sent at most once. Money parsing
is disciplined (`money(str(...))` everywhere on the wire; `money()` rejects
`float`). PaperBroker is port-pure and deterministic. The Kraken signing vector
and Binance signing vector both pass.

But there are two findings that put **real money at risk** and must be fixed
before any live trading. **B-1 (Critical): the signed URL — which for Binance
carries `&signature=<hmac>` and the full API query — is embedded verbatim into
`HTTPError`/`AmbiguousRequestError` messages and into every `logger.warning` in
`http.py`.** A single 5xx/429/timeout on any Binance call leaks the request
signature (and full parameters) into logs and exception traces, directly
violating "secrets never logged". **B-2 (Critical): neither broker quantizes
qty/price to the venue tick/lot before submitting** — `str(order.qty)` /
`str(order.limit_price)` are sent raw despite `Instrument.price_precision` /
`qty_precision` and a `quantize()` helper existing; an over-precise or
sub-min-lot size is silently sent, and a strategy that rounds *up* can oversell.
Then a set of High findings: the Kraken nonce has no monotonic guarantee under
concurrency or clock rollback (B-3); no venue error code → domain error mapping
exists (everything is a flat `BrokerError` string, so the engine cannot react to
insufficient-funds vs invalid-nonce vs rate-limit — B-4); Binance client-order-id
is dropped when it doesn't fit the venue charset, which then breaks
reconcile-by-`client_order_id` (B-5); and the Binance rate limiter is a flat
per-second token bucket that ignores Binance's weight budget and 418 IP-ban /
`Retry-After` semantics (B-6). Remaining items are Medium/Low/Info.

| ID | Severity | Location | Title |
|----|----------|----------|-------|
| B-1 | Critical | `transport/http.py:63-67,90-97,374,387,377-419` | Signed Binance URL (with HMAC signature) leaked into error messages and logs |
| B-2 | Critical | `brokers/kraken.py:482-494`; `brokers/binance.py:584-606` | Order qty/price never quantized to venue tick/lot before submit |
| B-3 | High | `brokers/kraken.py:237-242,300-304` | Kraken nonce not monotonic under concurrency / clock rollback; no lock |
| B-4 | High | `brokers/kraken.py:246-266`; `brokers/binance.py:328-341` | No venue-error-code → domain-error taxonomy; all failures flatten to `BrokerError` |
| B-5 | High | `brokers/binance.py:601-606,678`; `application/reconcile.py:182-197` | Dropped `newClientOrderId` breaks reconcile-by-client-order-id (double-ingest / false orphan) |
| B-6 | High | `transport/ratelimit.py:54-62`; `brokers/binance.py:56-61,268-270` | Binance limiter ignores weight budget, 418 IP-ban and `Retry-After` |
| B-7 | Medium | `brokers/binance.py:328-341,651-652` | `_raise_on_error` cannot detect a Binance error returned inside a JSON array |
| B-8 | Medium | `brokers/binance.py:691-696`; `brokers/kraken.py:570-576` | `open_orders` partial-fill uses limit/stop price as avg fill price (wrong PnL basis) |
| B-9 | Medium | `transport/http.py:371-382` | `retry=False` 429 raises immediately, discarding `Retry-After` (correct for dedup, but no signal to caller) |
| B-10 | Medium | `brokers/kraken_ws.py:145-158,416,433` | Private-WS carries no reconcile-on-fill-gap; `ts` parse failures silently become 0 |
| B-11 | Low | `transport/http.py:178-183` | No connection-pool limits, no explicit `trust_env`/proxy policy, no response-size cap; connect vs read timeout not distinguished |
| B-12 | Low | `brokers/kraken.py:542-544`; `brokers/binance.py:659-661` | Unknown venue order-type silently coerced to `LIMIT` on rebuild |
| B-13 | Low | `brokers/paper.py` | PaperBroker diverges from live semantics (instant full fill, no dedup, no min-notional/precision rejection) |
| B-14 | Info | tests | Named missing tests (see finding) |
| B-15 | Info | `brokers/kraken.py:467-494`; `brokers/binance.py:575-606` | Kraken never forwards a client-order-id venue-side (userref/cl_ord_id unused) |

## Findings

### B-1 [Critical] Signed Binance URL (with HMAC signature) leaked into error messages and logs

**Location:** `transport/http.py:63-67` (`HTTPError.__init__` → `f"HTTP {status} from {url}: {body[:200]}"`), `:90-97` (`AmbiguousRequestError.__init__` embeds `url`), `:374`/`:387` (pass `url` into those errors), `:377-379`, `:390-395`, `:413-419` (`logger.warning(... url ...)`). Produced by `brokers/binance.py:392-402`, where the signed request URL is built as `f"{base}/api/v3/{endpoint}?{query}&signature={signature}"` and handed to `client.request(method, url, ...)`.

**Evidence:** Binance signs on the *query string*; `_signed_request` puts `recvWindow`, `timestamp`, all order params **and** `&signature=<hex-hmac>` into `url`. The transport then, on any 429 (`:374`), 5xx (`:387`), or transport error (`:413`), logs that full `url` at WARNING and/or embeds it in an exception whose message is `f"HTTP {status} from {url}: ..."`. So a transient failure on *any* Binance private call (place/cancel/balances/fills/openOrders) writes the request signature and every parameter into the log stream and into any traceback that surfaces the exception. The module docstring and every broker docstring claim "Key material is never logged" / "Redact keys in any log line" — this breaks that invariant. (Kraken is not affected here: it signs in a header and posts the body, so its `url` is just the endpoint. But the leak is venue-neutral transport code, so the fix belongs there.) Note also httpx's own `httpx` logger, if enabled at DEBUG, logs request URLs — a second exposure vector the code does nothing to suppress.

**Impact:** A leaked HMAC-SHA256 signature is single-use (bound to that exact query + timestamp + recvWindow) so it is not directly replayable, but it is still secret request material, it reveals full account activity (sizes, prices, symbols) in plaintext logs, and logs are frequently shipped to third parties. This is exactly the class of leak the invariant forbids. Severity Critical because it is a secret-in-logs regression on the live-money path and fires on ordinary transient errors.

**Recommendation:** Redact the query/signature before logging or storing in an error. Simplest robust fix: in `_request`, log and store only a *sanitized* URL — strip the query string entirely (`httpx.URL(url).copy_with(query=None)`), or redact `signature`, `timestamp`, `recvWindow`. Better: have the broker pass signed params as `params=` (or a body) rather than pre-baking them into `url`, and have the transport never log query strings. Add a regression test asserting `signature` never appears in `HTTPError`/`AmbiguousRequestError` messages or in captured log records for a Binance 503/429/timeout.

### B-2 [Critical] Order qty/price never quantized to venue tick/lot before submit

**Location:** `brokers/kraken.py:482-494` (`_add_order_params`: `"volume": str(order.qty)`, `params["price"] = str(order.limit_price)` / `str(order.stop_price)`), `brokers/binance.py:584-606` (`_order_params`: `"quantity": str(order.qty)`, `"price": str(order.limit_price)`). The router submits the order verbatim (`application/order_router.py:279 venue_id = await self._broker.place_order(order)`) with no rounding step anywhere upstream.

**Evidence:** `Instrument` carries `price_precision` / `qty_precision` (`domain/instrument.py:359-361`) and `domain/money.py` provides `quantize(value, tick, rounding=ROUND_DOWN)`, but nothing in the order path calls it. The brokers only *read* those precisions when *building* an instrument (`kraken.py:345-355`, `binance.py:443-457`); they never *apply* them when placing an order. A signal-derived quantity like `Decimal("0.123456789123")` is sent as-is. Two failure modes: (a) Binance rejects an order whose quantity is finer than `LOT_SIZE.stepSize` or price finer than `PRICE_FILTER.tickSize` (`-1013`/`-2010`), or below `MIN_NOTIONAL` — an order the strategy believes it placed silently never rests; (b) worse, if the value is rounded *up* (or the venue rounds up), a sell can dispatch more base than the position holds → oversell. The audit prompt calls this out explicitly ("rounding the wrong way can oversell or get orders rejected"). Kraken's `pair_decimals`/`lot_decimals` and Binance's `stepSize`/`tickSize`/`minQty`/`minNotional` are captured nowhere in the submit path.

**Impact:** On the live path this yields silently-rejected orders (strategy diverges from reality) or, in the round-up case, an oversell that breaches position/risk assumptions. Real money. Severity Critical.

**Recommendation:** Quantize in the broker's `_*_order_params` (the venue-specific boundary) using the instrument's precision — `quantize(order.qty, Decimal(1).scaleb(-qty_precision), rounding=ROUND_DOWN)` for size, price toward the passive side (buy price ROUND_DOWN, sell price ROUND_UP is safer, or ROUND_DOWN uniformly with a documented rationale), and reject (raise `BrokerError`/a new `OrderRejected`) when the rounded size is below the venue minimum rather than sending a doomed order. Fetch and cache `minQty`/`minNotional` (Binance) and `ordermin` (Kraken `AssetPairs`). Add tests: over-precise qty is rounded down before submit; a below-min order raises rather than submitting; a sell is never rounded up past `remaining_qty`.

### B-3 [High] Kraken nonce not monotonic under concurrency / clock rollback; no lock

**Location:** `brokers/kraken.py:237-242` (`_nonce` = `str(int(time.time() * 1_000_000))`), used at `:300-304` inside `_private_post` with no lock.

**Evidence:** Kraken requires every private request's nonce to be strictly greater than the previous one for that key, else it returns `EAPI:Invalid nonce`. The nonce here is wall-clock microseconds. Three problems: (1) **Concurrency** — `_private_post` takes no lock, and the broker is explicitly designed for concurrent use (the shared `AsyncHTTPClient` is reference-counted for "two concurrent operations", `http.py:168-174`). Two private calls scheduled in the same microsecond (plausible on a fast host, and `time.time()` resolution is not guaranteed to be sub-microsecond) get equal nonces → the second is rejected. (2) **Clock rollback** — `time.time()` is not monotonic; an NTP step backwards produces a nonce ≤ a prior one, rejecting every private call until wall-clock catches up. (3) The docstring claims "monotonically-increasing" but nothing enforces it. Contrast the deliberate monotonic seams elsewhere (`time.monotonic` in `ratelimit.py`).

**Impact:** Intermittent `EAPI:Invalid nonce` rejections on live private calls. For reads this is a retried transient annoyance; for `AddOrder` (which is `retry=False`) a nonce collision surfaces as a hard `BrokerError` and the order never places — a missed trade, or (combined with B-4) an error the engine cannot distinguish from a real rejection. Severity High.

**Recommendation:** Make the nonce a process-wide strictly-increasing counter guarded by a lock: keep `max(last_nonce + 1, int(time.time()*1e6))` under an `asyncio.Lock` (or `itertools.count` seeded from the clock). This guarantees monotonicity across concurrent calls and survives a backwards clock step. Add a test that two concurrent `_private_post` calls produce distinct increasing nonces, and that a backwards clock still yields an increasing nonce.

### B-4 [High] No venue-error-code → domain-error taxonomy; all failures flatten to `BrokerError`

**Location:** `brokers/kraken.py:246-266` (`_raise_on_error` joins Kraken's `error` strings into one `BrokerError`), `brokers/binance.py:328-341` (`_raise_on_error` → `BrokerError(f"... {msg} (code {code})")`).

**Evidence:** The domain defines precise errors — `InsufficientFunds`, `RiskLimitBreached`, `MissingOrder`, etc. (`domain/errors.py`) — but neither broker maps venue codes onto them. Kraken `EOrder:Insufficient funds`, `EAPI:Invalid nonce`, `EAPI:Rate limit exceeded`, `EService:Unavailable`, `EOrder:Invalid price` all collapse to the same opaque `BrokerError` distinguished only by substring. Binance `-2010` (insufficient balance), `-1021` (timestamp/recvWindow), `-1013`/`-2010` (filter failures), `-1003` (too many requests / IP ban) likewise. The audit brief specifically asks for the mapping and for the "EServiceUnavailable fix" to be robust: recent commits (`d1cfff3 fix error with EServiceUnvailable from Kraken exchange`) suggest this was handled reactively as a string, but there is **no** code in the audited layer that classifies `EService:Unavailable` — it flows through as a generic `BrokerError` and, since it arrives in Kraken's `error` array on an HTTP 200 (Kraken returns 200 with an error body), it never even reaches the transport's 5xx retry path. So a Kraken service-unavailable on `AddOrder` is a terminal `BrokerError`, not a retry, and on a read it is also terminal (no retry, because HTTP was 200). Unknown codes are surfaced (good — not swallowed) but as untyped strings the engine cannot branch on.

**Impact:** The engine/risk layer cannot react differently to "insufficient funds" (stop the strategy) vs "invalid nonce/rate limit" (retriable) vs "service unavailable" (retriable) vs "invalid price" (fix and resubmit). A retriable Kraken `EService:Unavailable` / `EAPI:Rate limit exceeded` returned on HTTP 200 is treated as a hard failure. Severity High (invariant: "error taxonomy mapping; unknown errors must not be swallowed" — they are not swallowed, but they are un-typed, and retriable venue errors on HTTP-200 bodies are not retried).

**Recommendation:** Add a venue-code → domain-error classifier in each broker's `_raise_on_error`: map insufficient-funds codes to `InsufficientFunds`, and introduce (or reuse) retriable-venue-error and rate-limit domain errors so the transport/caller can retry Kraken's HTTP-200 `EService:Unavailable`/`EAPI:Rate limit exceeded` and Binance `-1003`. Keep the raw code/message on the exception for diagnostics. Add tests per code class, including a Kraken 200-with-`EService:Unavailable` that is classified retriable.

### B-5 [High] Dropped `newClientOrderId` breaks reconcile-by-client-order-id

**Location:** `brokers/binance.py:601-606` (forward `newClientOrderId` only if `_is_valid_new_client_order_id`), `:678` (`open_orders` rebuild sets `client_order_id = str(info.get("clientOrderId") or info.get("orderId"))`), consumed by `application/reconcile.py:182-197` (`venue_open_cids = {o.client_order_id for o in open_orders}`; `router.ingest`/orphan logic keyed on `client_order_id`).

**Evidence:** When the domain `client_order_id` fails Binance's charset/length constraint (`>36` chars or outside `[.A-Za-z0-9:/_-]`), the adapter simply omits `newClientOrderId` (documented at `:127-132` and `:601-604`). Binance then assigns its own random `clientOrderId`. On the next `reconcile()`, `open_orders()` rebuilds that order with `client_order_id` = Binance's generated id — which does **not** match the router's tracked id. Reconcile therefore (a) *ingests* it as a brand-new unknown order (`reconcile.py:189-196`, `already=False` → `ingested`), and (b) sees the router's original tracked order absent from `venue_open_cids` and, if non-terminal, treats it as an **orphan** and closes+forgets it (`reconcile.py:199-211`). The single live venue order is now double-counted and the engine's original handle is dropped. This defeats the very idempotency/reconcile guarantee the venue-side dedup was meant to provide, precisely in the case where venue-side dedup was skipped. The `-2010` "duplicate id" dedup the docstring relies on (`binance.py:208-209`) also does nothing here, since no id was sent.

**Impact:** Position/order state diverges from the venue after any reconcile (startup, post-disconnect) whenever an over-long/invalid client-order-id is used. The runner's `f"{name}-{step}"` ids happen to fit, so this is latent — but any strategy or manual order with a UUID-style or long id triggers it. Severity High (idempotency + "reconcile, never assume" invariants).

**Recommendation:** Never silently drop the idempotency key. Either (a) derive a deterministic venue-compatible id from the domain id (e.g. a stable hash truncated to the charset/length) and always forward it, storing the mapping so reconcile can match; or (b) refuse to place an order whose id cannot be forwarded (raise), forcing callers to use compatible ids. Add a test: place with an over-long id, then `open_orders`/reconcile round-trips to the *same* tracked order (no double-ingest, no orphan-close).

### B-6 [High] Binance limiter ignores weight budget, 418 IP-ban and `Retry-After`

**Location:** `transport/ratelimit.py:54-62` (`binance: 10.0` flat req/s), `brokers/binance.py:56-61` (docstring admits "weight-budget accounting is future work"), `:268-270` (wires a generic `RateLimiter`). Transport 429 handling: `http.py:371-382`; **418 is not handled at all** (falls into the `>= 400` branch at `:400-401` → non-retryable `HTTPError`).

**Evidence:** Binance's limit is a **weight** budget (1200 weight/min per IP; `exchangeInfo` costs 10-40 weight, `myTrades` 10-20, `order` 1, etc.) plus an order-rate limit, not a flat 10 req/s. The limiter here consumes exactly one token per request regardless of endpoint weight, so a burst of heavy calls (e.g. per-symbol `myTrades` in `fills()`, `:733-741`) can blow the weight budget while staying under 10 req/s. On breach Binance returns **429** (respected as a backstop, retried) then, if ignored, **418** (IP ban with a `Retry-After`). The transport treats 418 as an ordinary 4xx → immediate non-retryable `HTTPError`, and does not read `Retry-After` on 418; worse, it will keep firing other requests (limiter unaware of the ban) and deepen the ban. The `X-MBX-USED-WEIGHT-*` / `Retry-After` response headers are never inspected.

**Impact:** Under load the account's IP gets rate-limit-banned; the engine keeps hammering (extending the ban) and surfaces 418s as hard errors. Reads and, transiently, order placement fail. Severity High ("never exceed the venue's budget"; 418 handling).

**Recommendation:** Implement weight-aware accounting for Binance (a token bucket sized in weight/min, debited per-endpoint weight; a separate order-rate bucket), read `X-MBX-USED-WEIGHT-1M` to stay honest, and handle **418** explicitly in the transport: honour its `Retry-After`, and treat it as a hard back-off that also pauses the limiter. At minimum, add 418 to the `Retry-After`-honouring branch alongside 429. Tests: a weighted burst is paced under the weight budget; a 418 with `Retry-After` backs off and does not immediately raise.

### B-7 [Medium] `_raise_on_error` cannot detect a Binance error returned inside a JSON array

**Location:** `brokers/binance.py:328-341` (`_raise_on_error` only checks `isinstance(payload, dict) and "code" in payload`), used by `open_orders` (`:651-652`, iterates `payload` as a list) and `fills` (`:738-740`).

**Evidence:** The error check only fires for a top-level dict with `code`+`msg`. `open_orders`/`fills`/some list endpoints expect a JSON *array*; if Binance returns an error object there it is caught, but if any endpoint returns a list whose elements carry errors, or a shape the code then indexes (`symbols[0]` at `binance.py:439`) that is empty/malformed, the guard misses it and a later `.get`/index raises a raw `TypeError`/`IndexError` rather than a clean `BrokerError`. `exchangeInfo` empty-symbols is handled (`:435-438`), but the general list path is not defended.

**Impact:** A malformed/unexpected Binance response surfaces as an untyped Python exception instead of a `BrokerError`, harder to classify and reconcile. Medium.

**Recommendation:** Normalize: if `_signed_request`/`_public_get` returns a non-list where a list is expected (or vice-versa), raise `BrokerError` with the raw payload (redacted). Defend the `symbols[0]`/list iterations. Test with a `{"code":-1121,"msg":"Invalid symbol."}` body returned where a list is expected.

### B-8 [Medium] `open_orders` partial-fill uses order price as avg fill price (wrong PnL basis)

**Location:** `brokers/binance.py:691-696` (`avg_price = limit_price or stop_price; order.apply_fill(executed, avg_price)`), `brokers/kraken.py:570-576` (`avg_price = info.get("price") or descr.get("price") or "0"`).

**Evidence:** When rebuilding a partially-filled open order, both adapters synthesize the average fill price from the order's *limit/stop* price, not the venue's reported *executed average*. Binance provides `cummulativeQuoteQty` (÷ `executedQty` = true avg); Kraken's open-order `info["price"]` is the average executed price for the *order type that supports it* but `descr["price"]` is the limit — the `or` chain can pick the limit. Since "fills are the source of truth for PnL", rebuilding filled quantity at the limit price rather than the actual execution price puts a wrong cost basis into any position derived from `open_orders`. (Reconcile rebuilds positions from `fills()` not `open_orders`, `reconcile.py:216`, which mitigates it — but any consumer of `open_orders().avg_fill_price` gets a wrong number.)

**Impact:** Incorrect avg-fill/PnL for partially-filled orders read via `open_orders`. Medium (mitigated by reconcile using `fills()` for positions, but the order object itself is wrong).

**Recommendation:** Use `cummulativeQuoteQty/executedQty` (Binance) and Kraken's executed-average field for the rebuilt `apply_fill` price; fall back to the limit only when the venue reports no execution average. Test a partial fill whose execution avg differs from the limit price.

### B-9 [Medium] `retry=False` 429 raises immediately, discarding `Retry-After`

**Location:** `transport/http.py:371-375` (429 with `retry=False` → `AmbiguousRequestError` immediately, no `Retry-After` read).

**Evidence:** For a non-idempotent POST a 429 is (correctly) not retried, but the `Retry-After` the venue supplied is dropped. The caller only learns "ambiguous, reconcile", with no hint how long to wait. For an order POST this is defensible (never blind-retry), but the caller has no structured way to honour the venue's back-off before its reconcile-then-resubmit.

**Impact:** The engine may reconcile-and-resubmit into a still-active rate limit. Medium.

**Recommendation:** Carry the `Retry-After` value on `AmbiguousRequestError` (e.g. a `retry_after: float | None` attribute) so the caller can pace its reconcile/resubmit. Not a blind retry — just surfacing the hint.

### B-10 [Medium] Private-WS has no fill-gap reconcile; timestamp parse failures silently become 0

**Location:** `brokers/kraken_ws.py:265-291` (`on_connect` re-subscribes and calls the optional `on_connected` reconcile hook), `:145-158` (`_parse_iso_ms` returns 0 on parse failure), `:416`,`:433` (`ts=_parse_iso_ms(...)`).

**Evidence:** Reconnect + resubscribe + token refresh are correct and tested (`test_reconnect_refetches_token_and_resubscribes`), and there is an `on_connected` hook a caller *may* wire to reconcile after each reconnect (`:287-291`, failure logged not fatal — good). But: (1) the hook is optional and the *default* wiring (`from_broker`) does not supply one, so a caller who forgets it silently loses any fills that executed during the disconnect gap — the subscription re-establishes but Kraken's `executions` snapshot on resubscribe (`snap_trades=True`) is the only recovery, and there is no assertion/documentation that the snapshot back-fills the gap reliably. (2) `_parse_iso_ms` maps an unparseable/missing timestamp to `0` silently (`:152-158`); the docstring says `ts` is "record-keeping only", but a `Fill` with `ts=0` fed to `fills(since_ms=...)` filtering (`paper.py:583`, and any store query) would be wrongly included/excluded. Message ordering is trusted as arrival order with no sequence check.

**Impact:** Possible missed fills across a reconnect if the caller does not wire the reconcile hook; `ts=0` fills can corrupt time-window queries. Medium.

**Recommendation:** Make the reconcile-on-reconnect the default (wire `on_connected` to `reconcile()` in `from_broker`, or document loudly that a live caller must). Have `_parse_iso_ms` log a warning on parse failure rather than silently returning 0, or reject the fill. Consider validating Kraken v2 sequence numbers if present.

### B-11 [Low] Transport: no pool limits, no explicit proxy/`trust_env`, no response-size cap, connect vs read timeout not distinguished

**Location:** `transport/http.py:178-183` (`httpx.AsyncClient(base_url=..., timeout=self._timeout, headers=..., follow_redirects=True)`).

**Evidence:** TLS verification is on (httpx default `verify=True` — good; no `verify=False` anywhere). But: (1) `timeout` is a single scalar applied to all phases — connect-timeout and read-timeout are not distinguished, yet the audit brief flags that a **read**-timeout on an order submit means UNKNOWN outcome (must reconcile) whereas a **connect**-timeout means the request likely never left (safe to retry). Both currently map to `httpx.TransportError` → `AmbiguousRequestError` on `retry=False`, which is *safe* (treats both as ambiguous) but *over-conservative* for connect-timeouts and gives no finer signal. (2) No `httpx.Limits` (max connections / keepalive) is set, so pool behaviour is httpx defaults. (3) `follow_redirects=True` on a signed API client is mildly risky (a redirect could replay the signed query to another host); Binance/Kraken never redirect, so low. (4) No cap on response body size before `resp.json()` (`:403`) — a hostile/huge body is fully buffered and parsed. (5) `trust_env` is default `True`, so `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` and `SSL_CERT_FILE` are honoured implicitly — fine if intended, but undocumented.

**Impact:** Low individually. The connect/read distinction is the most material (finer retry decisions); the others are hardening.

**Recommendation:** Use `httpx.Timeout(connect=..., read=..., write=..., pool=...)`; distinguish `httpx.ConnectTimeout`/`ConnectError` (likely-not-sent → could retry even for orders) from `httpx.ReadTimeout` (sent, unknown → must reconcile) in `_request`. Set explicit `httpx.Limits`. Consider `follow_redirects=False` for signed clients. Document the `trust_env`/proxy posture.

### B-12 [Low] Unknown venue order-type silently coerced to `LIMIT` on rebuild

**Location:** `brokers/kraken.py:542-544` (`_KRAKEN_TO_ORDERTYPE.get(..., OrderType.LIMIT)`), `brokers/binance.py:659-661` (`_BINANCE_TO_ORDERTYPE.get(..., OrderType.LIMIT)`).

**Evidence:** An open order of a type the map doesn't cover (Kraken `take-profit`, `settle-position`, `trailing-stop`; Binance `TAKE_PROFIT`, `LIMIT_MAKER` is mapped, `TAKE_PROFIT_LIMIT` is not) is rebuilt as a plain `LIMIT`, losing its true semantics. Reconcile then adopts a mislabeled order.

**Impact:** Misclassified open orders after reconcile for uncommon order types. Low (MVP uses market/limit/stop only).

**Recommendation:** Raise/skip-with-warning on an unmapped venue order type rather than defaulting to `LIMIT`, or extend the maps. Test an unmapped type.

### B-13 [Low] PaperBroker diverges from live semantics (validates strategies that behave differently live)

**Location:** `brokers/paper.py` — fill model (`:313-367`), no client-order-id dedup, no precision/min-notional rejection, immediate full fill at limit price.

**Evidence:** PaperBroker is faithful within its stated model (port-pure, exact Decimal, deterministic, partial-fill support — all well tested). But vs a live venue it: (a) fills a LIMIT order **immediately and fully at its limit price** regardless of market (`_execution_price`, `:369-384`) — live, a limit far from market never fills; (b) does **no** client-order-id dedup, so re-placing the same domain order id twice creates two paper orders, whereas Binance would reject `-2010` (so the paper broker does not exercise the venue-side dedup guard the invariants rely on); (c) does **not** enforce tick/lot precision or min-notional (docstring admits fees are not quantized) — so B-2's oversell/reject conditions are invisible in paper. These divergences mean a strategy validated on paper can behave differently live. The audit brief flags paper/live divergence as a High concern *in principle*; scored Low here because each divergence is documented and the paper broker is explicitly a simulator, but they are worth tracking.

**Impact:** Green paper runs can mask live-only failures (unfilled far limits, duplicate-id rejects, precision rejects). Low-to-Medium.

**Recommendation:** Add an optional stricter paper mode: (a) only fill a LIMIT when the mark crosses the limit; (b) dedup on `client_order_id` and reject a duplicate (mirroring `-2010`); (c) reject sub-min-lot / over-precise orders once instrument precision is known (couples with B-2). At minimum document these divergences in the go-live checklist.

### B-14 [Info] Named missing tests

**Location:** `trading_bot/tests/brokers/`, `trading_bot/tests/transport/`.

**Evidence:** The suite is strong on signing vectors, the `retry=False` ambiguity path, rate-limit timelines, and paper fidelity. Gaps that map to the findings above and to invariants:
- **Read-timeout vs connect-timeout on order submit** — no test distinguishing them; both are lumped as ambiguous (relates B-11). Specifically: "read-timeout on `place_order` → `AmbiguousRequestError` (reconcile)" and "connect-timeout could be retried" have no coverage.
- **Secret redaction** — no test asserting a Binance `signature`/key never appears in a log record or exception message on 5xx/429/timeout (relates B-1).
- **Quantization** — no test that an over-precise qty/price is rounded to the instrument tick/lot before submit, nor that a sub-min order is rejected (relates B-2).
- **Nonce monotonicity** — no test for concurrent `_private_post` nonces, nor for a backwards clock (relates B-3).
- **Error taxonomy** — no test mapping `EOrder:Insufficient funds`/`-2010` to `InsufficientFunds`, nor Kraken 200-with-`EService:Unavailable` being retriable (relates B-4).
- **Reconcile with dropped client-order-id** — no test that an over-long id round-trips through `open_orders`/reconcile without double-ingest/orphan (relates B-5).
- **418 IP-ban handling** for Binance (relates B-6).
- **Malformed list response** raising `BrokerError` not `TypeError` (relates B-7).

**Recommendation:** Add the above; each is a small, offline, mock-driven test.

### B-15 [Info] Kraken never forwards a client-order-id venue-side

**Location:** `brokers/kraken.py:467-494` (`_add_order_params` omits `userref`/`cl_ord_id`; documented at `:474-481`).

**Evidence:** The adapter deliberately does not send Kraken's `userref` (32-bit int) or `cl_ord_id` (UUID) because the domain id is arbitrary text, relying instead on `retry=False` + engine-side `OrderRouter` dedup. This is a *defensible design choice* and is documented, but it means Kraken has **no venue-side dedup** for `AddOrder` — the audit brief explicitly asks that client-order-id be passed to the venue "on EVERY order type for BOTH Kraken (userref/cl_ord_id) and Binance". So Kraken's idempotency rests entirely on the transport `retry=False` guard (correct) plus the local router map; there is no second, venue-enforced guard as there is for Binance (`-2010`). Combined with B-3 (a nonce collision surfacing on `AddOrder`), the only defenses against a Kraken duplicate are transport-level. Info, not a bug — but worth an explicit ADR, and consider deriving a stable 32-bit `userref` from the domain id (e.g. `crc32(client_order_id)`) to add the venue-side guard Kraken *does* offer.

**Recommendation:** Either derive and forward a `userref` (e.g. `zlib.crc32(client_order_id.encode()) & 0x7FFFFFFF`) to gain Kraken venue-side dedup + easier reconcile matching, or record an ADR justifying transport-only idempotency for Kraken. If forwarded, reconcile can match on `userref` and B-3's risk is partly mitigated.
