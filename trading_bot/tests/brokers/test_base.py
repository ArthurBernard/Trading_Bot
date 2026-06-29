"""Tests for the :class:`Broker` port and :class:`Capability` model.

This is the interface layer — there is no live I/O. The contract is proven
*implementable and coherent* by a :class:`StubBroker` that satisfies the
:class:`~trading_bot.brokers.base.Broker` :class:`~typing.Protocol` purely with
domain objects, round-trips an :class:`~trading_bot.domain.order.Order` through
``place_order`` -> ``open_orders``, and returns ``Decimal``/domain types from
``balances``/``fills``/``ticker``. The
:func:`~trading_bot.brokers.base.require` capability gate is covered too.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_bot.brokers import (
    Broker,
    BrokerError,
    Capability,
    require,
)
from trading_bot.domain import (
    Fill,
    Instrument,
    Money,
    NoCapability,
    Order,
    OrderSide,
    OrderType,
    Symbol,
    money,
)

# A canonical instrument reused across the stub and assertions.
BTC_USD = Instrument(Symbol("BTC", "USD"))


class StubBroker:
    """An in-memory :class:`Broker` for tests — every method, domain types only.

    Implements the full port without inheriting it (structural typing): a
    placed order is stored under a synthesised venue id and surfaced again by
    :meth:`open_orders`, so a round-trip can be asserted. ``balances``,
    ``fills`` and ``ticker`` return ``Decimal``/domain objects.

    The declared :attr:`_capabilities` is configurable so a test can build a
    broker that is *missing* a capability and check the :func:`require` gate.
    """

    def __init__(
        self,
        name: str = "stub",
        capabilities: set[Capability] | None = None,
    ) -> None:
        self.name = name
        self._capabilities = (
            capabilities
            if capabilities is not None
            else set(Capability)  # full menu by default
        )
        self._open: dict[str, Order] = {}
        self._next_id = 0

    async def place_order(self, order: Order) -> str:
        self._next_id += 1
        venue_order_id = f"VID-{self._next_id}"
        # Drive the domain order through its lifecycle so the stored order is a
        # realistic, live order keyed by the venue id we return.
        order.submit()
        order.open(venue_order_id)
        self._open[venue_order_id] = order
        return venue_order_id

    async def cancel_order(self, venue_order_id: str) -> None:
        order = self._open.pop(venue_order_id, None)
        if order is None:
            raise BrokerError(f"unknown venue order id {venue_order_id!r}")
        order.cancel()

    async def open_orders(self) -> list[Order]:
        return list(self._open.values())

    async def balances(self) -> dict[str, Money]:
        return {"USD": money("1000.50"), "BTC": money("0.25")}

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        fill = Fill(
            fill_id="T1",
            client_order_id="cid-1",
            instrument=BTC_USD,
            side=OrderSide.BUY,
            qty=money("1"),
            price=money("30000"),
            fee=money("12"),
            ts=1_700_000_000_000,
        )
        if since_ms is not None and fill.ts < since_ms:
            return []
        return [fill]

    async def ticker(self, instrument: Instrument) -> Money:
        return money("30100.5")

    def capabilities(self) -> set[Capability]:
        return self._capabilities


def _make_order(client_order_id: str = "cid-1") -> Order:
    """Build a fresh limit :class:`Order` for round-trip tests."""
    return Order(
        client_order_id=client_order_id,
        instrument=BTC_USD,
        side=OrderSide.BUY,
        qty=money("2"),
        type=OrderType.LIMIT,
        limit_price=money("30000"),
    )


# --- the port is satisfiable with domain types ----------------------------- #


def test_stub_satisfies_broker_protocol() -> None:
    """The stub is a :class:`Broker` structurally (runtime-checkable Protocol)."""
    assert isinstance(StubBroker(), Broker)


async def test_place_order_returns_venue_id_and_round_trips() -> None:
    """``place_order`` returns an id; the order resurfaces via ``open_orders``."""
    broker = StubBroker()
    order = _make_order()

    venue_order_id = await broker.place_order(order)

    assert isinstance(venue_order_id, str) and venue_order_id
    assert order.venue_order_id == venue_order_id

    open_orders = await broker.open_orders()
    assert order in open_orders
    assert open_orders[0].client_order_id == "cid-1"


async def test_cancel_order_removes_it() -> None:
    """A placed order can be cancelled and then no longer appears as open."""
    broker = StubBroker()
    venue_order_id = await broker.place_order(_make_order())

    await broker.cancel_order(venue_order_id)

    assert await broker.open_orders() == []


async def test_cancel_unknown_order_raises_broker_error() -> None:
    """Cancelling an unknown venue id raises :class:`BrokerError`."""
    broker = StubBroker()
    with pytest.raises(BrokerError):
        await broker.cancel_order("VID-does-not-exist")


async def test_balances_returns_decimal_per_asset() -> None:
    """``balances`` returns a per-asset map of exact ``Decimal`` values."""
    balances = await StubBroker().balances()
    assert balances == {"USD": Decimal("1000.50"), "BTC": Decimal("0.25")}
    assert all(isinstance(v, Decimal) for v in balances.values())


async def test_fills_returns_domain_fills() -> None:
    """``fills`` returns domain :class:`Fill` objects with ``Decimal`` amounts."""
    fills = await StubBroker().fills()
    assert len(fills) == 1
    fill = fills[0]
    assert isinstance(fill, Fill)
    assert fill.qty == Decimal("1")
    assert fill.price == Decimal("30000")


async def test_fills_since_ms_filters() -> None:
    """``since_ms`` past the only fill's ``ts`` yields an empty list."""
    assert await StubBroker().fills(since_ms=1_800_000_000_000) == []


async def test_ticker_returns_decimal_price() -> None:
    """``ticker`` returns an exact ``Decimal`` price for the instrument."""
    price = await StubBroker().ticker(BTC_USD)
    assert price == Decimal("30100.5")
    assert isinstance(price, Decimal)


# --- capability gate ------------------------------------------------------- #


def test_require_passes_when_capability_declared() -> None:
    """``require`` is a no-op when the broker declares the capability."""
    broker = StubBroker(capabilities={Capability.PLACE_ORDER})
    require(broker, Capability.PLACE_ORDER)  # must not raise


def test_require_raises_no_capability_when_missing() -> None:
    """``require`` raises :class:`NoCapability` for an undeclared capability."""
    broker = StubBroker(name="kraken", capabilities={Capability.TICKER})
    with pytest.raises(NoCapability) as excinfo:
        require(broker, Capability.PLACE_ORDER)

    err = excinfo.value
    assert err.venue == "kraken"
    assert err.capability == Capability.PLACE_ORDER.value


def test_capabilities_default_full_menu() -> None:
    """The default stub declares every :class:`Capability`."""
    assert StubBroker().capabilities() == set(Capability)
