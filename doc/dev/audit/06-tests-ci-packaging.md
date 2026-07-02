# Audit — tests, CI & packaging (2026-07-02)

## Scope & method (incl. exact gate results)

Deep review of test quality, CI, packaging and tooling for the live-trading
engine at `/home/arthur/dev/Trading_Bot` (branch `develop`, tree clean at start).

Read + ran (all offline, **never** `-m network`; nothing installed/uninstalled;
no file modified except this deliverable):

- The whole suite `trading_bot/tests/` (~19.7 kLOC across `domain/`, `transport/`,
  `brokers/`, `storage/`, `application/`, `hardening/`, `interfaces/`, `fixtures/`)
  — mapped file-by-file; no `tests/e2e/` dir (network tests live inline, gated).
- `pyproject.toml` (deps/extras/pins, ruff/mypy/pytest/coverage/interrogate),
  `.python-version` (`trading_bot_env`), `MANIFEST.in`, `.github/workflows/ci.yml`,
  `.pre-commit-config.yaml`, `.claude/workflow.json`, `.gitignore`, `CHANGELOG.md`.
- No `Makefile`, no `.githooks/`, **no `conftest.py` anywhere in the repo.**

### Exact gate results (Python 3.12.13, `trading_bot_env`)

| Gate | Command | Result | Runtime |
|------|---------|--------|---------|
| Lint | `ruff check trading_bot/` | **PASS** — "All checks passed!" | 0.02 s |
| Types | `mypy trading_bot/` | **PASS** — "Success: no issues found in 50 source files" | 0.50 s |
| Tests (as configured) | `python -m pytest -q` | **FAIL — 1 failed, 810 passed, 9 deselected** (aborted early by `--exitfirst`) | 9.0 s |
| Tests (full, no `-x`) | `pytest -o addopts="--cov=trading_bot -m 'not network'"` | **FAIL — 4 failed, 894 passed, 9 deselected** | 9.5 s |
| Coverage | (from the full run) | **TOTAL 96 %** (4387 stmts, 181 miss) | — |

**The pytest gate is RED locally** (4 failures). Verbatim (truncated):

```
FAILED trading_bot/tests/interfaces/test_dashboard.py::test_dashboard_builds_app_and_calls_uvicorn
    assert kwargs["host"] == "127.0.0.1"  -> AssertionError: '0.0.0.0' == '127.0.0.1'
FAILED trading_bot/tests/interfaces/test_dashboard.py::test_dashboard_default_config_is_paper
    test_client.get("/api/health").json()["mode"]  -> KeyError: 'mode'
FAILED trading_bot/tests/interfaces/test_dashboard.py::test_dashboard_non_loopback_without_token_refuses
    assert result.exit_code != 0  -> assert 0 != 0
FAILED trading_bot/tests/interfaces/test_dashboard.py::test_dashboard_read_only_flag
    test_client.get("/api/health").json()["read_only"]  -> KeyError: 'read_only'
```

Root cause (fully diagnosed, see **T-1**): these four tests invoke the `dashboard`
command with no `-c`, which resolves the **CWD-relative** default manifest
`configs/dashboard.yaml` (`interfaces/cli/main.py:890`). The developer's real,
gitignored `configs/dashboard.yaml` exists at the repo root with `ui.host: 0.0.0.0`
and a live `ui.token`; the tests read it, so the app binds `0.0.0.0`, auth is on,
`/api/health` returns `401 {"detail":"Unauthorized"}` (hence the `KeyError`s), and
the non-loopback-refusal never triggers (a token is present). From a **clean CWD**
(no `configs/dashboard.yaml`, i.e. a fresh CI clone) all four **PASS** — verified.
So **CI is green while the local gate is red**: the suite's pass/fail depends on
developer-local filesystem state.

Worst-covered source files (per-file coverage from the full run):

