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

### Changed

- Parked the pre-2026 implementation under `trading_bot/legacy/` (reference only,
  excluded from lint/type-check/tests) ahead of the hexagonal rewrite.
- Bumped version to `0.2.0.dev0` to mark the start of the rewrite.

### Removed

- `setup.py` and `requirements.txt` — folded into `pyproject.toml`.
