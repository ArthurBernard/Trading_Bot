"""The instrument-spec resolver — one metadata-rich :class:`Instrument` per venue.

Order preparation needs the venue's real minimums and precisions (see
``doc/dev/plans/venue-minimums/00-plan.md``): a rebalance leg cannot be rounded
or skipped against the venue minimum without first knowing what that minimum
*is*. :class:`Instrument` already models that spec (``min_qty`` /
``min_notional`` / precisions, ``domain/instrument.py``), and both live
adapters already fetch it from a **public** endpoint — Kraken ``AssetPairs``
(:meth:`~trading_bot.brokers.kraken.KrakenBroker.instrument`), Binance
``exchangeInfo`` (:meth:`~trading_bot.brokers.binance.BinanceBroker.instrument`)
— but nothing on the runner path calls them: ``signals_from_weights`` builds a
bare ``Instrument(symbol)`` (``application/portfolio.py``). This module is the
one place that gap is closed.

Why here, not on the ``Broker`` port
-------------------------------------
``instrument()`` is adapter-specific, not part of the venue-neutral
:class:`~trading_bot.brokers.base.Broker` port — the port models *trading*
(place/cancel/query), not venue metadata, and widening it would force every
adapter (including the simulator) to answer a question only two live venues
can. ``application/`` already imports concrete adapters for exactly this kind
of venue dispatch (``service_factory.py``), so the resolver lives here, one
layer above the port, and ``brokers/`` gains no new cross-adapter import.

Cache policy
------------
Resolved instruments are cached **for the process lifetime**, keyed by
``(exchange, str(symbol))``. Venue minimums change on venue announcements, not
intraday, so there is no TTL — a daemon restart is what refreshes it. A single
:class:`asyncio.Lock` serialises the whole check-then-fetch-then-store
section, so two concurrent first calls for the same key (or for different
keys — the lock is resolver-wide, not per-key) can never race into a double
fetch; at the scale this resolver runs at (roughly one call per configured
symbol at startup, not per tick), serialising a handful of one-off HTTP calls
behind one lock is a non-issue and far simpler than a lock-per-key registry.

Permissive fallback — degrade, never block trading
----------------------------------------------------
On **any** fetch failure (a mapped :class:`~trading_bot.domain.errors.
BrokerError`, a transport error, a timeout — anything the adapter's
``instrument()`` can raise) :meth:`InstrumentSpecResolver.resolve` falls back
to a bare ``Instrument(symbol)`` (today's permissive behaviour — no spec, no
quantization, no minimum enforced) and **caches that bare result**, so a
broken endpoint is not re-hit on every subsequent call for the same key. The
failure is remembered on :attr:`InstrumentSpecResolver.degraded` — the set of
exchange names that have had at least one fetch failure — so a caller (the
order-prep policy, leaf 02) can emit its own one-per-lifetime warning instead
of the resolver logging on every degraded call. ``degraded`` tracks **fetch
failures only**, not "exchange has no adapter" (``kraken``/``binance`` are the
only two dispatched venues; any other exchange name deliberately returns a
bare instrument with **no** fetch attempted at all, which is not a
degradation to warn about here — the caller already knows which venues it
configured).
"""

from __future__ import annotations

# Built-in
import asyncio
import logging
from typing import Protocol, runtime_checkable

# Local
from trading_bot.brokers.binance import BinanceBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.domain.instrument import Instrument, Symbol

__all__ = ["InstrumentSpecResolver"]

logger = logging.getLogger(__name__)


@runtime_checkable
class _InstrumentSource(Protocol):
    """The narrow structural shape the resolver actually needs from an adapter.

    Both :class:`~trading_bot.brokers.kraken.KrakenBroker` and
    :class:`~trading_bot.brokers.binance.BinanceBroker` satisfy this (they each
    implement ``instrument()`` for their own public metadata endpoint, even
    though it is not part of the venue-neutral :class:`~trading_bot.brokers.
    base.Broker` port). Narrowing to just this method — rather than typing the
    constructor against the concrete adapter classes — is what lets tests
    inject a minimal fake in their place.
    """

    async def instrument(self, symbol: Symbol) -> Instrument:
        """Fetch (or build) the venue's :class:`Instrument` spec for ``symbol``."""
        ...