| File | Cover | Notably-uncovered (critical) |
|------|-------|------------------------------|
| `interfaces/cli/main.py` | **82 %** | `dashboard`/`start --serve` serve paths, several command error branches (429-461, 862-881) |
| `transport/ws.py` | **87 %** | reconnect/close branches (76-78, 191-193, 219, 225) |
| `brokers/kraken_ws.py` | **91 %** | token-missing `BrokerError` (135-137), malformed-entry/fill guards (363, 374, 389, 393) |
| `interfaces/api/app.py` | **93 %** | auth/SSE/error branches (33 lines) |
| `application/risk.py` | **94 %** | **kill-switch cancel-failure `except` blocks (373, 376-388)** |
| `application/supervisor.py` | **95 %** | 21 lines incl. start/stop error paths (1267-1271) |
| `brokers/kraken.py` | **95 %** | signed-error branches (255, 261-263, 341, 462) |
| `application/run_app.py` | **96 %** | 496-497, 655, 681-684 |
| `application/reconcile.py` | **96 %** | 255-259 (a divergence branch) |
| `storage/sqlite_store.py`, `transport/http.py` | **96 %** | migration/retry tail branches |

## Summary

The **type and lint gates are healthy** (ruff + mypy both clean, sub-second,
mypy strict on `domain/` only as intended). The **test gate is not trustworthy as
a gate**: it is red on the developer's machine yet green in CI, because at least
five `dashboard` tests read a CWD-relative, secret-bearing config file that no
fixture isolates — the suite is **non-hermetic** (there is *no* `conftest.py` and
*no* autouse CWD/env isolation anywhere). Coverage is a genuine 96 %, but it is
**measured, never enforced** (no `fail_under`, no `--cov-fail-under`), and the
uncovered ~4 % is concentrated on exactly the money-critical error branches
(kill-switch cancel-on-failure, order-router forbidden-transition, WS reconnect,
reconcile divergence). CI is a single job doing lint→mypy→pytest, but **`pytest`
in CI inherits `--exitfirst`** (first failure hides all the rest) and CI runs
`ruff check` **without `ruff format --check`** despite pre-commit auto-formatting
(a parity gap that can wedge CI). Packaging is mostly sound — **all 23 UI assets
are covered by the `package-data` globs** (verified by simulating the globs), the
console script and `python_requires` are correct, version is consistent
(`pyproject 0.7.0` == CHANGELOG `[0.7.0]` == tag `v0.7.0`) — but `MANIFEST.in`
carries a dead `recursive-include *.ini` directive and the sdist relies solely on
declared `package-data`. The **`-m network` suite is safe**: exactly one write
path exists and it is Binance-**testnet**-pinned, key-gated and cancels in
`finally`; **no mainnet order path exists**. Sibling-repo deps (`fynance`, `dccd`)
are all guarded by `importorskip` and would **skip**, not error, on a fresh clone;
`strategies.alloc1` appears only as unresolved config strings. `interrogate` is
configured (`fail-under=80`) but **invoked by nothing** (dead tooling). No stray
`__pycache__`/`*.sqlite`/`var/` is git-tracked; `.gitignore` is thorough.

