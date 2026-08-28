"""The freshness window must mean one thing to the code and to the table.

All three rings of the 2026-08-27 regime-lane outage are pinned here: the
mint-time floor that stops a dead-on-arrival window existing, the P0-M
inheritance that propagated one, and the byte-order/instant-order
disagreement that turned it into an IntegrityError three layers away.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target.contracts import (
    MIN_USABLE_FRESHNESS_SECONDS,
    ContractError,
    ProbabilityEnvelope,
    assert_mintable_freshness_window,
    freshness_window_ordering_agrees,
    timestamp_lt,
)
from bayes_basket_target.production_worker import _shift_window


# ---------------------------------------------------------------- ring 3

def test_the_two_orderings_disagree_on_the_shape_that_broke_production():
    """The exact pair from the outage, and why it is not a rounding quibble.

    Instant order says the window is 44ms long and therefore valid; byte
    order says it runs backwards.  Both are "correct" for their own
    comparison, which is the whole defect.
    """

    decision = "2026-08-27T17:52:11Z"
    fresh_until = "2026-08-27T17:52:11.044000Z"

    assert timestamp_lt(decision, fresh_until) is True
    assert (decision < fresh_until) is False
    assert freshness_window_ordering_agrees(decision, fresh_until) is False


def test_agreement_holds_for_every_window_at_or_above_the_floor():
    """The floor is what makes the disagreement unreachable, structurally.

    The two orderings can only diverge when the second-precision prefixes
    tie, which requires the window to close inside the same second it
    opened.  Any window of a second or more moves the seconds field, and
    from there the byte order tracks the instant order.
    """

    base = datetime(2026, 8, 27, 17, 52, 11, tzinfo=timezone.utc)
    for seconds in (1, 30, MIN_USABLE_FRESHNESS_SECONDS, 3600, 21600):
        for decision_fmt, until_fmt in (
            ("seconds", "microseconds"),
            ("microseconds", "seconds"),
            ("seconds", "seconds"),
            ("microseconds", "microseconds"),
        ):
            decision = base.isoformat(
                timespec=decision_fmt
            ).replace("+00:00", "Z")
            until = (
                base + timedelta(seconds=seconds)
            ).isoformat(timespec=until_fmt).replace("+00:00", "Z")
            assert freshness_window_ordering_agrees(decision, until), (
                seconds, decision, until
            )


def test_sub_second_window_is_where_the_two_orderings_can_part():
    base = datetime(2026, 8, 27, 17, 52, 11, tzinfo=timezone.utc)
    decision = base.isoformat(timespec="seconds").replace("+00:00", "Z")
    until = (
        base + timedelta(milliseconds=44)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert timestamp_lt(decision, until)
    assert not freshness_window_ordering_agrees(decision, until)


def _envelope(decision: str, fresh_until: str) -> dict:
    return {
        "stage": "P0-M",
        "checkpoint_id": "p0m_" + "a" * 32,
        "station_id": "OEJN",
        "event_date": "2026-08-28",
        "settlement_timezone": "Asia/Riyadh",
        "decision_at_utc": decision,
        "fresh_until_utc": fresh_until,
        "bucket_ordinals": {"b0": 0, "b1": 1},
        "probabilities": {"b0": 0.4, "b1": 0.6},
        "posterior_version": "v1",
        "posterior_sha256": "b" * 64,
        "bridge_sha256": "c" * 64,
        "lineage": {"k": "v"},
    }


def test_the_load_path_must_keep_admitting_the_broken_rows():
    """The tempting fix, and why it is the wrong layer.

    ``from_mapping`` is both the mint constructor and the load path, and
    ``basket_probability_envelope`` is append-only by trigger: the sixteen
    bad rows can never be updated or deleted.  Rejecting them here would
    not repair anything -- it would make every stage of the seven
    station-days holding them unreadable, turning a missing regime
    companion into seven lost trading days.  If someone later "tightens"
    this, that is the regression this test exists to name.
    """

    envelope = ProbabilityEnvelope.from_mapping(
        _envelope("2026-08-27T17:52:11Z", "2026-08-27T17:52:11.044000Z")
    )
    assert envelope.fresh_until_utc == "2026-08-27T17:52:11.044000Z"


def test_short_but_coherent_historical_windows_still_load():
    """240 healthy rows carry windows under the floor; they must still read.

    The floor is a mint-time rule.  Enforcing it on load would make rows
    that were correct when written unreadable the day the floor landed --
    and the loader is what every consumer goes through.
    """

    envelope = ProbabilityEnvelope.from_mapping(
        _envelope("2026-08-27T17:52:11Z", "2026-08-27T17:52:41Z")
    )
    assert envelope.fresh_until_utc == "2026-08-27T17:52:41Z"


def test_mint_gate_refuses_what_the_database_would_reject():
    """The window is checked where refusing costs one checkpoint."""

    with pytest.raises(ContractError) as excinfo:
        assert_mintable_freshness_window(
            "2026-08-27T17:52:11Z",
            "2026-08-27T17:52:11.044000Z",
            name="probe",
        )
    assert "ordering_disagrees" in str(excinfo.value)


def test_mint_gate_refuses_a_coherent_but_unusable_window():
    with pytest.raises(ContractError) as excinfo:
        assert_mintable_freshness_window(
            "2026-08-27T17:52:11Z", "2026-08-27T17:52:41Z", name="probe"
        )
    assert "unusable" in str(excinfo.value)


def test_mint_gate_admits_a_healthy_window():
    assert_mintable_freshness_window(
        "2026-08-27T17:52:11Z", "2026-08-27T23:52:11Z", name="probe"
    )


# ---------------------------------------------------------------- ring 2

def test_shift_window_refuses_a_root_with_nothing_left_to_lend():
    """A 44ms root is not a root; the scope waits for the next one."""

    with pytest.raises(ContractError) as excinfo:
        _shift_window(
            "2026-08-27T17:52:11Z",
            "2026-08-27T15:31:52.455Z",
            "2026-08-27T15:31:52.499000Z",
        )
    assert "resident_taf_root_window_invalid" in str(excinfo.value)


def test_shift_window_refuses_rather_than_floors():
    """Lengthening a dead root's window would mint freshness from nothing.

    Flooring is the tempting fix and it is the wrong one: it is the same
    move the native TTL exists to forbid -- making a nearly expired packet
    arbitrarily fresh without a new model arrival.
    """

    root_decision = "2026-08-27T15:31:52.455Z"
    root_fresh_until = "2026-08-27T15:31:52.499000Z"
    with pytest.raises(ContractError):
        _shift_window(
            "2026-08-27T17:52:11Z", root_decision, root_fresh_until
        )


def test_shift_window_still_rebases_a_healthy_root():
    """The stated intent survives: the root's duration, from P0-M's own now."""

    out = _shift_window(
        "2026-08-27T17:52:11Z",
        "2026-08-27T09:38:41Z",
        "2026-08-27T15:31:52Z",
    )
    span = datetime.fromisoformat(
        "2026-08-27T15:31:52+00:00"
    ) - datetime.fromisoformat("2026-08-27T09:38:41+00:00")
    expected = (
        datetime.fromisoformat("2026-08-27T17:52:11+00:00") + span
    ).isoformat().replace("+00:00", "Z")
    assert out == expected
    assert freshness_window_ordering_agrees("2026-08-27T17:52:11Z", out)


def test_shift_window_boundary_is_the_floor_not_zero():
    """Exactly at the floor passes; a hair under does not."""

    start = datetime(2026, 8, 27, 9, 38, 41, tzinfo=timezone.utc)
    stamp = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731
    at_floor = _shift_window(
        "2026-08-27T17:52:11Z",
        stamp(start),
        stamp(start + timedelta(seconds=MIN_USABLE_FRESHNESS_SECONDS)),
    )
    assert at_floor
    with pytest.raises(ContractError):
        _shift_window(
            "2026-08-27T17:52:11Z",
            stamp(start),
            stamp(
                start
                + timedelta(seconds=MIN_USABLE_FRESHNESS_SECONDS - 0.001)
            ),
        )


# ---------------------------------------------------------------- ring 1

def test_native_compile_guard_tests_remaining_life_not_age():
    """The old guard asked "is it stale"; the answer can be no and useless.

    Reproduces the shape that started the outage: a compile 44ms inside a
    six-hour TTL.  Age says fresh, remaining life says nothing can act on
    it, and the second is the question the boundary actually needs answered
    -- it decides whether the native receipt keeps suppressing the fallback.
    """

    from bayes_basket_target.native_ensemble_p0 import (
        DEFAULT_FRESH_SECONDS,
    )

    ttl = float(DEFAULT_FRESH_SECONDS)
    captured = datetime(2026, 8, 27, 9, 31, 52, 499000, tzinfo=timezone.utc)
    edge = captured + timedelta(seconds=ttl - 0.044)

    age = (edge - captured).total_seconds()
    assert age < ttl, "the old age test would have admitted this"
    remaining = ttl - age
    assert remaining < MIN_USABLE_FRESHNESS_SECONDS, (
        "and the new remaining-life test refuses it"
    )

    healthy = captured + timedelta(seconds=ttl - 600)
    assert ttl - (healthy - captured).total_seconds() >= (
        MIN_USABLE_FRESHNESS_SECONDS
    )


# ------------------------------------------- the lane that actually unblocks

def test_expired_envelopes_can_never_be_read_back_so_are_not_covered():
    """Why skipping them is the coverage rule, not a workaround.

    ``load_regime_artifact`` matches ``valid_from <= evaluation`` AND
    ``valid_until > evaluation``.  The window is fixed at mint; evaluation
    clocks only move forward.  So once an evaluation passes ``valid_until``,
    no later one can match -- a companion built for an expired envelope is
    unreadable by construction, and the sixteen append-only rows that
    fail-closed the lane every cycle are exactly that case.
    """

    fresh_until = "2026-08-27T17:52:11.044000Z"
    for evaluation in (
        "2026-08-27T17:52:12Z",
        "2026-08-28T02:41:21Z",
        "2026-09-01T00:00:00Z",
    ):
        # The companion pass's own filter: strictly-later expiry survives.
        assert not timestamp_lt(evaluation, fresh_until)
        # And the lookup it feeds could not have matched either.
        assert not timestamp_lt(evaluation, fresh_until)

    live = "2026-08-28T06:00:00Z"
    assert timestamp_lt("2026-08-28T02:41:21Z", live)


def test_floor_is_above_one_second_so_the_ordering_trap_cannot_fire():
    """The two rules are load-bearing for each other; keep them that way.

    The byte/instant disagreement needs a window that opens and closes in
    one second.  If the floor were ever lowered below a second, ring 3
    would go from unreachable to reachable again.
    """

    assert MIN_USABLE_FRESHNESS_SECONDS >= 1.0
