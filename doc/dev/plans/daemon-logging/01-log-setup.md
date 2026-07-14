---
plan: daemon-logging/01-log-setup
kind: leaf
status: executing
complexity: medium
depends: []
parallel: false
branch: feat/daemon-log-setup
pr: ""
---

# 01 — Logging spine: config, rotation, timestamps

## Goal

A real logging setup for the daemon path: ISO-8601-timestamped (with numeric
tz offset), midnight-rotated, level-configurable via the manifest; the
existing daemon lifecycle lines routed through it; tick errors captured with
tracebacks. Interactive CLI commands (`status`, `kpi`, `canary`, …) keep their
rich-console output unchanged — this leaf touches only the daemon
(`start --serve` / `_run_daemon`) path.

## Files to change

- `trading_bot/application/log_setup.py` — **new**: `configure_daemon_logging()`
- `trading_bot/application/config.py` — new `LoggingConfig` model + optional
  `logging:` section on `AppConfig`
- `trading_bot/interfaces/cli/main.py` — daemon path only: call the setup,
  route lifecycle prints through a logger
- `trading_bot/tests/application/test_log_setup.py` — **new**
- `trading_bot/tests/application/test_config.py` — extend

## Steps

1. `LoggingConfig(BaseModel)` in `config.py`, mirroring the existing section
   models (`StorageConfig` / `UIConfig` style):
   - `level: str = "INFO"` — validated case-insensitively against
     `logging.getLevelNamesMapping()`; stored upper-case.
   - `dir: Path = Path("logs")` — resolved like the other relative paths in
     the manifest (relative to the CWD the daemon runs from, as today).
   - `retention_days: int = 14` — `ge=1`.
   - Field on `AppConfig`: `logging: LoggingConfig = Field(default_factory=LoggingConfig)`
     — **additive**, a manifest without a `logging:` section keeps defaults.
2. `log_setup.py` — `configure_daemon_logging(cfg: LoggingConfig) -> None`:
   - Create `cfg.dir` if missing; attach to the **root logger**:
     `TimedRotatingFileHandler(cfg.dir / "daemon.log", when="midnight",
     backupCount=cfg.retention_days, encoding="utf-8")` + a
     `StreamHandler(sys.stderr)` with the same formatter (systemd/journal
     still sees output; harmless under nohup).
   - Formatter: `%(asctime)s %(levelname)-8s %(name)s — %(message)s` with
     `formatTime` overridden to
     `datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")`
     — local time **with numeric offset** (`2026-07-14T13:35:40.123+02:00`).
     The offset is non-negotiable (audit finding: UTC-vs-CEST ambiguity).
   - Levels: root at `cfg.level`; cap the noisy third-party namespaces —
     `logging.getLogger(ns).setLevel(logging.WARNING)` for `apscheduler`,
     `uvicorn`, `uvicorn.access`, `httpx`, `websockets` — so INFO stays the
     engine's story (APScheduler misfire WARNINGs still flow).
   - **Idempotent**: tag the handlers it owns (attribute marker) and remove
     stale owned handlers on re-call — a second call must not double-write.
3. `cli/main.py` daemon path (`_run_daemon`): call
   `configure_daemon_logging(config.logging)` right after the manifest loads;
   then route the lifecycle lines through `logger = logging.getLogger("trading_bot.daemon")`:
   - `"daemon started (mode=…): N strateg(ies), tick=…"` → `logger.info`
     (keep the `_console.print` too — interactive foreground UX unchanged).
   - the per-tick `"daemon tick: stepped N strategy(ies)"` → `logger.info`
     (drop the console print for this one — it is daemon-only noise).
   - `"daemon tick error: …"` (`cli/main.py:1279`) → `logger.exception(...)`
     — tracebacks land in the file (invisible today).
   - `"daemon stopped (all strategies shut down)"` → `logger.info` + console.
4. No change to `portfolio_feed`/`portfolio_runner`/`order_router` loggers in
   this leaf — the handler existing is what makes their WARNINGs land in the
   rotated file; elevating the per-unit story is leaf 02.
5. Secrets discipline: the formatter adds no context beyond
   time/level/name/message; grep the touched lines for any credential-adjacent
   value (none expected — assert in review).

## Tests

- `test_log_setup.py`:
  - handler wiring: after configure, root has exactly one owned
    `TimedRotatingFileHandler` (`when == "MIDNIGHT"`, `backupCount == cfg.retention_days`)
    and one owned `StreamHandler`; calling configure twice leaves exactly one
    of each (idempotence).
  - formatter: an emitted record's line matches
    `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} `
    and contains the level and logger name.
  - level: `cfg.level="DEBUG"` makes a DEBUG record reach the file;
    `apscheduler` logger is capped at WARNING.
  - file: the log file is created under a tmp_path dir passed as `cfg.dir`.
- `test_config.py`: `LoggingConfig` defaults; invalid level rejected;
  `retention_days=0` rejected; manifest YAML without `logging:` yields
  defaults; with a `logging: {level: debug}` section parses and upper-cases.
- Full suite + `ruff check trading_bot/` green.

## Verification on real data

**Never touch the live daemon (port 8000) or `var/` originals.** Scratch
instance instead:

1. Build a scratch manifest in the session scratchpad: copy of
   `configs/dashboard.yaml` with `ui.port: 8099`, a throwaway `ui.token`,
   `db_path`s pointing at **copies** of the two books in the scratchpad,
   `logging: {dir: <scratchpad>/logs}`, same read-only dccd `data_path`.
2. Run `trading-bot start --serve -c <scratch manifest>` (venv python,
   foreground or background **without** any shell redirect), let it tick
   ≥ 3 minutes, then SIGINT.
3. Read `<scratchpad>/logs/daemon.log` and report verbatim: the
   `daemon started` line, ≥ 2 tick lines ~60 s apart, the `daemon stopped`
   line — every line timestamped with a `+02:00`-style offset; confirm the
   handler's `backupCount`/`when` by introspection in the tests (rotation
   itself is unit-tested; the real check here is file creation + format +
   flow).
4. Confirm the live daemon on port 8000 was untouched
   (`curl -s 127.0.0.1:8000/health` unchanged, pid 1348485 still running).

## Closeout

- CHANGELOG `### Added`: "daemon logs: timestamped (ISO-8601 with tz offset),
  midnight-rotated `logs/daemon.log` (retention 14 d), level via the manifest
  `logging:` section; tick errors now carry tracebacks (#XX)".
- ADR: logging config lives in `application/` (composition seam, domain stays
  pure); root-logger handlers + third-party namespace caps; local-time ISO
  timestamps **with offset** (rejected: UTC-only — operator reads local;
  rejected: per-module handlers — one spine, N emitters).
- `06-status.md`: note the daemon-logs spine landed (leaf 1/3 of
  `daemon-logging`).
- Tick leaf 01 in `00-plan.md`; archive this leaf file. Roadmap line stays
  (leaves 02–03 open).
