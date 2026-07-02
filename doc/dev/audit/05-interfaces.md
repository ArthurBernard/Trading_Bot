# Audit — interfaces (CLI, API, UI) & web security (2026-07-02)

## Scope & method

Deep read of the whole `interfaces/` layer plus its immediate collaborators for
the security angle:

- `trading_bot/interfaces/api/app.py` (1615 l) — the read-only API (`create_app`),
  the unified monitoring+control dashboard (`create_dashboard_app`), the retired
  `create_control_app` alias, and the token-login auth (`_install_control_auth`).
- `trading_bot/interfaces/cli/main.py` (1067 l) + `_render.py` (223 l).
- `trading_bot/interfaces/ui/` — `__init__.py`, all 9 templates
  (`base.html`, `login.html`, `overview.html`, `strategies.html`, `orders.html`,
  `pnl.html`, `logs.html`, plus the legacy `dashboard.html` / `control.html`),
  `static/` (`app.js`, `control.js`, `style.css`, `uplot.min.{js,css}`, fonts,
  logo/favicon SVGs, `VENDOR.md`).
- Tests: `tests/interfaces/test_dashboard.py` (1579 l), `test_control_api.py`,
  `test_api.py`, `test_ui.py`, `test_cli_*.py`.
- Collaborators consulted for the gate/RCE/secret paths: `application/supervisor.py`
  (`set_mode`, `add_unit`, `_slice_for`, `start`, `_mode_of`), `application/config.py`
  (`UIConfig`, `to_yaml`, `from_yaml`, `SignalRefConfig`), `application/strategy.py`
  (`_resolve_ref`).

Method: read-only static review + **in-process ASGI probing** with FastAPI's
`TestClient` (loopback only, no server on 0.0.0.0) via throwaway scripts under
`/tmp`. The offline interfaces suite was run (`pytest trading_bot/tests/interfaces
-q`, never `-m network`). No source modified, nothing committed. The real token in
`configs/dashboard.yaml` was never read or quoted — referred to as `<token>`.

Probes empirically confirmed: `/docs`+`/openapi.json` auth-gating (303 under a
token, 200 without), `?token=` query auth, SSE `/api/events` auth (401), the
X-Forwarded-For rate-limit key (spoof does **not** reset the bucket), the
`Secure`-cookie / `X-Forwarded-Proto` trust, the `next=` open-redirect guard, the
deploy `mode:"live"` handling (ignored — seeded paper), `db_path` traversal
(accepted), the live-confirm gate on a raw API call (`confirm:true` alone flips
mode with no server-side phrase check), body-size (100k name accepted), and
logout server-side session invalidation.

## Summary

The read-only invariant is upheld on `create_app` (every route GET; a POST to an
order path is 405/404). The unified dashboard's **control** surface is correctly
centralised in one `_register_strategy_control` + two CRUD routes, each gated on
`read_only` and (when a token is set) behind the auth middleware; the probes found
**no auth bypass, no read_only bypass, and no live-gate bypass** through the
transport. Token comparison is constant-time (`secrets.compare_digest`), sessions
are 256-bit `token_urlsafe(32)`, logout invalidates server-side, login is
rate-limited on the real peer address (XFF spoofing does not help), and the
`next=` redirect is validated. Autoescape is on and every JS `innerHTML` sink runs
strategy-controlled strings through `escapeHtml`/`esc`.

The material findings are: **(I-1)** the deploy `signal` `ref` is a dotted import
path handed to `importlib.import_module` at unit start — arbitrary-module-import
(RCE-adjacent) for anyone holding the token; **(I-2)** the live-confirm "typed
phrase" (`I UNDERSTAND`) is enforced **only in the browser JS** — the server accepts
a bare `{confirm:true}`, so the deliberate-acknowledgement guarantee is client-side
only; **(I-3)** `db_path` in the deploy body is an unsanitised, traversable
filesystem path (write-anywhere SQLite) whereas the auto-derived one from `name` is
sanitised; **(I-4)** `X-Forwarded-Proto` is trusted from the client to set the
`Secure` cookie flag, but the tailnet exposure is plain-HTTP uvicorn with no
stripping proxy; **(I-5)** `run --serve` has no non-loopback/token guard (unlike
`dashboard` and `start --serve`), though its app is GET-only read-only. Plus a
cluster of Medium/Low robustness and hygiene items (no request-body cap, unbounded
in-memory session/rate maps, a cwd-relative default-manifest that breaks 4 tests on
the deployment host, and dead legacy assets).

