"""An order should not outlive the belief that posted it.

The paper ledger's resting rung already matches production in the half that
gets discussed: the dwell it re-decides on IS production's poll cadence, and
the decision IS the kernel's ``resting_rung_action_v1``.  The half that did
not match is the horizon.  Production signs a GTD whose expiry is the served
belief's own ``fresh_until_utc`` -- charter 127 (二), measured on that column
as 4.44 hours mean at P0 (capped at the 6-hour run period), 4.31 at P0-M and
exactly 60 minutes at P1 -- and this book rested its rung into the afternoon
instead, buying on a claim nobody was serving any more.

``PAPER_LEDGER_RESTING_LIFECYCLE=production_v127`` makes that expiry a fourth
bound beside the peak cutoff, the day end and the configured horizon.  The
default is unchanged, and these tests hold it there.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLACED = "2026-09-10T02:00:00Z"


def _ledger(lifecycle: str | None):
    """A fresh module instance: the switch is read at import."""

    previous = os.environ.get("PAPER_LEDGER_RESTING_LIFECYCLE")
    if lifecycle is None:
        os.environ.pop("PAPER_LEDGER_RESTING_LIFECYCLE", None)
    else:
        os.environ["PAPER_LEDGER_RESTING_LIFECYCLE"] = lifecycle
    try:
        import paper_trading_ledger  # noqa: PLC0415

        return importlib.reload(paper_trading_ledger)
    finally:
        if previous is None:
            os.environ.pop("PAPER_LEDGER_RESTING_LIFECYCLE", None)
        else:
            os.environ["PAPER_LEDGER_RESTING_LIFECYCLE"] = previous


def _deadline(module, *, belief: str, cutoff: int | None = None) -> str:
    return module._resting_deadline(
        placed_at_utc=PLACED,
        settlement_timezone="UTC",
        event_date="2026-09-10",
        cutoff_local_minutes=cutoff,
        belief_fresh_until_utc=belief,
    )


def test_the_belief_expiry_bounds_the_order_under_the_production_lifecycle() -> None:
    module = _ledger("production_v127")
    # A P1 belief: exactly sixty minutes, which is the observation window.
    assert _deadline(module, belief="2026-09-10T03:00:00Z") == (
        "2026-09-10T03:00:00Z"
    )
    # A P0 belief: five and a half hours, which is the run cadence.
    assert _deadline(module, belief="2026-09-10T07:30:00Z") == (
        "2026-09-10T07:30:00Z"
    )


def test_the_default_lifecycle_ignores_the_belief_entirely() -> None:
    """The shipped book must not move before the arm is read."""

    module = _ledger(None)
    assert module.RESTING_LIFECYCLE == "ledger"
    with_belief = _deadline(module, belief="2026-09-10T03:00:00Z")
    without = _deadline(module, belief="")
    assert with_belief == without


def test_the_earlier_bound_wins_and_says_which_one_it_was() -> None:
    module = _ledger("production_v127")
    # The peak cutoff at 04:00 UTC beats a belief good until 07:30.
    assert _deadline(
        module, belief="2026-09-10T07:30:00Z", cutoff=4 * 60
    ) == "2026-09-10T04:00:00Z"
    reason = module._resting_deadline_reason(
        placed_at_utc=PLACED, settlement_timezone="UTC",
        event_date="2026-09-10", cutoff_local_minutes=4 * 60,
        belief_fresh_until_utc="2026-09-10T07:30:00Z",
    )
    assert "达峰截止" in reason
    # And the other way: a P1 belief expiring before the peak is the
    # binding rule, and the page has to say so or a reader goes looking at
    # the cutoff for an order the venue killed an hour in.
    assert _deadline(
        module, belief="2026-09-10T03:00:00Z", cutoff=8 * 60
    ) == "2026-09-10T03:00:00Z"
    assert "信念新鲜期" in module._resting_deadline_reason(
        placed_at_utc=PLACED, settlement_timezone="UTC",
        event_date="2026-09-10", cutoff_local_minutes=8 * 60,
        belief_fresh_until_utc="2026-09-10T03:00:00Z",
    )


def test_a_belief_already_stale_at_placement_gets_the_venues_grace() -> None:
    """Not a stamp in the past: production could not have signed that."""

    module = _ledger("production_v127")
    deadline = _deadline(module, belief="2026-09-10T01:00:00Z")
    assert deadline > PLACED
    # Production's number, imported rather than restated: the gateway
    # enforces the same floor and two copies stop being one number.
    from bayes_basket_target.production import (  # noqa: PLC0415
        VENUE_GTD_MINIMUM_HORIZON_SECONDS,
    )

    assert module.VENUE_GTD_MINIMUM_HORIZON_SECONDS is (
        VENUE_GTD_MINIMUM_HORIZON_SECONDS
    )
    assert VENUE_GTD_MINIMUM_HORIZON_SECONDS == 60.0
    assert deadline == "2026-09-10T02:01:00Z"


def test_the_served_belief_is_the_newest_one_at_or_before_placement() -> None:
    module = _ledger("production_v127")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, state TEXT,"
        " decision_at_utc TEXT, fresh_until_utc TEXT)"
    )
    rows = [
        # Older, superseded.
        ("KMIA", "2026-09-10", "COMMITTED", "2026-09-10T00:00:00Z",
         "2026-09-10T04:00:00Z"),
        # Serving at placement -- this is the one.
        ("KMIA", "2026-09-10", "COMMITTED", "2026-09-10T01:30:00Z",
         "2026-09-10T06:00:00Z"),
        # Not yet served when the order was signed.
        ("KMIA", "2026-09-10", "COMMITTED", "2026-09-10T03:00:00Z",
         "2026-09-10T09:00:00Z"),
        # Never committed.
        ("KMIA", "2026-09-10", "PENDING", "2026-09-10T01:45:00Z",
         "2026-09-10T08:00:00Z"),
    ]
    conn.executemany(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()

    assert module._belief_fresh_until(
        conn, station_id="KMIA", event_date="2026-09-10",
        placed_at_utc=PLACED,
    ) == "2026-09-10T06:00:00Z"
    # A station-day the bridge cannot answer for is "no belief bound",
    # never zero -- a zero would expire every order at placement.
    assert module._belief_fresh_until(
        conn, station_id="KLGA", event_date="2026-09-10",
        placed_at_utc=PLACED,
    ) == ""


def test_a_database_without_the_bridge_does_not_expire_every_order() -> None:
    module = _ledger("production_v127")
    conn = sqlite3.connect(":memory:")
    assert module._belief_fresh_until(
        conn, station_id="KMIA", event_date="2026-09-10",
        placed_at_utc=PLACED,
    ) == ""
    assert _deadline(module, belief="") == _deadline(
        _ledger(None), belief=""
    )


def test_the_arm_is_in_the_fingerprint_and_the_default_is_not() -> None:
    default = _ledger(None).SELECTION_RULE_SHA256
    arm = _ledger("production_v127").SELECTION_RULE_SHA256
    assert default != arm
    # Restored: the reload above must not leave the default book rehashed.
    assert _ledger(None).SELECTION_RULE_SHA256 == default


def test_an_unknown_lifecycle_is_refused_rather_than_silently_ignored() -> None:
    import pytest  # noqa: PLC0415

    with pytest.raises(SystemExit):
        _ledger("whatever_i_typed")


def test_both_sides_read_the_same_column_for_the_expiry() -> None:
    """Charter 127 (二) names one source; two readers must not drift."""

    production = (
        ROOT / "bayes_basket_target" / "production.py"
    ).read_text(encoding="utf-8")
    assert "fresh_until_utc" in production
    assert "belief_fresh_until" in production
    ledger = (
        ROOT / "scripts" / "paper_trading_ledger.py"
    ).read_text(encoding="utf-8")
    assert "fresh_until_utc" in ledger
    # And the ledger asks the bridge history, which is where the compile
    # put it -- not a second table, and not a horizon of its own.
    assert (
        "FROM basket_probability_bridge_history" in ledger
        and "fresh_until_utc" in ledger
    )


def test_the_dwell_half_was_already_productions_and_is_left_alone() -> None:
    """The change is the horizon, not the re-pricing.

    Worth pinning: the plan for this work item assumed the ledger re-posted
    on a clock of its own, following the bid.  It does not -- that was
    replaced by the kernel's rung action, the dwell is pinned equal to
    production's, and every escalation the ledger once had is off.  So the
    arm measures one difference rather than several.
    """

    module = _ledger(None)
    assert module.MAKER_RESTING_ANCHORED is True
    assert module.MAKER_RESTING_REPRICE_MINUTES == (
        module.RESTING_REPRICE_DWELL_SECONDS / 60.0
    )
    for dormant in (
        "MAKER_RESTING_ESCALATE_SHARE",
        "MAKER_RESTING_CONFIRM_TAKE_TICKS",
        "MAKER_RESTING_CRASH_TICKS",
    ):
        assert float(getattr(module, dormant)) == 0.0, dormant
    assert module.MAKER_RESTING_COVERAGE_BUDGET < 0.0
    assert not module.MAKER_RESTING_BELIEF_GATE
    assert module.MAKER_RESTING_PPOST_COMPONENTS == frozenset()
