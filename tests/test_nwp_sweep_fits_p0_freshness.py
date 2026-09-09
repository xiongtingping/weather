"""The rotation must come back to an event date before its packets expire.

A date's official-native packets are only re-captured when the rotation cursor
lands on one of that date's scopes and finds the inbox stale.  So the sweep
period is the real re-capture period, and it has to sit inside the compiler's
freshness window or a date goes dark with nobody asking why.

2026-09-09 is the judgement: 2026-09-10 was captured 02:42Z, expired 08:42Z,
and its scopes' next turn had still not arrived by 12:20Z -- 45 stations
fail-closed on ``native_ensemble_snapshot_stale`` across the whole morning
while the lane reported itself healthy.
"""

from bayes_basket_target.production_worker import (
    NATIVE_P0_COMPILE_FRESH_SECONDS,
)
from scripts.weather_basket_yes_daemon import (
    TARGET_NWP_POLL_INTERVAL_SEC,
    TARGET_NWP_SWEEP_BUDGET_SEC,
    _nwp_scopes_per_poll,
    nwp_sweep_health,
)

# The resident's own shape, read off rotation_state.json: 381 on 2026-09-09
# morning, 402 by that afternoon.  It grows -- with the fleet, with the open
# days, with the lead labels -- and the growth is the thing this bound has to
# survive, so the number here is the live one and not the arithmetic that
# happened to produce it once.
#
# At 402 the nominal lap is 1.17h, comfortably inside the bound; at the
# achieved cadence measured that day (27 minutes a poll) it is 3.15h, which
# is NOT, and the runtime reading says so -- nwp_sweep_health reports
# fits_freshness_window false and the board raises it.  That split is
# deliberate: this test guards the declaration, the receipt guards the day.
LIVE_CATALOG_SCOPE_COUNT = 402


def _sweep_seconds(scope_count: int) -> float:
    per_poll = _nwp_scopes_per_poll(scope_count)
    polls = -(-scope_count // per_poll)
    return polls * TARGET_NWP_POLL_INTERVAL_SEC


def test_sweep_fits_inside_p0_freshness_with_margin() -> None:
    sweep = _sweep_seconds(LIVE_CATALOG_SCOPE_COUNT)
    # Half the window, so a poll the forecast lane loses to the production
    # writer turn still leaves a whole re-capture opportunity inside the TTL.
    assert sweep <= NATIVE_P0_COMPILE_FRESH_SECONDS / 2.0, (
        f"sweep {sweep / 3600:.2f}h exceeds half of the "
        f"{NATIVE_P0_COMPILE_FRESH_SECONDS / 3600:.1f}h P0 freshness window"
    )


def test_per_poll_step_honours_the_declared_sweep_budget() -> None:
    # The ceiling must not silently defeat the budget: whatever the budget
    # asks for at the live catalog size is what the rotation advances.
    polls_in_budget = TARGET_NWP_SWEEP_BUDGET_SEC / TARGET_NWP_POLL_INTERVAL_SEC
    needed = -(-LIVE_CATALOG_SCOPE_COUNT // int(polls_in_budget))
    assert _nwp_scopes_per_poll(LIVE_CATALOG_SCOPE_COUNT) == needed


def test_small_catalog_still_uses_the_historical_floor() -> None:
    assert _nwp_scopes_per_poll(1) >= 3
    assert _nwp_scopes_per_poll(0) >= 3


# ------------------------------------------------------------------ achieved
# The two tests above pin the DECLARED shape, and the declaration is not what
# failed.  The lane polls only when it wins its turn against the production
# cycle and the hosted sweeps, so its achieved interval was 27 minutes on the
# day 2026-09-10 went dark -- 2.7x the 600s the sweep was sized against.  A
# guard that reads the constant is green through exactly that outage.

OBSERVED_GAP_SEC = 27.0 * 60.0  # measured between two polls, 2026-09-09


def test_the_sweep_reading_is_taken_from_the_achieved_cadence() -> None:
    """Same catalogue, two cadences, two different laps."""

    declared = nwp_sweep_health(
        catalog_scope_count=LIVE_CATALOG_SCOPE_COUNT,
        poll_intervals_sec=(),
        freshness_sec=NATIVE_P0_COMPILE_FRESH_SECONDS,
    )
    achieved = nwp_sweep_health(
        catalog_scope_count=LIVE_CATALOG_SCOPE_COUNT,
        poll_intervals_sec=[OBSERVED_GAP_SEC] * 5,
        freshness_sec=NATIVE_P0_COMPILE_FRESH_SECONDS,
    )
    assert declared["poll_interval_sample_count"] == 0
    assert declared["contention_factor"] == 1.0
    assert achieved["sweep_achieved_hours"] > declared["sweep_achieved_hours"]
    assert achieved["contention_factor"] > 2.0, achieved


def test_a_lane_that_stops_winning_its_turn_stops_fitting() -> None:
    """The verdict has to be able to go false, or it is not a reading.

    At the live catalogue the current step leaves a 2.7h lap against a 3h
    bound -- real margin, and not much of it.  A lane stretched to a poll
    every three quarters of an hour no longer fits, and that is the state
    this receipt exists to make visible before a date goes dark rather than
    after.
    """

    stretched = nwp_sweep_health(
        catalog_scope_count=LIVE_CATALOG_SCOPE_COUNT,
        poll_intervals_sec=[45.0 * 60.0] * 5,
        freshness_sec=NATIVE_P0_COMPILE_FRESH_SECONDS,
    )
    assert stretched["fits_freshness_window"] is False, stretched
    # At the live catalogue it fits, and the reading is what proved it had
    # stopped.  402 scopes under a per-poll ceiling of 64 need seven polls,
    # which at the observed cadence is 3.15h against a 3.0h bound -- the
    # ceiling raised that morning for a 381-scope catalogue was capping the
    # budget again by the afternoon.  With the budget governing it is 67 a
    # poll, six polls, 2.70h.
    at_observed = nwp_sweep_health(
        catalog_scope_count=LIVE_CATALOG_SCOPE_COUNT,
        poll_intervals_sec=[OBSERVED_GAP_SEC] * 5,
        freshness_sec=NATIVE_P0_COMPILE_FRESH_SECONDS,
    )
    assert at_observed["fits_freshness_window"] is True, at_observed
    assert at_observed["polls_per_sweep"] == 6, at_observed


def test_the_median_is_read_not_the_luckiest_gap() -> None:
    """One gap is whichever turn the lane happened to win."""

    reading = nwp_sweep_health(
        catalog_scope_count=LIVE_CATALOG_SCOPE_COUNT,
        # Four stretched polls and one that got straight through.
        poll_intervals_sec=[60.0, 1620.0, 1620.0, 1620.0, 1620.0],
        freshness_sec=NATIVE_P0_COMPILE_FRESH_SECONDS,
    )
    assert reading["poll_interval_achieved_sec"] == 1620.0, reading
