"""The venue-neutral :class:`Broker` port + capability model.

This is the **execution-side port** of the trading bot: the single, async,
venue-neutral contract every exchange adapter (Kraken next, others later)
implements. It speaks **domain types only** — :class:`~trading_bot.domain.
order.Order`, :class:`~trading_bot.domain.fill.Fill`,
:class:`~trading_bot.domain.instrument.Instrument`, :class:`~trading_bot.domain.
money.Money` — and may use the :mod:`trading_bot.transport` plumbing in its
concrete adapters; no domain code ever imports a broker. All money and
quantities are exact :class:`~decimal.Decimal` (:data:`~trading_bot.domain.
money.Money`).

Protocol, not ABC
-----------------
The port is a :func:`~typing.runtime_checkable` :class:`~typing.Protocol`
rather than an abstract base class. The reasons, carried into the ADR:

* **Structural, not nominal.** A concrete adapter (the Kraken adapter, a test
  stub) is a :class:`Broker` because it *has the methods*, not because it
  inherited a base — no import coupling from adapters back to this module, which
  keeps the venue layer a flat set of independent adapters. This mirrors the
  spirit of dccd's source mixins while avoiding the inheritance ceremony.
* **Registry-friendly.** ``@runtime_checkable`` lets the
  :class:`~trading_bot.brokers.registry.BrokerRegistry` (and the
  :func:`require` helper) ``isinstance``-check what it stores, so a misconfigured
  registration fails loudly at the boundary rather than at first call.
* **Capabilities are declared, not inherited.** Whether an adapter actually
  *supports* an operation is answered by :meth:`Broker.capabilities` returning a
  :class:`Capability` set, **not** by which base classes it subclasses. The port
  surface is the full menu; the capability set is the honest subset a given
  venue serves. The engine gates every operation through :func:`require` so a
  venue is never asked for something it has not declared.

The trade-off of a ``Protocol`` is that it gives adapters no default method
bodies — but a port should have none anyway (every method is venue-specific
I/O), so there is nothing to inherit. ``runtime_checkable`` only checks method
*presence*, not signatures; the typed signatures here are enforced statically by
mypy on each concrete adapter.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from trading_bot.domain.errors import BrokerError, NoCapability
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money
from trading_bot.domain.order import Order

__all__ = [
    "Broker",
    "Capability",
    "BrokerError",
    "require",
]


class Capability(Enum):
    """A discrete operation a :class:`Broker` adapter may declare it supports.

    An adapter's :meth:`Broker.capabilities` returns the **honest subset** of
    these it actually serves. The engine gates every operation through
    :func:`require`, so a venue is never asked for an operation it did not
    declare (which would otherwise surface as an opaque venue error or a silent
    no-op).

    Members map one-to-one onto the :class:`Broker` surface, plus transport-shape
    flags that have no single method (``PRIVATE_WS`` for a private/authenticated
    WebSocket stream of order/fill updates).
    """

    #: :meth:`Broker.place_order` — submit a new order.
    PLACE_ORDER = "place_order"
    #: :meth:`Broker.cancel_order` — cancel a live order by venue id.
    CANCEL = "cancel"
    #: :meth:`Broker.open_orders` — list currently open orders.
    OPEN_ORDERS = "open_orders"
    #: :meth:`Broker.balances` — per-asset free balances.
    BALANCES = "balances"
    #: :meth:`Broker.fills` — historical/recent executions.
    FILLS = "fills"
    #: :meth:`Broker.ticker` — public last/mark price.
    TICKER = "ticker"
    #: A private/authenticated WebSocket feed of order & fill updates.
    PRIVATE_WS = "private_ws"


@runtime_checkable
class Broker(Protocol):
    """The async, venue-neutral execution port every exchange adapter implements.

    A broker submits and cancels orders, reports open orders, balances and
    fills, and reads a public ticker — all in **domain types**, with money and
    quantities as exact :class:`~decimal.Decimal`. Concrete adapters wire these
    to a venue's REST/WebSocket APIs via :mod:`trading_bot.transport`; this
    module is a pure interface and the next leaf implements Kraken.

    An adapter declares which of these it genuinely serves via
    :meth:`capabilities`; callers gate through :func:`require` before use.

    Attributes
    ----------
    name : str
        The venue key (e.g. ``"kraken"``). Used as the registry key.

    """

    #: The venue key (e.g. ``"kraken"``); the registry key for this adapter.
    name: str

    async def place_order(self, order: Order) -> str:
        """Submit ``order`` to the venue and return its venue order id.

        Parameters
        ----------
        order : Order
            The domain order to submit. Its lifecycle is driven by the caller;
            the adapter only transmits it and reports the venue's id back.

        Returns
        -------
        str
            The venue's identifier for the now-live order (the value the caller
            passes to :meth:`Order.open` and later to :meth:`cancel_order`).

        Raises
        ------
        BrokerError
            If the venue rejects or fails the submission.

        """
        ...

    async def cancel_order(self, venue_order_id: str) -> None:
        """Cancel the live order identified by ``venue_order_id``.

        Parameters
        ----------
        venue_order_id : str
            The venue's order id (as returned by :meth:`place_order`).

        Raises
        ------
        BrokerError
            If the venue rejects or fails the cancellation.

        """
        ...

    async def open_orders(self) -> list[Order]:
        """Return the venue's currently open orders as domain :class:`Order`s.

        Returns
        -------
        list of Order
            The open orders, reconstructed into domain objects (status, fills
            and ``venue_order_id`` populated from the venue's view).

        """
        ...

    async def balances(self) -> dict[str, Money]:
        """Return free balances keyed by canonical asset code.

        Returns
        -------
        dict of str to Decimal
            Asset code (e.g. ``"USD"``, ``"BTC"``) to its **free** (available)
            balance as an exact :class:`~decimal.Decimal`.

        """
        ...

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        """Return executions as domain :class:`Fill`s, optionally since a time.

        Parameters
        ----------
        since_ms : int, optional
            Lower time bound as **milliseconds since the Unix epoch (UTC)**.
            ``None`` (default) returns the venue's default recent window.

        Returns
        -------
        list of Fill
            The matching executions as immutable domain fills.

        """
        ...

    async def ticker(self, instrument: Instrument) -> Money:
        """Return the public last/mark price for ``instrument``.

        Parameters
        ----------
        instrument : Instrument
            The instrument to price.

        Returns
        -------
        Decimal
            The current last/mark price in quote units, exact.

        """
        ...

    def capabilities(self) -> set[Capability]:
        """Return the set of :class:`Capability` this adapter actually serves.

        Declared honestly: the :class:`Broker` surface is the full menu, but a
        given venue may not serve every operation. Callers gate through
        :func:`require` before invoking an operation.
        """
        ...


def require(broker: Broker, capability: Capability) -> None:
    """Assert ``broker`` declares ``capability``; raise :class:`NoCapability` if not.

    The single gate every caller uses before invoking a :class:`Broker`
    operation, so a venue is never asked for an operation it has not declared in
    :meth:`Broker.capabilities`.

    Parameters
    ----------
    broker : Broker
        The adapter to check.
    capability : Capability
        The capability the caller is about to use.

    Raises
    ------
    NoCapability
        If ``broker`` does not declare ``capability``. The error names the
        broker (its ``name``) and the missing capability's value.

    """
    if capability not in broker.capabilities():
        raise NoCapability(broker.name, capability.value)
