"""The width floor: a served claim narrower than the floor is disbelieved.

Measured 2026-09-05 on P0's own serving cells (9 days, 3,589 cells, anchored
at the served-arithmetic boundary): where the served distribution calls itself
narrower than 1.44 rungs the standardised error's standard deviation is 0.95,
and where it calls itself wider it is 0.67.  One pooled lambda cannot serve
both -- 0.75 shrinks a group that is already too narrow and under-shrinks the
rest -- so the claim is floored and the pooled pair refitted underneath it.

Two arms were measured on the same forward window.  Flooring then refitting
one lambda gained +0.0132 nats against the incumbent; fitting a *second*
lambda per tier gained only +0.0101, because the heterogeneity lives inside
the narrow tier rather than between the tiers.  These tests pin the arm that
won, its entitlement bounds, and the cross-unit reading charter 53(b) needs.
"""

from __future__ import annotations

import math

import pytest

from bayes_basket_target.center_width_calibration import (
    MAX_SIGMA_FLOOR_LIFT,
    MAX_SIGMA_FLOOR_RUNGS,
    apply_center_width_calibration,
    bucket_rung_width_c,
    sigma_floor_lift_v1,
)
from bayes_basket_target.center_width_store import (
    SIGMA_FLOOR_RUNGS,
    _mean_nll,
    _search,
)

# A Celsius market: open-ended edges placed half of EDGE_WIDTH_C beyond their
# finite side, so the first and last gaps are 1.5 and the rung is 1.0.
KEYS_C = ["23C or below", "24C", "25C", "26C", "27C", "28C", "29C or higher"]
CENTERS_C = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
# The same shape on a Fahrenheit market, whose rung is 2F = 10/9 C.
RUNG_F = 10.0 / 9.0
KEYS_F = [f"f{index}" for index in range(7)]
CENTERS_F = [10.0 + RUNG_F * index for index in range(7)]


def _sigma(dist: dict[str, float], centers: list[float]) -> float:
    total = sum(dist.values())
    mean = sum(v * c for v, c in zip(dist.values(), centers)) / total
    return math.sqrt(
        sum(v / total * (c - mean) ** 2 for v, c in zip(dist.values(), centers))
    )


def _dist(keys: list[str], weights: list[float]) -> dict[str, float]:
    return dict(zip(keys, weights))


# sigma 0.69 rungs: short of the floor by more than the lift may claim.
NARROW = [0.0, 0.02, 0.16, 0.64, 0.16, 0.02, 0.0]
# sigma 1.04 rungs: short of the floor, and reachable inside the bound.
MILD = [0.0, 0.08, 0.22, 0.40, 0.22, 0.08, 0.0]
# sigma 1.97 rungs: already past the floor.
WIDE = [0.10, 0.14, 0.16, 0.20, 0.16, 0.14, 0.10]


def test_floor_is_identity_when_it_is_not_asked_for() -> None:
    """The whole history of this layer is floor zero, byte for byte."""

    probability = _dist(KEYS_C, NARROW)
    lone, _ = apply_center_width_calibration(
        probability, delta_c=0.1, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
    )
    zeroed, receipt = apply_center_width_calibration(
        probability, delta_c=0.1, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=0.0,
    )
    assert zeroed == lone
    assert receipt["sigma_floor_lift"] == 1.0
    assert receipt["sigma_floor_rungs"] == 0.0


def test_a_claim_at_or_above_the_floor_is_left_alone() -> None:
    """The wide tier is not a second case; it is where the lift is one."""

    probability = _dist(KEYS_C, WIDE)
    assert _sigma(probability, CENTERS_C) > SIGMA_FLOOR_RUNGS
    lift, sigma_c, rung_c = sigma_floor_lift_v1(
        probability, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert lift == 1.0
    assert rung_c == 1.0
    assert sigma_c == pytest.approx(_sigma(probability, CENTERS_C))
    floored, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    plain, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.75,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
    )
    assert floored == plain


