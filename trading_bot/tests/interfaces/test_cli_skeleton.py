"""Tests for the Typer CLI skeleton + the console-script target.

These prove the entrypoint wiring:

* the console-script target ``trading_bot.interfaces.cli.main:app`` imports and
  is a Typer app;
* the ``version`` command exits 0 and prints ``trading_bot.__version__``.

Real commands (start/stop/status/KPI) arrive in the next CLI leaf; this leaf
only proves the skeleton resolves and runs.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from trading_bot import __version__
from trading_bot.interfaces.cli.main import app

runner = CliRunner()


def test_console_script_target_imports() -> None:
    """The console-script target resolves to a Typer app."""
    # This is exactly what `[project.scripts] trading-bot = ...:app` resolves.
    from trading_bot.interfaces.cli.main import app as target

    assert isinstance(target, typer.Typer)


def test_version_command_prints_version() -> None:
    """`trading-bot version` exits 0 and prints the package version."""
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    """Bare invocation shows help (no_args_is_help) rather than erroring out."""
    result = runner.invoke(app, [])
    # Typer exits 0 (or 2) showing help; the help banner must mention the app.
    assert "trading-bot" in result.stdout or "Usage" in result.stdout
