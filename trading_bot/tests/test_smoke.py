"""Smoke tests — the package imports cleanly and exposes a version string.

These are deliberately trivial: they guard the Phase 0 skeleton (packaging,
imports) before the real domain/transport/broker layers land. Replace/extend as
those layers are built (see ``doc/dev/07-roadmap.md``).
"""
from __future__ import annotations

import trading_bot


def test_package_exposes_version() -> None:
    assert isinstance(trading_bot.__version__, str)
    assert trading_bot.__version__
