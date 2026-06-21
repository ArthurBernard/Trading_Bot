# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Modern packaging via `pyproject.toml`; dev tooling (ruff, mypy, pytest,
  interrogate, pre-commit) and GitHub Actions CI across Python 3.11–3.13.
- Claude Code developer workflow: `CLAUDE.md`, `.claude/` (workflow.json, hooks,
  settings), and the `doc/dev/` orientation pack + plan-tree scaffold.
- Git Flow (`develop` / `master`) with `CONTRIBUTING.md` and a `pre-push` hook.
- Domain primitives — Decimal `money` (float-guarded), venue-neutral `instrument`
  with Kraken normalisation, and the `errors` hierarchy. (#7)
- `Order` aggregate + lifecycle state machine and order types
  (market/limit/stop-loss/best-limit), with exact Decimal fill accounting. (#8)
- `Fill` and `Position` — net exposure rebuilt from an ordered fill sequence
  (flips, fee-aware realised PnL). (#9)
- `Signal` — venue-neutral strategy target (fractional exposure or explicit
  target quantity) with `delta_to(position)`. (#10)
- Pure PnL/KPI performance functions — `pnl`/`cum_pnl`/`equity_curve` (Decimal),
  with Sharpe/Sortino/max-drawdown/Calmar delegated to fynance. Completes the
  **E1 domain core**. (#11)
- `transport.AsyncHTTPClient` — async httpx wrapper (get/post, retry with
  increasing exponential backoff, `Retry-After` on 429, timeouts). (#13)
- `transport.WebSocketBase` — async WS base: `stream_raw()` + increasing
  exponential reconnect, `on_connect` hook, `send()`. (#14)
- `transport.RateLimiter` + `KrakenCallCounter` — per-exchange token-bucket plus
  Kraken's decaying call-counter (tiers, per-endpoint costs). Completes the
  **E2 transport** layer. (#15)
- `brokers.Broker` port (runtime-checkable Protocol over domain types) +
  `Capability` model + `BrokerRegistry`. (#17)
- `brokers.KrakenBroker` — Kraken REST adapter: HMAC-SHA512 request signing
  (verified vs Kraken's published vector), signed orders/balances/fills, public
  market data. Credentials via env; public data works key-free. (#18)
- `brokers.KrakenPrivateWS` — Kraken v2 private-WS `executions` parsing into domain
  `Fill`s / order updates (token-auth, mock-verified; live gated on a key).
  Completes the **E3 Kraken adapter**. (#19)
- `application` kernel — `AppConfig` (pydantic v2, paper-default) + async `EventBus`
  (fan-out queues + sync subscribers; `OrderEvent`/`FillEvent`/`LogEvent`). (#22)
- `brokers.PaperBroker` — in-process fill simulation (immediate/partial fill
  models, fee model), the default broker so the engine runs with no venue.
- `application.OrderRouter` — idempotent order submission (client-order-id dedup,
  incl. concurrent) + order-lifecycle driving + events.
- `application.PositionTracker` — live per-instrument `Position`s folded from
  broker-confirmed `FillEvent`s (delegates to `Position.from_fills`).

### Changed

- `brokers.PaperBroker` is now **port-pure**: `place_order` no longer mutates the
  caller's `Order` (the `OrderRouter` owns the state machine); it returns a venue id
  and reports fills via `fills()` / `FillEvent`s. Removed the router's
  self-driving-broker workaround.

### Changed

- Parked the pre-2026 implementation under `trading_bot/legacy/` (reference only,
  excluded from lint/type-check/tests) ahead of the hexagonal rewrite.
- Bumped version to `0.2.0.dev0` to mark the start of the rewrite.

### Removed

- `setup.py` and `requirements.txt` — folded into `pyproject.toml`.
