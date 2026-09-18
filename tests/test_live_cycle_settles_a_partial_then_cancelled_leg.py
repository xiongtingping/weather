"""A cycle whose only fill is a rung that filled in part and was then
cancelled is over, and settles.

``resolve_cancelled_after_partial_fill`` -- charter 94's ordinary ending for
a resting rung -- leaves the intent ``cancelled`` over a ``resolved_partial``
reconciliation that holds the shares.  The live-cycle settle counted a fill
only on an intent ``filled``, so such a cycle stayed ``prepared`` as the
station-day's standing candidate; supersession then (correctly) refused to
retire a cycle that had traded, and every later decision on the day read
``prepared_supersession_execution_fact_present``.  KLGA 2026-09-18 (4.99 of
78-79F filled 09-17 04:22Z, the rung then cancelled) and ZUCK 2026-09-18
(1.78) were held all day.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bayes_basket_target.atomic_ledger import (
    AtomicLedgerError,
    settle_live_cycle_after_terminal_intents,
)
from bayes_basket_target.production_worker import TargetProductionWorker
from tests.test_bayes_basket_target_atomic_ledger import _db
from tests.test_charter_84_ladder_balance import _live_cycle_with_fill, _pool


def _partial_then_cancelled(conn, *, index: int, filled: float = 4.99, notional: float = 1.25):
    cycle_id, intent_id = _live_cycle_with_fill(conn, index=index, intent_state="cancelled")
    conn.execute(
        "UPDATE basket_target_order_reconciliations SET state='resolved_partial',"
        " filled_size=?, filled_notional_usdc=? WHERE order_intent_id=?",
        (filled, notional, intent_id),
    )
    conn.commit()
    return cycle_id, intent_id


def test_the_ledger_settles_a_cycle_whose_only_fill_was_cancelled_after_part_filled() -> None:
    with _db() as conn:
        cycle_id, _ = _partial_then_cancelled(conn, index=80)
        balance_before, reserved_before = _pool(conn)
        settled = settle_live_cycle_after_terminal_intents(
            conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:20:00Z",
        )
        assert settled["state"] == "settled"
        assert settled["consumed_usdc"] == pytest.approx(1.25)
        assert conn.execute(
            "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?", (cycle_id,),
        ).fetchone()[0] == "settled"
        balance_after, reserved_after = _pool(conn)
        assert balance_after == pytest.approx(balance_before - 1.25)
        assert reserved_after == pytest.approx(reserved_before - 3.0)


def test_a_cancelled_leg_with_nothing_filled_is_still_not_a_fill() -> None:
    with _db() as conn:
        cycle_id, _ = _partial_then_cancelled(conn, index=81, filled=0.0, notional=0.0)
        with pytest.raises(AtomicLedgerError, match="nothing_filled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:20:00Z",
            )


def test_the_resident_sweep_picks_up_the_partial_then_cancelled_cycle() -> None:
    with _db() as conn:
        partial, _ = _partial_then_cancelled(conn, index=82)
        empty_partial, _ = _partial_then_cancelled(conn, index=83, filled=0.0, notional=0.0)
        receipt = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:30:00Z",
        )
        assert receipt["state"] == "committed"
        assert receipt["settled"] == 1
        assert receipt["settled_scopes"][0]["cycle_id"] == partial
        assert receipt["failures"] == []
        states = dict(conn.execute("SELECT cycle_id, state FROM basket_target_execution_cycles"))
        assert states[partial] == "settled"
        assert states[empty_partial] == "prepared"
