"""Charter 190: the width layer carries an open edge bucket as a tail.

Fixture: ZUUU 2026-09-22 P0, checkpoint
``p0-native-eb7862c9427608c53c8e7a1d0fce0581`` (decision
2026-09-21T10:41:31.382Z).  ``INPUT`` is the vector its centre/width receipt
recorded as the layer's input (``center_width_json.input_probability``); the
pinned table served lambda 0.75, delta 0, and the band table was identity on
this row.  Served under the point rule it read ``22°C or below`` 1.1%, 23°C
13.0%, 24°C 11.3%: a second peak at 23 that no member made.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import center_width_calibration as C  # noqa: E402
from bayes_basket_target import center_width_store as S  # noqa: E402
from bayes_basket_target import native_ensemble_p0 as P0  # noqa: E402
from bayes_basket_target import serving_arithmetic as SA  # noqa: E402
from bayes_basket_target.served_shape import (  # noqa: E402
    CenterWidthLayer,
    apply_served_table_layers,
)

INPUT = {
    "22°C or below": 0.11552778,
    "23°C": 0.073697102,
    "24°C": 0.109374733,
    "25°C": 0.155926826,
    "26°C": 0.197254894,
    "27°C": 0.153066964,
    "28°C": 0.107003632,
    "29°C": 0.048628947,
    "30°C": 0.024246595,
    "31°C": 0.010779456,
    "32°C or higher": 0.004493071,
}
KEYS = list(INPUT)
INTERVALS = {"22°C or below": [None, 22.5]}
INTERVALS.update({f"{t}°C": [t - 0.5, t + 0.5] for t in range(23, 32)})
INTERVALS["32°C or higher"] = [31.5, None]

# What the point-rule kernel returned for these inputs, captured from the
# tree just before charter 190 landed (the kernel as charter 142 left it).
# A closed axis, or an open end holding no mass, must still return exactly
# these floats.
ZUUU_POINT_RULE = [
    0.010601856221410041, 0.13011330058061957, 0.11323428962145782,
    0.19400638275408968, 0.25613903231917307, 0.164074858118111,
    0.08684162928728381, 0.03340020681786339, 0.009754232921722561,
    0.0018342113582690066, 0.0,
]
CLOSED_POINT_RULE = [
    0.0, 0.01931742685502844, 0.124173587562894, 0.19401839578232813,
    0.30767051074486246, 0.19660459917823603, 0.10352804950150425,
    0.04473035902746758, 0.009957071347679168, 0.0, 0.0,
]
CLOSED_SHIFTED_POINT_RULE = [
    0.0, 0.007079072365390883, 0.11203423597199103, 0.17478306357995588,
    0.2447812461425332, 0.23485236773991267, 0.14712867692721832,
    0.05288854284336066, 0.022588360471343365, 0.0038644339582939976, 0.0,
]
GRID_KEYS = [str(t) for t in range(18, 31)]
GRID_INPUT = dict(zip(GRID_KEYS, [
    0.02, 0.03, 0.05, 0.08, 0.12, 0.16, 0.17, 0.14, 0.10, 0.07, 0.04, 0.015,
    0.005,
]))
GRID_POINT_RULE = [
    0.0, 0.016187499999999987, 0.0475, 0.07821249999999999,
    0.13474999999999993, 0.22988750000000005, 0.19678750000000003,
    0.1457125, 0.10191250000000004, 0.03690625000000001,
    0.011012500000000003, 0.0011312500000000014, 0.0,
]


def _served(probability, *, bounds, delta=0.0, lam=0.75):
    centers, _ = C.served_bucket_axis(INTERVALS, KEYS)
    out, receipt = apply_served_table_layers(
        probability,
        ordered_keys=KEYS,
        key_centers_c=centers,
        center_width=CenterWidthLayer(delta_c=delta, width_lambda=lam),
        open_edge_bounds_c=bounds,
    )
    return [out[key] for key in KEYS], receipt


def test_the_axis_is_the_one_serving_always_used() -> None:
    centers, bounds = C.served_bucket_axis(INTERVALS, KEYS)
    assert centers == [22.0 + step for step in range(11)]
    assert bounds == (22.5, 31.5)
    assert P0._bucket_centers(INTERVALS, KEYS) == centers
    # A Fahrenheit ladder: 2F rungs, open ends half a degree C out.
    f_intervals = {
        "79F or below": [None, 26.388888888888889],
        "80-81F": [26.388888888888889, 27.5],
        "82-83F": [27.5, 28.611111111111111],
        "84F or higher": [28.611111111111111, None],
    }
    f_keys = list(f_intervals)
    f_centers, f_bounds = C.served_bucket_axis(f_intervals, f_keys)
    assert P0._bucket_centers(f_intervals, f_keys) == f_centers
    assert f_bounds == (26.388888888888889, 28.611111111111111)
    closed, closed_bounds = C.served_bucket_axis(
        {"20": [19.5, 20.5], "21": [20.5, 21.5]}, ["20", "21"]
    )
    assert closed == [20.0, 21.0] and closed_bounds == (None, None)


def test_zuuu_loses_its_false_peak() -> None:
    old, _ = _served(INPUT, bounds=None)
    assert old == ZUUU_POINT_RULE
    assert old[1] > old[2]  # the defect: 23 over 24
    new, receipt = _served(INPUT, bounds=(22.5, 31.5))
    # Rising from the open edge to the mode at 26, no second peak.
    assert new[0] < new[1] < new[2] < new[3] < new[4]
    assert new[4] == max(new)
    assert new[0] == pytest.approx(0.0593, abs=5e-4)
    assert new[1] == pytest.approx(0.0814, abs=5e-4)
    assert new[2] == pytest.approx(old[2], abs=1e-12)  # 24 untouched by the tail
    # The top edge keeps a sliver instead of an exact zero.
    assert 0.0 < new[-1] < 1e-4
    assert receipt["open_edge_rule"] == C.OPEN_EDGE_TAIL_RULE
    assert receipt["open_edge_bounds_c"] == [22.5, 31.5]
    assert receipt["open_edge_tail_scale_c"]["lower"] == pytest.approx(
        0.11552778 / 0.073697102
    )
    assert receipt["open_edge_mass_released"]["lower"] == pytest.approx(
        0.11552778 - new[0] * receipt["pre_normalisation_mass"], abs=1e-12
    )


def test_mass_is_conserved() -> None:
    new, receipt = _served(INPUT, bounds=(22.5, 31.5))
    assert math.fsum(new) == pytest.approx(1.0, abs=1e-12)
    assert receipt["pre_normalisation_mass"] == pytest.approx(
        math.fsum(INPUT.values()), abs=1e-12
    )
    assert all(value >= 0.0 for value in new)
    for delta, lam in ((0.0, 0.5), (1.2, 0.75), (-1.5, 1.4), (0.3, 1.0)):
        out, receipt = _served(INPUT, bounds=(22.5, 31.5), delta=delta, lam=lam)
        assert receipt["pre_normalisation_mass"] == pytest.approx(
            math.fsum(INPUT.values()), abs=1e-12
        )
        assert all(value >= 0.0 for value in out)


def test_the_receipt_keeps_its_mean_and_sigma() -> None:
    """The anchor, sigma, floor and lambda are read off the key centres
    exactly as before: only where the tail's mass goes changed."""

    _, old = _served(INPUT, bounds=None)
    _, new = _served(INPUT, bounds=(22.5, 31.5))
    for key in (
        "distribution_mean_c", "distribution_sigma_c", "width_lambda",
        "delta_c", "sigma_floor_lift", "bucket_rung_c", "input_probability",
    ):
        assert new[key] == old[key], key
    assert "open_edge_rule" not in old


