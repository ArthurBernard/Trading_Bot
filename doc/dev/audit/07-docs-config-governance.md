# Audit — docs, config & governance (2026-07-02)

## Scope & method

A read-only deep audit of **documentation accuracy, configuration & secrets
hygiene, and repo governance** for the live-trading engine. Nothing was
modified except this report. Branch `develop`, clean tree, v0.7.0.

What was checked and how:

- **Docs vs reality.** Read `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`,
  `CHANGELOG.md`, and the full `doc/dev/` pack (README, 01–10, plans/README +
  in-flight plans). Every command in `CLAUDE.md`/`README`/`CONTRIBUTING` was
  parsed against the installed CLI (`trading-bot --help` + every subcommand
  help: `version/run/status/kpi/serve/start/dashboard`), `ruff --version`,
  `mypy --version`, and `pytest --collect-only`. `pyproject.toml` addopts,
  extras and classifiers were read directly. Every relative markdown link in
  the doc set was resolved programmatically (0 broken). `04-brokers.md` matrix
  checked against `trading_bot/brokers/`; `09-go-live.md` gates checked against
  `application/service_factory.py` + `interfaces/cli/main.py`; `10-deploy.md`
  checked against `deploy/trading-bot.service`.
- **Secrets & config hygiene.** `git ls-files` (236 tracked files) reviewed for
  any file that should be local; `.gitignore` completeness verified; tracked
  files scanned with `git grep -nE` for key/secret/token/base64/hex patterns;
  git history scanned for accidentally-committed secret files
  (`--diff-filter=A`); the one historical DB blob was `strings`-scanned for
  key-like content. File modes of `.env` and `configs/dashboard.yaml` checked.
  Example/template configs inspected for real values. **No secret value is
  printed in this report — locations and patterns only, `<redacted>`.**
- **Governance.** `gh pr list --state open`; PR #122 diff + body assessed
  against the "strategy content stays local" rule. `git log --first-parent`
  on `develop` (merge-only check). Tags vs CHANGELOG releases. Remote branch
  inventory. `[Unreleased]` size vs 0.7.0. ADR PR numbers spot-checked with
  `gh pr view`. `plans/` and `_archive/` state.

## Summary

The engine code and the money-safety documentation (`09-go-live.md`,
`10-deploy.md` gates, CLI help) are accurate and internally consistent — the
go-live gate chain the runbook describes (mode → `live_enabled` → credentials →
risk limits → CLI ack) matches `service_factory.build_engine` /
`_resolve_mode` line-for-line. **No secret is leaked in any tracked file or in
git history.** The secrets/config hygiene is largely sound: `.env`, `configs/`,
`var/`, `*.sqlite`, `.python-version`, `doc/dev/_archive/` are all correctly
gitignored and untracked; PR #122 is CHANGELOG-only and compliant with the
local-only strategy rule.

