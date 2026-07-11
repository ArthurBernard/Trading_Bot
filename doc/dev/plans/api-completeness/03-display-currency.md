---
plan: api-completeness/03-display-currency
kind: leaf
status: planned
complexity: medium
depends: [02]
parallel: false
branch: feat/display-currency
pr: ""
---

# 03 — display currency: global default, per-exchange override, static rates

## Goal

One configured display currency so cross-quote figures can be read (and
later aggregated) in a single unit — converted **server-side** so every
consumer agrees (pinned decision). All additive.

Config (`trading_bot/application/config.py`, app/daemon level — same
altitude as `paper_strict`):

- `display_currency: str = "USD"` (canonical asset code);
- `display_currency_overrides: dict[str, str] = {}` (exchange → currency,
  e.g. `{"binance": "USDT"}`);
- `conversion_rates: dict[str, Decimal] = {}` — rate of ONE unit of a quote
  currency IN the display currency (e.g. `{"USDT": "1", "EUR": "1.08"}`);
  identity rate implied for the display currency itself. Validate: positive
  Decimals via the money-field pattern.

Serialization (additive):

- position rows (`_position_row_dict`): `display_currency` (resolved for the
  row's exchange) + `value_display` / `unrealised_display` (converted from
  the row's quote via the rates; `null` when no rate is declared for that
  quote — NEVER guess a rate);
- strategy rows (`_status_dict`) and `/api/kpi` aggregate rows: same pair on
  the money aggregates (`total_value_display` etc. — pick names mirroring
  the existing fields + `_display` suffix, consistently);
- a small pure helper `application/display_ccy.py` (`resolve_currency
  (exchange, config)`, `convert(amount, from_ccy, config) -> Money | None`)
  so supervisor/app share one conversion — no float, exact Decimal, `None`
  on missing rate.

## Files to change

- `trading_bot/application/config.py` — the three fields.
- `trading_bot/application/display_ccy.py` — **new** pure helper.
- `trading_bot/interfaces/api/app.py` — row/aggregate serialization.
- `trading_bot/tests/application/test_display_ccy.py` — **new**; dashboard
  test module — API-shape tests.

## Steps

1. Read the config validation patterns + where `_position_row_dict` /
   `_status_dict` / `_kpi_row_dict` live; implement helper → config →
   serialization.
2. All three gates green.

## Tests

- Pure: identity conversion; declared-rate conversion (Decimal-exact);
  missing rate → `None`; override resolution (binance→USDT, default
  elsewhere); invalid rate rejected at config parse.
- API: rows carry `value_display`/`display_currency`; missing rate →
  `null` displays with native fields untouched; contract regression on all
  pre-existing fields.

## Verification on real data

Real supervisor over the two book copies with
`display_currency="USD"`, `overrides={"binance":"USDT"}`,
`rates={"USDT":"1"}`: `/api/positions` — Binance rows display in USDT
(identity vs native), Kraken rows in USD; declare `rates={}` and verify the
displays go `null` while native values stay; report sample JSON rows.
Originals and port 8000 untouched.

## Closeout

- CHANGELOG `[Unreleased] > Added`: "display currency (global +
  per-exchange override + static `conversion_rates`) — server-side converted
  `*_display` money fields on position/strategy/KPI rows; missing rate →
  null, never a guessed conversion (#XX)."
- ADR: server-side conversion + never-guess-a-rate policy.
- Tick leaf 03 in `00-plan.md`; archive per `/finish-task`.