| ID | Severity | Location | Title |
|----|----------|----------|-------|
| T-1 | **Critical** | `interfaces/cli/main.py:890`, `tests/interfaces/test_dashboard.py:1208+` | Pytest gate red locally / green in CI: non-hermetic tests read CWD-relative secret-bearing `configs/dashboard.yaml` |
| T-2 | High | `pyproject.toml:106` + `ci.yml` | `--exitfirst` baked into `addopts` — CI hides all failures after the first; also invalidates coverage totals |
| T-3 | High | `pyproject.toml` / `ci.yml` | Coverage measured but never enforced (no `fail_under`), and CI uploads no coverage artifact |
| T-4 | High | `application/risk.py:373-388`, `order_router.py:307-311,347`, `reconcile.py:255-259`, `kraken_ws.py`, `transport/ws.py` | Money-critical error branches (kill-switch cancel-failure, forbidden reject transition, reconcile divergence, WS reconnect) are the uncovered ~4 % |
| T-5 | Medium | `.pre-commit-config.yaml` vs `ci.yml` | Hook/CI parity gap: pre-commit `ruff-format` + `--fix`; CI runs only `ruff check` (no `format --check`), never mypy/pytest in the hook |
| T-6 | Medium | `MANIFEST.in` | Dead `recursive-include trading_bot *.ini` (no `.ini` exists); sdist asset shipping rests solely on `package-data` |
| T-7 | Medium | `ci.yml` | No dependency pinning/lock/hashes; actions pinned by moving tag (`@v4`/`@v5`) not SHA — supply-chain drift |
| T-8 | Medium | `pyproject.toml:105-108` | No `filterwarnings`, no `asyncio_default_fixture_loop_scope` (pytest-asyncio 1.4.0) — warnings unpoliced |
| T-9 | Low | `tests/interfaces/test_cli_commands.py:430` | One real wall-clock `asyncio.sleep(0.12)` — timing-flaky under load |
| T-10 | Low | `pyproject.toml:112-116`, `interrogate` config | `interrogate` configured (`fail-under=80`) but run by nothing — dead tooling |
| T-11 | Low | `interfaces/api/app.py:807` | `parents[3]` repo-root walk for `strategies/` discovery is source-checkout-only; misleading "sibling of the installed package" comment |
| T-12 | Low | `tests/hardening/_faulty_broker.py:185`, `test_dashboard.py:1323`, `test_run_app.py:431` | Tests coupled to implementation details (private `_fills`, exact log/error strings) |
| T-13 | Info | `ci.yml` | CI is one job (lint→mypy→pytest) with `pip` cache + concurrency cancel; `.[triptych]`/`.[daemon]` never exercised in CI beyond `.[dev]` |
| T-14 | Info | `tests/*` | `-m network` suite: exactly one write path, Binance-testnet-pinned + key-gated + cancels in `finally`; **no mainnet order path** |
| T-15 | Info | (whole suite) | No `conftest.py`, no autouse isolation; fakes are hand-written (no `unittest.mock`) — good, but isolation is per-test and inconsistent |

## Findings

### T-1 [Critical] Pytest gate is red locally / green in CI — non-hermetic tests read a CWD-relative, secret-bearing default config

**Location.** `trading_bot/interfaces/cli/main.py:890` (`_DEFAULT_MANIFEST =
pathlib.Path("configs/dashboard.yaml")` — a **relative** path resolved against the
process CWD); tests `trading_bot/tests/interfaces/test_dashboard.py:1208`
(`test_dashboard_default_config_is_paper`, invoke at line 1221), `:1173`
(`test_dashboard_builds_app_and_calls_uvicorn`, line 1193), `:1305`
(`test_dashboard_non_loopback_without_token_refuses`, line 1320), `:1327`
(`test_dashboard_read_only_flag`, line 1338). No `conftest.py` exists to isolate CWD.

**Evidence.** `python -m pytest -q` fails; the full run yields the four failures
quoted in *Scope & method*. Reproduced the mechanism directly: invoking the
`dashboard` command with the repo's real `configs/dashboard.yaml` present binds
`host=0.0.0.0`, prints "dashboard auth: token login enabled", and `/api/health`
returns `401 {"detail":"Unauthorized"}` (→ `KeyError: 'mode'`/`'read_only'`). Ran
the same four tests from a clean CWD (`/tmp`, no `configs/dashboard.yaml`, same
interpreter): **5 passed**. The real file (gitignored, so it never reaches CI) has
`ui.host: 0.0.0.0` and a live `ui.token` — the tests inherit it because nothing
`chdir`s them to `tmp_path`. Two sibling tests (`:1407`, `:1449`) *do* `os.chdir`
themselves, proving the author knows the hazard but applied it inconsistently.

**Impact.** The primary quality gate is **lying in both directions**: (a) it is
**red on the developer's machine** (blocking `/finish-task`, and any local
"pytest must pass before commit" rule per CLAUDE.md), and (b) it is **green in CI**
on the identical commit because a fresh clone lacks the file — so a real
non-hermeticity bug ships undetected. The tests also **write** `configs/dashboard.yaml`
into whatever CWD they run in when it is absent (the create path), polluting the
tree and making results order-dependent. For a money engine, a gate whose verdict
depends on unversioned local filesystem state is a Critical process defect.