The problems are concentrated in the **architecture/status documentation**,
which has drifted badly behind a feature-complete, heavily-shipped engine.
Several `doc/dev/` files still describe the system as a *rewrite in progress*
with a `trading_bot/legacy/` tree that no longer exists, a `BrokerRegistry`
that was removed, a *read-only* dashboard that is actually a full control
plane, and a broker set of "Kraken + paper" that omits the shipped Binance
adapter. `doc/dev/README.md`'s top banner directly contradicts the repo-root
`CLAUDE.md`. Two smaller hygiene issues: `.env` is group/world-readable (0644-
class, should be 0600), and a runtime paper DB was once committed then removed
(now history-only, contains no secrets). A large `[Unreleased]` pile (15
entries / ~150 lines, all the dashboard epic #113–#125) means a release is
overdue.

| ID | Severity | Location | Title |
|----|----------|----------|-------|
| G-1 | High | doc/dev/README.md:13–16 | "Rewrite in progress" + `trading_bot/legacy/` banner contradicts reality & CLAUDE.md |
| G-2 | Medium | doc/dev/04-brokers.md (whole) | Broker matrix stale: Kraken "being built", WS "planned", Binance absent, `legacy/` refs |
| G-3 | Medium | doc/dev/02-architecture.md:1,49–84,86,94 | Architecture doc: "target/later" framing, missing modules, `scheduler.py`/`registry` that don't exist, dashboard called read-only |
| G-4 | Medium | doc/dev/05-testing.md:3–4,8 | False `--ignore=trading_bot/legacy` pytest claim (no such flag, no legacy tree) |
| G-5 | Medium | doc/dev/01-overview.md:34,51,78 | Overview stale: Kraken-only brokers, missing modules, `registry` in history table |
| G-6 | Medium | doc/dev/08-program-plan.md (whole) | Rewrite plan framed as future work; open "rename" leaf contradicts settled name decision |
| G-7 | Low | .env (mode 0664) | Secrets file is group/world-readable; should be 0600 |
| G-8 | Low | CONTRIBUTING.md:41 | False "legacy excluded" pytest comment |
| G-9 | Low | doc/dev/06-status.md:22 | Stale test count ("~718" vs 898 collected) and "Last updated" predates 7 shipped PRs |
| G-10 | Low | CHANGELOG.md `[Unreleased]` | 15-entry / ~150-line pile since 0.7.0 — release overdue |
| G-11 | Info | git history 3b2246a `var/alloc1.sqlite` | Runtime paper DB was once committed then removed (no secrets in blob) |
| G-12 | Info | configs/dashboard.yaml:26 | UI token stored inline in a local 0600 file (doc recommends env var instead) |
| G-13 | Info | CLAUDE.md:41 vs README.md:45 | Install-extra inconsistency (`[dev,daemon]` vs `[dev]`) — both work, not aligned |
| G-14 | Info | data_base/, general_config_example.yaml, execution_scripts/ | Pre-rewrite legacy artifacts still tracked (dead, but harmless) |

No **Critical** findings (no leaked secret in a tracked file or reachable history).

---

## Findings

### G-1 [High] "Rewrite in progress" + `trading_bot/legacy/` banner contradicts reality and CLAUDE.md

- **Location**: `doc/dev/README.md:13–16` (also :22–23, :26).
- **Evidence**: The callout reads *"**Rewrite in progress.** Much of the
  architecture here is the **target**, not the current reality. The pre-2026
  code is parked under `trading_bot/legacy/`."* There is **no `trading_bot/legacy/`**
  directory in-tree (`git ls-files | grep legacy` → nothing; it was deleted, git
  history only). The repo-root `CLAUDE.md` explicitly states *"rewrite complete
  through the MVP … no in-tree legacy package … lives in git history only"* and
  `06-status.md:7` says *"the engine is feature-complete and hardened."*
  `doc/dev/README.md` is the **entry point** of the Claude-oriented dev brief
  (`CLAUDE.md` points readers here first), so this is the first thing a reader
  sees and it is wrong on two independent counts.
- **Impact**: The single most misleading doc in the repo. An agent (or human)
  starting from the documented entry point will believe the architecture is
  aspirational and that a `legacy/` tree exists to consult — both false — and
  may re-plan already-shipped work or search for a non-existent directory.
- **Recommendation**: Rewrite the banner to "Rewrite complete; feature-complete
  through v0.7.0. No in-tree `legacy/` — pre-2026 code is in git history only."
  Drop the "target" qualifier from the 02-architecture pointer (:22–23) and add
  Binance to the 04-brokers one-liner (:26).

### G-2 [Medium] Broker capability matrix is stale (Kraken "being built", WS "planned", Binance absent, `legacy/` refs)

- **Location**: `doc/dev/04-brokers.md` (whole file; matrix at :17–22, caveats
  :24–32).
- **Evidence**:
  - :19 — Kraken row: *"REST ✅ | WS (private fills) **planned** | … | **MVP
    target — being built**"*. Reality: `brokers/kraken.py` (REST) **and**
    `brokers/kraken_ws.py` (private fills WS) are both implemented and wired —
    `07-roadmap.md:35` itself says *"Live fill streaming — done. `KrakenPrivateWS`
    is wired into the run loop via `LiveFillStreamer`."* So 04-brokers contradicts
    the roadmap.
  - The matrix has **no Binance row at all**, yet `brokers/binance.py` is a
    shipped second live venue (`06-status.md:26–31`, README:23).
  - :21 references `legacy/exchanges/API_bfx.py`; :24 *"mined from
    `legacy/exchanges/API_kraken.py`"*; :29 `legacy/tools/call_counters.py`;
    :30 `legacy/exchanges/API_kraken.py` again. None of these paths exist
    (no `legacy/` tree).
  - :3 and :5 — *"target: `brokers/base.py`"* and *"only Kraken is implemented at
    MVP"* — both stale (base.py exists; Kraken + Binance + paper implemented).
- **Impact**: The per-broker capability matrix is a doc-of-record for what each
  venue can do; a reader routing an order or planning a venue would be misled
  about Kraken WS support and Binance's existence, and would chase dead
  `legacy/` file references.
- **Recommendation**: Rewrite the matrix: Kraken REST ✅ / WS ✅ / **implemented**;
  add a Binance row (REST ✅, spot, testnet-capable); PaperBroker default. Replace
  every `legacy/…` reference with the actual module (`transport/ratelimit.py`,
  `domain/instrument.py`) or a note that the mapping already shipped.

### G-3 [Medium] Architecture doc framed as "target/later", lists modules that don't exist, dashboard called read-only

- **Location**: `doc/dev/02-architecture.md` — title :1, :3–5, brokers :49–53,
  application :64–76, interfaces :78–84, data-flow :86,:94.
- **Evidence**:
  - :1 title *"# 02 — Architecture (target)"*, :3–5 *"The MVP layers are in
    place"*, :86 *"## Data flow (target)"* — the architecture is shipped/feature-
    complete, not a target.
  - Brokers table (:49–53) lists only `kraken.py` + `paper.py`; **Binance
    missing**; :94 data-flow diagram shows *"Broker (paper | kraken)"* only.
  - Application table (:64–76) names **`performance.py`** (:73) — the real module
    is `application/performance_service.py` (there is a `domain/performance.py`
    but no `application/performance.py`) — and **`scheduler.py`** (:75), which
    does **not exist** in `application/` (orchestration lives in `orchestrator.py`
    / `supervisor.py` / `portfolio_runner.py`). :76 *"service_factory … builds
    brokers, stores, **registries**"* — no registry exists (removed). The table
    omits ~12 shipped modules: `data_feed`, `data_provider`, `live_fills`,
    `orchestrator`, `pnl_series`, `portfolio`, `portfolio_feed`, `portfolio_runner`,
    `reconcile`, `run_app`, `strategy`, `supervisor`.
  - :83–84 *"`api/` + `ui/` — FastAPI + Jinja2 dashboard (positions/orders/PnL),
    **later**"* — false twice: it shipped (not "later"), and it is a **control
    plane** (start/stop, mode switch, deploy/remove — `03-decisions.md:27`
    "Retire the split web apps onto one dashboard", PR #120), with read-only an
    opt-in posture, not a read-only reporting view.
- **Impact**: The canonical architecture reference misnames a module
  (`scheduler.py`, `application/performance.py`), points at a removed abstraction
  (registry), understates the broker set, and mis-describes the dashboard as
  read-only reporting.
- **Recommendation**: Drop "(target)" from the title and data-flow heading;
  refresh both tables to the actual module set; add Binance; describe the
  dashboard as the shipped unified control plane (read-only = opt-in).

### G-4 [Medium] False `--ignore=trading_bot/legacy` pytest claim

- **Location**: `doc/dev/05-testing.md:3–4` and :8.
- **Evidence**: :3–4 *"The legacy tree is excluded from collection
  (`--ignore=trading_bot/legacy`)."* :8 *"pytest # full suite (legacy & network
  excluded)"*. The actual `pyproject.toml:106` addopts is
  `--exitfirst -vv --cov=trading_bot --cov-report=term-missing -m 'not network'`
  — there is **no `--ignore` flag** and no `legacy` tree to ignore. Only the
  `network` marker is deselected.
- **Impact**: A contributor debugging collection or CI would look for a
  non-existent `--ignore` flag / `legacy` path.
- **Recommendation**: Replace with the real addopts; state that only `network`
  tests are excluded by default. Also refresh the ":37 Invariants under test (as
  layers land)" framing — the engine is complete, not landing.

