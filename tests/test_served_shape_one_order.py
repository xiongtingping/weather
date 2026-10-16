"""P0's contest, gate and compiler shape a blend with one arithmetic.

Model-layer review 2026-09-13, finding R1.  The same pinned band table and
centre/width scalars were applied three ways: serving ran centre/width then
band, the OOS gate band then centre/width, and the subset contest band alone.
An artifact was certified on one vector, seated by a second, served a third.
The order now lives in ``served_shape``; these tests hold each face to it by
behaviour rather than by how the source reads.

Charter 232 (2026-09-26) retired the band table, so the order is one layer
long; the faces still share it, and a retired layer's name is refused as an
ablation target like any other unknown layer.
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
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import (  # noqa: E402
    persist_and_activate_center_width,
)
from bayes_basket_target import cross_consensus
from bayes_basket_target.cross_consensus import (  # noqa: E402
    apply_neighbour_direction_tilt,
)

KEYS = [str(index) for index in range(7)]


VECTOR = {"0": 0.03, "1": 0.09, "2": 0.18, "3": 0.28, "4": 0.22,
          "5": 0.13, "6": 0.07}


def _width(vector):
    return apply_center_width_calibration(
        vector, delta_c=0.0, width_lambda=0.75,
        ordered_keys=KEYS, key_centers_c=[float(k) for k in range(7)],
    )[0]


def test_the_table_layer_is_centre_width_alone() -> None:
    served, cw_receipt = served_shape.apply_served_table_layers(
        VECTOR,
        ordered_keys=KEYS,
        key_centers_c=[float(k) for k in range(7)],
        center_width=served_shape.CenterWidthLayer(width_lambda=0.75),
    )
    assert served_shape.SERVED_TABLE_LAYER_ORDER == ("center_width",)
    assert cw_receipt is not None
    assert served == pytest.approx(_width(VECTOR), abs=1e-15)


def test_a_skipped_layer_is_left_out_and_a_retired_one_is_refused() -> None:
    kwargs = dict(
        ordered_keys=KEYS,
        key_centers_c=[float(k) for k in range(7)],
        center_width=served_shape.CenterWidthLayer(width_lambda=0.75),
    )
    no_width, cw_receipt = served_shape.apply_served_table_layers(
        VECTOR, skip="center_width", **kwargs
    )
    assert cw_receipt is None
    assert no_width == pytest.approx(VECTOR, abs=1e-15)
    for retired_or_unknown in ("band_reliability", "neighbour_direction"):
        with pytest.raises(ValueError):
            served_shape.apply_served_table_layers(
                VECTOR, skip=retired_or_unknown, **kwargs
            )


def test_the_holdout_leans_around_the_models_centre_not_the_shifted_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving anchors the neighbour tilt on the weighted calibrated centres.

    A radiation shift of -2C moves the histogram's mean two buckets colder
    and leaves the models' centre where it was; the gate and the contest used
    to lean around the moved mean.  The tilt is retired (charter 147), so
    this pins the research arm: the anchor rule still holds when it is on.
    """

    monkeypatch.setattr(cross_consensus, "NEIGHBOUR_DIRECTION_MODE", "on")
    grid = [str(value) for value in range(18, 27)]
    base = {key: 0.0 for key in grid}
    base.update({"21": 0.2, "22": 0.6, "23": 0.2})
    centre_c = 22.0
    # The shift rides in sigmas of the blend's own spread (sd 0.632C here).
    spread_c = (0.2 * 1.0 + 0.2 * 1.0) ** 0.5
    shaped = native_ensemble_oos._holdout_shape_stack(
        base,
        grid=grid,
        cooling_shift_sigma=-2.0 / spread_c,
        station_latitude=50.0,
        neighbour_center_c=centre_c,
        center_width_layer=None,
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
    assert shaped == pytest.approx(around_models, abs=1e-12)
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
    persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.1, width_lambda=0.75,
        evidence={"schema": "test"}, serving_day_count=20,
        fitted_at_utc="2026-06-30T00:00:00Z",
    )


def test_the_contest_scores_the_blend_the_gate_certifies(monkeypatch) -> None:
    """The contest ranks each candidate on the exact vectors the gate scores.

    With the table non-identity, the winning subset's contest score and the
    certified cross metrics are one computation over the same holdout days.
    Before 2026-09-13 the contest skipped centre/width and the gate put it
    after band, so the two numbers differed on every such scope.
    """
    # The table-layer path, which pre-273 artifacts are still served on;
    # charter 273's spread-adjusted pool takes the tables out of the gate.
    from bayes_basket_target import spread_adjusted_pool
    monkeypatch.setattr(spread_adjusted_pool, "SLP_MODE", "off")

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
    shaped_layers: list[object] = []
    real_stack = native_ensemble_oos._holdout_shape_stack

    def _spy(*args, **kwargs):
        assert "band_layer" not in kwargs
        shaped_layers.append(kwargs["center_width_layer"])
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
    for center_width_layer in shaped_layers:
        # Every shaping call -- contest, gate, ablation, diagnostics -- saw
        # the fitted table.
        assert center_width_layer is not None
        assert center_width_layer.width_lambda == pytest.approx(0.75)
    for candidate in candidates:
        winner = candidate["subset_selection"]["winner"]
        certified = candidate["oos_metrics"]
        for metric in ("crps", "nll", "brier"):
            assert winner[metric] == pytest.approx(
                certified[metric], rel=1e-12, abs=1e-12
            ), metric


def test_the_compiler_applies_the_pinned_table_through_the_one_function(
    monkeypatch,
) -> None:
    # The table-layer path, which pre-273 artifacts are still served on;
    # charter 273's spread-adjusted pool takes the tables out of the gate.
    from bayes_basket_target import spread_adjusted_pool
    monkeypatch.setattr(spread_adjusted_pool, "SLP_MODE", "off")
    from tests.test_native_ensemble_p0 import (
        _install_mixed_cycle_calibrations,
        _mixed_packet,
    )

    # The installer expects one contest winner per cohort; the table layers
    # are the same under either membership rule.
    monkeypatch.setattr(native_ensemble_oos, "P0_MEMBERSHIP_MODE", "subset_contest")
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
    assert "band" not in call
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert "center_width_json" in lineage
    assert "band_reliability_json" not in lineage
