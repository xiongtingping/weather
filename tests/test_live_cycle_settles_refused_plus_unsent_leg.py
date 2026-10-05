"""A cycle whose legs were refused or cancelled before sending is over, and settles.

ZSQD 2026-10-05 (cycle created 10-03 14:07Z): two legs NEVER_REACHED_EXCHANGE
(``resolved_not_placed``), the third cancelled by the quote-lease retire
before it was ever sent -- no reconciliation, no event.  Settle refused (not
"all refused"), supersession refused (the refusals are execution facts), and
the station-day's next decision read
``prepared_supersession_execution_fact_present``.  Eight such cycles stood
with ~35 USDC reserved against orders that never left.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from bayes_basket_target.atomic_ledger import (
    AtomicLedgerError,
    settle_live_cycle_after_terminal_intents,
)
from bayes_basket_target.production_worker import TargetProductionWorker
from tests.test_bayes_basket_target_atomic_ledger import _committed_intent, _db
from tests.test_charter_84_ladder_balance import _pool


def _refused_plus_unsent(conn, *, index: int, unsent_has_event: bool = False):
    bundle, refused = _committed_intent(conn, index=index)
    cycle_id = bundle["cycle"]["cycle_id"]
    cols = [row[1] for row in conn.execute("PRAGMA table_info(basket_target_order_intents)")]
    row = dict(zip(cols, conn.execute(
        "SELECT * FROM basket_target_order_intents WHERE order_intent_id=?", (refused,),
    ).fetchone()))
    unsent = refused[:-8] + "unsent01"
    row.update(order_intent_id=unsent, idempotency_key=row["idempotency_key"] + "u", state="cancelled")
    conn.execute(
        f"INSERT INTO basket_target_order_intents({','.join(row)}) VALUES({','.join('?' * len(row))})",
        list(row.values()),
    )
    conn.execute("UPDATE basket_target_order_intents SET state='failed' WHERE order_intent_id=?", (refused,))
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations(order_intent_id,exchange_order_id,state,"
        "filled_size,filled_notional_usdc,last_event_id,blocking_reason,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (refused, None, "resolved_not_placed", 0.0, 0.0, None, "egress_red_hold", "2026-07-23T04:10:00Z"),
    )
    for intent_id, kind in ((refused, "NEVER_REACHED_EXCHANGE"),) + (
        ((unsent, "UNKNOWN_POST"),) if unsent_has_event else ()
    ):
        conn.execute(
            "INSERT INTO basket_target_order_events(event_id,order_intent_id,exchange_order_id,"
            "event_type,observed_at,matched_size,matched_notional_usdc,event_json,event_sha256)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("ev-" + intent_id, intent_id, None, kind, "2026-07-23T04:10:00Z", 0.0, 0.0, "{}",
             hashlib.sha256(intent_id.encode()).hexdigest()),
        )
    conn.commit()
    return cycle_id, refused, unsent


def test_refused_legs_and_an_unsent_cancel_release_the_cycle() -> None:
    with _db() as conn:
        cycle_id, refused, unsent = _refused_plus_unsent(conn, index=90)
        balance_before, reserved_before = _pool(conn)
        settled = settle_live_cycle_after_terminal_intents(
            conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:20:00Z",
        )
        assert settled["outcome"] == "released"
        assert settled["consumed_usdc"] == 0.0
        assert settled["refused_intent_ids"] == [refused]
        assert settled["unsent_cancelled_intent_ids"] == [unsent]
        balance_after, reserved_after = _pool(conn)
        assert balance_after == pytest.approx(balance_before)
        assert reserved_after == pytest.approx(reserved_before - 3.0)


def test_a_cancelled_leg_that_did_reach_out_is_not_unsent() -> None:
    with _db() as conn:
        cycle_id, _refused, _unsent = _refused_plus_unsent(conn, index=91, unsent_has_event=True)
        with pytest.raises(AtomicLedgerError, match="nothing_filled"):
            settle_live_cycle_after_terminal_intents(
                conn, cycle_id=cycle_id, settled_at_utc="2026-07-23T04:20:00Z",
            )


def test_the_resident_sweep_picks_up_only_the_unsent_case() -> None:
    with _db() as conn:
        settleable, _r, _u = _refused_plus_unsent(conn, index=92)
        reached, _r2, _u2 = _refused_plus_unsent(conn, index=93, unsent_has_event=True)
        receipt = TargetProductionWorker._settle_live_cycles(
            SimpleNamespace(commit_conn=conn), as_of_utc="2026-07-23T04:30:00Z",
        )
        assert receipt["settled"] == 1
        assert receipt["settled_scopes"][0]["cycle_id"] == settleable
        assert receipt["failures"] == []
        states = dict(conn.execute("SELECT cycle_id, state FROM basket_target_execution_cycles"))
        assert states[settleable] == "settled"
        assert states[reached] == "prepared"
