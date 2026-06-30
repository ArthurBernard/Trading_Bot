"""Smoke tests — the package imports cleanly and exposes a version string.

These are deliberately trivial: they guard the Phase 0 skeleton (packaging,
imports) before the real domain/transport/broker layers land. Replace/extend as
those layers are built (see ``doc/dev/07-roadmap.md``).
"""
from __future__ import annotations

from importlib import metadata

import trading_bot


def test_package_exposes_version() -> None:
    assert isinstance(trading_bot.__version__, str)
    assert trading_bot.__version__


def test_version_reads_from_installed_metadata_not_a_stale_constant() -> None:
    """`__version__` tracks the installed package metadata (i.e. pyproject).

    Regression: it used to be a hand-bumped constant (`"0.2.0"`) that the release
    flow never updated, so `trading-bot version` / the dashboard went stale across
    releases. It must now equal the installed metadata version.
    """
    assert trading_bot.__version__ == metadata.version("trading_bot")
