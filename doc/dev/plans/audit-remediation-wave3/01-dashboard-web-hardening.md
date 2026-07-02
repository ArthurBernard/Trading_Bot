---
plan: audit-remediation-wave3/01-dashboard-web-hardening
kind: leaf
status: executing
complexity: high
depends: []
parallel: true
branch: fix/dashboard-web-hardening
pr: ""
---

# Leaf 01 — dashboard-web-hardening

## Goal
Harden the dashboard/serve web surface. Fixes `I-4`, `I-5`, `I-6`, `I-7`, `I-9`,
`I-10`, `I-11`, `I-13`, `A-4`.

## Files
`trading_bot/interfaces/api/app.py`, `trading_bot/application/config.py` (`UIConfig`),
`trading_bot/interfaces/cli/main.py` (`run --serve` guard), tests under
`trading_bot/tests/interfaces/` and `trading_bot/tests/application/`.

## Steps
- `I-4`: don't trust client `X-Forwarded-Proto` for the `Secure` cookie flag unless
  a trusted-proxy posture is configured (the tailnet is plain-HTTP uvicorn); document.
- `I-5`: `run --serve` has no non-loopback/token guard (unlike `dashboard`/`start
  --serve`) — add the same guard (read-only app, but consistent).
- `I-6`: cap the request-body size on the deploy/mutating endpoints (reject oversized).
- `I-7`: bound/prune the in-memory session + rate-limit maps (no unbounded growth).
- `I-9`: the deploy `mode` field is silently ignored — either honour it or reject it
  (no misleading API).
- `I-10`: rate-limit keyed on `request.client.host` — behind a proxy every client
  shares one bucket; key on the real peer / a configured header safely.
- `I-11`: add `Cache-Control: no-store` on authed JSON + `X-Content-Type-Options`
  /basic security headers.
- `I-13`: add CSRF protection to `/login`/`/logout` (or document why SameSite covers it).
- `A-4`: enforce the "non-loopback host requires a token" rule in `UIConfig` (a
  validator), so a hand-edited manifest can't encode a wide-open control surface.

## Tests
Extend the interfaces suite: `run --serve` non-loopback without token refuses; an
oversized body is rejected; session/rate maps are pruned; a `UIConfig` with a
non-loopback host and no token fails validation; security headers present; deploy
`mode` handled; CSRF on login.

## Verification on real data
Offline in-process `TestClient` only (loopback, no bind, no order): assert the new
guards/headers/limits behave as specified.

## Closeout
CHANGELOG (Fixed/Security). ADR: web-hardening posture (proxy trust, config-layer
bind guard).
