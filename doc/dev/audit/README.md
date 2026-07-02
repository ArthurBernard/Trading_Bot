# Audit — Trading_Bot (2026-07-02)

Full, deep audit of the `trading_bot` execution engine (the third pillar of the
dccd / fynance / trading_bot triptych). Six specialist passes, one per area, each
in its own file; this README is the **executive summary + index** across all of
them.

> **Scope reminder.** The standing operating constraint is **paper / testnet only,
> no real orders**. That is why almost none of the money-risk findings can bite
> *today* — they are **latent**: the moment a strategy is flipped to `live`, the
> Critical/High broker + gate findings become active. Read this audit as *"what
> must be true before the first live order"*, not *"what is on fire now"*.

## The six reports

| # | Area | File | Findings (C/H/M/L/I) |
|---|------|------|----------------------|
| 02 | Domain & storage | [`02-domain-storage.md`](02-domain-storage.md) | 1 / 3 / 6 / 4 / 3 |
| 03 | Application (engine) | [`03-application.md`](03-application.md) | 0 / 2 / 6 / 4 / 2 |
| 04 | Brokers & transport | [`04-brokers-transport.md`](04-brokers-transport.md) | 2 / 4 / 4 / 3 / 2 |
| 05 | Interfaces (CLI/API/UI) & web-sec | [`05-interfaces.md`](05-interfaces.md) | 0 / 3 / 5 / 5 / 2 |
| 06 | Tests, CI & packaging | [`06-tests-ci-packaging.md`](06-tests-ci-packaging.md) | 1 / 3 / 4 / 4 / 3 |
| 07 | Docs, config & governance | [`07-docs-config-governance.md`](07-docs-config-governance.md) | 0 / 1 / 5 / 4 / 4 |
| | **Total** | | **4 / 16 / 30 / 24 / 16 = 90** |

Severity: **Critical** = money loss/corruption, secret leak, or a broken gate ·
**High** = correctness bug or invariant violation · **Medium** = robustness/design
debt · **Low** = minor · **Info** = observation.

## Overall verdict

The rewrite is **structurally sound and the hard architectural invariants hold**:
the domain layer is genuinely pure (no I/O, no upward imports), money is `Decimal`
end-to-end on the wire and through SQLite (verified round-trips), the order state
machine is table-driven and guarded, `Position` fold math is correct, the
transport-level idempotency guard is right (ambiguous 5xx/timeout on a submit
`raise`s rather than blind-retries), the paper→live commingling bug is fixed with
no sibling regression, and the dashboard has **no auth bypass, no read-only
bypass, and no transport-level live-gate bypass** (constant-time token compare,
256-bit sessions, non-spoofable login rate-limit).

The problems cluster in five themes below. **Four Criticals** and a handful of
Highs are the real content of this audit; everything else is debt to burn down
over time.

## The 4 Critical findings

1. **`D-1` — Money fields accept raw `float` at runtime.** `Order`/`Fill`/`Signal`
   `__post_init__` range-check but never route through the `money()` guard, so a
   `float` price/qty silently enters the PnL source of truth and only explodes
   later (or persists corrupt). The one defence the domain owns is not applied to
   its own constructors.
2. **`B-1` — Binance request signature leaked into logs & exceptions.** Binance
   signs on the query string; the *full signed URL* (`…&signature=<hmac>` + all
   params) is embedded verbatim in `HTTPError`/`AmbiguousRequestError` messages and
   in every `logger.warning` in `transport/http.py`. A single 429/5xx/timeout on
   any Binance call writes the signature into logs — a direct "secrets never
   logged" violation.
3. **`B-2` — Orders are never quantized to the venue tick/lot before submit.**
   `str(order.qty)` / `str(order.limit_price)` are sent raw even though
   `Instrument.price_precision`/`qty_precision` and a `quantize()` helper exist and
   are unused. An over-precise or sub-min-lot size is silently rejected by the
   venue, and a strategy that rounds *up* can **oversell**.
