"""The pending-correction blocker must retire the auto-clear throttle.

A correction halts that station-day's P1 -- the only writer of the
reported-high hard bound -- until the standing auto-clear policy runs.  That
policy runs on a six-hour timer, so the halt outlived the correction by hours:
of 128 corrections since 2026-08-23, 55 waited over an hour for their receipt
and one waited sixteen.  The blocker names the pending work, so it clears the
timer and the next poll runs the policy.
"""

from __future__ import annotations

from bayes_basket_target.production_worker import (
    CORRECTION_AUTO_CLEAR_RETRY_SEC,
    correction_review_pending,
)


def test_correction_review_blocker_is_recognised() -> None:
    assert correction_review_pending([
        {
            "station_id": "KMIA",
            "event_date": "2026-08-29",
            "reason": (
                "ContractError:resident_p1_correction_requires_manual_review:"
                "correction_review_required:rtobs_88056c67"
            ),
        },
    ])


def test_unrelated_blockers_leave_the_throttle_alone() -> None:
    # A future-day or lane blocker says nothing about pending review work, and
    # must not turn every blocked scope into a subprocess trigger.
    assert not correction_review_pending([
        {
            "station_id": "OEJN",
            "event_date": "2026-08-30",
            "reason": "ContractError:future_event_day_not_allowed",
        },
        {
            "station_id": "ZBAA",
            "event_date": "2026-08-29",
            "reason": "ContractError:resident_p1_source_lane_registry_mismatch",
        },
    ])


def test_no_blockers_and_malformed_input_are_not_pending() -> None:
    assert not correction_review_pending([])
    assert not correction_review_pending(None)
    assert not correction_review_pending(["not-a-mapping", 7])
    assert not correction_review_pending([{"station_id": "KMIA"}])


def test_quarantine_decision_is_not_a_pending_review() -> None:
    # A station-day the reviewer deliberately quarantined is resolved, not
    # pending; waking the pass for it would spawn a subprocess every cycle for
    # a halt no policy run can lift.
    assert not correction_review_pending([
        {
            "station_id": "KMIA",
            "event_date": "2026-08-29",
            "reason": (
                "ContractError:resident_p1_correction_requires_manual_review:"
                "correction_review_quarantines_station_day:rtobs_88056c67"
            ),
        },
    ])


def test_throttle_window_is_still_the_standing_six_hours() -> None:
    # The wake-up shortens the wait for a named halt; it does not change the
    # cadence of the unprompted sweep.
    assert CORRECTION_AUTO_CLEAR_RETRY_SEC == 6 * 3600.0
