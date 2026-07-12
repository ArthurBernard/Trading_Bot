"""Tests for :mod:`trading_bot.application.mark_cache`.

Proves the cache's whole contract:

* ``update``/``get`` round-trip the exact ``Decimal`` price, the ``asof_ms``
  and the ``"bar_close"`` source (:func:`test_update_get_round_trips_exact_decimal`);
* ``get`` on a never-published symbol is ``None``
  (:func:`test_get_unknown_symbol_is_none`);
* a second ``update`` for the same symbol overwrites the first — latest write
  wins, never accumulates (:func:`test_second_update_overwrites_the_first`);
* ``all`` snapshots every published symbol and mutating the returned dict never
  affects the cache (:func:`test_all_snapshots_every_symbol_and_is_a_copy`).
"""

from __future__ import annotations

# Local
from trading_bot.application.mark_cache import Mark, MarkCache
from trading_bot.domain.instrument import Symbol
from trading_bot.domain.money import money

BTC = Symbol("BTC", "USDT")
ETH = Symbol("ETH", "USDT")


def test_update_get_round_trips_exact_decimal() -> None:
    """``update`` then ``get`` returns the exact price, asof and bar_close source."""
    cache = MarkCache()

    cache.update(BTC, money("50000.12345678"), 1_700_000_000_000)

    mark = cache.get(BTC)
    assert mark is not None
    assert mark.price == money("50000.12345678")
    assert mark.asof_ms == 1_700_000_000_000
    assert mark.source == "bar_close"
    assert mark == Mark(
        price=money("50000.12345678"), asof_ms=1_700_000_000_000, source="bar_close"
    )


def test_get_unknown_symbol_is_none() -> None:
    """A symbol never published to the cache returns ``None``, not a KeyError."""
    cache = MarkCache()

    assert cache.get(BTC) is None


def test_second_update_overwrites_the_first() -> None:
    """A later ``update`` for the same symbol replaces the mark — no accumulation."""
    cache = MarkCache()
    cache.update(BTC, money("50000"), 1_700_000_000_000)

    cache.update(BTC, money("51000"), 1_700_000_060_000)

    mark = cache.get(BTC)
    assert mark is not None
    assert mark.price == money("51000")
    assert mark.asof_ms == 1_700_000_060_000


def test_all_snapshots_every_symbol_and_is_a_copy() -> None:
    """``all`` returns every published symbol's mark, and mutating it is inert."""
    cache = MarkCache()
    cache.update(BTC, money("50000"), 1_700_000_000_000)
    cache.update(ETH, money("2500"), 1_700_000_000_000)

    snapshot = cache.all()

    assert snapshot == {
        BTC: Mark(price=money("50000"), asof_ms=1_700_000_000_000, source="bar_close"),
        ETH: Mark(price=money("2500"), asof_ms=1_700_000_000_000, source="bar_close"),
    }

    # Mutating the returned dict must never leak back into the cache.
    del snapshot[BTC]
    assert cache.get(BTC) is not None