**Recommendation.** (1) Add a repo-wide `trading_bot/tests/conftest.py` with an
**autouse fixture that `monkeypatch.chdir(tmp_path)`** (and scrubs
`TRADING_BOT_UI_TOKEN` and other `TRADING_BOT_*`/broker env vars) for every test,
so no test can ever read a developer-local file. (2) Make the four dashboard tests
pass an explicit `--config <tmp manifest>` (or chdir to `tmp_path`) rather than
relying on the default. (3) Consider anchoring `_DEFAULT_MANIFEST` to a
user/config dir (e.g. `$XDG_CONFIG_HOME` / `platformdirs`) instead of a
CWD-relative path, so `trading-bot dashboard` behaves the same regardless of the
directory it is launched from.

### T-2 [High] `--exitfirst` baked into `addopts` — CI stops at the first failure and coverage totals are invalid

**Location.** `pyproject.toml:106` — `addopts = "--exitfirst -vv --cov=trading_bot
--cov-report=term-missing -m 'not network'"`; `.github/workflows/ci.yml` runs a
bare `pytest` (inheriting `addopts`).

**Evidence.** `python -m pytest -q` reported `1 failed, 810 passed` and
`!!! stopping after 1 failures !!!`; dropping `-x` revealed **4** failures and 84
more passing tests. With `-x`, coverage is computed over a truncated run, so the
`term-missing` numbers are meaningless whenever anything fails.

**Impact.** In CI, a single early failure masks every later failure and skews the
coverage report — you fix one thing, push, hit the next hidden failure, repeat.
For a suite this size (~900 tests) that is a slow, misleading loop, and it means a
green CI badge only proves "no failure *before* the first one". Combined with T-1,
the local `-x` run aborts on the dashboard failure before the rest even run.

**Recommendation.** Remove `--exitfirst` from `addopts` (let the full suite run and
report every failure + honest coverage). If a fast-fail local mode is wanted, keep
it as an opt-in alias (`pytest -x`) or a separate `addopts` profile, never the
default that CI inherits.

### T-3 [High] Coverage is measured but never enforced; CI uploads no coverage artifact

**Location.** `pyproject.toml` `[tool.coverage.run]` (only `omit`), no
`[tool.coverage.report] fail_under`; `ci.yml` has no `--cov-fail-under` and no
coverage upload step.

**Evidence.** `grep fail_under / --cov-fail-under` across `pyproject.toml` and
`ci.yml` → none. Coverage is printed (`term-missing`) but nothing acts on the
number.

**Impact.** The 96 % is decorative — a PR can silently drop coverage (e.g. leave a
new order path untested) and CI stays green. No coverage.xml/HTML is uploaded, so
regressions are invisible in review. For a live-trading engine this is the
difference between "we test the money paths" and "we happened to, once".

**Recommendation.** Add `[tool.coverage.report] fail_under = 90` (or the current
floor) so CI red-lines on a drop, `show_missing = true`, and consider
`exclude_lines` for genuinely-unreachable defensive branches. Add a CI step to
upload `coverage.xml` (artifact or a coverage service). Optionally gate the
critical modules (`risk`, `order_router`, `reconcile`) at a higher per-module
floor via `--cov-fail-under` on those paths.

### T-4 [High] The uncovered ~4 % is concentrated on money-critical error branches

**Location.** `application/risk.py:373,376-388` (kill-switch cancel-failure
`except` blocks in `_cancel_via_router` / `_cancel_via_broker`);
`application/order_router.py:307-311` (order could-not-transition-to-REJECTED
branch), `:347` (`MissingOrder` guard on cancel); `application/reconcile.py:255-259`
(a divergence branch); `brokers/kraken_ws.py:135-137` (token-missing `BrokerError`)
+ `:363,374,389,393` (malformed-entry / fill-field guards); `transport/ws.py:76-78,
191-193,219,225` (reconnect/close).

