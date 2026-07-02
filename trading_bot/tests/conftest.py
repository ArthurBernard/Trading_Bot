"""Suite-wide hermeticity — isolate every test from the developer's environment.

The execution engine's pytest gate must be *trustworthy*: a green run on a
teammate's laptop and a green run in CI have to mean the same thing. Two ambient
influences would otherwise leak into a test and make the gate flap:

* **The current working directory.** The dashboard/serve CLI reads a
  CWD-relative default manifest (``configs/dashboard.yaml`` — see
  :data:`trading_bot.interfaces.cli.main._DEFAULT_MANIFEST`) and *creates* it on
  first launch. A developer with a real, secret-bearing ``configs/dashboard.yaml``
  in the repo root would see the dashboard tests pick it up (auth turned on,
  extra strategies declared) and fail — while CI, lacking that file, stays green.
  The autouse fixture below runs each test from a throwaway temp directory, so the
  repo-root manifest is never on the resolution path (and a test that *writes* the
  default manifest litters the temp dir, not the repo).

* **The process environment.** ``TRADING_BOT_UI_TOKEN`` (and any other
  ``TRADING_BOT_*`` knob) is read by the CLI as an override; a value exported in
  the developer's shell would silently enable auth on a dashboard the test
  expects to be open. The fixture clears every ``TRADING_BOT_*`` variable for the
  duration of each test.

Both isolations are **autouse** — every test gets them without opting in — and
both are undone at teardown (``monkeypatch`` restores the CWD and the
environment), so nothing bleeds between tests. Tests that need a specific
manifest still pass an explicit path (``-c``/``tmp_path``); this only removes the
*implicit*, developer-specific one.
"""

from __future__ import annotations

# Built-in
import os
import pathlib

# Third-party
import pytest


@pytest.fixture(autouse=True)
def _hermetic_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Run each test from a temp CWD with a scrubbed ``TRADING_BOT_*`` environment.

    Guarantees the repo-root ``configs/dashboard.yaml`` is never the resolved
    default manifest and that no ``TRADING_BOT_*`` override (e.g. a developer's
    ``TRADING_BOT_UI_TOKEN``) leaks in. ``monkeypatch`` restores both at teardown.
    """
    for key in [k for k in os.environ if k.startswith("TRADING_BOT_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
