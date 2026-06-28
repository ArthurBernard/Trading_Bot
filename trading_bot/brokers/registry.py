"""The :class:`BrokerRegistry` — venue key to :class:`Broker` adapter.

Mirrors dccd's ``SourceRegistry``: a thin, case-insensitive map from a venue
name to a single registered :class:`~trading_bot.brokers.base.Broker` adapter.
The trading bot wires one adapter per venue (one Kraken adapter, etc.) rather
than the fine-grained per-(data-type x mode) lookups dccd needs, so the surface
is just :meth:`register` / :meth:`get` plus a read-only listing — capability
gating lives on the adapter (:func:`~trading_bot.brokers.base.require`), not the
registry.
"""

from __future__ import annotations

import logging

from trading_bot.brokers.base import Broker
from trading_bot.domain.errors import BrokerError

__all__ = ["BrokerRegistry"]

logger = logging.getLogger(__name__)


class BrokerRegistry:
    """Maps venue names to :class:`~trading_bot.brokers.base.Broker` adapters.

    Names are matched case-insensitively (stored lower-cased), so ``"Kraken"``
    and ``"kraken"`` resolve to the same adapter.

    Examples
    --------
    >>> reg = BrokerRegistry()
    >>> # reg.register("kraken", KrakenBroker())
    >>> # broker = reg.get("kraken")
    """

    def __init__(self) -> None:
        self._adapters: dict[str, Broker] = {}

    def register(self, name: str, broker: Broker) -> None:
        """Register ``broker`` under venue ``name`` (case-insensitive key).

        Parameters
        ----------
        name : str
            The venue key (e.g. ``"kraken"``).
        broker : Broker
            The adapter to register. A later registration under the same name
            replaces the earlier one.

        """
        self._adapters[name.lower()] = broker

    def get(self, name: str) -> Broker:
        """Return the adapter registered for ``name``.

        Parameters
        ----------
        name : str
            The venue key (case-insensitive).

        Returns
        -------
        Broker
            The registered adapter.

        Raises
        ------
        BrokerError
            If no adapter is registered under ``name``.

        """
        key = name.lower()
        if key not in self._adapters:
            raise BrokerError(f"no broker registered for venue {name!r}")
        return self._adapters[key]

    @property
    def venues(self) -> list[str]:
        """Names (lower-cased) of all registered venues."""
        return list(self._adapters.keys())

    @property
    def adapters(self) -> dict[str, Broker]:
        """A read-only copy of all registered adapters keyed by venue name."""
        return dict(self._adapters)
