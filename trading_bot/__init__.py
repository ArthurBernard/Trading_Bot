"""trading_bot — execution & orchestration layer of the trading triptych.

The third pillar alongside **dccd** (market data) and **fynance** (research /
signals): it runs strategies live, routes and manages orders across exchanges,
and tracks positions / PnL / risk. Hexagonal, async-first — see ``CLAUDE.md``
and ``doc/dev/`` for the architecture and the developer brief.

The pre-2026 implementation lives in git history only (no in-tree legacy
package). See ``doc/dev/07-roadmap.md`` for the rewrite roadmap.
"""
from __future__ import annotations

from importlib import metadata

try:
    #: The installed package version — read from metadata so it always matches
    #: ``pyproject.toml`` (the single release source) instead of a hand-bumped
    #: string that silently goes stale across releases.
    __version__ = metadata.version("trading_bot")
except metadata.PackageNotFoundError:  # pragma: no cover - not installed (raw checkout)
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
