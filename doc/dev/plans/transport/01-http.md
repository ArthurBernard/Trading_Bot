---
plan: transport/01-http
kind: leaf
status: done
complexity: medium
depends: []
parallel: false
branch: feat/transport-http
pr: "#13"
---

# AsyncHTTPClient — httpx wrapper with retry/backoff

## Goal

`trading_bot/transport/http.py`: a thin async httpx wrapper with retry/backoff and
timeouts, exposing `get` **and** `post` (order placement needs POST). Mirrors
dccd's `AsyncHTTPClient` (`/home/arthur/dev/Download_Crypto_Currencies_Data/dccd/transport/http.py`),
adapted for execution. Async, fully typed.

## Files to change

- `trading_bot/transport/__init__.py` — new; export `AsyncHTTPClient`, `HTTPError`.
- `trading_bot/transport/http.py` — new.
- `trading_bot/tests/transport/__init__.py` — new (empty).
- `trading_bot/tests/transport/test_http.py` — new.
- `pyproject.toml` — add `pytest-httpx` to the `[dev]` extra.

## Steps

1. Read dccd's `transport/http.py` for the pattern. Implement `AsyncHTTPClient`:
   - `__init__(self, *, base_url=None, max_retries=3, backoff_base=0.5, timeout=10.0, headers=None, exchange=None, limiter=None)`.
   - Async context manager (`__aenter__`/`__aexit__`) holding an `httpx.AsyncClient`; reference-count nested entry (`_depth`) like dccd.
   - `async get(url, params=None) -> Any` and `async post(url, *, data=None, json=None, headers=None) -> Any` — both with retry/backoff on transient errors (5xx, `httpx` network errors) and `Retry-After` handling on 429; raise `HTTPError(status, url, body)` on non-retryable 4xx; return parsed JSON.
   - If a `limiter` (E2-03) is set, `await limiter.acquire(exchange)` before each request (import lazily / type as a small Protocol to avoid a hard dep ordering).
2. `HTTPError(Exception)` carrying `status`, `url`, `body`.
3. Inject the sleep (an `asyncio.sleep`-compatible callable) as a constructor seam so retry timing is testable without real waits.

## Tests

- `pytest-httpx` mock: a 503 then 200 → retried, returns the 200 JSON; assert the injected sleep was called with backoff timing.
- Non-retryable 400 → raises `HTTPError` with status/url/body.
- `post` sends the body and parses JSON (mocked).
- 429 with `Retry-After` → waits that long (via the seam) then retries.

## Verification on real data

Network is reachable. Do a **real GET** against Kraken's public API
(`https://api.kraken.com/0/public/Time`) with a live `AsyncHTTPClient` and assert
a sane JSON shape (`result.unixtime` present). Mark it `@pytest.mark.network`
(opt-in) so the default suite stays offline. Demonstrate by running it.

## Closeout

- CHANGELOG (Added): "`transport.AsyncHTTPClient` — async httpx wrapper (get/post, retry/backoff, timeouts)."
- ADR: short note if the retry/timeout policy or the limiter seam is non-obvious.
- Status/roadmap: deferred to leaf 03.