class InstrumentSpecResolver:
    """Resolve a metadata-rich :class:`Instrument` for any ``(exchange, symbol)``.

    Dispatches by exchange name — the same strings
    :func:`~trading_bot.application.service_factory._build_broker` matches on
    (``"kraken"``, ``"binance"``, case-insensitive) — to a lazily-built,
    **keyless** adapter used only for its public ``instrument()`` call; any
    other exchange name returns a bare ``Instrument(symbol)`` with no fetch
    attempted. See the module docstring for the cache and fallback policy.

    Parameters
    ----------
    kraken : object, optional
        A pre-built Kraken-shaped adapter (anything satisfying
        :class:`_InstrumentSource`) to use instead of a fresh keyless
        :class:`~trading_bot.brokers.kraken.KrakenBroker` — the test seam.
        Built lazily (and keylessly) on first use when omitted.
    binance : object, optional
        Same as ``kraken``, for the ``"binance"`` dispatch.

    Attributes
    ----------
    degraded : set of str
        Exchange names that have had at least one fetch failure fall back to
        a bare instrument (see the module docstring). Empty until a fetch
        actually fails.

    """

    def __init__(
        self,
        *,
        kraken: _InstrumentSource | None = None,
        binance: _InstrumentSource | None = None,
    ) -> None:
        self._kraken = kraken
        self._binance = binance
        self._cache: dict[tuple[str, str], Instrument] = {}
        self.degraded: set[str] = set()
        # Resolver-wide (not per-key) — see "Cache policy" in the module
        # docstring for why one lock is enough at this scale.
        self._lock = asyncio.Lock()

    async def resolve(self, exchange: str, symbol: Symbol) -> Instrument:
        """Return the cached or freshly-fetched :class:`Instrument` for the pair.

        Parameters
        ----------
        exchange : str
            The venue key (``"kraken"``, ``"binance"``, ...; case-insensitive).
        symbol : Symbol
            The canonical pair to describe.

        Returns
        -------
        Instrument
            The venue's spec on a successful fetch (first call) or cache hit
            (subsequent calls); a bare, unquantized ``Instrument(symbol)`` for
            an unrecognised exchange or a failed fetch (see the module
            docstring's fallback policy).

        """
        exchange = exchange.lower()
        key = (exchange, str(symbol))
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            instrument = await self._fetch(exchange, symbol)
            self._cache[key] = instrument
            return instrument

    async def _fetch(self, exchange: str, symbol: Symbol) -> Instrument:
        """Fetch ``symbol``'s spec from ``exchange``'s adapter, or a bare fallback.

        Called only while holding :attr:`_lock`, on a cache miss. Never raises
        — any adapter failure is caught, logged once, remembered in
        :attr:`degraded` and downgraded to a bare instrument.
        """
        adapter = self._adapter_for(exchange)
        if adapter is None:
            # Not a dispatched venue: bare by design, no fetch attempted, not
            # a degradation (see the module docstring).
            return Instrument(symbol)
        try:
            return await adapter.instrument(symbol)
        except Exception:
            logger.warning(
                "InstrumentSpecResolver: %s instrument() fetch failed for %s; "
                "falling back to a bare (unquantized) instrument and caching "
                "it so this endpoint is not re-hit every call",
                exchange,
                symbol,
                exc_info=True,
            )
            self.degraded.add(exchange)
            return Instrument(symbol)

    def _adapter_for(self, exchange: str) -> _InstrumentSource | None:
        """The lazily-built, keyless adapter for ``exchange``, or ``None``.

        Builds and caches :attr:`_kraken` / :attr:`_binance` on first use
        (unless a pre-built adapter was injected at construction); a venue
        outside this dispatch (including ``"paper"``) returns ``None``.
        """
        if exchange == "kraken":
            if self._kraken is None:
                self._kraken = KrakenBroker()
            return self._kraken
        if exchange == "binance":
            if self._binance is None:
                self._binance = BinanceBroker()
            return self._binance
        return None
