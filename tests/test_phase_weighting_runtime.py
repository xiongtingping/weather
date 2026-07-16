from phase_weighting_runtime import (
    SCHEMA_VERSION,
    awc_release_window_for_time,
    build_phase_weighting_snapshot,
    phase_weighting_contract,
)


def test_d1_early_and_late_schedule_uses_station_local_time():
    early = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-01T23:00:00+08:00",
    )
    late = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-02T13:00:00+08:00",
    )

    assert early["schema"] == SCHEMA_VERSION
    assert early["read_only"] is True
    assert early["production_entry_enabled"] is False
    assert early["guards"]["writes_archive_db"] is False
    assert early["phase"] == "D1_early"
    assert early["beta_weak"] == 0.65
    assert early["beta_strong"] == 0.0
    assert late["phase"] == "D1_late"
    assert late["beta_weak"] == 0.85
    assert late["beta_strong"] == 0.0


def test_phase_weighting_contract_declares_d1_d0_fields_and_awc_windows():
    contract = phase_weighting_contract()

    assert contract["schema"] == "phase_weighting_contract_v1"
    assert contract["read_only"] is True
    assert contract["guards"]["writes_archive_db"] is False
    assert "phase" in contract["required_output_fields"]
    assert "beta_weak" in contract["required_output_fields"]
    assert "beta_strong" in contract["required_output_fields"]
    assert contract["field_sources"]["hard_mask_reason"] == "hard_source_or_settlement_or_observed_max_signal_summary"
    assert contract["awc_windows"]["hour_plus_0_to_plus_5"]["can_place_orders"] is False
    assert contract["awc_windows"]["hour_plus_5_to_plus_6"]["slo_ms"] == 3000
    assert any(row["phase"] == "D0_breakout_window" for row in contract["schedule"])


def test_d0_schedule_progresses_from_morning_to_late():
    cases = [
        ("2026-07-03T06:30:00+08:00", "D0_pre_dawn", 0.70, 0.15),
        ("2026-07-03T09:00:00+08:00", "D0_morning", 0.55, 0.35),
        ("2026-07-03T11:00:00+08:00", "D0_breakout_window", 0.40, 0.65),
        ("2026-07-03T12:30:00+08:00", "D0_midday", 0.20, 0.85),
        ("2026-07-03T15:59:00+08:00", "D0_late", 0.08, 1.00),
    ]

    for as_of, phase, beta_weak, beta_strong in cases:
        row = build_phase_weighting_snapshot(event_date="2026-07-03", as_of=as_of, include_schedule=False)
        assert row["phase"] == phase
        assert row["beta_weak"] == beta_weak
        assert row["beta_strong"] == beta_strong
        assert row["beta_constraints"]["beta_weak_and_beta_strong_need_not_sum_to_one"] is True


def test_hard_lock_uses_d0_hard_and_preserves_hard_mask_reason():
    row = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-03T11:05:10+08:00",
        hard_mask_signals={
            "hard_source_direct_target_realized": True,
            "observed_today_max_c": 32.0,
            "masked_bucket_count": 3,
        },
    )

    assert row["phase"] == "D0_hard"
    assert row["beta_weak"] == 0.0
    assert row["beta_strong"] == 1.0
    assert row["hard_lock_active"] is True
    assert row["hard_mask_reason"] == "hard_source_direct_target_realized+observed_today_max_c+masked_bucket_count"
    assert row["beta_constraints"]["hard_mask_not_tempered_by_beta"] is True


def test_observed_max_without_lock_keeps_time_phase_but_records_hard_mask_reason():
    row = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-03T12:30:00+08:00",
        hard_mask_signals={"observed_today_max_c": 30.0},
    )

    assert row["phase"] == "D0_midday"
    assert row["hard_lock_active"] is False
    assert row["hard_mask_reason"] == "observed_today_max_c"


def test_awc_preparation_and_signal_windows_are_explicit():
    prep = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-03T15:03:30+08:00",
        include_schedule=False,
    )
    signal = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-03T15:05:30+08:00",
        include_schedule=False,
    )
    outside = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-03T15:06:00+08:00",
        include_schedule=False,
    )

    assert prep["awc_release_window"]["window"] == "awc_preparation_window"
    assert signal["awc_release_window"]["window"] == "awc_hard_source_signal_window"
    assert outside["awc_release_window"]["window"] == "outside_awc_hard_source_signal_window"


def test_awc_release_window_helper_uses_local_minute():
    row = build_phase_weighting_snapshot(
        event_date="2026-07-03",
        as_of="2026-07-03T07:05:59Z",
        include_schedule=False,
    )

    assert row["as_of_local"].startswith("2026-07-03T15:05:59")
    assert row["awc_release_window"]["minute_after_hour"] == 5
    assert awc_release_window_for_time.__name__ == "awc_release_window_for_time"