**Evidence.** Read each uncovered range. Example — the kill-switch's failure
handling is entirely uncovered:

```python
try:
    await router.cancel(cid)
except Exception:
    logger.exception("kill: failed to cancel order %s", cid)   # risk.py ~376 — UNCOVERED
```

The hardening suite (`test_kill_switch.py`, `test_idempotency.py`,
`test_reconciliation.py`) exercises the happy/adopt paths but not "a cancel *fails*
mid-kill" or "the state machine *forbids* the REJECT transition".

**Impact.** These are precisely the branches that decide behaviour when a venue
misbehaves — the kill-switch failing to cancel one order (does it keep going for
the rest?), a reject that the FSM refuses (is the id still recorded so a retry is
deduped?), a reconcile divergence, a WS reconnect. They are the hardest to hit and
the most expensive to get wrong (real money). Untested here means "we assume the
error path works".

**Recommendation.** Extend `tests/hardening/` using the existing `FaultyBroker`
(which already injects rejections/ambiguous failures) to drive: (a) kill-switch
where `cancel` raises for one order — assert the others are still cancelled and the
switch still halts; (b) an order-router submit where the order is already terminal
— assert the cid is recorded (retry-dedup) and no double-submit; (c) a
`kraken_ws` frame missing token/qty/price — assert it degrades to `None`, not a
crash. Then raise the module coverage floor (T-3) to lock it in.

### T-5 [Medium] pre-commit ↔ CI parity gap (format check missing in CI; mypy/pytest missing in hook)

**Location.** `.pre-commit-config.yaml` (ruff `--fix` + `ruff-format`) vs
`.github/workflows/ci.yml` (`ruff check` only, then mypy, then pytest).

**Evidence.** pre-commit runs `ruff-format` (which *reformats* files) and `ruff`
with `--fix`; CI runs `ruff check trading_bot/` with **no `ruff format --check`**.
CI has mypy + pytest; the hook has neither.

**Impact.** A contributor who skips pre-commit (`--no-verify`, or a fresh clone
that never `pre-commit install`ed) can push formatting CI never checks, and CI can
still be red on lint that the hook would have auto-fixed — divergent verdicts.
Conversely the hook can't catch the mypy/pytest failures that only CI runs, so "the
hook passed" gives false confidence. Also `ruff-format` is pinned at `v0.4.0` in
the hook while `ruff>=0.4` floats in `[dev]` — format-rule drift over time.

**Recommendation.** Add `ruff format --check trading_bot/` as a CI step (before or
alongside `ruff check`), so CI enforces exactly what the hook auto-applies. Pin the
hook `rev` and the `[dev]` ruff to the same version. Optionally add a
`language: system` mypy hook so type errors surface pre-push too.

### T-6 [Medium] Dead `MANIFEST.in` directive; sdist asset shipping rests solely on `package-data`

**Location.** `MANIFEST.in` — `recursive-include trading_bot *.ini`.

**Evidence.** `find trading_bot -name '*.ini'` → nothing; the directive matches no
files. Simulating the `[tool.setuptools.package-data]` globs against the real
`interfaces/ui/` tree, **all 23 assets** (10 templates + 13 static incl. 6 nested
`.woff2` fonts) are matched — templates/static ship correctly in the **wheel**.

**Impact.** Low functional risk (the wheel is correct), but the `.ini` directive is
confusing dead weight, and there is **no explicit `graft`/`recursive-include` of
`templates/`/`static/` in `MANIFEST.in`** — the sdist relies entirely on setuptools
auto-including declared `package-data`. That holds for current setuptools, but it
is fragile: if `package-data` is ever refactored to `MANIFEST`-driven inclusion (or
a build backend changes), the dashboard assets could silently drop from the sdist
and break `pip install .` from source. This is the classic deploy-breaker; it is
currently *avoided*, not *guarded*.