| ID | Severity | Location | Title |
|----|----------|----------|-------|
| I-1 | High | `api/app.py:710`, `application/strategy.py:251-278` | Deploy `signal.ref` is an `importlib` import path — arbitrary module import for a token holder |
| I-2 | High | `api/app.py:919-942`; `ui/templates/strategies.html:294-321` | Typed live-confirmation phrase enforced only client-side; server accepts bare `confirm:true` |
| I-3 | High | `api/app.py:1579-1582`, `_entry_from_body`; `_auto_db_path:752` | `db_path` in deploy body is unsanitised — path traversal / write-anywhere SQLite |
| I-4 | Medium | `api/app.py:1024-1028` (`_is_https`) | `X-Forwarded-Proto` trusted from client for the `Secure` cookie flag; no TLS/stripping proxy on the tailnet |
| I-5 | Medium | `cli/main.py:268-280,415-461` (`run --serve`) | `run --serve` has no non-loopback/token guard (read-only app, but inconsistent) |
| I-6 | Medium | `api/app.py:1554-1557`, `_CreateStrategyBody` | No request-body size cap; 100k-char fields accepted; unbounded growth vectors |
| I-7 | Medium | `api/app.py:1004-1045` | In-memory session + rate-limit maps grow unbounded; rate buckets never pruned |
| I-8 | Medium | `cli/main.py:890,1002-1003` (`_DEFAULT_MANIFEST`) | Cwd-relative default manifest bleeds into tests — 4 interface tests fail on the deployment host |
| I-9 | Low | `_CreateStrategyBody.mode` (`api/app.py:658`); `supervisor.add_unit` | Deploy `mode` field is silently ignored (unit seeded from manifest global mode) — misleading API |
| I-10 | Low | `api/app.py:1027`, `_rate_allow:1035-1045` | Rate-limit keyed on `request.client.host`; behind a real proxy every client shares one bucket (login DoS) |
| I-11 | Low | `api/app.py` (no `Cache-Control`/security headers) | No `Cache-Control: no-store` on authed JSON; no `X-Content-Type-Options`/CSP/frame headers |
| I-12 | Low | `cli/main.py:281-320`; `ui/static/{app.js,control.js}`, `templates/{dashboard,control}.html` | Dead/legacy single-engine dashboard assets still shipped; `control.html`/`control.js` unreferenced |
| I-13 | Low | `api/app.py:1082-1108` (`/login`) | No CSRF token on `/login`/`/logout`; login CSRF (session-fixation-style) possible; relies on SameSite=lax |
| I-14 | Info | `ui/static/VENDOR.md`; `pnl.html:58` | uPlot/font vendoring accurate; minor version-string duplication; a11y quick pass |
| I-15 | Info | `api/app.py` `/docs` open without auth | OpenAPI docs served unauthenticated when no token (loopback-only posture) — acceptable, documented |

## Findings

### I-1 [High] Deploy `signal.ref` is an `importlib` import path — arbitrary module import for a token holder

**Location:** `api/app.py:710` (`_entry_from_body` builds `SignalRefConfig(ref=body.signal, ...)`); resolved at unit start via `application/strategy.py:251-278` (`_resolve_ref` → `importlib.import_module(module_name)` + `getattr`) and the portfolio analogue `application/portfolio.py:260-267`. The deploy route is `POST /api/strategies` (`api/app.py:1554`).

**Evidence:** `POST /api/strategies` accepts a free-form `signal: str`. For a
`"module:function"` ref, `_resolve_ref` does `importlib.import_module(module_name)`
on the attacker-supplied `module_name`. Probe: deploying `signal:
"antigravity:something"` returns 200 (deploy itself does not import — the import
fires on `start`). Any module reachable on `sys.path` is importable, and **importing
a module executes its top-level code**. This is not `exec` of user-authored source,
but it is arbitrary-module-import: a token holder can cause the daemon process to
import any installed/importable module (side effects on import), and the discovery
scanner `_discover_signals` (`api/app.py:812-831`) already `import_module`s every
`strategies/*/signal.py` at `GET /api/signals`.

