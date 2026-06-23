"""The Typer ``trading-bot`` application — the CLI entrypoint (skeleton).

This module defines :data:`app`, the :class:`typer.Typer` instance the
``trading-bot`` console script points at (target
``trading_bot.interfaces.cli.main:app``). It is intentionally minimal in this
leaf — a single ``version`` command — so the wiring (console script,
:func:`~trading_bot.application.service_factory.build_engine`) is provable end to
end before the real commands (``start`` / ``stop`` / ``status`` / KPI table)
land in the next CLI leaf.

The CLI holds no business logic: commands delegate to the use-cases the
:func:`~trading_bot.application.service_factory.build_engine` factory wires.
"""

from __future__ import annotations

import typer

from trading_bot import __version__

app = typer.Typer(
    name="trading-bot",
    help="Execution & orchestration engine of the trading triptych.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main() -> None:
    """``trading-bot`` — the engine's command-line interface.

    A no-op group callback. Its sole purpose is to keep Typer treating the app
    as a *multi-command* group even while only one command (``version``) exists:
    without it, a lone command is collapsed into the root callback and
    ``trading-bot version`` would reject ``version`` as an extra argument. As the
    real commands (``start`` / ``stop`` / ``status`` / KPI) land, they slot in
    alongside ``version`` with no change here.
    """


@app.command()
def version() -> None:
    """Print the installed ``trading_bot`` version and exit."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