**Recommendation.** Remove the dead `*.ini` line. Add an explicit
`recursive-include trading_bot/interfaces/ui/templates *.html` and
`recursive-include trading_bot/interfaces/ui/static *` (belt-and-suspenders with
`package-data`), and add a CI/release smoke step that builds the sdist+wheel and
asserts the templates/static are present (e.g. `python -m build` then unzip-grep, or
`check-manifest`).

### T-7 [Medium] No dependency locking/hashes; GitHub Actions pinned by moving tag, not SHA

**Location.** `.github/workflows/ci.yml` — `actions/checkout@v4`,
`actions/setup-python@v5`, `pip install -e ".[dev]"` with only floor pins
(`>=`); no lockfile, no `--require-hashes`.

**Evidence.** Every dep in `pyproject.toml` is a lower-bound (`httpx>=0.27`,
`pydantic>=2.0`, `fastapi>=0.110`, …). CI resolves fresh from PyPI on every run;
actions reference moving major-version tags.

**Impact.** Non-reproducible builds: a transitive/backend release can turn CI red
(or, worse, green-but-different) with no code change; a compromised action tag or a
malicious dep release executes in CI with repo/token access. For an engine that
handles real credentials, supply-chain drift is a real risk surface. (The
StarletteDeprecationWarning already observed is a mild instance of unpinned drift.)

**Recommendation.** Pin actions by commit SHA (`actions/checkout@<sha> # v4`).
Add a locked install for CI (`pip-tools`/`uv` compiled `requirements-dev.txt` with
hashes, or `--require-hashes`), keeping the loose `>=` in `pyproject` for library
consumers. Optionally add Dependabot for both actions and pip.

### T-8 [Medium] No `filterwarnings` and no pytest-asyncio fixture-loop-scope config

**Location.** `pyproject.toml:105-108` (`[tool.pytest.ini_options]`); pytest-asyncio
`1.4.0` installed with `asyncio_mode = "auto"`.

**Evidence.** No `filterwarnings` key (the StarletteDeprecationWarning surfaces
unfiltered and un-erroring in every run). No `asyncio_default_fixture_loop_scope`
set — pytest-asyncio ≥0.24 warns when it is unset (no warning fired only because
the suite has no async fixtures).

**Impact.** Deprecations accumulate silently; a real, meaningful warning (e.g. a
pydantic/starlette deprecation that precedes a breaking change) is lost in the
noise instead of failing the build. Leaving the asyncio fixture scope implicit is a
latent break for the next pytest-asyncio major.

**Recommendation.** Add `filterwarnings = ["error", "ignore::DeprecationWarning:starlette.*"]`
(escalate to error, allow-list the few known-benign ones), and set
`asyncio_default_fixture_loop_scope = "function"` explicitly.

### T-9 [Low] One real wall-clock sleep — timing-flaky

**Location.** `trading_bot/tests/interfaces/test_cli_commands.py:430` —
`await asyncio.sleep(0.12)  # let it start + tick a couple of times`.

**Evidence.** The rest of the suite is deterministic (all backoff via
`RecordingSleep`/`FakeClock`; the only RNG is seeded `np.random.default_rng(42)` at
`test_strategy.py:255`; `asyncio.sleep(0)` elsewhere is a scheduler yield). This one
0.12 s wall-clock wait is the sole real-time dependency.

**Impact.** Under CI load / slow runners the 0.12 s can be insufficient (ticks not
yet happened), producing an intermittent failure with no code change — the worst
kind of flake to debug.

**Recommendation.** Replace the sleep with an explicit synchronisation (poll a
condition/`Event`, or a `FakeClock`-driven tick) so the test waits for the
observable state rather than a fixed duration.

### T-10 [Low] `interrogate` is configured but invoked by nothing

**Location.** `pyproject.toml:112-116` (`[tool.interrogate] fail-under = 80`);
absent from `ci.yml`, `.pre-commit-config.yaml`, `.claude/workflow.json`.

**Evidence.** `grep interrogate` across CI/hook/workflow → no hits; it is listed in
`[dev]` deps but never run.

**Impact.** Dead tooling: the docstring-coverage floor is documented but
unenforced, so it can drift below 80 % without anyone noticing — and a reader
assumes it *is* enforced.