def test_no_open_mass_is_bit_identical() -> None:
    closed = dict(INPUT)
    closed["22°C or below"] = 0.0
    closed["32°C or higher"] = 0.0
    with_bounds, _ = _served(closed, bounds=(22.5, 31.5))
    without, _ = _served(closed, bounds=None)
    assert with_bounds == without == CLOSED_POINT_RULE
    shifted, _ = _served(closed, bounds=(22.5, 31.5), delta=0.3, lam=0.8)
    assert shifted == CLOSED_SHIFTED_POINT_RULE


def test_a_closed_axis_is_bit_identical() -> None:
    """The holdout's integer grid declares no open end and is served the
    point rule it was gated on."""

    out, receipt = C.apply_center_width_calibration(
        GRID_INPUT,
        delta_c=-0.2,
        width_lambda=0.75,
        ordered_keys=GRID_KEYS,
        key_centers_c=[float(int(key)) for key in GRID_KEYS],
    )
    assert [out[key] for key in GRID_KEYS] == GRID_POINT_RULE
    assert "open_edge_rule" not in receipt


def test_an_outward_map_keeps_the_tail_whole() -> None:
    """Widening moves the edge outward: nothing inside it crosses in."""

    out, receipt = _served(INPUT, bounds=(22.5, 31.5), lam=1.3)
    assert receipt["open_edge_mass_released"]["lower"] == 0.0
    assert receipt["open_edge_mass_released"]["upper"] == 0.0
    assert out[0] >= INPUT["22°C or below"] / receipt["pre_normalisation_mass"]


def test_a_tail_beside_an_empty_neighbour_stays() -> None:
    probability = dict(INPUT)
    probability["23°C"] = 0.0
    out, receipt = _served(probability, bounds=(22.5, 31.5))
    assert receipt["open_edge_tail_scale_c"]["lower"] is None
    assert receipt["open_edge_mass_released"]["lower"] == 0.0


def test_a_bound_off_the_axis_fails_closed() -> None:
    centers, _ = C.served_bucket_axis(INTERVALS, KEYS)
    with pytest.raises(ValueError, match="open_edge_bounds_invalid"):
        C.apply_center_width_calibration(
            INPUT, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
            key_centers_c=centers, open_edge_bounds_c=(23.5, None),
        )


