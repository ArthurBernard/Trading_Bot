"""The engine's mark cache — the last close each runner saw, with its as-of.

Part of the ``api-completeness`` epic's pinned mark policy (see
``doc/dev/plans/api-completeness/00-plan.md``): a mark is **the last dccd bar
close** the strategy evaluated on — never a fresh fetch. The API layer must
serialize a position's current mark without any I/O of its own, so every
rebalance publishes its per-symbol closes here, in-process, for the API
thread-context to read back later (leaf 02 wires the read side; this leaf only
publishes).

Sharing discipline
-------------------
One :class:`MarkCache` per :class:`~trading_bot.application.service_factory
.Engine` (so, under the supervisor, one per managed unit — mirroring
:attr:`~trading_bot.application.service_factory.Engine.spec_resolver`). It is
written from the runner's async loop (:meth:`~trading_bot.application
.portfolio_runner.PortfolioRunner.rebalance`) and read from elsewhere in the
same process — exactly the sharing shape the runner's own ``last_eval_ms`` /
``last_asof_ms`` already have (see
:attr:`~trading_bot.application.portfolio_runner.PortfolioRunner.last_asof_ms`,
read directly by
:meth:`~trading_bot.application.supervisor.StrategySupervisor._status_of`):
a plain attribute — here, a plain ``dict`` — with **no lock**. This is safe for
the same reason: the daemon runs everything (the scheduler's ticks, the API
handlers) cooperatively on one asyncio event loop, so a read never interleaves
with a write mid-mutation. Do not add locking here without first revisiting
that discipline for the whole engine.
"""

from __future__ import annotations

# Built-in
from dataclasses import dataclass
from typing import Literal

# Local
from trading_bot.domain.instrument import Symbol
from trading_bot.domain.money import Money

__all__ = ["Mark", "MarkCache"]


@dataclass(frozen=True, slots=True)
class Mark:
    """One symbol's last-known price, timestamped and sourced.

    Attributes
    ----------
    price : Money
        The exact (``Decimal``) price — a bar close on this path.
    asof_ms : int
        Milliseconds since the Unix epoch (UTC) of the data the price came
        from — **not** wall-clock time the cache was written. The UI must
        never mistake a stale mark for a live price, so this timestamp is
        always carried alongside the price (never optional).
    source : {"bar_close", "last_fill"}
        How the mark was derived. Always ``"bar_close"`` on the publish path
        in this module (:meth:`MarkCache.update`); ``"last_fill"`` is the
        read-side fallback a consumer applies when no bar-close mark exists
        yet (leaf 02), never written here.

    """

    price: Money
    asof_ms: int
    source: Literal["bar_close", "last_fill"]


class MarkCache:
    """Per-engine, latest-write-wins cache of every symbol's last mark.

    One instance per :class:`~trading_bot.application.service_factory.Engine`
    (default-constructed there, like
    :class:`~trading_bot.application.instrument_specs.InstrumentSpecResolver`).
    A plain ``dict`` keyed by :class:`~trading_bot.domain.instrument.Symbol` —
    see the module docstring for why no lock is needed.
    """

    def __init__(self) -> None:
        self._marks: dict[Symbol, Mark] = {}

    def update(self, symbol: Symbol, price: Money, asof_ms: int) -> None:
        """Publish ``symbol``'s latest close, overwriting any prior mark.

        Parameters
        ----------
        symbol : Symbol
            The traded pair this close belongs to.
        price : Money
            The exact (``Decimal``) close.
        asof_ms : int
            The as-of (ms since epoch, UTC) of the bar this close came from —
            the same value the caller stamps on its signals for this tick.

        Notes
        -----
        The source is always ``"bar_close"``: this is the runner's publish
        path (the *only* writer of this cache); the ``"last_fill"`` fallback
        is a read-side concern for a consumer with no cached mark yet.

        """
        self._marks[symbol] = Mark(price=price, asof_ms=asof_ms, source="bar_close")

    def get(self, symbol: Symbol) -> Mark | None:
        """The symbol's latest published mark, or ``None`` if never published."""
        return self._marks.get(symbol)

    def all(self) -> dict[Symbol, Mark]:
        """A snapshot of every symbol's latest mark.

        Returns
        -------
        dict of Symbol to Mark
            A fresh copy of the cache's map (the :class:`Mark` values are
            frozen and shared) — mutating the returned dict never affects the
            cache.

        """
        return dict(self._marks)
