---
plan: redact-token-logs/01-access-log-redaction
kind: leaf
status: executing
complexity: medium
depends: []
parallel: false
branch: fix/access-log-token-redaction
pr: ""
---

# 01 — Redact secret query values from the uvicorn access log

## Goal

No secret query value ever reaches a uvicorn log line. Today the documented
`?token=` script auth is written **verbatim** by uvicorn's access logger — on the
systemd deploy (2026-08-02) the real dashboard token landed in journald as
`GET /api/strategies?token=<value> HTTP/1.1 200 OK` (the token was rotated).
This violates the repo invariant *"Secrets never logged — redact keys in any log
line."* The transport layer already solved the identical problem for signed
broker URLs (`trading_bot/transport/http.py:_redact_url`, key set
`{signature, api_key, apikey, token, nonce}`, marker `<redacted>`); the fix
reuses that exact semantic at the web-serving surface.

## Files to change

- `trading_bot/transport/http.py` — expose the existing scrubber under a public
  name: `redact_url = _redact_url` (module-level alias right after the def, with
  a short comment: other layers reuse the same key set + marker so a URL is
  redacted identically wherever it could reach a log). No behaviour change.
- `trading_bot/application/log_setup.py` — add:
  - `class AccessLogRedactionFilter(logging.Filter)` — mutates the record in
    place and always returns `True`: applies `redact_url` to `record.msg` (when
    `str`) and to every `str` element of `record.args` (tuple args; leave dict
    args untouched). `redact_url` is a no-op on strings without a query string,
    so non-URL args (`"GET"`, client addr, HTTP version) pass through unchanged.
  - `def install_access_log_redaction() -> None` — attaches one instance to the
    `uvicorn.access` **and** `uvicorn.error` loggers. **Idempotent**: skip if a
    filter of this class is already present (repeated CLI invocations in one
    process — the test suite — must not stack filters).
  - Why logger-level (load-bearing): `logging.config.dictConfig` (which uvicorn
    applies at startup with its default `LOGGING_CONFIG`) replaces a configured
    logger's **handlers** but does **not** clear programmatically-attached
    logger **filters** — so installing before `uvicorn.run()` survives uvicorn's
    own logging setup. A test must lock this assumption (below).
- `trading_bot/interfaces/cli/main.py` — call `install_access_log_redaction()`
  immediately before each of the **three** uvicorn launch sites:
  1. `serve` — `uvicorn.run(...)` (~line 1210);
  2. `start --serve` — the `uvicorn.Server(uvicorn.Config(...))` site (~line 1477);
  3. `dashboard` — `uvicorn.run(...)` (~line 1861).
  Import in the `# Local` import group. Match each site's surrounding comment
  style (one line on why: the access log would otherwise write `?token=` URLs
  verbatim).

## Steps

1. Add the `redact_url` public alias in `transport/http.py`.
2. Implement `AccessLogRedactionFilter` + `install_access_log_redaction()` in
   `application/log_setup.py` (module docstring already frames the logging
   spine; extend it with one line on the access-log scrubber).
3. Wire the three CLI sites.
4. Write the tests (below); run the gates until green:
   `python -m pytest`, `ruff check trading_bot/`, `ruff format --check .`,
   `mypy trading_bot/` (all under `~/.pyenv/versions/trading_bot_env/bin/python`).
5. Real-data verification (below).

## Tests

New `trading_bot/tests/application/test_access_log_redaction.py`:

- **Redacts the uvicorn access record shape**: build
  `logging.LogRecord(name="uvicorn.access", msg='%s - "%s %s HTTP/%s" %d',
  args=("127.0.0.1:5", "GET", "/api/health?token=SENTINEL&x=1", "1.1", 200), …)`,
  run the filter, assert `record.getMessage()` contains `<redacted>` and `x=1`
  and does **not** contain `SENTINEL`; method/addr/status unchanged.
- **All sensitive keys**: `token`, `signature`, `api_key`, `apiKey`, `nonce`
  each redacted (case-insensitive); a non-sensitive query (`?symbol=BTCUSDT`)
  and a query-less path pass through byte-identical.
- **Idempotent install**: `install_access_log_redaction()` twice → exactly one
  `AccessLogRedactionFilter` on `uvicorn.access` and on `uvicorn.error`.
- **Survives uvicorn's dictConfig**: install, then
  `logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)`, assert the filter
  is still attached and still redacts (this locks the load-bearing assumption).
- Tests must clean up the filters they install (fixture removing the filter from
  the shared loggers) so state never leaks across the suite.

In `trading_bot/tests/interfaces/test_cli_commands.py` (follow the existing
pattern that patches `uvicorn.run` / `uvicorn.Server`):

- After invoking each of `serve`, `dashboard`, and the `start --serve` path,
  assert the filter is present on `uvicorn.access` (the install happens even
  though uvicorn itself is patched out).

## Verification on real data

Launch the real dashboard on a **spare port** and read the actual access log:

1. `~/.pyenv/versions/trading_bot_env/bin/trading-bot dashboard --port 8010`
   (default paper config, loopback; capture stdout+stderr to a file).
2. `curl "http://127.0.0.1:8010/api/health?token=LEAKCANARY123"` (and one
   clean request without query).
3. Read the captured log: the line for the probe must contain `<redacted>` and
   must **not** contain `LEAKCANARY123`; the clean request's line is unchanged.
4. Stop the daemon with `SIGTERM` **to the python PID** (find it via
   `ss -tlnp`/`pgrep -f "trading-bot dashboard"` — a bash wrapper PID ignores
   the signal) and confirm clean exit.

**Constraints (do not violate):**

- **Never touch port 8000, the running `trading-bot` systemd service,
  `configs/dashboard.yaml`, or the real token.** All verification runs on port
  8010 with the throwaway `LEAKCANARY123` value.
- Secrets: the canary value is fake by construction; never read or echo real
  credentials (`.env` stays closed).

## Closeout (orchestrator, not the agent)

- CHANGELOG (Fixed): access log redacts secret query values (`?token=` …) at
  every uvicorn launch site.
- ADR: reuse of the transport scrubber via a public alias + a logger-level
  filter that survives uvicorn's dictConfig (vs forking uvicorn's log-config
  dict per site, vs duplicating the key set at the interface layer).
- Status: dashboard runs under systemd on the ops machine (2026-08-02) and the
  access log no longer leaks the token; roadmap: remove the "Access-log token
  redaction" line (single-leaf tree) and refresh the now-false "today it is a
  nohup" parenthetical in road-to-1.0 item 4.
