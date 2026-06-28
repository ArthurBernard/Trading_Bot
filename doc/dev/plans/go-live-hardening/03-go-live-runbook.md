---
plan: go-live-hardening/03-go-live-runbook
kind: leaf
status: done
complexity: medium
depends: [01, 02]
parallel: false
branch: feat/go-live-opt-in
pr: ""
---

# Go-live runbook + explicit opt-in guard (live OFF by default)

## Goal

A **go-live runbook** (the deliberate opt-in steps, credentials via `.env`, the
pre-trade safety checklist, what's proven vs what still needs a real-key sandbox), and
a **`LiveTradingNotEnabled` opt-in guard** that makes live trading an explicit,
documented, off-by-default choice: paper stays the default; the live path raises a
clear "not enabled — read the runbook" until a deliberate opt-in flag is set. **No real
order is ever sent.** The last E10 leaf — closes the E10 roadmap line (the deferred
**final name** decision stays open).

## Files to change

- `doc/dev/09-go-live.md` — new; the go-live runbook/checklist.
- `trading_bot/domain/errors.py` — add `LiveTradingNotEnabled(TradingBotError)`.
- `trading_bot/application/service_factory.py` — gate live: when `config.mode == "live"`,
  `build_engine` raises `LiveTradingNotEnabled` unless an explicit opt-in is set
  (e.g. `config.live_enabled: bool = False` **and** credentials present), with a message
  pointing at the runbook. Keep the existing credential check. Paper unaffected.
- `trading_bot/application/config.py` — add the `live_enabled: bool = False` opt-in flag.
- `trading_bot/interfaces/cli/main.py` — `run --live` / `serve` surface the guard clearly
  (the `--live` ack already exists; now it also needs `live_enabled` + the runbook ack).
- Tests: `tests/application/test_service_factory.py`, `tests/interfaces/` as needed.

## Steps

1. `LiveTradingNotEnabled` error (rooted at `TradingBotError`) with a message that names
   the runbook and the opt-in flag.
2. `config.live_enabled: bool = False`. `build_engine`: for `mode == "live"`, require
   **both** `live_enabled is True` **and** credentials, else raise `LiveTradingNotEnabled`
   (off-by-default). Paper path is unchanged. Document that even enabled, the real-venue
   adapter still needs a real-key sandbox validation (deferred) before any real order —
   this leaf does NOT send a real order.
3. CLI: the `--live` ack flow now also checks `live_enabled` (config) and points the user
   at `doc/dev/09-go-live.md`; a clear refusal otherwise. No order placed on refusal.
4. `doc/dev/09-go-live.md` runbook: the explicit enable steps (set `live_enabled`,
   provide `.env` credentials, run the hardening suite, validate against a real-key
   sandbox); the pre-trade safety checklist (risk limits set, kill-switch tested,
   reconcile on startup, paper-validated strategy); a "proven vs pending" table
   (what E10-01 demonstrated offline vs what needs a real key/sandbox); the paper-default
   and "never trade money you can't lose" disclaimer.
5. Update the program docs: `06-status` (E10 hardening done; **final name still
   deferred**), `01-overview`/`README` go-live note if useful.

## Tests (via `.venv`)

- `build_engine` with `mode="live"`, `live_enabled=False` → `LiveTradingNotEnabled`
  (clear message), regardless of credentials. Paper config builds fine.
- `mode="live"`, `live_enabled=True`, **no** credentials → still refused (credential
  check). With dummy creds + `live_enabled=True` → builds the live adapter object (but
  the test asserts **no network/order** — construction only, or assert it's the kraken
  adapter without calling it).
- CLI `run --live` without the runbook opt-in → refused, nothing placed.

## Verification on real data

In-process. Assert the live path is off by default (raises `LiveTradingNotEnabled`),
that paper is unaffected (the whole suite still runs paper), and that enabling the flag
+ dummy creds constructs the adapter without ever sending an order. Gates via `.venv`.

## Closeout

- CHANGELOG (Added): "Go-live runbook (`doc/dev/09-go-live.md`) + `LiveTradingNotEnabled` opt-in guard — live is off by default and explicit."
- ADR: live opt-in is off-by-default + the runbook; restate **no real order sent**; note
  the **final project name remains deferred** (not decided by E10).
- Status/roadmap: **remove the E10 line** from `07-roadmap.md`; mark E10 (hardening) done
  in `06-status.md`, and keep the **final-name** deferred decision open.