4. **`T-1` — The pytest gate is red locally but green in CI.** At least five
   dashboard tests read the CWD-relative, secret-bearing `configs/dashboard.yaml`
   because there is *no `conftest.py`* and *no autouse CWD/env isolation*. The
   suite is non-hermetic: the pre-commit "tests must pass" invariant is effectively
   not enforced on the dev box, and the discrepancy hid until this audit.

## Cross-cutting themes

**T1 — Money safety is defence-in-*breadth* but not defence-in-*depth*.** The
`Decimal` discipline is real everywhere it's *used*, but the guards aren't applied
where they'd catch a mistake: constructors accept `float` (`D-1`), YAML `Decimal`
fields can be parsed from floats, average-price divisions run under the
process-global 28-digit context so a repeating quotient is silently rounded and
non-deterministic (`D-4`), and `TARGET_QTY` accepts `NaN`/`Inf` (`D-7`). None lose
money in paper mode; each is a latent live-mode correctness hole. → *Add `money()`
to the constructors, pin a `Decimal` context on the money math, reject non-finite.*

**T2 — Venue fidelity: paper validates behaviour that live won't reproduce.**
`PaperBroker` fills instantly and fully at request price, applies no min-notional /
precision rejection, and has no client-order-id dedup (`B-13`), while the live
adapters *don't quantize* (`B-2`), *drop* an out-of-charset Binance
`newClientOrderId` — which then breaks reconcile-by-client-order-id and can
double-ingest or false-orphan an order (`B-5`), never forward a client-order-id to
Kraken at all (`B-15`), have a non-monotonic Kraken nonce under concurrency/clock
rollback (`B-3`), and flatten every venue error to a stringy `BrokerError` with no
code→domain mapping, so retriable Kraken `EService:Unavailable`/`EAPI:Rate limit`
(returned on HTTP 200) are treated as terminal (`B-4`). A strategy green in paper
can behave materially differently on first contact with a real venue. → *This is
the single biggest "before live" work item; see the blocker list.*

**T3 — The dashboard is a control plane on `0.0.0.0`, and three server-side gates
are softer than they look.** No transport bypass was found, but for a token holder
on the tailnet: the deploy `signal.ref` is a dotted import path handed to
`importlib` (arbitrary-module import, RCE-adjacent — `I-1`); the typed live-confirm
phrase (`I UNDERSTAND`) is enforced **only in browser JS**, the server accepts a
bare `{mode:"live",confirm:true}` (`I-2`); and an explicit `db_path` in the deploy
body is unsanitised → write-anywhere SQLite via `../../` (`I-3`). The token is the
*only* thing between the tailnet and live trading, and two of the three
"deliberate, multi-step" live gates are client-side or bypassable. → *Move the
live-confirm check server-side, sanitise `db_path`, allow-list/telemeter
`signal.ref`.*

**T4 — The safety net has holes exactly where money is at risk.** Coverage is a
real 96 % but **never enforced** (no `fail_under`, no artifact — `T-3`), CI bakes
`--exitfirst` so the first failure hides all the rest and invalidates the coverage
total (`T-2`), and the uncovered ~4 % is concentrated on the money-critical error
branches: kill-switch cancel-on-failure, order-router forbidden-transition,
reconcile divergence, WS reconnect (`T-4`). Plus the `max_daily_loss` breaker is
wired to *cumulative session* PnL and never resets at a day boundary — "daily" is a
misnomer and, once tripped, it latches the kill-switch permanently (`A-1`). → *Fix
the daily-loss semantics, enforce coverage, drop `--exitfirst` in CI, add the
missing error-branch tests.*

