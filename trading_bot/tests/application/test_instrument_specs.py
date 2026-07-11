"""Tests for :mod:`trading_bot.application.instrument_specs`.

Offline against **injected fake adapters** (no real Kraken/Binance, no
network):

* the right fake is dispatched per exchange name, an unknown exchange returns
  a bare instrument with no fetch attempted at all
  (:func:`test_dispatch_kraken_binance_bare`);
* a resolved instrument is cached — a second ``resolve`` for the same key does
  not call the adapter again, and neither do two *concurrent* first calls
  (:func:`test_cache_single_fetch`);
* an adapter that raises falls back to a bare instrument, the exchange is
  remembered on ``degraded``, and the failure is not retried on a later call
  for the same key (:func:`test_fetch_failure_falls_back_bare_and_caches`);
* the default (no injected adapters) resolver builds its adapters keylessly —
  no credentials, nothing raises at construction
  (:func:`test_keyless_construction`).

One opt-in ``@pytest.mark.network`` test hits the real Binance/Kraken public
endpoints (mirrors ``test_binance_rest.py`` / ``test_kraken_rest.py``'s style).
"""

from __future__ import annotations

# Built-in
import asyncio

# Third-party
import pytest

# Local
from trading_bot.application.instrument_specs import InstrumentSpecResolver
from trading_bot.brokers.binance import BinanceBroker
from trading_bot.brokers.kraken import KrakenBroker
from trading_bot.domain.errors import BrokerError
from trading_bot.domain.instrument import Instrument, Symbol

BTC_USD = Symbol("BTC", "USD")
BTC_USDT = Symbol("BTC", "USDT")


class _FakeAdapter:
    """A fake ``instrument()`` source recording calls; returns a canned spec."""

    def __init__(self, instrument: Instrument, *, delay: float = 0.0) -> None:
        self._instrument = instrument
        self._delay = delay
        self.calls: list[Symbol] = []

    async def instrument(self, symbol: Symbol) -> Instrument:
        self.calls.append(symbol)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._instrument


class _RaisingAdapter:
    """A fake ``instrument()`` source that always raises ``BrokerError``."""

    def __init__(self) -> None:
        self.calls: list[Symbol] = []

    async def instrument(self, symbol: Symbol) -> Instrument:
        self.calls.append(symbol)
        raise BrokerError("venue unreachable")


async def test_dispatch_kraken_binance_bare() -> None:
    """Each exchange dispatches to its own adapter; an unknown one is bare."""
    kraken_spec = Instrument(BTC_USD, price_precision=1, qty_precision=8)
    binance_spec = Instrument(BTC_USDT, price_precision=2, qty_precision=6)
    kraken_fake = _FakeAdapter(kraken_spec)
    binance_fake = _FakeAdapter(binance_spec)
    resolver = InstrumentSpecResolver(kraken=kraken_fake, binance=binance_fake)

    assert await resolver.resolve("kraken", BTC_USD) is kraken_spec
    assert await resolver.resolve("binance", BTC_USDT) is binance_spec
    assert kraken_fake.calls == [BTC_USD]
    assert binance_fake.calls == [BTC_USDT]

    # An unrecognised exchange never touches either fake and returns bare.
    other_symbol = Symbol("ETH", "EUR")
    result = await resolver.resolve("some-other-venue", other_symbol)
    assert result == Instrument(other_symbol)
    assert kraken_fake.calls == [BTC_USD]
    assert binance_fake.calls == [BTC_USDT]
    assert resolver.degraded == set()


async def test_cache_single_fetch() -> None:
    """A key is fetched once: a repeat call, and concurrent first calls, hit cache."""
    spec = Instrument(BTC_USD, price_precision=1, qty_precision=8)
    fake = _FakeAdapter(spec, delay=0.02)
    resolver = InstrumentSpecResolver(kraken=fake)

    first = await resolver.resolve("kraken", BTC_USD)
    second = await resolver.resolve("kraken", BTC_USD)
    assert first is spec
    assert second is spec
    assert fake.calls == [BTC_USD]

    # Two concurrent first calls for a *different* key must still fetch once.
    other_fake = _FakeAdapter(
        Instrument(BTC_USDT, price_precision=2, qty_precision=6), delay=0.02
    )
    resolver_concurrent = InstrumentSpecResolver(kraken=other_fake)
    results = await asyncio.gather(
        resolver_concurrent.resolve("kraken", BTC_USDT),
        resolver_concurrent.resolve("kraken", BTC_USDT),
    )
    assert results[0] is results[1]
    assert len(other_fake.calls) == 1


async def test_fetch_failure_falls_back_bare_and_caches() -> None:
    """A raising adapter degrades to a bare instrument, remembered, not retried."""
    fake = _RaisingAdapter()
    resolver = InstrumentSpecResolver(kraken=fake)

    result = await resolver.resolve("kraken", BTC_USD)
    assert result == Instrument(BTC_USD)
    assert resolver.degraded == {"kraken"}
    assert fake.calls == [BTC_USD]

    # Second call for the same key hits the cached bare result, no re-fetch.
    result_again = await resolver.resolve("kraken", BTC_USD)
    assert result_again == Instrument(BTC_USD)
    assert fake.calls == [BTC_USD]


def test_keyless_construction() -> None:
    """The default resolver builds Kraken/Binance adapters with no credentials."""
    resolver = InstrumentSpecResolver()
    kraken = resolver._adapter_for("kraken")
    binance = resolver._adapter_for("binance")
    assert isinstance(kraken, KrakenBroker)
    assert isinstance(binance, BinanceBroker)
    assert kraken.has_credentials is False
    assert binance.has_credentials is False
    # Lazy build is memoised: a second lookup returns the same instances.
    assert resolver._adapter_for("kraken") is kraken
    assert resolver._adapter_for("binance") is binance


# --- real public smoke (opt-in: ``-m network``) --------------------------- #


@pytest.mark.network
async def test_real_resolve_binance_kraken_public() -> None:
    """Real resolve of BTC/USDT on Binance and BTC/USD on Kraken (no keys)."""
    resolver = InstrumentSpecResolver()

    binance_inst = await resolver.resolve("binance", BTC_USDT)
    assert binance_inst.min_notional is not None
    assert binance_inst.min_notional > 0

    kraken_inst = await resolver.resolve("kraken", BTC_USD)
    assert kraken_inst.min_qty is not None
    assert kraken_inst.min_qty > 0

    assert resolver.degraded == set()
