"""The :class:`CapitalService` — the ledger's application-side driver.

Where :mod:`trading_bot.domain.capital` owns the *pure* capital vocabulary (the
immutable :class:`~trading_bot.domain.capital.CapitalEvent` and the
:func:`~trading_bot.domain.capital.contributed_capital` fold) and
:class:`~trading_bot.storage.sqlite_store.SqliteStore` owns the *append-only*
persistence, :class:`CapitalService` is the thin application seam that joins the
two and turns them into the one number the runners actually size against:

* **seed the genesis** — a strategy's declared ``allocation`` (or a portfolio's
  ``capital``) is folded into the ledger *once*, as a deterministic
  ``FUNDING`` event, by :meth:`ensure_genesis`. The config value only ever
  **seeds** the ledger; after that the ledger is the single source of truth for
  contributed capital (a re-deploy never double-funds — see below).
* **fold the ledger** — :meth:`contributed` re-reads and folds the strategy's
  capital events into ``C = Σ deposits − Σ withdrawals`` on demand (never a
  stored cache), so a mid-run ``DEPOSIT`` / ``WITHDRAWAL`` is reflected on the
  **next** read with no engine rebuild.
* **derive the sizing base** — :meth:`sizing_base` turns ``C`` and the strategy's
  realised PnL ``R`` into the base ``B`` the runners size against, per the
  strategy's :class:`~trading_bot.application.config` ``capital_policy``:
  ``fixed → B = C`` (size against the contributed base, ignore PnL) or
  ``compound → B = C + R`` (reinvest *realised* PnL only — never unrealised).

Genesis idempotency (carried into the ADR)
------------------------------------------
:meth:`ensure_genesis` mints the funding event with the **deterministic** id
``f"{strategy}:funding"`` and hands it to
:meth:`~trading_bot.storage.sqlite_store.SqliteStore.record_capital_event`, whose
``INSERT OR IGNORE`` on the composite ``(event_id, strategy, mode)`` primary key
makes a re-record a silent no-op. So calling :meth:`ensure_genesis` on every
deploy / restart is safe: the strategy is funded exactly once, and the config
``allocation`` can be edited afterwards without ever re-funding (the edit is
inert once the genesis exists — the ledger has taken over).

The compound floor (carried into the ADR)
-----------------------------------------
``compound`` reinvests realised PnL, so a drawdown *shrinks* the base. The base
is **floored at zero**: a realised loss deeper than the contributed capital must
never emit a **negative** sizing base (which would flip every target's sign).
A wiped-out compound strategy sizes against ``0`` (holds nothing), it never
sizes short by accident.

This module lives in the application layer: it composes the pure domain fold and
the storage read/write, holds money as :class:`~decimal.Decimal` end to end, and
performs I/O only through the injected store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from trading_bot.domain.capital import (
    CapitalEvent,
    CapitalEventType,
    contributed_capital,
)
from trading_bot.domain.money import Money, money

if TYPE_CHECKING:
    from trading_bot.storage.sqlite_store import SqliteStore

__all__ = ["CapitalPolicy", "CapitalService"]

#: The capital-evolution policy a strategy sizes under (mirrors the config field
#: :attr:`~trading_bot.application.config.StrategyConfig.capital_policy`).
CapitalPolicy = Literal["fixed", "compound"]

_ZERO: Money = money("0")

#: ``ts`` (epoch ms) stamped on the genesis funding event. Fixed at ``0`` so the
#: genesis always sorts **before** any real fill (whose ``ts`` is a live epoch-ms
#: bar time) on the merged value timeline
#: (:func:`~trading_bot.domain.capital.value_series`) — the funded capital is in
#: place for the first fill, and the derived value curve reconciles to the
#: fill-only equity curve. Matches the domain tests' genesis convention.
_GENESIS_TS_MS = 0


class CapitalService:
    """Seed and fold one strategy's capital ledger, and derive its sizing base.

    A per-strategy application helper over a shared
    :class:`~trading_bot.storage.sqlite_store.SqliteStore`: it seeds the genesis
    funding once (:meth:`ensure_genesis`), folds the ledger on demand
    (:meth:`contributed`), and turns the fold plus realised PnL into the sizing
    base the runners read every tick (:meth:`sizing_base`). See the module
    docstring for the genesis-idempotency guarantee and the compound floor.

    Parameters
    ----------
    store : SqliteStore
        The append-only store this strategy's capital events are recorded into
        and folded from. The genesis write is tagged with the store's current
        deployment ``mode`` / ``venue`` (see
        :meth:`~trading_bot.storage.sqlite_store.SqliteStore.set_context`),
        exactly like a fill.
    strategy : str
        The managed unit's logical id — the ``strategy`` every capital event is
        keyed by, and the ``f"{strategy}:funding"`` genesis-id seed.
    mode : str
        The deployment mode the ledger writes are made under (``"paper"`` /
        ``"testnet"`` / ``"live"``). Carried for the control plane (a later leaf
        deposits / withdraws per mode); the store's own ``set_context`` is what
        actually tags each row.

    """

    def __init__(self, store: SqliteStore, strategy: str, mode: str) -> None:
        self._store = store
        self._strategy = strategy
        self._mode = mode

    def ensure_genesis(self, amount: Money) -> bool:
        """Seed the strategy's genesis ``FUNDING`` event — idempotently.

        Records a ``FUNDING`` :class:`~trading_bot.domain.capital.CapitalEvent`
        of ``amount`` under the deterministic id ``f"{strategy}:funding"``. The
        store's ``INSERT OR IGNORE`` makes this a no-op once the genesis exists,
        so it is safe to call on every deploy / restart — the strategy is funded
        exactly once and the config ``allocation`` never re-funds after the
        first seeding (it has gone inert; the ledger is now the source of truth).

        Parameters
        ----------
        amount : Money
            The genesis capital (quote units) to fund the strategy with — the
            declared ``allocation`` (or a portfolio's ``capital``). Routed
            through :func:`~trading_bot.domain.money.money`, so it is exact and
            must be strictly positive.

        Returns
        -------
        bool
            ``True`` if the genesis was newly recorded, ``False`` if it already
            existed (an idempotent re-deploy).

        """
        event = CapitalEvent(
            event_id=f"{self._strategy}:funding",
            strategy=self._strategy,
            event_type=CapitalEventType.FUNDING,
            amount=money(amount),
            ts=_GENESIS_TS_MS,
            note="genesis funding",
        )
        return self._store.record_capital_event(event)

    def contributed(self) -> Money:
        """Fold the strategy's ledger into its net contributed capital ``C``.

        Re-reads every capital event for the strategy and folds it via
        :func:`~trading_bot.domain.capital.contributed_capital`
        (``Σ FUNDING/DEPOSIT − Σ WITHDRAWAL``). Read on demand, never cached, so
        a mid-run deposit / withdrawal is reflected on the next call with no
        engine rebuild — the property the lazy ``capital_provider`` relies on.

        Returns
        -------
        Money
            The net contributed capital ``C`` (``0`` before any genesis).

        """
        events = self._store.capital_events(strategy=self._strategy)
        return contributed_capital(events)

    def sizing_base(self, policy: CapitalPolicy, realised_pnl: Money) -> Money:
        """Derive the sizing base ``B`` the runners size against, per ``policy``.

        ``fixed`` sizes against the contributed base ``C`` alone (realised PnL is
        not reinvested); ``compound`` reinvests **realised** PnL into the base
        (``C + R``) — never unrealised, which is a mark, not money. The compound
        base is **floored at zero**: a realised loss deeper than the contributed
        capital must never emit a negative sizing base (see the module
        docstring).

        Parameters
        ----------
        policy : {"fixed", "compound"}
            The strategy's capital-evolution policy (its
            :attr:`~trading_bot.application.config.StrategyConfig.capital_policy`).
        realised_pnl : Money
            The strategy's realised PnL ``R`` (net of fees) — the fill-driven
            source of truth, read straight off the unit's performance service.
            Consulted only for ``compound``.

        Returns
        -------
        Money
            The sizing base ``B``: ``C`` for ``fixed``; ``max(0, C + R)`` for
            ``compound``.

        """
        contributed = self.contributed()
        if policy == "compound":
            base = contributed + realised_pnl
            return base if base > _ZERO else _ZERO
        return contributed