**Impact:** Anyone with the dashboard token (now the whole tailnet, gated only by
the token) can drive Python imports in the trading daemon — the process that holds
live-broker credentials. The blast radius is bounded by "must already be importable
on the daemon's `sys.path`" (no upload path, no source authoring), but on a
developer host with a rich venv that is a wide surface (e.g. importing a module with
a destructive import-time side effect). Combined with I-2 and the go-live gates,
this is the strongest reason to treat the token as a full-trust credential.

**Recommendation:** Constrain `body.signal` server-side to a **whitelist**: builtin
names + the refs actually returned by `_discover_signals()` (reject any other
`module:function`). Reject any `module_name` outside an allowlisted top-level
package (e.g. must start with `strategies.` or `trading_bot.`). Document explicitly
in the go-live runbook that the dashboard token is a code-execution-capable
credential and must never be shared.

### I-2 [High] Typed live-confirmation phrase enforced only client-side; server accepts bare `confirm:true`

**Location:** `api/app.py:919-942` (`set_strategy_mode` → `set_mode(..., confirm_live=body.confirm)`); the typed-phrase check lives only in `ui/templates/strategies.html:294-321` (and legacy `ui/static/control.js:150-177`). Server gate: `application/supervisor.py:645-650`.

**Evidence:** The UI requires the operator to type `I UNDERSTAND` before the "Go
live" button enables, and that only sets `{confirm:true}`. The **server never sees
or checks the phrase** — `_ModeBody` has only `mode` + `confirm: bool`. Probe (auth
off, raw call): `POST /api/strategies/btc-ma/mode {"mode":"live","confirm":true}` →
**200**, `mode` flips to `live` with no phrase, no second factor. The CLAUDE.md
invariant is "requires typed confirmation + confirm:true → 403 otherwise"; in
reality the *typed* part is cosmetic and the whole gate reduces to a single boolean
in the JSON body.

**Impact:** The "deliberate acknowledgement" is defeated by any client that posts
`confirm:true` — a script, a curl one-liner with the token, or a CSRF-style
top-level navigation is not needed (SameSite blocks that), but any token holder or
any XSS foothold (see I-11/I-13) can silently arm live mode. Note the actual *order
placement* still needs `live_enabled` + credentials + a matching broker at `start`,
so this alone does not place a real order — but it removes the intended human
speed-bump on the money-moving state transition.

**Recommendation:** Either (a) accept the intended design and **downgrade the docs**
to "server requires `confirm:true`; the typed phrase is a UI affordance only", or
(b) enforce a server-side second factor for live: require the request to echo the
phrase (`_ModeBody.acknowledgement == "I UNDERSTAND"`) *and* re-verify
`live_enabled`/credentials at mode-switch time (not only at `start`). Given real
money, (b) is preferable; at minimum add an audit log line on every live switch.

### I-3 [High] `db_path` in the deploy body is unsanitised — path traversal / write-anywhere SQLite

**Location:** `api/app.py:1579-1582` (`db_path = body.db_path or _auto_db_path(...)`), `_entry_from_body:719-747` stamps it onto the config unchanged; `_auto_db_path:752-776` sanitises only the *name*-derived path, not an explicit `body.db_path`.

**Evidence:** `_auto_db_path` carefully sanitises `name` to `[A-Za-z0-9-_.]` for the
auto-assigned store, but an **explicit `db_path` in the body bypasses that entirely**
and is written verbatim into the persisted manifest and used as the SQLite store
path at `start`. Probe: `POST /api/strategies` with `db_path:
"../../../../tmp/evil.sqlite"` → **200**, the traversing path round-trips into the
manifest. When the unit starts, `SqliteStore` opens/creates that file — a
write-anywhere-the-daemon-can-write primitive (the daemon may run as a service
account with broad write access).