def test_a_narrow_claim_is_lifted_exactly_to_the_floor() -> None:
    """The lift is what it takes to reach the floor, and no more."""

    probability = _dist(KEYS_C, MILD)
    sigma_c = _sigma(probability, CENTERS_C)
    assert sigma_c < SIGMA_FLOOR_RUNGS
    lift, reported, rung_c = sigma_floor_lift_v1(
        probability, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert reported == pytest.approx(sigma_c)
    assert lift == pytest.approx(SIGMA_FLOOR_RUNGS * rung_c / sigma_c)
    # Serving at lambda 1 under the floor lands the spread on the floor,
    # up to the histogram's own re-splitting, which only ever widens.
    floored, receipt = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=1.0,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert _sigma(floored, CENTERS_C) >= SIGMA_FLOOR_RUNGS * rung_c - 1e-9
    assert receipt["width_lambda"] == pytest.approx(lift)
    assert receipt["distribution_sigma_c"] == pytest.approx(sigma_c)
    assert receipt["bucket_rung_c"] == rung_c


def test_the_floor_is_read_in_rungs_not_degrees() -> None:
    """Charter 53(b): a rule counted in rungs is synonymous across units.

    The same shape on a Fahrenheit ladder must be lifted by the same factor,
    not by the ratio of the two units -- the defect this repository has now
    written down three times.
    """

    celsius = _dist(KEYS_C, NARROW)
    fahrenheit = _dist(KEYS_F, NARROW)
    lift_c, _, rung_c = sigma_floor_lift_v1(
        celsius, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    lift_f, _, rung_f = sigma_floor_lift_v1(
        fahrenheit, ordered_keys=KEYS_F, key_centers_c=CENTERS_F,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert rung_c == 1.0
    assert rung_f == pytest.approx(RUNG_F)
    assert lift_f == pytest.approx(lift_c)


def test_the_rung_is_the_smallest_gap_so_open_edges_cannot_set_it() -> None:
    assert bucket_rung_width_c(CENTERS_C) == 1.0
    # A five-bucket ladder has four gaps, two of them edges; the median of an
    # even count lands on an edge gap and reads the rung half again too wide.
    assert bucket_rung_width_c([0.0, 1.5, 2.5, 3.5, 5.0]) == 1.0


def test_the_lift_carries_its_own_entitlement_bound() -> None:
    """Doubling is the mirror of the halving the shrink may already claim."""

    point_mass = {key: 0.0 for key in KEYS_C}
    point_mass[KEYS_C[3]] = 1.0
    lift, sigma_c, _ = sigma_floor_lift_v1(
        point_mass, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert sigma_c == 0.0
    assert lift == MAX_SIGMA_FLOOR_LIFT
    # A floor wider than this is describing something structural, not a
    # correction this layer is entitled to make.
    with pytest.raises(ValueError):
        sigma_floor_lift_v1(
            _dist(KEYS_C, NARROW), ordered_keys=KEYS_C,
            key_centers_c=CENTERS_C,
            floor_rungs=MAX_SIGMA_FLOOR_RUNGS + 0.1,
        )
    with pytest.raises(ValueError):
        apply_center_width_calibration(
            _dist(KEYS_C, NARROW), delta_c=0.0, width_lambda=1.0,
            ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
            sigma_floor_rungs=-1.0,
        )


def test_the_lift_composes_onto_the_lambda_like_the_station_scale() -> None:
    """Two scalings about the same mean multiply; each keeps its own bounds.

    The product may leave the lambda's own range, which is exactly why the
    floor is a separate argument rather than something a caller folds in.
    """

    probability = _dist(KEYS_C, MILD)
    lift, _, _ = sigma_floor_lift_v1(
        probability, ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    floored, receipt = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.725,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert receipt["width_lambda"] == pytest.approx(0.725 * lift)
    equivalent, _ = apply_center_width_calibration(
        probability, delta_c=0.0, width_lambda=0.725 * lift,
        ordered_keys=KEYS_C, key_centers_c=CENTERS_C,
    )
    assert floored == equivalent


def test_the_search_scores_the_floor_it_would_serve() -> None:
    """The grid searched under a floor must score under that same floor.

    A search that floored the candidate but scored the unfloored claim would
    be the serve/train split the spread cap taught this codebase once.
    """

    cells = [
        {
            "probabilities": _dist(KEYS_C, NARROW),
            "ordered_keys": KEYS_C,
            "key_centers_c": CENTERS_C,
            "excluded_buckets": (),
            "settled_bucket": KEYS_C[5],
        },
        {
            "probabilities": _dist(KEYS_C, WIDE),
            "ordered_keys": KEYS_C,
            "key_centers_c": CENTERS_C,
            "excluded_buckets": (),
            "settled_bucket": KEYS_C[2],
        },
    ]
    delta, lam, score = _search(cells, sigma_floor_rungs=SIGMA_FLOOR_RUNGS)
    assert score == pytest.approx(
        _mean_nll(
            cells, delta_c=delta, width_lambda=lam,
            sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
        )
    )
    # And the floored score beats the unfloored one on a cell whose truth
    # settled two rungs out from a claim that called itself certain.
    unfloored = _mean_nll(cells, delta_c=delta, width_lambda=lam)
    assert score < unfloored


def _cell(weights: list[float], settled: str) -> dict[str, object]:
    return {
        "probabilities": _dist(KEYS_C, weights),
        "ordered_keys": KEYS_C,
        "key_centers_c": CENTERS_C,
        "excluded_buckets": (),
        "settled_bucket": settled,
    }


def test_the_floor_must_beat_the_pooled_candidate_not_just_the_incumbent(
) -> None:
    """The floored fit carries a refitted lambda with it.

    A lane admitted for beating the incumbent alone could be winning on that
    refit rather than on the floor, so the hurdle is against the pooled
    candidate -- what would serve that day otherwise.
    """

    from bayes_basket_target.center_width_store import _sigma_floor_lane

    forward = [_cell(NARROW, KEYS_C[1]), _cell(WIDE, KEYS_C[4])]
    lane = _sigma_floor_lane(
        forward=forward,
        pooled=(0.0, 0.75), floored=(0.0, 0.725),
        pooled_nll=1.60, floored_nll=1.50, incumbent_nll=1.61,
        incumbent_floor_rungs=0.0,
    )
    # Measured on its own evidence, and served only once the paper ledger
    # promotes it -- the default mode is shadow.
    assert lane["measured_admitted"] is True
    assert lane["state"] in {"ADMITTED", "SHADOW_ADMITTED"}
    assert lane["admitted"] is (lane["mode"] == "serve")
    assert lane["sigma_floor_rungs"] == SIGMA_FLOOR_RUNGS
    assert lane["forward_lifted_count"] == 1
    assert lane["forward_cell_count"] == 2
    # Beating the incumbent while losing to the pooled candidate is a refusal.
    refused = _sigma_floor_lane(
        forward=forward,
        pooled=(0.0, 0.75), floored=(0.0, 0.725),
        pooled_nll=1.50, floored_nll=1.55, incumbent_nll=1.61,
        incumbent_floor_rungs=0.0,
    )
    assert refused["admitted"] is False
    assert refused["measured_admitted"] is False
    assert refused["state"] == "REFUSED_FORWARD"
    assert refused["forward_nll_gain_vs_incumbent"] > 0.0


def test_the_floor_is_shadow_until_the_ledger_promotes_it() -> None:
    """Default mode serves nothing, whatever the forward gate measured.

    The lane's hurdle is a probability argument.  This repository has been
    taught twice that a probability argument is not a book argument -- the
    fleet-mean layer improved the claim by 24% and cost the book 7.3 points --
    so promotion waits for the replay.
    """

    from bayes_basket_target.center_width_store import (
        SIGMA_FLOOR_MODE,
        _sigma_floor_lane,
    )

    assert SIGMA_FLOOR_MODE == "shadow"
    lane = _sigma_floor_lane(
        forward=[_cell(NARROW, KEYS_C[1])],
        pooled=(0.0, 0.75), floored=(0.0, 0.725),
        pooled_nll=1.60, floored_nll=1.50, incumbent_nll=1.61,
        incumbent_floor_rungs=0.0,
    )
    assert lane["measured_admitted"] is True
    assert lane["admitted"] is False
    assert lane["state"] == "SHADOW_ADMITTED"


def test_the_digest_carries_the_floor_and_legacy_digests_do_not_move() -> None:
    """The floor is part of what the scalars mean, so it is part of the sha.

    0.725 under a floor and 0.725 without one are different arithmetic, and an
    artifact pinning the digest must be served the one its holdout scored.
    Rows written before the floor existed keep their digest exactly, or the
    pinned-version contract stops resolving them.
    """

    from bayes_basket_target.center_width_store import center_width_sha256

    legacy = center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.725)
    unchanged = center_width_sha256(
        stage="P0", delta_c=0.0, width_lambda=0.725, sigma_floor_rungs=0.0,
    )
    floored = center_width_sha256(
        stage="P0", delta_c=0.0, width_lambda=0.725,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert legacy == unchanged
    assert floored != legacy


def test_the_floor_reader_is_zero_for_a_row_that_predates_it() -> None:
    """Absence reads as no floor, which is an identity in this factor."""

    import sqlite3

    from bayes_basket_target.center_width_store import (
        center_width_sha256,
        ensure_center_width_schema,
        load_center_width_floor,
        persist_and_activate_center_width,
    )

    conn = sqlite3.connect(":memory:")
    ensure_center_width_schema(conn)
    # Nothing fitted at all.
    assert load_center_width_floor(conn, stage="P0") == 0.0
    persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75,
        evidence={}, serving_day_count=9,
    )
    assert load_center_width_floor(conn, stage="P0") == 0.0
    persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.725,
        evidence={}, serving_day_count=9,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert load_center_width_floor(conn, stage="P0") == SIGMA_FLOOR_RUNGS
    # And by pin, which is how serving actually asks.
    digest = center_width_sha256(
        stage="P0", delta_c=0.0, width_lambda=0.725,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    assert load_center_width_floor(
        conn, stage="P0", table_sha256=digest
    ) == SIGMA_FLOOR_RUNGS


def test_serving_and_the_ledger_both_read_the_floor() -> None:
    """Production and the paper book must price the same distribution.

    Measured once before: changing one side alone leaves the ledger scoring a
    strategy production does not run.
    """

    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]

    def source(relative: str) -> str:
        return (root / relative).read_text(encoding="utf-8")

    # P0 serves it from the pinned row.
    p0 = source("bayes_basket_target/native_ensemble_p0.py")
    assert "load_center_width_floor(" in p0
    assert "sigma_floor_rungs=sigma_floor_rungs," in p0
    assert "load_center_width_floor" in p0
    # The holdout that gates the artifact scores it under the same floor.
    oos = source("bayes_basket_target/native_ensemble_oos.py")
    assert "sigma_floor_rungs=center_width_floor," in oos
    # Charter 150: P0-M and P1 carry no centre/width layer at all.
    materialization = source("bayes_basket_target/materialization.py")
    assert "center_width_floor_rungs" not in materialization
    # And the ledger applies the floor its replay table recorded.
    ledger = source("scripts/paper_trading_ledger.py")
    assert 'entry.get("sigma_floor_rungs")' in ledger
    builder = source("scripts/fit_center_width_replay_table.py")
    assert "SIGMA_FLOOR_RUNGS if admitted else 0.0," in builder
    assert "sigma_floor_rungs=SIGMA_FLOOR_RUNGS," in builder
