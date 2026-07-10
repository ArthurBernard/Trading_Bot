"""Tests for the :class:`~trading_bot.application.capital_service.CapitalService`.

Proves the ledger's application-side driver against a real (tmp-file)
:class:`~trading_bot.storage.sqlite_store.SqliteStore`, money exact end to end:

* **genesis idempotency** — two :meth:`ensure_genesis` calls record **one**
  event (the deterministic ``f"{strategy}:funding"`` id + ``INSERT OR IGNORE``);
  a *restart* (a fresh service over the same store) never re-funds;
* **contributed fold** — :meth:`contributed` folds ``FUNDING`` + ``DEPOSIT`` in,
  ``WITHDRAWAL`` out, live (a mid-run deposit grows the *next* read, no rebuild);
* **sizing base** — ``fixed`` sizes on ``C`` (ignores realised PnL); ``compound``
  sizes on ``C + R`` (realised only), floored at ``0`` on a deep drawdown.
"""

from __future__ import annotations

from trading_bot.application.capital_service import CapitalService
from trading_bot.domain.capital import CapitalEvent, CapitalEventType
from trading_bot.domain.money import money
from trading_bot.storage.sqlite_store import SqliteStore


def _store(tmp_path) -> SqliteStore:  # noqa: ANN001
    """A fresh file-backed store (an in-memory one does not persist per-op)."""
    return SqliteStore(str(tmp_path / "ledger.sqlite"))


def test_ensure_genesis_records_one_deterministic_event(tmp_path) -> None:  # noqa: ANN001
    """The genesis lands once, under the deterministic ``<name>:funding`` id."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")

    # Built-in
    import time

    before_ms = int(time.time() * 1000)
    assert cap.ensure_genesis(money("100")) is True  # newly recorded
    after_ms = int(time.time() * 1000)
    events = store.capital_events(strategy="alloc1")
    assert len(events) == 1
    assert events[0].event_id == "alloc1:funding"
    assert events[0].event_type is CapitalEventType.FUNDING
    assert events[0].amount == money("100")
    # The genesis carries the wall clock at first seeding (the audit trail shows
    # WHEN the strategy was funded — not the retired ts=0 sentinel, which the UI
    # rendered as 1970-01-01).
    assert before_ms <= events[0].ts <= after_ms


def test_ensure_genesis_is_idempotent(tmp_path) -> None:  # noqa: ANN001
    """A second ensure_genesis (same or new amount) is a no-op — never double-funds."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")
    assert cap.ensure_genesis(money("100")) is True
    # A repeat — even with a different amount — is ignored (the id already exists).
    assert cap.ensure_genesis(money("100")) is False
    assert cap.ensure_genesis(money("999")) is False
    assert len(store.capital_events(strategy="alloc1")) == 1
    assert cap.contributed() == money("100")


def test_restart_never_refunds(tmp_path) -> None:  # noqa: ANN001
    """A fresh service over the same store re-seeds nothing (re-deploy safety)."""
    store = _store(tmp_path)
    CapitalService(store, "alloc1", "paper").ensure_genesis(money("100"))

    # A brand-new service (a redeploy / restart) over the SAME store.
    restarted = CapitalService(store, "alloc1", "paper")
    assert restarted.ensure_genesis(money("100")) is False
    assert len(store.capital_events(strategy="alloc1")) == 1
    assert restarted.contributed() == money("100")


def test_contributed_folds_deposits_and_withdrawals(tmp_path) -> None:  # noqa: ANN001
    """contributed folds FUNDING/DEPOSIT in and WITHDRAWAL out, live per read."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")
    cap.ensure_genesis(money("100"))
    assert cap.contributed() == money("100")

    # A mid-run deposit grows the NEXT read, with no service rebuild.
    store.record_capital_event(
        CapitalEvent("D1", "alloc1", CapitalEventType.DEPOSIT, money("50"), ts=10)
    )
    assert cap.contributed() == money("150")

    # A withdrawal shrinks it.
    store.record_capital_event(
        CapitalEvent("W1", "alloc1", CapitalEventType.WITHDRAWAL, money("30"), ts=20)
    )
    assert cap.contributed() == money("120")


def test_sizing_base_fixed_ignores_realised_pnl(tmp_path) -> None:  # noqa: ANN001
    """fixed sizes on C alone — realised profit does not grow the base."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")
    cap.ensure_genesis(money("100"))
    # Even a big realised profit is ignored by the fixed base.
    assert cap.sizing_base("fixed", money("40")) == money("100")
    assert cap.sizing_base("fixed", money("-40")) == money("100")


def test_sizing_base_compound_reinvests_realised_pnl(tmp_path) -> None:  # noqa: ANN001
    """compound sizes on C + R (realised only)."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")
    cap.ensure_genesis(money("100"))
    assert cap.sizing_base("compound", money("40")) == money("140")
    assert cap.sizing_base("compound", money("-25")) == money("75")


def test_sizing_base_compound_floored_at_zero(tmp_path) -> None:  # noqa: ANN001
    """A realised loss deeper than C floors the compound base at 0 (never negative)."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")
    cap.ensure_genesis(money("100"))
    # A 150 loss would give -50; the base is floored at 0 (no negative sizing).
    assert cap.sizing_base("compound", money("-150")) == money("0")


def test_deposit_grows_compound_base_live(tmp_path) -> None:  # noqa: ANN001
    """A deposit lifts both the fixed and compound base on the next read."""
    store = _store(tmp_path)
    cap = CapitalService(store, "alloc1", "paper")
    cap.ensure_genesis(money("100"))
    store.record_capital_event(
        CapitalEvent("D1", "alloc1", CapitalEventType.DEPOSIT, money("100"), ts=10)
    )
    assert cap.sizing_base("fixed", money("10")) == money("200")
    assert cap.sizing_base("compound", money("10")) == money("210")