**Impact:** A token holder can plant/overwrite a SQLite file at an arbitrary
filesystem location (subject to the daemon's fs permissions) and cause the daemon to
open it. Not direct code execution, but a filesystem-integrity and
denial/tampering vector, and it lands in the *persisted* manifest (survives
restart). Same trust boundary as I-1.

**Recommendation:** Validate `body.db_path` server-side: resolve it and require it to
stay within the manifest's storage directory (reject `..`, absolute paths, and
anything resolving outside the sandbox), or drop the field from the API and always
auto-derive from the sanitised name. At minimum apply the same
`isalnum()/-_.`-only sanitisation to the stem and force the `dashboard/` subdir.

### I-4 [Medium] `X-Forwarded-Proto` trusted from the client for the `Secure` cookie flag

**Location:** `api/app.py:1024-1028` (`_is_https`), used at `1105` (`secure=_is_https(request)`).

**Evidence:** `_is_https` returns True if `request.url.scheme == "https"` **or** the
`X-Forwarded-Proto` header's first token is `https`. Probe: sending `X-Forwarded-Proto:
https` on a plain-HTTP request makes the login set `... SameSite=lax; Secure`. On
the described deployment the dashboard is bound directly by uvicorn on `0.0.0.0`
over **plain HTTP with no reverse proxy** stripping/overwriting that header, so the
value is fully client-controlled and there is no HTTPS anyway.

**Impact:** Two-sided and both minor. (a) A client can *force* `Secure` on, which
over the plain-HTTP tailnet means the browser then refuses to send the cookie —
self-inflicted session breakage, a nuisance not an escalation. (b) The header is
untrusted, so `_is_https` cannot be relied on for any real HTTPS decision. Because
there is no TLS on the tailnet path, the session cookie already travels in
cleartext regardless of this flag — the tailnet's own WireGuard encryption is what
protects it, not `Secure`.

**Recommendation:** Do not trust `X-Forwarded-Proto` unless a known proxy sets it;
gate the trust behind an explicit `trusted_proxy`/`forwarded_allow_ips` config, or
drop the header branch and set `Secure` only for a genuinely-`https` scheme. Document
that the tailnet (WireGuard) is the transport-security layer and the dashboard
assumes it; prefer running behind a TLS-terminating proxy if exposed beyond the
tailnet.

### I-5 [Medium] `run --serve` has no non-loopback / token guard (inconsistent with the other serve paths)

**Location:** `cli/main.py:268-280` (`run`'s `--serve-host` default `127.0.0.1`, no token option) and `_run_and_serve:415-461` (calls `create_app`, `uvicorn.Server` on `host:port` unconditionally). Contrast `dashboard` (`1015-1021`) and `start --serve` (`781-787`) which both refuse a non-loopback host without a token.

**Evidence:** `run --serve --serve-host 0.0.0.0` binds `create_app` (the read-only
engine view) on all interfaces with **no auth and no refusal**. Probe: `create_app`
exposes only GET routes (`/api/health|positions|orders|kpi|events`, `/`) — a POST to
a control path is 404. So this leaks *observation* data (positions, orders, PnL,
fills, live/paper mode) to anyone on the network but cannot trade.

**Impact:** Information disclosure of the live book to the whole network segment, and
an inconsistency an operator can trip over (the muscle-memory "serve is guarded" is
false for `run --serve`). No trading path.

**Recommendation:** Apply the same non-loopback-requires-token guard (or at least a
loud warning) in `run --serve`, or explicitly document that `run --serve` is
loopback-only-by-contract and refuse a non-loopback `--serve-host`.

### I-6 [Medium] No request-body size cap; oversized fields accepted

**Location:** `api/app.py:1554-1557` (`create_strategy` takes `_CreateStrategyBody`); no `max_length` on the string fields; `/login` reads `await request.body()` unbounded (`1094`).

**Evidence:** Probe: a deploy body with a 100 000-character `name` returns 200 (then
persists into the manifest and the on-disk filename). No `Content-Length` ceiling is
enforced by the app; uvicorn's defaults are generous. `parse_qs(await
request.body())` on `/login` also buffers the whole body.

**Impact:** Memory/disk-amplification DoS from a token holder (or unauthenticated on
the loopback/`run --serve` path): a giant `name` becomes a giant manifest and a
giant filename; repeated large bodies pressure memory. Low likelihood (token-gated)
but real for a control plane.

**Recommendation:** Add pydantic `max_length` to `name`/`venue`/`symbol`/`signal`
and cap `universe`/`params` sizes; reject bodies over a sane `Content-Length`
(middleware or uvicorn `--limit-*`).

### I-7 [Medium] In-memory session and rate-limit maps grow unbounded

**Location:** `api/app.py:1004-1005` (`app.state.sessions`, `app.state.login_buckets`), `_prune:1007-1010` (prunes sessions only), `_rate_allow:1035-1045` (never prunes buckets).

**Evidence:** `sessions` is pruned lazily on each `_valid_session` call (by TTL), but
`login_buckets` is keyed by client host and **never pruned** — every distinct source
address that ever hits `/login` leaves a permanent entry. `_new_session` has no cap
on live sessions. Sessions are process-memory only (reset on restart, acknowledged).

**Impact:** Slow unbounded memory growth over a long-lived daemon, especially the
never-pruned rate buckets under address churn. Not exploitable to bypass auth, but a
stability wart on a process meant to run for weeks.

**Recommendation:** Prune `login_buckets` on the same TTL sweep (drop full buckets
older than a window), and cap/evict `sessions` (LRU or a max count). Consider a
signed stateless session (itsdangerous/JWT with the token as key) to avoid the maps
entirely.

### I-8 [Medium] Cwd-relative default manifest bleeds into tests — 4 interface tests fail on the deployment host

**Location:** `cli/main.py:890` (`_DEFAULT_MANIFEST = pathlib.Path("configs/dashboard.yaml")`), read at `1002-1003`; the affected tests invoke `dashboard`/`serve` with no `-c` (`test_dashboard.py:1173,1208,1305,1327`).

**Evidence:** `pytest trading_bot/tests/interfaces -q` on this host: **4 failed, 138
passed**. All four are the same root cause: the CLI resolves `_DEFAULT_MANIFEST`
relative to the process cwd, and on this machine `configs/dashboard.yaml` exists (the
real deployment manifest: `ui.host=0.0.0.0`, token set, `read_only=false`, mode
paper, file mode `0o600`, correctly gitignored). The tests assert the default host is
`127.0.0.1` and that a no-token non-loopback host refuses — both wrong once the
real manifest is picked up. In CI (no such file) they pass, so this is latent test
fragility, not a shipped regression — but it means the suite is **not green on the
deployment host**, exactly where a pre-commit check runs.

**Impact:** False confidence: a developer running `pytest` in the repo root with a
live manifest present gets 4 red tests unrelated to their change, and the "tests must
pass before commit" rule is undermined. Secondary: the CLI silently reading a
cwd-relative file is a mild surprise (run from the wrong directory → wrong manifest).

**Recommendation:** Isolate these tests with `monkeypatch.chdir(tmp_path)` (or patch
`_DEFAULT_MANIFEST`) so they never see a real manifest. Separately, consider anchoring
the default manifest to a well-known config dir (XDG / an explicit `TRADING_BOT_HOME`)
rather than the process cwd.

### I-9 [Low] Deploy `mode` field is silently ignored (unit seeded from manifest global mode)

**Location:** `api/app.py:658` (`_CreateStrategyBody.mode: Literal["paper","testnet","live"] = "paper"`); `_entry_from_body:671-749` never threads `body.mode` into the config; `supervisor.add_unit:414` seeds via `_mode_of(new_base)` (the manifest's global mode).

**Evidence:** Probe: `POST /api/strategies {... "mode":"live" ...}` → 200 with the
resulting unit `mode == "paper"` (the manifest is paper). The body advertises a
`mode` (including `live`) that the server accepts and then discards. This is
*safe* (deploying never creates a live unit off a paper manifest, and never
auto-starts), but the API contract is misleading and the docstring implies the seed
mode is honoured ("The seed deployment mode").

**Impact:** Confusing API — a caller setting `mode:testnet`/`live` gets a paper unit
with no error. No security impact (errs safe), but a correctness/consistency wart and
a latent trap if `add_unit` is ever changed to honour a per-entry mode.

**Recommendation:** Either honour `body.mode` (and then re-apply the live-confirm gate
at deploy, since seeding a live unit is a money-adjacent state) or drop the field and
document that deployments inherit the manifest mode and are switched via
`.../mode` afterwards.

### I-10 [Low] Rate-limit keyed on `request.client.host` — one shared bucket behind a real proxy

**Location:** `api/app.py:1084-1085` (`_rate_allow(client.host ...)`), `_rate_allow:1035-1045`.

**Evidence:** The login throttle keys on the transport peer (`request.client.host`).
This is the *correct* choice for the direct-uvicorn tailnet deployment (probe: 15 bad
logins with rotating `X-Forwarded-For` still trip 429 at attempt 11 — XFF spoofing
does **not** reset the bucket, good). But if the dashboard is ever fronted by a
reverse proxy, every client collapses to the proxy's address and shares one 10/min
bucket → a single attacker can lock out all users, and a legitimate flurry
self-throttles.

**Impact:** Correct today; a foot-gun the moment a proxy is introduced. Also the
global 10/min is quite permissive for a brute-force guard against a high-entropy
token (fine) but note there is **no lockout escalation** — a determined attacker gets
10 guesses/min indefinitely (acceptable given `token_urlsafe`-class tokens).

**Recommendation:** Keep peer-address keying for the direct deployment; if a proxy is
adopted, switch to a trusted `X-Forwarded-For` parse gated on a proxy allowlist.
Document the assumption (no proxy) next to `_rate_allow`.

### I-11 [Low] No security response headers / no `Cache-Control: no-store` on authed JSON

**Location:** `api/app.py` — `_DecimalJSONResponse` and all handlers set no
`Cache-Control`, `X-Content-Type-Options`, `Content-Security-Policy`,
`X-Frame-Options`/`frame-ancestors`, or `Referrer-Policy`.

**Evidence:** Authenticated JSON (positions, orders, PnL — the live book) is returned
with default headers, cacheable by intermediaries/browser; the HTML shell has no CSP
and no anti-clickjacking header. The inline `<script>` in `base.html`/pages means a
strict CSP would need nonces, but `frame-ancestors 'none'` and `nosniff` are free.

**Impact:** Low on a private tailnet. Absent CSP, any future reflected/stored XSS (the
JS is careful with `escapeHtml`, but the surface is large) has no second line of
defence; absent `frame-ancestors`, the control UI is framable for clickjacking the
start/stop/live controls.

**Recommendation:** Add a small middleware setting `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` (or CSP `frame-ancestors
'none'`), and `Cache-Control: no-store` on `/api/*`. Consider a CSP with hashed/nonced
inline scripts as a later hardening.

### I-12 [Low] Dead / legacy single-engine dashboard assets still shipped

**Location:** `ui/static/app.js`, `ui/static/control.js`, `ui/templates/dashboard.html`, `ui/templates/control.html`; `dashboard.html` is still rendered by `create_app` (`api/app.py:457-474`) for `run --serve`.

**Evidence:** The unified dashboard (`create_dashboard_app`) renders `base.html`-derived
pages with **inline** scripts and never loads `app.js`/`control.js`. `control.html` /
`control.js` are referenced by **no** factory (grep confirms: only doc mentions).
`dashboard.html` + `app.js` are still live via `create_app` (the `run --serve`
read-only view). So `app.js`/`dashboard.html` are *reachable but legacy*;
`control.html`/`control.js` are *fully dead*. `control.js` also duplicates the
client-only-phrase live-confirm (same I-2 shape).

**Impact:** Dead code ships in the wheel, widens the audit surface, and the two
live-confirm implementations can drift. Both legacy files render `{{ mode }}`/state
into a template referencing `/static/style.css` — but `style.css` is 249 l and used
only by these legacy pages + `login.html` (the unified pages inline their CSS in
`base.html`). So `style.css` is near-dead too (login page is its only live consumer).

**Recommendation:** Delete `control.html` + `control.js` (unreferenced). Decide whether
`run --serve`/`create_app` + `dashboard.html` + `app.js` are still wanted; if the
unified dashboard's `read_only` mode supersedes them, retire them and point
`run --serve` at `create_dashboard_app(read_only=True)`. Trim `style.css` to what
`login.html` needs (or inline it).

### I-13 [Low] No CSRF token on `/login` / `/logout`; login-CSRF possible, mitigated by SameSite

**Location:** `api/app.py:1082-1108` (`/login`), `1110-1117` (`/logout`); no CSRF field; cookie is `SameSite=lax` (`1104`).

**Evidence:** The state-changing control routes (`start`/`stop`/`mode`/deploy/remove)
are all `POST`/`DELETE` with JSON bodies and are cookie-authed — but `SameSite=lax`
blocks a cross-site POST from carrying the cookie, and the JSON `Content-Type` +
non-simple method means a cross-origin fetch is preflighted and blocked by the
same-origin policy (no CORS is configured, so no cross-origin write). So the mutating
routes are adequately CSRF-protected **by the current cookie config**. The gap is
narrower: `/login` and `/logout` themselves have no CSRF token, enabling a
login-CSRF (an attacker submits their own token to log the victim into an
attacker-controlled session) — but here the token *is* the single shared secret, so
login-CSRF has little value.

**Impact:** Practically low given a single shared token and SameSite=lax. Worth
recording because it is the classic gap and because any future move to per-user
credentials or a relaxed SameSite would make it real.

**Recommendation:** Accept for now (document that SameSite=lax + same-origin + shared
token is the CSRF posture). If the auth model grows, add a double-submit CSRF token to
`/login`/`/logout` and to the mutating `/api/*` routes.

### I-14 [Info] Vendoring accuracy, self-hosted assets, accessibility

**Location:** `ui/static/VENDOR.md`; `pnl.html:58-59`; fonts under `ui/static/fonts/`; `base.html`.

**Evidence:** `VENDOR.md` accurately lists **uPlot v1.6.31 (MIT)** — the shipped
`uplot.min.js` header confirms `v1.6.31` and the MIT/uPlot origin (50 KB), and
`uplot.min.css` is a real 1.8 KB stylesheet (the earlier "0-byte" impression was
stale; both files are present and non-empty). Fonts (Martian Mono OFL, Spline Sans
OFL, latin woff2) are self-hosted — no CDN, no external fetch, matching the
"leaks nothing / works offline" claim. `logo.svg`/`favicon.svg` are shared brand
marks. Accessibility quick pass: the live modal has `role="dialog"
aria-modal="true" aria-labelledby`, tables have headers, toggles use
`role="tablist"`, the log feed uses `aria-live="polite"`, and `prefers-reduced-motion`
is honoured — solid. Minor: the modal does not trap focus, and the connection-dot
state is colour-only (no text-independent cue beyond the label).

**Impact:** None (informational). Vendoring/licensing is honest and offline-safe.

**Recommendation:** Optionally add focus-trapping to the live modal and a
non-colour cue to the connection dot. No action required otherwise.

### I-15 [Info] OpenAPI `/docs` served unauthenticated when no token (loopback posture)

**Location:** FastAPI default `/docs`, `/redoc`, `/openapi.json`; auth middleware `api/app.py:1047-1059`.

**Evidence:** Probe: with a token set, `/docs`, `/redoc`, `/openapi.json` all **303 to
`/login`** (the middleware treats non-`/api/`, non-open-prefix paths as pages needing
a session — good, the schema is not disclosed pre-auth). With **no token**, they
return **200** (full schema). Since a token is mandatory for any non-loopback bind
(I-5 aside), the unauthenticated `/docs` only happens on the loopback/tunnel posture,
where full local access is already assumed.

**Impact:** None beyond the documented loopback-only-without-token stance. The schema
enumerates the control routes, but they are equally gated.

**Recommendation:** Acceptable as-is. If desired, disable `/docs`/`/openapi.json` in
production (`FastAPI(docs_url=None, ...)`) or fold them under the same auth even in
the no-token case.