### G-5 [Medium] Overview understates brokers, lists missing modules, references removed `registry`

- **Location**: `doc/dev/01-overview.md:34`, :51, :78 (also :43, :55).
- **Evidence**:
  - :34 lists application as *"router/risk/tracker/perf/reconcile/strategy/
    datafeed/runner/orchestrator"* — omits the portfolio stack, `supervisor`,
    `live_fills`, `pnl_series`, `data_provider`, `run_app`.
  - :34 / :51 brokers described as *"Kraken + paper"* — Binance shipped.
  - :55 *"interfaces/ # Typer CLI"* — omits `api/` + `ui/` (full dashboard).
  - :78 history table row *"`brokers/kraken.py` (+ port, **registry**)"* — the
    `BrokerRegistry` was removed (`06-status.md:53` "removed the dead
    `BrokerRegistry`").
  - :43 *"multi-exchange-ready with **Kraken first**"* — now understated
    (Binance is a second implemented venue).
- **Impact**: Overview is the second doc a reader hits; understates the shipped
  surface and references a removed abstraction.
- **Recommendation**: Add Binance and the missing modules; drop "registry" from
  the history table; mention the dashboard under `interfaces/`.

### G-6 [Medium] Program plan framed as future work; contains an open "rename" leaf that contradicts the settled name decision

- **Location**: `doc/dev/08-program-plan.md` — whole file; specifically the
  status table :39–45, :36–37, E3 registry :77, E7 legacy-removal :116, E9
  dashboard :130–131, E10-03 rename :133/:139/:150, deferred decisions :148–150.
- **Evidence**: The document reads as the forthcoming E1→E10 rewrite plan
  ("written epic by epic, just before that epic is executed"), but all epics
  shipped through v0.7.0. The status table (:39–45) still shows *E2…E10 "⏳
  written via /plan at the start of each epic."* Two settled decisions are still
  listed as **open**: :139 *"03 rename | chore/rename | … **Resolves: final
  project name**; apply to package/repo/docs (do last)"* and :148–150 list
  "final project name" + "paper-vs-live default" as unresolved — but
  `07-roadmap.md:46` and `01-overview.md:43` record the name as **decided (no
  rename)** and paper-default is shipped. :77 references an E3 `registry` (removed);
  :116 an E7 "Delete the legacy modules" leaf (done).
- **Impact**: A `/pick-task` reader could resurrect a closed "rename" task or
  treat the whole rewrite as pending. Contradicts the roadmap's own "name —
  decided; closed" note.
- **Recommendation**: Either mark 08-program-plan as a historical/archived record
  ("all epics shipped — see git + CHANGELOG") or update the status table to
  ✅-shipped and strike the settled decisions (name, paper-default) and the
  registry/legacy leaves.

### G-7 [Low] `.env` is group/world-readable (should be 0600)

- **Location**: repo-root `.env` (mode **0664**).
- **Evidence**: `stat -c '%a' .env` → `664`. This is the file `09-go-live.md`
  and `10-deploy.md` designate for `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` and
  Binance keys. By contrast `configs/dashboard.yaml` is correctly `0600`.
  `.env` is gitignored (not committed), so this is a **local filesystem
  permission** issue, not a repo leak. The docs themselves insist credentials be
  "NEVER world-readable" (`10-deploy.md:11`, unit :48).
- **Impact**: Any other local user / process can read live-trading API secrets
  from `~/dev/Trading_Bot/.env`. Low because single-user machine, but it's the
  one place real keys live and the project's own stance is 0600.
- **Recommendation**: `chmod 600 .env`. Consider having `session_start` or a
  make/CI check warn when `.env` is not 0600.

### G-8 [Low] CONTRIBUTING.md repeats the false "legacy excluded" claim

- **Location**: `CONTRIBUTING.md:41`.
- **Evidence**: `pytest # full suite (legacy excluded, network excluded)` — same
  non-existent legacy exclusion as G-4. (The setup/Git-Flow/commit sections of
  CONTRIBUTING are otherwise accurate; `pip install -e ".[dev]"` +
  `".[dev,triptych]"` and `ruff`/`mypy` invocations all parse.)
- **Impact**: Minor; misleads on pytest behaviour.
- **Recommendation**: Change to "network excluded" only.

### G-9 [Low] Status doc test count and date are stale

- **Location**: `doc/dev/06-status.md:22` (and header :3 "_Last updated:
  2026-06-29_").
- **Evidence**: :22 says *"~718 tests green"*. `pytest --collect-only` now reports
  **898 tests collected (9 deselected)**; `grep -c 'def test_'` ≈ 866. The "Last
  updated 2026-06-29" predates the seven dashboard PRs #113–#125 merged into
  `develop` (the whole unified-dashboard epic), which the status prose does not
  reflect (it still describes a "read-only dashboard (`trading-bot serve`)" at
  :17/:26 as the headline UI, though the shipped dashboard is a control plane —
  same read-only mischaracterisation as G-3).
- **Impact**: The where-things-stand doc undercounts tests by ~25% and predates
  a whole shipped epic; a reader gets an out-of-date picture of the UI.
- **Recommendation**: Refresh the count, bump the date, and note the unified
  control dashboard (not just read-only serve) as the current UI.

### G-10 [Low] CHANGELOG `[Unreleased]` pile — release overdue

- **Location**: `CHANGELOG.md` `[Unreleased]` section (:7–157, before
  `## [0.7.0] - 2026-06-30` at :158).
- **Evidence**: `[Unreleased]` spans ~150 lines / **15 bold-lead feature
  entries**, covering the entire unified-dashboard epic (#113, #115, #117, #118,
  #119, #120, #123, #125). 0.7.0 was cut 2026-06-30; a comparable amount of work
  has accumulated since. Per the repo's own release cadence (v0.2.0→v0.7.0 all
  within a week) this is a large unreleased pile.
- **Impact**: Governance/hygiene — a big `[Unreleased]` pile is the signal to
  run `/release`; the auto-memory note "release freeze before feature work"
  makes an overdue release a friction point.
- **Recommendation**: Cut v0.8.0 (`/release`) to freeze the dashboard epic before
  more features pile on.

### G-11 [Info] Runtime paper DB was committed then removed (no secrets)

- **Location**: git history — `var/alloc1.sqlite` added in `3b2246a`
  ("feat: ALLOC1 Kraken config (paper) …"), removed in `1040bcb`
  ("chore: untrack var/alloc1.sqlite (leaked runtime DB)").
- **Evidence**: The blob (36 864 bytes) is **absent from `develop`, `master`,
  `origin/develop`, `origin/master`** (verified with `git cat-file -e`). A
  `strings` scan of the historical blob for `api.?key|api.?secret|BEGIN.*PRIVATE`
  and long base64/hex found **no key-like content** — it is a paper-mode
  order/fill SQLite (no credentials are ever stored in the engine DB). The local
  working-copy `var/alloc1.sqlite` is now untracked and gitignored
  (`git check-ignore` → `.gitignore:33 var/`).
- **Impact**: Not a secret leak. The only residue is a ~36 KB paper-book blob in
  unreachable history (dangling on the old feature branch's history). No action
  needed for security; noted for completeness because a runtime DB should never
  have been committed.
- **Recommendation**: No rewrite of history needed (no secret, off both mainline
  refs). The `.gitignore` `var/` + `*.sqlite` rules already prevent recurrence.

### G-12 [Info] UI token stored inline in the local dashboard manifest

- **Location**: `configs/dashboard.yaml:26` (`ui.token: <redacted>`).
- **Evidence**: The gitignored, `0600` `configs/dashboard.yaml` carries a
  `ui.token:` value inline. The file's own header comment (:19–20) and
  `10-deploy.md:110–116` recommend keeping the token in the
  `TRADING_BOT_UI_TOKEN` env var *"so it never sits in a file"*. It is correctly
  0600 and untracked (`git ls-files` shows no `configs/` tracked), so there is
  no repo leak.
- **Impact**: None externally; the token is a local dashboard-auth secret, not an
  exchange key, and the file is 0600 + gitignored. Noted only as a deviation from
  the project's own stated preference.
- **Recommendation**: Optionally move the token to `TRADING_BOT_UI_TOKEN` per the
  doc; leave `ui.host/port/read_only` in the manifest.

### G-13 [Info] Install-extra inconsistency across CLAUDE.md / README / CONTRIBUTING

- **Location**: `CLAUDE.md:41` vs `README.md:45,51` vs `CONTRIBUTING.md:8,11`.
- **Evidence**: `CLAUDE.md` says `pip install -e ".[dev,daemon]"`; README and
  CONTRIBUTING say `pip install -e ".[dev]"` then `".[dev,triptych]"`. In
  `pyproject.toml` the `dev` extra **already pulls in** typer, rich, fastapi,
  uvicorn, jinja2, apscheduler (the same packages `daemon` provides), so
  `".[dev]"` alone yields a working CLI + dashboard + daemon for tests — both
  forms work. But the docs are not aligned, and only `CLAUDE.md` names `daemon`.
- **Impact**: Cosmetic; no functional breakage. A reader following README won't
  install the `daemon` extra by name but gets its contents via `dev` anyway.
- **Recommendation**: Pick one canonical install line and mirror it across the
  three docs (e.g. `".[dev]"` for contributors, `".[daemon]"` for a runtime-only
  deploy as the systemd unit uses).

### G-14 [Info] Pre-rewrite legacy artifacts still tracked

- **Location**: `data_base/example/*.dat` (+ `other_example/`),
  `general_config_example.yaml`, `execution_scripts/{bot.sh,bot_manager.sh,
  var_example.sh,var_instructions.sh}`.
- **Evidence**: These are pre-2026 (pre-rewrite) sample data and shell wrappers
  still in `git ls-files`. `general_config_example.yaml` contains placeholder
  values only (`password_example`, `address_example`, `/home/user/example/…`) —
  **no real secret**. The `.dat` files are old OHLC sample rows. None are
  referenced by the current engine (the CLI/config path is `examples/config.example.yaml`
  + `strategies/example*/`).
- **Impact**: Dead weight / mild confusion — a reader might think `data_base/` or
  `execution_scripts/bot.sh` is part of the current engine. No security issue
  (placeholders only).
- **Recommendation**: Remove these legacy artifacts (or move under a clearly
  labelled `_legacy_examples/`) in a `chore:` PR; they belong in git history like
  the rest of the pre-rewrite code.

---

### Cross-cutting note — what is correct (checked, no finding)

- **No leaked secret** in any tracked file: the only long-hex/base64 hits are
  documented public **test vectors** (`test_binance_rest.py:51/57`,
  `test_kraken_rest.py:41/54` — Binance/Kraken's own signing-vector fixtures) and
  code identifiers (`get_credentials()`, `TokenProvider`), not credentials.
- **`.gitignore` is complete** for the sensitive set: `.env`, `configs/`,
  `strategies/*` (with example allowlist), `var/`, `*.sqlite`, `.python-version`,
  `doc/dev/_archive/` all covered; `_archive/` and any `configs/*.yaml` are
  untracked as intended.
- **PR #122** (`docs/alloc1-kraken-data-complete`) touches **only `CHANGELOG.md`**
  and its body states the strategy configs stay local/gitignored — **compliant**
  with the "strategy content stays local" rule.
- **Git-flow branch protection holds**: `git log --first-parent develop` is
  merge-commits only (PRs #97…#125) — no direct feature commits on `develop`;
  `.githooks/pre-push` hard-blocks `master` and warns on `develop`.
- **Tags ↔ CHANGELOG consistent**: tags `v0.2.0…v0.7.0` match the CHANGELOG
  release headers.
- **All documented commands parse**: every `trading-bot` subcommand help renders;
  `README:27`'s `run --serve` **does exist** (a real read-only monitoring flag);
  `ruff`/`mypy`/`pytest` invocations run; the `pytest trading_bot/tests/test_smoke.py`
  path exists.
- **09-go-live.md gates are accurate** end-to-end against `service_factory.py`
  (mode → `live_enabled` → credentials → all-three-risk-limits `BrokerError` →
  CLI `--yes-i-understand`); the testnet hard-pin exemption is correctly
  described.
- **10-deploy.md ↔ `deploy/trading-bot.service` agree**: `ExecStart` runs
  `trading-bot dashboard` (matches the CLI); `EnvironmentFile` guidance is 0600;
  hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`,
  `PrivateTmp`, `StateDirectory`) is sane. One nuance worth a maintainer's eye
  (not raised as a formal finding since the shipped unit points `--config` at
  `/etc/trading-bot/config.yaml`, and the dashboard's manifest-rewrite on
  add/remove would hit a read-only `/etc` under `ProtectSystem=strict`): if you
  use the dashboard's deploy/remove CRUD in production, point `--config` at a
  path under `StateDirectory` (`/var/lib/trading-bot`, the only `ReadWritePaths`
  entry) so manifest persistence succeeds.
- **ADR PR numbers real**: spot-checked #125/#120/#118/#117/#76/#75/#71/#66 with
  `gh pr view` — all exist and are merged. `07-roadmap.md`'s two open lines
  (Binance futures testnet; real-key live enablement) are still accurate.
  `doc/dev/_archive/` is untracked; `plans/` holds the executed epics' trees (the
  workflow keeps them as durable records rather than deleting — consistent with
  `plans/README.md`).
