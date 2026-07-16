from scripts.evaluate_ncei_zuuu_smooth_peak_origin_v313 import (
    DEDUP_GFS_NUMERIC,
    DEDUP_MODEL_SCHEMA,
    DEDUP_MULTIMODEL_NUMERIC,
    DEDUP_REMOVED_NUMERIC,
    SMOOTH_GROUP_PENALTY_MULTIPLIER,
    SMOOTH_NUMERIC,
    SmoothPeakOriginDesign,
    _penalty_diagonal,
    smooth_peak_origin_values,
    solar_state,
)


def test_deduplicated_basis_removes_only_confirmed_aliases():
    assert set(DEDUP_REMOVED_NUMERIC) == {
        "temperature_vs_observed_max_c",
        "reports_below_observed_max",
    }
    for name in DEDUP_REMOVED_NUMERIC:
        assert name not in DEDUP_GFS_NUMERIC
        assert name not in DEDUP_MULTIMODEL_NUMERIC


def test_solar_state_progresses_without_clock_hour_gate():
    progress_early, remaining_early = solar_state(192, 8.25)
    progress_later, remaining_later = solar_state(192, 10.25)
    assert 0.0 <= progress_early < progress_later <= 1.0
    assert 0.0 <= remaining_later < remaining_early <= 1.0


def test_peak_boundary_basis_is_cyclic_and_smooth():
    midnight_peak = smooth_peak_origin_values(
        {
            "event_date_local": "2026-07-11",
            "local_hour": 9.0,
            "hours_since_observed_max": 9.0,
            "observed_max_to_time_c": 29.0,
        },
        30.0,
    )
    noon_peak = smooth_peak_origin_values(
        {
            "event_date_local": "2026-07-11",
            "local_hour": 15.0,
            "hours_since_observed_max": 3.0,
            "observed_max_to_time_c": 29.0,
        },
        30.0,
    )
    assert midnight_peak["peak_boundary_closeness"] == 1.0
    assert abs(float(midnight_peak["peak_boundary_direction"])) < 1e-12
    assert noon_peak["peak_boundary_closeness"] == 0.0
    assert set(midnight_peak) == set(SMOOTH_NUMERIC)


def test_smooth_features_do_not_read_outcome_labels():
    base = {
        "event_date_local": "2026-07-11",
        "local_hour": 9.0,
        "hours_since_observed_max": 9.0,
        "observed_max_to_time_c": 29.0,
    }
    left = smooth_peak_origin_values({**base, "final_daily_max_c": 29.0, "remaining_warming_c": 0.0}, 30.0)
    right = smooth_peak_origin_values({**base, "final_daily_max_c": 40.0, "remaining_warming_c": 11.0}, 30.0)
    assert left == right


def test_added_smooth_group_receives_multiplicity_shrinkage():
    design = SmoothPeakOriginDesign([*DEDUP_GFS_NUMERIC, *SMOOTH_NUMERIC], [])
    penalties = _penalty_diagonal(design, base_alpha=12.0)
    base_index = 1 + design.names.index(DEDUP_GFS_NUMERIC[0])
    smooth_index = 1 + design.names.index(SMOOTH_NUMERIC[0])
    assert penalties[base_index] == 12.0
    assert penalties[smooth_index] == 12.0 * SMOOTH_GROUP_PENALTY_MULTIPLIER
    assert SMOOTH_GROUP_PENALTY_MULTIPLIER == len(SMOOTH_NUMERIC)


def test_selected_artifact_schema_names_the_deduplicated_candidate():
    assert DEDUP_MODEL_SCHEMA == "ncei_zuuu_deduplicated_peak_state_artifact_v313"