def _fit_cell() -> dict:
    envelope = {
        "lineage": {"bucket_intervals_c_json": json.dumps(INTERVALS)},
        "probabilities": dict(INPUT),
        "bucket_ordinals": {key: index for index, key in enumerate(KEYS)},
    }
    row = (
        "ZUUU", "2026-09-22", "2026-09-21T10:41:31.382Z", "cp", "0" * 64,
        json.dumps(envelope), 22.1,
    )
    cell = S._serving_cell(None, row, pre_band=False)
    assert cell is not None
    return cell


def test_the_fit_scores_on_servings_axis() -> None:
    """One axis: the fit's cell carries the centres and open ends serving
    hands the layer, and scores a candidate through the same tail."""

    cell = _fit_cell()
    centers, bounds = C.served_bucket_axis(INTERVALS, KEYS)
    assert cell["key_centers_c"] == centers
    assert tuple(cell["open_edge_bounds_c"]) == bounds
    assert cell["settled_bucket"] == "22°C or below"
    expected, _ = C.apply_center_width_calibration(
        INPUT, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
        key_centers_c=centers, open_edge_bounds_c=bounds,
    )
    nll = S._cell_nll(cell, delta_c=0.0, width_lambda=0.75)
    # Scored as served: the transform, then the charter 224 floor (charter 228).
    from bayes_basket_target.materialization import floor_probabilities

    served, _lifted = floor_probabilities(expected)
    assert nll == pytest.approx(-math.log(served["22°C or below"]))
    # Under the point rule this cell's truth sat on 1.1%; the tail keeps 5.9%.
    assert nll < -math.log(0.02)


def test_the_recovery_matrix_keeps_the_point_rule() -> None:
    """Charter 138's recovery solves rows that recorded no input, all of
    which were served under the point rule; its matrix stays that rule."""

    import numpy as np

    centers, _ = C.served_bucket_axis(INTERVALS, KEYS)
    _, receipt = C.apply_center_width_calibration(
        INPUT, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS,
        key_centers_c=centers,
    )
    matrix = S._center_width_forward_matrix(
        centers, mean_c=receipt["distribution_mean_c"], lam=0.75, delta=0.0
    )
    moved = matrix @ np.array([INPUT[key] for key in KEYS])
    moved = moved / moved.sum()
    assert list(moved) == pytest.approx(ZUUU_POINT_RULE, abs=1e-12)
    # A tail-rule row that lost its recorded input is refused, not solved.
    cell = _fit_cell()
    cell["center_width_receipt"] = {
        "state": "APPLIED", "width_lambda": 0.75, "delta_c": 0.0,
        "distribution_mean_c": receipt["distribution_mean_c"],
        "open_edge_rule": C.OPEN_EDGE_TAIL_RULE,
    }
    assert S._identity_counterfactual(cell, delta_c=0.0, width_lambda=0.75) == (
        None, "not_recoverable"
    )


def test_the_bump_is_declared_once_for_three_stages() -> None:
    import inspect

    from bayes_basket_target.materialization import (
        materialize_p0m_observed,
        materialize_p1,
    )

    p0m = inspect.signature(materialize_p0m_observed).parameters[
        "posterior_version"
    ].default
    p1 = inspect.signature(materialize_p1).parameters[
        "posterior_version"
    ].default
    # Charter 198 bumped all three again (P0 v9, P0-M v11, P1 v19); the
    # charter-190 versions keep their shared declaration.
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v22"
    # Charter 241 moved P0-M on to v23 (its own change).
    assert p0m == "p0m_taf_likelihood_v25"
    # Charter 200 moved P1 on to v20 (its own change).
    assert p1 == "p1_joint_likelihood_v33"
    start = SA.version_effective_from(
        "P0", "calibrated_native_cross_ensemble_p0_v8"
    )
    assert start and start.endswith("Z") and "T" in start
    assert SA.version_effective_from("P0-M", "p0m_taf_likelihood_v10") == start
    assert SA.version_effective_from("P1", "p1_joint_likelihood_v18") == start
    # Charter 198's shared declaration (P0 v9, P0-M v11, P1 v19), and
    # charter 204's after it (P0 v10, P0-M v12, P1 v21).
    start = SA.version_effective_from("P0", "calibrated_native_cross_ensemble_p0_v9")
    assert start and start.endswith("Z") and "T" in start
    assert SA.version_effective_from("P0-M", "p0m_taf_likelihood_v11") == start
    assert SA.version_effective_from("P1", "p1_joint_likelihood_v19") == start
    latest = SA.version_effective_from("P0", P0.POSTERIOR_VERSION)
    assert latest > start
    # The latest root bump (charter 243: P0 v22) is shared with P0-M v25 and
    # P1 v33; charter 241's P0-M v23 (its own change) sits between v20 and v21.
    assert SA.version_effective_from("P0-M", p0m) == latest
    assert SA.version_effective_from("P1", p1) == latest
    v20 = SA.version_effective_from("P0", "calibrated_native_cross_ensemble_p0_v20")
    v21 = SA.version_effective_from("P0", "calibrated_native_cross_ensemble_p0_v21")
    assert SA.version_effective_from("P0-M", "p0m_taf_likelihood_v22") == v20
    assert v20 < SA.version_effective_from("P0-M", "p0m_taf_likelihood_v23") < v21 < latest
