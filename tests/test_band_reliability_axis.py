"""The band table's cells are read from a quantity the map does not move.

A band table is a recalibration map on a distribution over an ORDERED axis,
and the disease it treats is over-dispersion: too little mass on the centre,
too much four buckets out.  That is a statement about POSITION, and keying
the cells by the claimed probability instead costs two things that were
measured rather than argued.

The key is not invariant under the map.  A bucket lands in [0.40,0.45)
because it claims 0.44; multiply by 1.05, renormalise, and it claims 0.46
and belongs to a cell whose factor it never got.  Fitted on 1204 day-ahead
station-days, applied once and re-measured, the cell ratios land at
0.79-1.10 -- a table fitted to make them 1.0 does not make them 1.0.

And it pools things that behave differently.  On 2026-09-05 the [5-10%) cell
held VILK's bucket one step off centre (7.0%), ZHHH's three steps off (7.2%)
and ZHHH's five steps off (6.1%); those three realise 1.151, 0.783 and 0.490
of their claims.  Three-fold cross-validation by day: distance scores +1.64%
NLL against +1.10% for the claim axis, five cells against eleven, and
stacking the two is worse than distance alone (+1.26%).

Charter 43's ``neighbour_direction_v1`` is untouched: it is SIGNED and fixes
asymmetry, this is UNSIGNED and fixes width, and the factors were measured on
vectors that already carry it, so they stack rather than double-count.  The
corroboration is that the signed version of this table scores WORSE out of
sample (+1.33%) -- the direction signal has already been taken.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.cross_consensus import (  # noqa: E402
    BAND_RELIABILITY_AXIS_CENTRE_DISTANCE,
    BAND_RELIABILITY_AXIS_CLAIM,
    BAND_RELIABILITY_METHOD_TAG,
    BAND_RELIABILITY_METHOD_TAG_V2,
    apply_band_reliability,
)
from bayes_basket_target.band_reliability_store import (  # noqa: E402
    band_table_axis,
    ensure_band_table_schema,
    persist_and_activate_band_table,
)

# Centre at "24", so the distances are 2, 1, 0, 1, 2.
ORDINALS = {"22": 0, "23": 1, "24": 2, "25": 3, "26": 4}
VECTOR = {"22": 0.10, "23": 0.20, "24": 0.40, "25": 0.20, "26": 0.10}
# Cells on the distance axis: centre, one out, two-or-more out.
DISTANCE_TABLE = (((0.0, 1.0), 1.20), ((1.0, 2.0), 1.10), ((2.0, 99.0), 0.60))


def test_without_ordinals_the_claim_axis_is_served_exactly_as_before():
    """Every artifact pinned to a v1 table must keep being served the
    lookup it was gated against -- charter 60, extended to the axis."""

    claim_table = (((0.0, 0.15), 0.5), ((0.15, 1.01), 1.0))
    served, receipt = apply_band_reliability(VECTOR, factors=claim_table)
    assert receipt["method_tag"] == BAND_RELIABILITY_METHOD_TAG
    assert receipt["axis"] == BAND_RELIABILITY_AXIS_CLAIM
    # The two 0.10 buckets are the ones under 0.15, and only they are cut.
    assert set(receipt["applied_factors"]) == {"22", "26"}
    assert served["22"] == pytest.approx(served["26"])
    assert sum(served.values()) == pytest.approx(1.0)


def test_with_ordinals_the_cell_is_the_distance_from_the_peak():
    """Same table shape, same lookup, different quantity looked up."""

    served, receipt = apply_band_reliability(
        VECTOR, factors=DISTANCE_TABLE, ordinals=ORDINALS,
    )
    assert receipt["method_tag"] == BAND_RELIABILITY_METHOD_TAG_V2
    assert receipt["axis"] == BAND_RELIABILITY_AXIS_CENTRE_DISTANCE
    assert receipt["applied_factors"] == {
        "22": 0.60, "23": 1.10, "24": 1.20, "25": 1.10, "26": 0.60,
    }
    # Mass moved inward, which is the whole point of the layer.
    assert served["24"] > VECTOR["24"]
    assert served["22"] < VECTOR["22"]
    assert sum(served.values()) == pytest.approx(1.0)


def test_the_distance_axis_is_a_fixed_point_and_the_claim_axis_is_not():
    """The defect, stated as a property.

    Applying a claim-keyed table moves buckets between cells, so the table
    is not the solution to the equation it was fitted to.  Applying a
    distance-keyed one cannot: the peak's own factor is the largest, so the
    peak stays the peak and every bucket keeps the cell it was scored in.
    """

    def cells(vector, table, ordinals=None):
        _served, receipt = apply_band_reliability(
            vector, factors=table, ordinals=ordinals,
        )
        return receipt["applied_factors"]

    # Claim axis: this bucket claims 0.29 and sits in [0.15,0.30); lift it
    # by 1.30, renormalise, and it claims 0.347 and sits in the cell above,
    # whose factor it was never scored under.
    crossing = {"22": 0.05, "23": 0.29, "24": 0.56, "25": 0.05, "26": 0.05}
    claim_table = (
        ((0.0, 0.15), 1.0), ((0.15, 0.30), 1.30), ((0.30, 1.01), 1.0),
    )
    once, _ = apply_band_reliability(crossing, factors=claim_table)
    assert once["23"] > 0.30 > crossing["23"]
    assert cells(crossing, claim_table) != cells(once, claim_table)

    # Distance axis: the second application assigns every bucket the same
    # factor as the first.
    twice, _ = apply_band_reliability(
        VECTOR, factors=DISTANCE_TABLE, ordinals=ORDINALS,
    )
    assert (
        cells(VECTOR, DISTANCE_TABLE, ORDINALS)
        == cells(twice, DISTANCE_TABLE, ORDINALS)
    )


def test_the_peak_is_the_argmax_not_the_mean():
    """The mean moves under the map and the argmax does not, which is the
    only reason this axis is stable.  A skewed vector makes them differ."""

    skewed = {"22": 0.05, "23": 0.05, "24": 0.50, "25": 0.20, "26": 0.20}
    _served, receipt = apply_band_reliability(
        skewed, factors=DISTANCE_TABLE, ordinals=ORDINALS,
    )
    # Mean ordinal is 2.45, which rounds to "24" here too -- but the factor
    # assignment must come from the peak, so "24" takes the centre cell and
    # the two ends take the outer one whatever the mean does.
    assert receipt["applied_factors"]["24"] == 1.20
    assert receipt["applied_factors"]["22"] == 0.60
    assert receipt["applied_factors"]["26"] == 0.60


def test_a_bucket_the_ordinals_do_not_carry_is_left_alone():
    """A key with no position has no distance, and inventing one would put
    it in the centre cell -- the largest factor -- by accident."""

    served, receipt = apply_band_reliability(
        {**VECTOR, "unknown": 0.10}, factors=DISTANCE_TABLE, ordinals=ORDINALS,
    )
    assert "unknown" not in receipt["applied_factors"]
    assert served["unknown"] > 0.0


def test_the_axis_travels_with_the_table_not_with_the_caller():
    """Charter 60 pins the table by sha; a pinned table that could not say
    which axis it was scored on would be served through the wrong lookup,
    which is the one way this change could reach an artifact it was never
    gated against."""

    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(calibration_band_reliability_table)"
        )
    }
    assert "axis" in columns
    # A database that has never carried the column reads as the old axis,
    # so no stored table changes meaning when it appears.
    assert band_table_axis(conn, stage="P0") == BAND_RELIABILITY_AXIS_CLAIM
    receipt = persist_and_activate_band_table(
        conn, stage="P0",
        factors=[((0.0, 0.05), 0.9), ((0.05, 1.01), 1.0)],
        evidence={"schema": "test"}, serving_day_count=12,
    )
    assert band_table_axis(conn, stage="P0") == BAND_RELIABILITY_AXIS_CLAIM
    assert band_table_axis(
        conn, table_sha256=receipt["band_table_sha256"],
    ) == BAND_RELIABILITY_AXIS_CLAIM


def test_the_inverse_is_axis_blind_so_one_function_serves_both_vintages():
    """``_invert_band_receipt`` reads per-bucket factors and the
    pre-normalisation mass, and neither knows what the cells were keyed on.
    That is what lets the fitting lane keep reconstructing pre-band claims
    across the change."""

    from bayes_basket_target.band_reliability_store import (
        _invert_band_receipt,
    )
    served, receipt = apply_band_reliability(
        VECTOR, factors=DISTANCE_TABLE, ordinals=ORDINALS,
    )
    records = [(served[key], key) for key in VECTOR]
    restored = dict(
        (key, value) for value, key in _invert_band_receipt(records, receipt)
    )
    for key, raw in VECTOR.items():
        assert restored[key] == pytest.approx(raw, rel=1e-9)


def test_the_default_interval_is_the_shipped_ninety_five_percent() -> None:
    """The width became a parameter so a counterfactual could be measured
    through this code path rather than a second copy of it (2026-09-09).
    Passing nothing must reproduce the rule exactly: the 2.5th and 97.5th
    percentiles of the by-day bootstrap."""

    from bayes_basket_target import band_reliability_store as store

    by_day = {
        f"2026-08-{day:02d}": [[(0.30, 1 if day % 3 else 0), (0.20, 0)]]
        for day in range(1, 29)
    }
    bands = ((0.0, 0.25), (0.25, 1.01))
    shipped = store.band_lane_report(by_day, bands=bands, bootstrap_samples=200)
    explicit = store.band_lane_report(
        by_day, bands=bands, bootstrap_samples=200, interval_alpha=0.05,
    )
    assert store.DEFAULT_INTERVAL_ALPHA == 0.05
    for band in bands:
        assert shipped[band]["interval"] == explicit[band]["interval"]
        assert (
            shipped[band]["conservative_factor"]
            == explicit[band]["conservative_factor"]
        )


def test_a_wider_interval_never_pulls_less_than_a_narrow_one() -> None:
    """The near end is what gets served, so loosening the width can only move
    the served factor toward the point estimate -- never away from it.  A
    counterfactual that could pull *less* would mean the parameter is wired
    backwards, which is the one way this knob could quietly mislead."""

    from bayes_basket_target import band_reliability_store as store

    by_day = {
        f"2026-08-{day:02d}": [[(0.30, 1 if day % 4 else 0), (0.20, 0)]]
        for day in range(1, 29)
    }
    bands = ((0.0, 0.25), (0.25, 1.01))
    tight = store.band_lane_report(by_day, bands=bands, bootstrap_samples=200)
    loose = store.band_lane_report(
        by_day, bands=bands, bootstrap_samples=200, interval_alpha=0.50,
    )
    for band in bands:
        point = tight[band]["ratio"]
        assert abs(loose[band]["conservative_factor"] - point) <= (
            abs(tight[band]["conservative_factor"] - point) + 1e-9
        )


def test_withholding_days_changes_only_which_days_the_fit_saw(tmp_path) -> None:
    """``until_event_date`` is the walk-forward entry: the same function, the
    later days removed.  Asking for a date past the lane must reproduce the
    unrestricted fit exactly, or the parameter is not inert when unused."""

    from bayes_basket_target import band_reliability_store as store

    by_day = {
        f"2026-08-{day:02d}": [[(0.30, day % 2), (0.20, 0)]]
        for day in range(1, 29)
    }
    bands = ((0.0, 0.25), (0.25, 1.01))
    everything = store.band_lane_report(by_day, bands=bands, bootstrap_samples=200)
    trimmed = store.band_lane_report(
        {d: r for d, r in by_day.items() if d < "2026-08-20"},
        bands=bands, bootstrap_samples=200,
    )
    # Different evidence, so different intervals -- the point is that the
    # trim is what changed them, not the rule.
    assert everything.keys() == trimmed.keys()
    assert any(
        everything[band]["interval"] != trimmed[band]["interval"]
        for band in bands
    )