**T5 — The docs of record have drifted behind a feature-complete engine.** No
secret is leaked and the *money-safety* docs (`09-go-live`, `10-deploy`, CLI help)
are accurate, but `doc/dev/README.md`, `01-overview`, `02-architecture`,
`04-brokers`, `05-testing`, `08-program-plan` still describe a "rewrite in
progress" with a `trading_bot/legacy/` tree and a `BrokerRegistry` that no longer
exist, a *read-only* dashboard (it's a control plane), and "Kraken + paper" brokers
that omit the shipped Binance adapter (`G-1`–`G-6`). Also: `.env` is 0664 not 0600
(`G-7`), and a 15-entry `[Unreleased]` pile means a **release is overdue** (`G-10`).
→ *A `/groom-docs` pass + `chmod 600 .env` + cut v0.8.0.*

## "Before the first live order" — blocker checklist

Derived from the Critical/High findings that only bite in live mode. **None block
paper/testnet operation; all block go-live.**

- [ ] **Quantize qty/price to venue tick/lot in the order path** (`B-2`) — wire
      `Instrument.quantize()` into both adapters' submit.
- [ ] **Stop leaking the Binance signature** into logs/exceptions (`B-1`) — redact
      the query string / sign in headers where possible, scrub error messages.
- [ ] **Guarantee a monotonic, locked Kraken nonce** (`B-3`).
- [ ] **Preserve & forward the client-order-id on both venues**, and reconcile on
      it reliably (`B-5`, `B-15`) — the idempotency invariant depends on it.
- [ ] **Map venue error codes → domain errors** and retry the retriable Kraken
      HTTP-200 errors (`B-4`).
- [ ] **Enforce the typed live-confirmation server-side** (`I-2`) and sanitise
      `db_path` / constrain `signal.ref` (`I-1`, `I-3`).
- [ ] **Apply `money()` in the domain constructors** (`D-1`) and pin the `Decimal`
      context on average-price math (`D-4`).
- [ ] **Fix `max_daily_loss` to be actually daily** and reset at the boundary
      (`A-1`); cover the kill-switch/reconcile/WS error branches (`T-4`).
- [ ] **Move persisted-order SQLite writes off the event loop** (`A-2`) — a
      blocking `open→commit→close` on every fill will not survive real throughput.

## Recommended remediation sequencing (as roadmap epics)

These map cleanly onto the `/pick-task → /plan → /execute-leaf` loop; each is one
epic of small PRs.

1. **Test hermeticity & gate honesty first** (`T-1`, `T-2`, `T-3`) — add a
   `conftest.py` with autouse CWD/env isolation, drop `--exitfirst` from CI addopts
   (keep it opt-in), enforce `--cov-fail-under`. *Do this before anything else so
   the gate can be trusted while fixing the rest.*
2. **Broker live-readiness** (`B-1`…`B-5`, `B-15`, `B-13`) — the "before live"
   heart; also tighten paper↔live fidelity.
3. **Domain money hardening** (`D-1`, `D-4`, `D-7`, `D-3` Kraken-pair mis-parse).
4. **Dashboard gate hardening** (`I-1`, `I-2`, `I-3`, plus `I-4` proxy/TLS note).
5. **Engine robustness** (`A-1` daily-loss, `A-2` async SQLite, `A-3` supervisor
   locking, storage `orders` migration `D-2`).
6. **Docs groom + release** (`G-1`…`G-10`, `chmod 600 .env`, cut v0.8.0).

## What is healthy (don't touch)

- Domain purity and layering: no upward imports, no `interfaces` import from
  `application`; verified by grep.
- `Decimal` on the wire and through storage — exact round-trips confirmed.
- Order state machine, `Position` fold math, transport idempotency guard.
- Paper→live PnL partitioning: the past `_replay_paper_book` commingling bug is
  fixed and **no sibling path** (`pnl_series`, `by_mode`, `combined_equity_series`,
  `_combined_ratios`) reintroduces it.
- Dashboard auth: constant-time token compare, 256-bit sessions, server-side
  logout invalidation, non-spoofable login rate-limit, guarded `next=` redirect,
  escaped `innerHTML` sinks, autoescape on.
- The `-m network` e2e suite: exactly one write path, **Binance-testnet-pinned +
  key-gated + cancels in `finally` — no mainnet order path exists.**
- Packaging: all 23 UI templates/static assets are covered by the wheel
  `package-data` globs; console script, `python_requires`, version all consistent
  (0.7.0 across pyproject/CHANGELOG/tag). Sibling deps `importorskip` cleanly on a
  fresh clone.
