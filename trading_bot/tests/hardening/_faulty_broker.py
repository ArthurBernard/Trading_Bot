"""``FaultyBroker`` — a fault-injecting wrapper around :class:`PaperBroker`.

The hardening suite needs to drive the engine *adversarially*: not "does the
happy path work?" but "does a money-safety invariant still hold when the broker
misbehaves?". A real venue misbehaves in three ways that matter for safety, and
each is a controllable fault here — everything in-process, no network, no key:

* **clean rejection** — :meth:`fail_next_place` makes the next
  :meth:`place_order` raise *without* recording anything on the venue. The order
  never landed; the engine should drive it to ``REJECTED`` and a retry is free to
  try again.
* **ambiguous failure** — :meth:`ambiguous_next_place` is the dangerous one: the
  next :meth:`place_order` **records the order on the wrapped venue** (so
  :meth:`open_orders` / :meth:`fills` show it) but **raises to the caller**, as if
  the venue accepted the order and the *response* was then lost. The engine
  cannot tell this apart from a clean rejection at submit time — the order may or
  may not be live. This is precisely the case where a naive retry would
  double-submit and reconciliation must adopt the truth instead.
* **disconnect window** — :meth:`seed_order` / :meth:`seed_fills` (and
  :meth:`disconnect` as a readable alias) put orders/fills directly on the wrapped
  broker that the local engine never tracked, simulating the gap where the engine
  was offline while the venue kept working. :func:`reconcile` then has real work.

Determinism
-----------
The wrapper holds no clock and no randomness: faults are *armed* one-shot flags
consumed by the next call, so a test reads top-to-bottom as a story ("arm the
ambiguous failure, submit, assert the engine did not lie"). Every non-faulty
method delegates straight to the wrapped :class:`PaperBroker`, so the venue's
recorded truth (open orders, fills, balances) is exactly the paper broker's —
the wrapper only changes *what the caller observes*, never corrupts the truth.

The wrapper satisfies the :class:`~trading_bot.brokers.base.Broker` port
structurally (it has every method + ``capabilities``), so it drops into the
:class:`~trading_bot.application.order_router.OrderRouter`,
:func:`~trading_bot.application.reconcile.reconcile` and
:meth:`~trading_bot.application.risk.RiskManager.kill` unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable

from trading_bot.brokers.base import Broker, Capability
from trading_bot.brokers.paper import PaperBroker
from trading_bot.domain.errors import BrokerError
from trading_bot.domain.fill import Fill
from trading_bot.domain.instrument import Instrument
from trading_bot.domain.money import Money
from trading_bot.domain.order import Order

__all__ = ["FaultyBroker"]


class FaultyBroker(Broker):
    """A :class:`PaperBroker` wrapper that injects controllable faults.

    Wrap a paper broker, then arm a one-shot fault before the call you want to
    perturb. Non-faulty calls delegate to the wrapped broker, so the venue's
    recorded state stays exactly the paper broker's truth — only what the
    *caller* observes changes.

    Parameters
    ----------
    inner : PaperBroker
        The wrapped broker that holds the real (simulated) venue state.

    Attributes
    ----------
    inner : PaperBroker
        The wrapped broker (exposed so a test can read the venue's truth, e.g.
        ``broker.inner.open_orders()`` is the same as ``broker.open_orders()``).
    name : str
        ``"faulty"`` — a distinct venue key from the wrapped ``"paper"``.

    """

    name = "faulty"

    def __init__(self, inner: PaperBroker) -> None:
        self.inner = inner
        # One-shot fault flags, consumed by the next ``place_order``.
        self._fail_next: BrokerError | None = None
        self._ambiguous_next = False
        # The venue id assigned to the most recent ambiguous placement (the order
        # that landed despite the caller seeing a failure); for test inspection.
        self.last_ambiguous_venue_id: str | None = None
        # Count of orders that actually reached the wrapped venue (delegated or
        # ambiguous placements — NOT clean rejections, which never touch it). The
        # honest "how many orders did the venue receive?" counter for tests.
        self.venue_place_count = 0

    # --- fault arming (one-shot) ------------------------------------------ #

    def fail_next_place(
        self, exc: BrokerError | None = None
    ) -> None:
        """Arm the next :meth:`place_order` to **cleanly reject** — no record.

        The next placement raises ``exc`` (default a generic
        :class:`~trading_bot.domain.errors.BrokerError`) *before* touching the
        wrapped venue: nothing is recorded, so the order genuinely never landed.
        Models a venue that rejected the request outright (bad params, rate
        limit, auth) — the safe failure, where a retry is correct.

        Parameters
        ----------
        exc : BrokerError, optional
            The error to raise. Defaults to ``BrokerError("simulated clean
            rejection")``.

        """
        self._fail_next = exc or BrokerError("simulated clean rejection")

    def ambiguous_next_place(self) -> None:
        """Arm the next :meth:`place_order` to **land then lose its response**.

        The next placement first records the order on the wrapped venue (so
        :meth:`open_orders` / :meth:`fills` will show it — the order *is* live)
        and **then** raises a :class:`~trading_bot.domain.errors.BrokerError`, as
        if the venue accepted the order and the acknowledgement was lost in
        transit. The caller cannot tell this from a clean rejection at submit
        time: the order may or may not be live. This is the failure that makes a
        naive retry double-submit, and the one reconciliation must resolve by
        adopting the venue's truth.
        """
        self._ambiguous_next = True

    async def disconnect(self, *orders: Order) -> list[str]:
        """Simulate a disconnect window: land ``orders`` the engine never saw.

        An ergonomic async seam for the reconciliation story — while the engine
        was "offline", the venue placed these ``orders`` (recorded on the wrapped
        broker, with their own simulated fills), but the local router/tracker
        never tracked any of it. :func:`reconcile` then has real divergence to
        converge. Equivalent to :meth:`seed_order` per order.

        Parameters
        ----------
        *orders : Order
            Orders to place directly on the wrapped broker (bypassing the
            engine), as if submitted before/while the engine was disconnected.

        Returns
        -------
        list of str
            The venue order ids the wrapped broker assigned, in order.

        """
        return [await self.seed_order(order) for order in orders]

    async def seed_order(self, order: Order) -> str:
        """Place ``order`` **directly** on the wrapped venue (no fault, no engine).

        The async seam used to set up a disconnect: the order is recorded on the
        wrapped paper broker exactly as a real venue would have it, but the
        engine's router never tracked it. Returns the venue order id.

        Parameters
        ----------
        order : Order
            The order to land directly on the venue.

        Returns
        -------
        str
            The synthetic venue order id from the wrapped broker.

        """
        return await self.inner.place_order(order)

    def seed_fills(self, fills: Iterable[Fill]) -> None:
        """Record ``fills`` directly on the wrapped venue's fill history.

        A standalone-fill seam for a disconnect: the venue confirmed these
        executions while the engine was offline, so they are the PnL truth a
        rebuild must fold in even with no matching tracked order.

        Parameters
        ----------
        fills : Iterable[Fill]
            The fills to append to the wrapped broker's recorded history.

        """
        self.inner._fills.extend(fills)

    # --- Broker port ------------------------------------------------------ #

    async def place_order(self, order: Order) -> str:
        """Submit ``order``, applying any armed one-shot fault.

        Resolves the armed fault (if any) first:

        * a :meth:`fail_next_place` arming raises **before** the wrapped broker
          is touched — nothing is recorded (clean rejection);
        * an :meth:`ambiguous_next_place` arming places on the wrapped broker
          **then** raises — the order is live but the caller sees a failure.

        With no fault armed, this delegates straight to the wrapped broker.
        """
        clean_fail = self._fail_next
        self._fail_next = None
        if clean_fail is not None:
            # Clean rejection: do not touch the venue. The order never landed.
            raise clean_fail

        ambiguous = self._ambiguous_next
        self._ambiguous_next = False
        if ambiguous:
            # The order DOES land on the venue (recorded by the wrapped broker)...
            self.venue_place_count += 1
            self.last_ambiguous_venue_id = await self.inner.place_order(order)
            # ...but the caller sees a failure, as if the response was lost.
            raise BrokerError(
                "simulated ambiguous failure: order placed on venue but "
                "acknowledgement lost in transit"
            )

        self.venue_place_count += 1
        return await self.inner.place_order(order)

    async def cancel_order(self, venue_order_id: str) -> None:
        """Cancel ``venue_order_id`` on the wrapped broker (no fault injected)."""
        await self.inner.cancel_order(venue_order_id)

    async def open_orders(self) -> list[Order]:
        """Return the wrapped broker's open orders — the venue's truth."""
        return await self.inner.open_orders()

    async def balances(self) -> dict[str, Money]:
        """Return the wrapped broker's balances — the venue's truth."""
        return await self.inner.balances()

    async def fills(self, since_ms: int | None = None) -> list[Fill]:
        """Return the wrapped broker's fills — the PnL source of truth."""
        return await self.inner.fills(since_ms)

    async def ticker(self, instrument: Instrument) -> Money:
        """Return the wrapped broker's mark price for ``instrument``."""
        return await self.inner.ticker(instrument)

    def capabilities(self) -> set[Capability]:
        """Mirror the wrapped broker's declared capabilities."""
        return self.inner.capabilities()
