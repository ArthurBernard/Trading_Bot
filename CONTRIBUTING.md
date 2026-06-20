# Contributing to trading_bot

## Setup

```bash
git clone https://github.com/ArthurBernard/Trading_Bot.git
cd Trading_Bot
pip install -e ".[dev]"

# Triptych integration deps (fynance from PyPI; dccd editable from its repo)
pip install -e ".[dev,triptych]"
pip install -e ../Download_Crypto_Currencies_Data

# Activate the project git hooks (run once per clone)
git config core.hooksPath .githooks
```

## Git Flow

```
master          ← stable releases only (tagged vX.Y.Z)
  └── develop   ← integration branch
        ├── feat/<topic>    new feature or rewrite axis
        ├── fix/<topic>     bug fix
        ├── chore/<topic>   tooling, CI, deps, refactor
        └── docs/<topic>    documentation only
```

**Rules:**
- Never commit directly to `master` — always go through `develop` via a PR.
- Never commit directly to `develop` for non-trivial work — use a feature branch.
- Branch off `develop`, not `master`.
- `develop` → `master` happens only at release time (version bump + tag).

**Commit style:** [Conventional Commits](https://www.conventionalcommits.org/) —
`feat:`, `fix:`, `chore:`, `docs:`. Do not add `Co-Authored-By` trailers (personal repo).

## Tests & linting

```bash
pytest                      # full suite (legacy excluded, network excluded)
ruff check trading_bot/
mypy trading_bot/
```

All must pass before opening a PR.

## Release process (maintainer only)

1. All planned work merged into `develop`, CI green.
2. Bump `version` in `pyproject.toml`.
3. Update `CHANGELOG.md` — move `[Unreleased]` to the new version.
4. Open PR `chore/release-X.Y.Z` into `develop`, then `develop` → `master`.
5. After merge to master: `git tag vX.Y.Z && git push origin vX.Y.Z`.
