"""The :class:`Orchestrator` — run many strategy loops concurrently, stop them all cleanly.

The orchestrator is the engine's **lifecycle conductor**: it owns a set of
:class:`~trading_bot.application.strategy_runner.StrategyRunner` loops and runs
them *concurrently*, then tears them all down on a single graceful-shutdown
signal. It is the async, in-process replacement for the legacy
multiprocessing manager (``legacy/bot_manager.py`` + ``legacy/_server.py``),
where each strategy ran in its own OS process under a manager that polled a
shared ``is_stop()`` flag and shut clients down one connection at a time. Here
there are no processes, no sockets and no polling loop: every runner is an
``asyncio`` task over its own :class:`~trading_bot.application.data_feed.DataFeed`,
and a single shared :class:`asyncio.Event` is the ``is_stop()`` flag every runner
cooperatively observes.

Concurrency primitive (carried into the ADR)
--------------------------------------------
:meth:`run` launches every registered runner as a task and awaits them with
``asyncio.gather(..., return_exceptions=True)``. This was chosen over a
:class:`asyncio.TaskGroup` deliberately:

* **gather is *non-fail-fast*.** With ``return_exceptions=True`` one runner
  raising does **not** auto-cancel its siblings — the siblings keep running and
  reach their own natural completion (a finite feed) or are stopped by an
  explicit :meth:`shutdown`. The orchestrator then *aggregates* the outcomes and
  re-raises the failure(s) itself, so a single bad strategy never silently
  poisons the others, and the caller still learns about every error. A
  ``TaskGroup`` would have the opposite policy (first error cancels the rest),
  which is wrong for independent strategy loops that should be allowed to keep
  trading their own books.
* The trade-off — gather will not cancel siblings *for* us — is handled
  explicitly by :meth:`shutdown` (set the shared stop event, let each runner
  drain to its next between-steps boundary).

Graceful shutdown (carried into the ADR)
----------------------------------------
There is **one** shared :class:`asyncio.Event` (:attr:`stop_event`). Every runner
is launched with ``runner.run(stop_event=self.stop_event)``, so it checks the
event *between* steps and exits cleanly the moment it is set — never mid-``step``,
so no order is ever left half-submitted. :meth:`shutdown` simply sets that event;
the in-flight :meth:`run`'s ``gather`` then resolves as each runner drains. This
is a *cooperative* stop: no task is force-cancelled while it might be awaiting a
venue submission. (A runner whose feed never yields between checks could in
principle ignore the event; the finite/poll feeds we ship always yield, so the
drain is prompt.)

Signal handling — opt-in, injectable (carried into the ADR)
-----------------------------------------------------------
SIGINT/SIGTERM handling is **not** installed on import and **not** installed by
:meth:`run`. The process entrypoint (the CLI) must call
:meth:`install_signal_handlers` explicitly, passing the running loop. That method
registers a handler (via :meth:`asyncio.loop.add_signal_handler` where supported,
falling back to :func:`signal.signal`) whose only job is to schedule
:meth:`shutdown`. Because installation is an explicit, injectable step:

* importing this module installs nothing (no global signal side effects);
* tests never depend on a real SIGINT — they either set :attr:`stop_event`
  directly, or call the registered handler with a fake loop and assert it
  triggers shutdown.

Per-runner failure policy (carried into the ADR)
------------------------------------------------
:meth:`run` returns a ``dict`` keyed by runner — its value is the runner's order
count on success, or the :class:`BaseException` it raised. After gather resolves,
if any runner raised, the orchestrator **re-raises**: a single failure is
re-raised as-is; multiple failures are wrapped in a :class:`RunnerGroupError`
(carrying the per-runner exception map). Siblings are *not* abandoned — they were
allowed to finish (gather is non-fail-fast) — so the engine never silently hangs
on one runner's crash.

This module lives in the application layer: it sequences ``StrategyRunner``\\ s and
an optional :class:`~trading_bot.application.events.EventBus`, holds no money logic
of its own, and performs no I/O (the runners' routers/brokers do).
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING

from trading_bot.application.events import EventBus, LogEvent

if TYPE_CHECKING:
    from collections.abc import Iterable

    from trading_bot.application.strategy_runner import StrategyRunner

__all__ = ["Orchestrator", "RunnerGroupError"]


class RunnerGroupError(Exception):
    """Several runners failed in one :meth:`Orchestrator.run`.

    Raised by :meth:`Orchestrator.run` when **more than one** registered runner
    raised during a concurrent run (a single failure is re-raised directly). It
    carries the full per-runner exception map so the caller can see every
    failure, not just the first.

    Parameters
    ----------
    errors : dict[StrategyRunner, BaseException]
        Map of each failed runner to the exception it raised.

    """

    def __init__(self, errors: dict[StrategyRunner, BaseException]) -> None:
        self.errors = errors
        names = ", ".join(_runner_name(r) for r in errors)
        super().__init__(f"{len(errors)} runner(s) failed: {names}")


def _runner_name(runner: StrategyRunner) -> str:
    """Best-effort human name for a runner (its strategy's ``name``).

    Used only for log/error text; falls back to ``repr`` if the runner does not
    expose a strategy name (never reaches into private state for control flow).
    """
    strat = getattr(runner, "_strategy", None)
    name = getattr(strat, "name", None)
    return str(name) if name is not None else repr(runner)


class Orchestrator:
    """Run many :class:`StrategyRunner` loops concurrently with graceful shutdown.

    Register one runner per strategy (each over its **own** feed/router/tracker),
    then :meth:`run` them all concurrently. A single shared :attr:`stop_event`
    gives every runner a cooperative stop: :meth:`shutdown` sets it and each
    runner drains to its next between-steps boundary (no order half-submitted).
    Signal handling is opt-in via :meth:`install_signal_handlers` (the process
    entrypoint calls it; tests never need a real signal). See the module
    docstring for the concurrency primitive, the shutdown model and the
    per-runner failure policy.

    Parameters
    ----------
    event_bus : EventBus, optional
        If given, the orchestrator emits a
        :class:`~trading_bot.application.events.LogEvent` on start, on shutdown
        request and on each runner's completion/failure (a human-readable trace
        of the lifecycle). Defaults to ``None`` (no trace emitted; the runners'
        own events still flow).

    Examples
    --------
    >>> # orch = Orchestrator(event_bus=bus)
    >>> # orch.add(runner_a); orch.add(runner_b)
    >>> # results = await orch.run()        # both run concurrently
    >>> # await orch.shutdown()             # (from a signal handler) stop both

    """

    def __init__(self, *, event_bus: EventBus | None = None) -> None:
        self._runners: list[StrategyRunner] = []
        self._bus = event_bus
        # The single shared cooperative-stop flag. Created eagerly; an
        # asyncio.Event does not bind to a running loop at construction (only
        # when first awaited), so building the orchestrator outside a loop is
        # fine — the runners simply read ``is_set()`` between steps.
        self._stop_event = asyncio.Event()
        # Signal handlers we installed (number -> previous handler), so
        # ``install_signal_handlers`` is idempotent and could be unwound.
        self._installed_signals: set[int] = set()

    @property
    def stop_event(self) -> asyncio.Event:
        """The shared cooperative-stop :class:`asyncio.Event` every runner observes.

        Setting it (directly, or via :meth:`shutdown`) asks every running runner
        to stop at its next between-steps boundary.
        """
        return self._stop_event

    @property
    def runners(self) -> tuple[StrategyRunner, ...]:
        """The registered runners, in registration order (a read-only snapshot)."""
        return tuple(self._runners)

    def add(self, runner: StrategyRunner) -> None:
        """Register one :class:`StrategyRunner` to be run by :meth:`run`."""
        self._runners.append(runner)

    def add_all(self, runners: Iterable[StrategyRunner]) -> None:
        """Register several runners at once (order preserved)."""
        self._runners.extend(runners)

    async def run(self) -> dict[StrategyRunner, int]:
        """Run every registered runner concurrently and await them all.

        Launches each runner as a task (``runner.run(stop_event=self.stop_event)``)
        and awaits them with ``asyncio.gather(..., return_exceptions=True)`` — the
        *non-fail-fast* primitive: one runner raising does not auto-cancel the
        others (see the module docstring). After all tasks resolve, the per-runner
        outcomes are collected; if any runner raised, the orchestrator re-raises
        (a lone failure as-is, several wrapped in :class:`RunnerGroupError`).

        Returns
        -------
        dict[StrategyRunner, int]
            Each registered runner mapped to the number of orders it submitted.
            Only returned when **every** runner succeeded.

        Raises
        ------
        BaseException
            The single exception a sole failing runner raised, re-raised as-is.
        RunnerGroupError
            When more than one runner failed — wraps the per-runner exceptions.

        """
        if not self._runners:
            return {}

        self._emit(f"orchestrator: starting {len(self._runners)} runner(s)")

        runners = list(self._runners)
        outcomes = await asyncio.gather(
            *(r.run(stop_event=self._stop_event) for r in runners),
            return_exceptions=True,
        )

        results: dict[StrategyRunner, int] = {}
        errors: dict[StrategyRunner, BaseException] = {}
        for runner, outcome in zip(runners, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors[runner] = outcome
                self._emit(
                    f"orchestrator: runner {_runner_name(runner)} failed: "
                    f"{outcome!r}",
                    level="error",
                )
            else:
                results[runner] = outcome
                self._emit(
                    f"orchestrator: runner {_runner_name(runner)} finished "
                    f"({outcome} order(s))"
                )

        if errors:
            if len(errors) == 1:
                # Re-raise the lone failure as-is so the caller sees the real
                # exception type/traceback (siblings already ran to completion).
                raise next(iter(errors.values()))
            raise RunnerGroupError(errors)

        return results

    async def shutdown(self) -> None:
        """Request a graceful stop of every running runner.

        Sets the shared :attr:`stop_event`; each runner observes it between steps
        and exits cleanly (no order left half-submitted). The in-flight
        :meth:`run` then resolves as the runners drain. Idempotent — calling it
        again is a no-op once the event is set. Does **not** force-cancel any
        task: a runner mid-``step`` finishes that step first.
        """
        if not self._stop_event.is_set():
            self._emit("orchestrator: shutdown requested (draining runners)")
            self._stop_event.set()

    def install_signal_handlers(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        signals: Iterable[int] = (signal.SIGINT, signal.SIGTERM),
    ) -> None:
        """Install SIGINT/SIGTERM handlers that trigger :meth:`shutdown`.

        **Opt-in and injectable** — call this only from the process entrypoint
        (the CLI), never on import and never from :meth:`run`. Each handler
        schedules :meth:`shutdown` on ``loop`` (so a Ctrl-C drains the runners
        instead of killing them). Prefers
        :meth:`asyncio.loop.add_signal_handler` (the loop-native path); on a
        platform/loop that does not support it (e.g. Windows) it falls back to
        :func:`signal.signal` scheduling the coroutine thread-safely.

        Tests never need a real signal: pass a fake/real loop and assert a
        handler was registered that triggers :meth:`shutdown` when invoked.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The loop to schedule :meth:`shutdown` on.
        signals : Iterable[int], optional
            The signal numbers to handle. Defaults to ``(SIGINT, SIGTERM)``.

        """

        def _on_signal() -> None:
            self._emit("orchestrator: signal received -> graceful shutdown")
            loop.create_task(self.shutdown())

        for sig in signals:
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (NotImplementedError, RuntimeError):
                # add_signal_handler is unavailable on some platforms (Windows)
                # / loops; fall back to the stdlib handler, hopping back onto the
                # loop thread-safely to schedule the coroutine.
                signal.signal(
                    sig,
                    lambda _s, _f: loop.call_soon_threadsafe(_on_signal),
                )
            self._installed_signals.add(sig)

    def _emit(self, message: str, *, level: str = "info") -> None:
        """Emit a :class:`LogEvent` on the bus if one was provided."""
        if self._bus is not None:
            self._bus.emit(LogEvent(message=message, level=level))
