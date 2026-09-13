"""P0's contest, gate and compiler shape a blend with one arithmetic.

Model-layer review 2026-09-13, finding R1.  The same pinned band table and
centre/width scalars were applied three ways: serving ran centre/width then
band, the OOS gate band then centre/width, and the subset contest band alone.
An artifact was certified on one vector, seated by a second, served a third.
The order now lives in ``served_shape``; these tests hold each face to it by
behaviour rather than by how the source reads.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos, native_ensemble_p0  # noqa: E402
from bayes_basket_target import served_shape  # noqa: E402
from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXIS_CENTRE_DISTANCE,
    persist_and_activate_band_table,
)
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import (  # noqa: E402
    persist_and_activate_center_width,
)
from bayes_basket_target.cross_consensus import (  # noqa: E402
    apply_band_reliability,
    apply_neighbour_direction_tilt,
)

# The live P0 distance table on 2026-09-13 (sha 4f522947...).
LIVE_P0_BAND = (
    ((0.0, 1.0), 1.0),
    ((1.0, 2.0), 1.047515),
    ((2.0, 3.0), 1.0),
    ((3.0, 4.0), 0.984),
    ((4.0, 99.0), 0.6943),
)
KEYS = [str(index) for index in range(7)]


VECTOR = {"0": 0.03, "1": 0.09, "2": 0.18, "3": 0.28, "4": 0.22,
          "5": 0.13, "6": 0.07}


def _width(vector):
    return apply_center_width_calibration(
        vector, delta_c=0.0, width_lambda=0.75,
        ordered_keys=KEYS, key_centers_c=[float(k) for k in range(7)],
    )[0]


def _band(vector):
    return apply_band_reliability(
        vector, factors=LIVE_P0_BAND,
        ordinals={key: index for index, key in enumerate(KEYS)},
    )[0]


def test_the_table_layers_are_centre_width_then_band() -> None:
    served, cw_receipt, band_receipt = served_shape.apply_served_table_layers(
        VECTOR,
        ordered_keys=KEYS,
        key_centers_c=[float(k) for k in range(7)],
        center_width=served_shape.CenterWidthLayer(width_lambda=0.75),
        band=served_shape.BandLayer(
            factors=LIVE_P0_BAND, axis=BAND_AXIS_CENTRE_DISTANCE
        ),
    )
    assert served_shape.SERVED_TABLE_LAYER_ORDER == (
        "center_width", "band_reliability"
    )
    assert cw_receipt is not None and band_receipt is not None
    geometry_first = _band(_width(VECTOR))
    claim_first = _width(_band(VECTOR))
    assert served == pytest.approx(geometry_first, abs=1e-15)
    # The review's counterexample: the two orders are not the same vector.
    assert served["3"] == pytest.approx(0.35378, abs=1e-5)
    assert claim_first["3"] == pytest.approx(0.35951, abs=1e-5)


def test_a_skipped_layer_is_left_out_and_nothing_else_moves() -> None:
    kwargs = dict(
        ordered_keys=KEYS,
        key_centers_c=[float(k) for k in range(7)],
        center_width=served_shape.CenterWidthLayer(width_lambda=0.75),
        band=served_shape.BandLayer(
            factors=LIVE_P0_BAND, axis=BAND_AXIS_CENTRE_DISTANCE
        ),
    )
    no_band, _cw, band_receipt = served_shape.apply_served_table_layers(
        VECTOR, skip="band_reliability", **kwargs
    )
    assert band_receipt is None
    assert no_band == pytest.approx(_width(VECTOR), abs=1e-15)
    no_width, cw_receipt, _b = served_shape.apply_served_table_layers(
        VECTOR, skip="center_width", **kwargs
    )
    assert cw_receipt is None
    assert no_width == pytest.approx(_band(VECTOR), abs=1e-15)
    with pytest.raises(ValueError):
        served_shape.apply_served_table_layers(
            VECTOR, skip="neighbour_direction", **kwargs
        )


def test_the_holdout_leans_around_the_models_centre_not_the_shifted_mean() -> None:
    """Serving anchors the neighbour tilt on the weighted calibrated centres.

    A radiation shift of -2C moves the histogram's mean two buckets colder
    and leaves the models' centre where it was; the gate and the contest used
    to lean around the moved mean.
    """

    grid = [str(value) for value in range(18, 27)]
    base = {key: 0.0 for key in grid}
    base.update({"21": 0.2, "22": 0.6, "23": 0.2})
    centre_c = 22.0
    shaped = native_ensemble_oos._holdout_shape_stack(
        base,
        grid=grid,
        cooling_shift_c=-2.0,
        station_latitude=50.0,
        neighbour_center_c=centre_c,
        center_width_layer=None,
        band_layer=None,
    )
    shifted = native_ensemble_oos._shift_bucketed_probability(
        base, ordered_keys=grid, shift_c=-2.0
    )
    ordinals = {key: index for index, key in enumerate(grid)}
    around_models, _ = apply_neighbour_direction_tilt(
        shifted, ordinals=ordinals, center_bucket="22", latitude=50.0
    )
    around_mean, _ = apply_neighbour_direction_tilt(
        shifted, ordinals=ordinals, center_bucket="20", latitude=50.0
    )
    assert shaped == pytest.approx(around_models, abs=1e-15)
    assert shaped != pytest.approx(around_mean, abs=1e-6)
    assert served_shape.model_center_c(
        {"A": 0.25, "B": 0.75}, {"A": 20.0, "B": 24.0}
    ) == pytest.approx(23.0)


def test_neither_p0_face_keeps_a_private_copy_of_the_table_primitives() -> None:
    for module in (native_ensemble_oos, native_ensemble_p0):
        assert module.apply_served_table_layers is (
            served_shape.apply_served_table_layers
        )
        assert not hasattr(module, "apply_band_reliability")
        assert not hasattr(module, "apply_center_width_calibration")


def _activate_non_identity_tables(conn: sqlite3.Connection) -> None:
    persist_and_activate_band_table(
        conn, stage="P0", factors=LIVE_P0_BAND, evidence={"schema": "test"},
        serving_day_count=20, axis=BAND_AXIS_CENTRE_DISTANCE,
        fitted_at_utc="2026-06-30T00:00:00Z",
    )
    persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.1, width_lambda=0.75,
        evidence={"schema": "test"}, serving_day_count=20,
        fitted_at_utc="2026-06-30T00:00:00Z",
    )


def test_the_contest_scores_the_blend_the_gate_certifies(monkeypatch) -> None:
    """The contest ranks each candidate on the exact vectors the gate scores.

    With both tables non-identity, the winning subset's contest score and the
    certified cross metrics are one computation over the same holdout days.
    Before 2026-09-13 the contest skipped centre/width and the gate put it
    after band, so the two numbers differed on every such scope.
    """

    from tests.test_native_ensemble_p0 import (
        _insert_official_native_mixed_cycle_archive,
    )
    from tests.test_native_ensemble_oos_p0_pipeline import _insert_fixture

    conn = sqlite3.connect(":memory:")
    cycle_by_system = {
        "ECMWF_IFS_TEST": "18Z",
        "ECMWF_AIFS_TEST": "00Z",
        "NOAA_GEFS_TEST": "18Z",
    }
    _insert_fixture(conn, cycle_by_system=cycle_by_system, lead_bucket="d2")
    _insert_official_native_mixed_cycle_archive(conn, cycle_by_system)
    _activate_non_identity_tables(conn)
    for system_id, cycle in cycle_by_system.items():
        monkeypatch.setitem(
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
            system_id,
            {
                "transport_provider_id": {
                    "ECMWF_IFS_TEST": "ECMWF_OPEN_DATA_S3",
                    "ECMWF_AIFS_TEST": "ECMWF_OPEN_DATA_S3",
                    "NOAA_GEFS_TEST": "NOAA_GEFS_PDS_S3",
                }[system_id],
                "lead_bucket": "d2",
                "run_cycle": cycle,
            },
        )
    shaped_layers: list[tuple[object, object]] = []
    real_stack = native_ensemble_oos._holdout_shape_stack

    def _spy(*args, **kwargs):
        shaped_layers.append(
            (kwargs["center_width_layer"], kwargs["band_layer"])
        )
        return real_stack(*args, **kwargs)

    monkeypatch.setattr(native_ensemble_oos, "_holdout_shape_stack", _spy)
    report = native_ensemble_oos.build_native_ensemble_oos(
        conn, station_id="TEST", as_of_utc="2026-07-01T00:00:00Z",
    )
    candidates = [
        candidate for candidate in report.get("cross_candidates") or []
        if isinstance(candidate, dict)
    ]
    assert candidates, "fixture produced no cross candidate"
    assert shaped_layers
    for center_width_layer, band_layer in shaped_layers:
        # Every shaping call -- contest, gate, ablation, diagnostics -- saw
        # both fitted tables.
        assert center_width_layer is not None
        assert center_width_layer.width_lambda == pytest.approx(0.75)
        assert band_layer.axis == BAND_AXIS_CENTRE_DISTANCE
    for candidate in candidates:
        winner = candidate["subset_selection"]["winner"]
        certified = candidate["oos_metrics"]
        for metric in ("crps", "nll", "brier"):
            assert winner[metric] == pytest.approx(
                certified[metric], rel=1e-12, abs=1e-12
            ), metric


def test_the_compiler_applies_both_pinned_tables_through_the_one_function(
    monkeypatch,
) -> None:
    from tests.test_native_ensemble_p0 import (
        _install_mixed_cycle_calibrations,
        _mixed_packet,
    )

    conn = sqlite3.connect(":memory:")
    _activate_non_identity_tables(conn)
    _install_mixed_cycle_calibrations(conn)
    calls: list[dict[str, object]] = []
    real = served_shape.apply_served_table_layers

    def _spy(probability, **kwargs):
        result = real(probability, **kwargs)
        calls.append({**kwargs, "result": result[0]})
        return result

    monkeypatch.setattr(native_ensemble_p0, "apply_served_table_layers", _spy)
    compiled = native_ensemble_p0.compile_native_ensemble_p0(
        conn,
        forward_packet=_mixed_packet(),
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    assert len(calls) == 1
    call = calls[0]
    assert call["center_width"] is not None
    assert call["center_width"].width_lambda == pytest.approx(0.75)
    assert call["band"] is not None
    assert call["band"].axis == BAND_AXIS_CENTRE_DISTANCE
    assert [
        (lower, upper, factor)
        for (lower, upper), factor in call["band"].factors
    ] == pytest.approx([
        (lower, upper, factor) for (lower, upper), factor in LIVE_P0_BAND
    ])
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert "center_width_json" in lineage and "band_reliability_json" in lineage
