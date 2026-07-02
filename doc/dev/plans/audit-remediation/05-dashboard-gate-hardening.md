---
plan: audit-remediation/05-dashboard-gate-hardening
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/dashboard-gate-hardening
pr: ""
---

# Leaf 05 — dashboard-gate-hardening

## Goal
Move the dashboard's soft (client-side / unsanitised) gates to the server. Fixes
`I-2`, `I-3`, `I-1`.

## Files to change
- `trading_bot/interfaces/api/app.py`, `trading_bot/application/strategy.py`
  (signal-ref import), templates only if needed + tests under
  `trading_bot/tests/interfaces/`.

## Steps
1. `I-2`: enforce the typed live-confirmation **server-side**. The mode-switch route
   (`app.py:919-942`) must require the explicit acknowledgement token (the typed
   `I UNDERSTAND` phrase, not just `confirm:true`) and return 403 otherwise. The
   browser JS check stays as UX but is no longer the only gate.
2. `I-3`: sanitise `db_path` in the deploy body (`_entry_from_body`,
   `app.py:1579-1582`) the same way the auto-derived path is (`_auto_db_path:752`):
   reject absolute paths and any `..` traversal; confine writes to the intended
   dashboard data dir. `../../../../tmp/evil.sqlite` must be refused (4xx).
3. `I-1`: constrain the deploy `signal.ref` (`app.py:710`,
   `strategy.py:251-278`): at minimum validate the dotted path shape and log an
   audit line on import; if feasible, allow-list acceptable module prefixes
   (e.g. `strategies.`, `fynance`, `trading_bot.`) so a token holder can't force an
   arbitrary module import. Document the residual blast radius.

## Tests
- POST mode=live with `confirm:true` but no/blank ack phrase → 403; with the correct
  phrase → 200 (paper→live in a fake supervisor). read_only → 403 on every mutating
  route (extend the existing matrix).
- Deploy with `db_path=../../evil.sqlite` (and absolute) → 4xx; deploy with a normal
  name → 200 and the store lands under the data dir.
- Deploy with a `signal.ref` outside the allow-list → 4xx / rejected.

## Verification on real data
Offline, in-process ASGI `TestClient` (loopback, no real server). Probe each route
and assert the status codes above. NO real broker, NO 0.0.0.0 bind, NO order.

## Closeout
CHANGELOG (Fixed / Security): server-side live-confirmation, `db_path` traversal
guard, constrained `signal.ref` import. ADR: the live gate and filesystem/import
surface are enforced server-side, not in the browser.
