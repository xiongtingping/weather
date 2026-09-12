"""Charter 125: the day's observed high floors bucket selection at every stage.

The settlement rule is the maximum over the whole local day, so a report of X
makes every bucket ending at or below X arithmetically dead.  Charter 61 put
that predicate in the P1 posterior; the selection layer then inherited it by
accident, because ``_support_windows`` draws its domain from
``probability > EPS`` and P1 is the only stage whose posterior zeroes those
rungs.  P0 never reads an observation at all and P0-M recomputes the bound
only when a TAF revision arrives.

ZUUU 2026-09-09 is the case: 27C at local midnight, cooling all day, and the
P0 envelope minted ten hours later still called 24C the mode at 38.6%.
"""

from __future__ import annotations

import json

import pytest

from bayes_basket_target import delta_v2_runtime
from bayes_basket_target.basket_kernel.contracts import (
    ContractError as KernelContractError,
    MaterializedEvidence,
    TargetBasketSnapshot,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.production import (
    assert_intents_above_observed_high,
)
from bayes_basket_target.delta_v2_runtime import _support_windows
from bayes_basket_target.observed_high_floor import (
    dead_buckets_for_running_high,
    dead_buckets_for_upper_bounds,
)
from bayes_basket_target.runtime import snapshot_from_probability_envelope

from tests.test_bayes_basket_target import SETTLEMENT_TIMEZONE, _envelope
from tests.test_charter_84_ladder_balance import _basket_policy, _held_snapshot


pytestmark = pytest.mark.skipif(
    delta_v2_runtime.CHARTER_125_OBSERVED_HIGH_FLOOR != "on",
    reason="charter 125 is switched off in this environment",
)



@pytest.fixture(autouse=True)
def _charter_81_arm_on(monkeypatch):
    """Charter 81 (the centre-window claim lift) was retired 2026-09-12 and
    the shipped default is identity.  The station-day fixtures in this file
    were written under the lift -- their raw claims clear the whole-basket
    positive-EV gate only when lifted -- and what they assert is charter
    84/121/125 mechanics, not the lift.  The retired table is switched on
    for the file so the fixtures keep exercising those mechanics; a fixture
    rebuilt on raw claims can drop this."""
    import bayes_basket_target.basket_kernel.delta_v2 as _k81
    monkeypatch.setattr(
        _k81, "CENTER_WINDOW_CLAIM_BANDS",
        _k81._parse_claim_bands(_k81.CENTER_WINDOW_CLAIM_RETIRED_TABLE),
    )

ZUUU_INTERVALS = json.dumps(
    {
        "23°C": [22.5, 23.5],
        "24°C": [23.5, 24.5],
        "25°C": [24.5, 25.5],
        "26°C": [25.5, 26.5],
        "27°C": [26.5, 27.5],
        "28°C": [27.5, 28.5],
    },
    ensure_ascii=False,
)
ZUUU_LADDER = ("23°C", "24°C", "25°C", "26°C", "27°C", "28°C")


# ---------------------------------------------------------------- predicate


def test_the_midnight_report_kills_every_rung_beneath_it() -> None:
    # ZUUU 2026-09-09: the 00:00 local METAR read 27C.  A daily maximum of
    # at least 27 cannot land in a bucket whose upper edge is 26.5.
    assert dead_buckets_for_running_high(
        ordered_buckets=ZUUU_LADDER,
        bucket_intervals_c_json=ZUUU_INTERVALS,
        running_high_c=27.0,
    ) == ("23°C", "24°C", "25°C", "26°C")


def test_the_bucket_holding_the_running_high_survives_it() -> None:
    # The boundary is the bucket's upper edge, not its centre: 27C is still
    # live at a running high of 27C, because the day can end there.
    assert "27°C" not in dead_buckets_for_running_high(
        ordered_buckets=ZUUU_LADDER,
        bucket_intervals_c_json=ZUUU_INTERVALS,
        running_high_c=27.0,
    )


def test_no_observation_floors_nothing() -> None:
    assert dead_buckets_for_running_high(
        ordered_buckets=ZUUU_LADDER,
        bucket_intervals_c_json=ZUUU_INTERVALS,
        running_high_c=None,
    ) == ()


def test_a_bound_that_would_empty_the_ladder_is_refused_not_applied() -> None:
    # A running high above the whole ladder is a bucket-map problem, not a
    # fact about the day.  Same refusal the P0-M bound already makes.
    assert dead_buckets_for_running_high(
        ordered_buckets=ZUUU_LADDER,
        bucket_intervals_c_json=ZUUU_INTERVALS,
        running_high_c=40.0,
    ) == ()


def test_an_open_top_bucket_never_dies() -> None:
    intervals = json.dumps({"29°C": [28.5, 29.5], "30°C or higher": [29.5, None]})
    assert dead_buckets_for_running_high(
        ordered_buckets=("29°C", "30°C or higher"),
        bucket_intervals_c_json=intervals,
        running_high_c=35.0,
    ) == ("29°C",)


def test_both_entries_are_the_same_predicate() -> None:
    uppers = {
        bucket: json.loads(ZUUU_INTERVALS)[bucket][1] for bucket in ZUUU_LADDER
    }
    assert dead_buckets_for_upper_bounds(
        ordered_buckets=ZUUU_LADDER,
        upper_bounds=uppers,
        running_high_c=26.0,
    ) == dead_buckets_for_running_high(
        ordered_buckets=ZUUU_LADDER,
        bucket_intervals_c_json=ZUUU_INTERVALS,
        running_high_c=26.0,
    )


# ----------------------------------------------------------------- evidence


def _evidence(**extra):
    return MaterializedEvidence.from_mapping({
        "probability_stage": "P0",
        "posterior_version": "p0-v1",
        "posterior_sha256": "a" * 64,
        **extra,
    })


def test_the_floor_is_carried_at_p0_where_the_p1_risk_lineage_may_not_be() -> None:
    evidence = _evidence(
        observed_running_high_c=27.0,
        observed_dead_buckets=["24°C", "25°C"],
    )
    assert evidence.observed_running_high_c == 27.0
    assert evidence.observed_dead_buckets == ("24°C", "25°C")
    # And the P1-only risk lineage is still P1-only: this pair does not
    # smuggle a chain root onto a forecast stage.
    with pytest.raises(KernelContractError):
        _evidence(
            reported_high_bucket="24°C",
            reported_high_ordinal=24,
            reported_high_observation_id="obs",
            hard_bound_id="bound",
        )


def test_a_dead_set_without_the_number_behind_it_is_refused() -> None:
    with pytest.raises(
        KernelContractError, match="observed_dead_buckets_without_running_high"
    ):
        _evidence(observed_dead_buckets=["24°C"])


def test_absent_by_default_so_a_pre_day_compile_is_unchanged() -> None:
    evidence = _evidence()
    assert evidence.observed_running_high_c is None
    assert evidence.observed_dead_buckets == ()


# ----------------------------------------------------------------- snapshot


def _market(**extra):
    return {
        "snapshot_id": "market-charter-125",
        "sequence": 1,
        "observed_at": "2026-07-23T00:00:00Z",
        "market_id": "market-zuuu",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "quote_version": "quote-125",
        "quote_observed_at": "2026-07-23T00:00:00Z",
        "inventory_version": "inventory-125",
        "order_state_version": "orders-125",
        "remaining_budget_usdc": 12.0,
        "buckets": [
            {
                "bucket": bucket,
                "bid_price": 0.10,
                "ask_levels": [{"price": 0.12, "shares": 100.0}],
                "maker_queue_ahead_shares": 1.0,
                "maker_queue_known": True,
            }
            for bucket in ("37", "38", "39")
        ],
        **extra,
    }


def test_a_p0_snapshot_carries_the_floor_the_worker_handed_it() -> None:
    # The envelope's own ladder is 37/38/39 with half-degree edges, so a
    # running high of 37.6 has already passed 37's upper edge of 37.5.
    snapshot = snapshot_from_probability_envelope(
        _envelope(), _market(observed_running_high_c=37.6)
    )

    assert snapshot.evidence.probability_stage == "P0"
    assert snapshot.evidence.observed_running_high_c == 37.6
    assert snapshot.evidence.observed_dead_buckets == ("37",)


def test_without_a_running_high_the_snapshot_is_what_it_always_was() -> None:
    snapshot = snapshot_from_probability_envelope(_envelope(), _market())

    assert snapshot.evidence.observed_running_high_c is None
    assert snapshot.evidence.observed_dead_buckets == ()


# ---------------------------------------------------------------- selection


def _floored_snapshot(dead: tuple[str, ...], running_high_c: float):
    """The charter-84 fixture with a running high the day has already made.

    Its ladder is 29..33 as bare ordinals and 31 is the centre.  A running
    high of 30 kills 29 and 30 -- the shape of a day that peaked early, which
    is ZUUU 2026-09-09 with the numbers moved onto a fixture that has a book.
    """

    base = _held_snapshot().to_dict()
    base["evidence"] = {
        **base["evidence"],
        "observed_running_high_c": running_high_c,
        "observed_dead_buckets": list(dead),
    }
    return TargetBasketSnapshot.from_mapping(base)


def _windows(snapshot, *, desired=("30", "31", "32")):
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    return _support_windows(
        snapshot=snapshot,
        desired=set(desired),
        probabilities=probabilities,
        fee_rate=float(_basket_policy().fee_rate),
        maximum_support_buckets=4,
        center_bucket="31",
        minimum_support_buckets=3,
        probability_stage=snapshot.evidence.probability_stage,
    )


def test_a_dead_rung_leaves_the_p0_domain_however_much_the_model_wants_it() -> None:
    windows, audit = _windows(_floored_snapshot(("29", "30"), 30.0))

    assert audit["observed_high_running_c"] == 30.0
    assert audit["observed_high_dead_buckets"] == ["29", "30"]
    # Charter 78 keeps the standing basket in the list whatever the price
    # says, so a held dead rung still appears there -- as the incumbent,
    # never as something the enumeration built.
    for window in windows:
        if window.get("is_incumbent"):
            continue
        assert not set(window["buckets"]) & {"29", "30"}


def test_the_control_arm_is_the_same_book_with_the_thermometer_removed() -> None:
    # Without this the test above would pass on a book that never wanted
    # those rungs.  Same quotes, same beliefs, same policy: only the day's
    # observed high differs, and it takes half the domain away.
    _open_windows, open_audit = _windows(_held_snapshot())
    _floored, floored_audit = _windows(_floored_snapshot(("29", "30"), 30.0))

    assert open_audit["observed_high_running_c"] is None
    assert open_audit["observed_high_dead_buckets"] == []
    assert open_audit["enumerated_window_count"] == 12
    assert floored_audit["enumerated_window_count"] == 6


def test_the_seat_moves_off_a_window_the_observations_have_killed() -> None:
    # The consequence that costs money, not just an audit key.  On the open
    # book charter 131 seats the symmetric {30,31,32}; once the day has
    # reported 31 every P0 shape holds a settled loss -- {31,32,33} is the
    # only three-rung shape around the surviving centre and 31 is dead --
    # so P0 seats nothing.  A P1 day seats the two-rung {32,33} instead
    # (charter 131 (10), pinned in the doctrine suite).
    open_windows, _ = _windows(_held_snapshot())
    floored_windows, _ = _windows(_floored_snapshot(("29", "30", "31"), 31.0))

    def seats(windows):
        # The ruled shape is also the held basket here, so the incumbent
        # row IS the seat; what matters is that it is admissible.
        return [
            tuple(window["buckets"])
            for window in windows
            if window.get("admissible")
        ]

    assert seats(open_windows) == [("30", "31", "32")]
    assert seats(floored_windows) == []


def test_a_standing_basket_holding_a_dead_rung_is_not_seatable() -> None:
    # It may be held -- nothing here sells -- and it may not be added to.
    windows, _audit = _windows(_floored_snapshot(("29", "30"), 30.0))
    incumbent = [
        window for window in windows if window.get("is_incumbent")
    ]

    assert [tuple(window["buckets"]) for window in incumbent] == [
        ("30", "31", "32")
    ]
    assert incumbent[0]["admissible"] is False


def test_completing_a_basket_never_completes_into_a_dead_rung() -> None:
    # Charter 84 buys the missing rungs of a partly held window.  A rung the
    # observations killed is missing for a reason no quote can undo, so it
    # joins the market-dead set rather than the buyable one.  32 is the
    # unheld rung of the fixture's standing basket.
    windows, _audit = _windows(_floored_snapshot(("29", "30", "31", "32"), 32.0))
    completions = [
        window["completion"] for window in windows
        if window.get("completion") is not None
    ]

    assert completions
    for completion in completions:
        assert "32" not in completion["buyable_buckets"]
        if "32" in completion["missing_buckets"]:
            assert "32" in completion["market_dead_buckets"]


# ---------------------------------------------------------- commit boundary


def _p0_envelope_mapping():
    envelope = _envelope().to_dict()
    assert envelope["stage"] == "P0"
    return envelope


def _buy(bucket: str):
    return {"bucket": bucket, "order_action": "BUY", "limit_price": 0.12}


def test_a_buy_on_a_dead_rung_is_refused_at_the_writer_boundary() -> None:
    """The fail-closed half: the gate routes around, the boundary refuses.

    The shape it is written for is a plan compiled without the floor -- a
    stale book, a compile that ran before the report, a caller that did not
    hand the running high down -- reaching the writer after the day has
    already settled the rung.  The envelope's ladder closes 37 at 37.5, so a
    running high of 37.6 kills 37 and nothing else.
    """

    with pytest.raises(
        ContractError,
        match="production_candidate_intent_below_observed_high:37",
    ):
        assert_intents_above_observed_high(
            [_buy("37"), _buy("38")],
            market_snapshot={"observed_running_high_c": 37.6},
            probability_envelope=_p0_envelope_mapping(),
        )


def test_the_live_rungs_of_the_same_plan_pass_and_the_dead_set_is_reported() -> None:
    assert assert_intents_above_observed_high(
        [_buy("38"), _buy("39")],
        market_snapshot={"observed_running_high_c": 37.6},
        probability_envelope=_p0_envelope_mapping(),
    ) == ("37",)


def test_without_a_running_high_the_boundary_checks_nothing() -> None:
    # The pre-day commit production has always made.
    assert assert_intents_above_observed_high(
        [_buy("37"), _buy("38")],
        market_snapshot={},
        probability_envelope=_p0_envelope_mapping(),
    ) == ()


def test_a_sell_is_not_a_buy_and_is_never_refused_here() -> None:
    # Nothing in this ruling forces or forbids exiting a dead leg.  It is
    # about what may be bought, and a SELL on the killed rung must stay
    # reachable -- refusing it here would trap the position the floor is
    # complaining about.
    assert assert_intents_above_observed_high(
        [{"bucket": "37", "order_action": "SELL"}],
        market_snapshot={"observed_running_high_c": 37.6},
        probability_envelope=_p0_envelope_mapping(),
    ) == ("37",)


def test_a_malformed_running_high_fails_closed_rather_than_reading_as_absent() -> None:
    with pytest.raises(ContractError, match="observed_running_high_c_invalid"):
        assert_intents_above_observed_high(
            [_buy("37")],
            market_snapshot={"observed_running_high_c": "not-a-number"},
            probability_envelope=_p0_envelope_mapping(),
        )


# ------------------------------------------------------------ hash stability


def test_a_pre_day_snapshot_hashes_exactly_as_it_did_before_the_ruling() -> None:
    """The identity leg must not churn on a day that reported nothing.

    ``snapshot.content_sha256`` is a leg of the plan identity, and
    ``asdict`` emits every dataclass field -- so a null pair would move
    every pre-day hash in the fleet for a fact that does not exist yet.
    """

    snapshot = snapshot_from_probability_envelope(_envelope(), _market())
    payload = snapshot.to_dict()

    assert "observed_running_high_c" not in payload["evidence"]
    assert "observed_dead_buckets" not in payload["evidence"]


def test_a_snapshot_that_has_a_floor_says_so_in_its_identity() -> None:
    # And when the day HAS reported, the identity moves -- it must, because
    # the decision now stands on a fact the earlier one did not have.
    floored = snapshot_from_probability_envelope(
        _envelope(), _market(observed_running_high_c=37.6)
    )
    payload = floored.to_dict()

    assert payload["evidence"]["observed_running_high_c"] == 37.6
    assert payload["evidence"]["observed_dead_buckets"] == ("37",)
    assert (
        floored.content_sha256
        != snapshot_from_probability_envelope(
            _envelope(), _market()
        ).content_sha256
    )