**Recommendation.** Either add `interrogate trading_bot/` as a CI step (making the
80 % real) or drop the config + the `[dev]` dep to remove the false signal.

### T-11 [Low] `parents[3]` strategy-discovery walk is source-checkout-only; misleading comment

**Location.** `trading_bot/interfaces/api/app.py:807` — `repo_root =
pathlib.Path(__file__).resolve().parents[3]` then `repo_root / "strategies"`.

**Evidence.** For an installed wheel, `__file__` is under `site-packages/`, so
`parents[3]` is `site-packages/` (or the env root) — `strategies/` won't be there;
the code degrades gracefully (returns builtins only). The comment ("`strategies/`
sits at the repo root, a sibling of the installed package") describes a
source-checkout layout, not an install.

**Impact.** Low — behaviour is correct (empty discovery when absent), but the
dashboard's "discovered signals" feature silently does nothing for installed
deployments, which the comment obscures. Template loading itself is fine (uses
`__file__`-relative `UI_DIR`, and the assets ship — T-6).

**Recommendation.** Discover `strategies/` from an explicit configured path (or
CWD/env), not a fixed `parents[3]` relative to the package; fix the comment to say
it only works from a source checkout.

### T-12 [Low] Tests coupled to implementation details

**Location.** `tests/hardening/_faulty_broker.py:185` (`self.inner._fills.extend(...)`
— reaches a private attr); `tests/interfaces/test_dashboard.py:1323`
(`assert "refusing to bind" in result.output` — exact log text);
`tests/application/test_run_app.py:431,441` &
`tests/application/test_portfolio_config.py:545` (`ConfigError, match="commingle"`
— exact message regex); `test_dashboard.py:1345` (`test_dashboard_calls_start_all`
spies internal `StrategySupervisor.start` call order).

**Evidence.** Cited above; also noted the suite otherwise avoids `unittest.mock`
entirely (hand-written fakes that delegate to real objects — a strength).

**Impact.** Low but real: these tests break on a benign rename/reword (private
attr, log string, error phrasing) rather than on a behaviour change — brittle
signal that erodes trust in the suite over time.

**Recommendation.** Assert on public behaviour where feasible (exit code + a stable
error *type* rather than the human message; the observable book state rather than
`_fills`). Where a message must be checked, match a stable substring/error code, not
prose.

### T-13 [Info] CI shape and extras coverage

**Location.** `.github/workflows/ci.yml`.

**Notes.** Single `test` job, matrix Python **3.11 / 3.12 / 3.13** (matches
`classifiers` + `requires-python>=3.11`), `fail-fast: false`, `cache: pip` enabled,
**concurrency cancel-in-progress present** (good). Steps: install `.[dev]` →
`ruff check` → `mypy` → `pytest`. So CI **does** run ruff *and* mypy (good).
Gaps: only `.[dev]` is installed — `.[triptych]` (fynance) and the dccd/`.[daemon]`
extras are never exercised, so the fynance-gated KPI tests and dccd feed tests
**always skip in CI** (they're `importorskip`/`network`-gated — see T-14/T-15), i.e.
the KPI/feed integration is untested by CI. No separate lint-only fast job; no
sdist/wheel build job (see T-6); no coverage upload (T-3).

**Recommendation.** Consider one matrix leg (or a separate job) that installs
`.[triptych]` (+ editable dccd if feasible) so the fynance KPI path is actually run
somewhere in CI. Optionally split a fast lint/type job from the test matrix.

### T-14 [Info] `-m network` e2e suite is safe — one write path, testnet-pinned, no mainnet order path

**Location.** 9 `@pytest.mark.network` tests across
`tests/brokers/test_kraken_rest.py:567`, `tests/brokers/test_binance_rest.py:760,783`,
`tests/transport/test_http.py:295`, `tests/transport/test_ws.py:221`,
`tests/application/test_data_feed.py:348`, `test_data_provider.py:285`,
`test_portfolio_feed.py:339`, `test_portfolio_config.py:742`.

**Evidence (audited read-only, never executed).** Only **one** test issues a real
broker write: `test_real_binance_testnet_round_trip`
(`tests/brokers/test_binance_rest.py:783`). It:
- pins `base_url=TESTNET_API_BASE` (`https://testnet.binance.vision`,
  `brokers/binance.py:101`) — every request URL is built from `self._base_url`, so
  the place (`POST /api/v3/order`) and cancel (`DELETE /api/v3/order`) physically
  hit **testnet only**; it cannot reach `api.binance.com`;
- places a non-filling **LIMIT BUY at 50 % of mark, qty 0.001** and **cancels it in
  a `finally`** (place at line 825, cancel at 841);
- `pytest.skip`s cleanly at line 805 when `BINANCE_TESTNET_*` creds are absent.
All other network tests are **read-only**: Kraken/Binance *public* endpoints (no
key), Kraken public WS, and dccd local-Parquet replays (no broker at all). The
Kraken network test constructs `KrakenBroker()` with no creds (public-only). Every
other `add_order`/`place_order`/`cancel` grep hit is under `httpx_mock` or a
`PaperBroker`/fake, not `network`-marked.

Credentials: no `conftest.py`, **no `pytest-dotenv`, no `load_dotenv`** in package
or tests — a `.env` exists but pytest does not source it; keys reach `os.environ`
only if the user exports them. Network tests are excluded by default
(`addopts … -m 'not network'`). Secrets flow only into HMAC signing / the
`X-MBX-APIKEY` header; no `print`/`logging` of key/secret exists; tests assert only
on a dummy key. **Confirmed: no mainnet private-write or mainnet order path exists
in the suite.** (No secret values were read or reproduced in this audit.)

**Recommendation (optional hardening).** In the testnet test, drop the
mainnet-key fallback (`test_binance_rest.py:799-804`) so it requires the
`BINANCE_TESTNET_*` names explicitly, and add an `assert broker.is_testnet`
(or assert the base URL) immediately before the `place_order` — belt-and-suspenders
so a future edit to the base-URL constant can never route a write to mainnet.

### T-15 [Info] No `conftest.py`; sibling-repo deps skip (not error) on a fresh clone; hygiene clean

**Location.** whole `tests/` tree; `.gitignore`.

**Evidence.** No `conftest.py` anywhere (root cause enabler of T-1) — isolation is
per-test and inconsistent. Fakes are all **hand-written** (no `unittest.mock` /
`MagicMock` / `AsyncMock` — a genuine strength: they delegate to real
`PaperBroker`/httpx and assert on real effects, so they wouldn't pass if the code
were broken). Sibling-repo deps on a `.[dev,daemon]`-only clone: `fynance` — every
use guarded by function-scope `pytest.importorskip("fynance")` (skips, never
errors); `dccd` — `importorskip("dccd")` **and** `@pytest.mark.network` (double
gated); `strategies.alloc1` / `fynance_research` — appear only as **unresolved
config-string refs** (`"strategies.alloc1.signal:…"`) and in comments, never
imported at collection/parse time. So a fresh clone collects and runs green (minus
skips). Hygiene: `git ls-files | grep -E '__pycache__|\.sqlite|\.db|^var/|\.pyc'`
→ **nothing tracked**; `.gitignore` covers caches, `var/`, `*.sqlite`, `.env`,
`configs/` (so the secret-bearing `configs/dashboard.yaml` is *not* committable —
but see T-1, it still pollutes test runs), templates deliberately **not** ignored.
Version consistency: `pyproject 0.7.0` == CHANGELOG `[0.7.0] - 2026-06-30` == tag
`v0.7.0` (a proper ancestor of HEAD; `develop` is 38 commits ahead — expected);
CHANGELOG follows Keep-a-Changelog with a live `[Unreleased]`.

**Recommendation.** Add the `conftest.py` from T-1 (autouse CWD+env isolation) as
the single highest-leverage fix; it closes the Critical gate hazard and makes the
whole suite hermetic. Keep the no-`unittest.mock` discipline.
