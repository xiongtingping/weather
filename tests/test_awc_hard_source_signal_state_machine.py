from awc_hard_source_signal_state_machine import (
    awc_hard_source_acceptance_contract,
    build_awc_hard_source_signal_candidate,
    classify_awc_signal_window,
)


def test_classify_awc_signal_window_accepts_plus_five_window():
    window = classify_awc_signal_window(
        awc_valid_time="2026-07-03T09:00:00Z",
        signal_seen_at="2026-07-03T09:05:37Z",
        acquire_window_start_min=5.0,
        acquire_window_duration_sec=60.0,
    )

    assert window.state == "acceptance_window"
    assert window.awc_pre_signal_deadline_at == "2026-07-03T09:05:00Z"
    assert window.detection_lag_ms == 337000.0


def test_awc_hard_source_acceptance_contract_lists_boundaries_and_guards():
    contract = awc_hard_source_acceptance_contract()

    assert contract["schema"] == "awc_hard_source_acceptance_contract_v1"
    assert contract["read_only"] is True
    assert contract["guards"]["writes_archive_db"] is False
    assert contract["guards"]["places_orders"] is False
    assert contract["accepted_source_ids"] == ["zuuu_metar_raw_amsc"]
    assert contract["primary_source_ids"] == ["zuuu_metar_raw_amsc"]
    assert contract["reference_source_ids"] == ["awc_cache_metars_csv_gz", "zuuu_metar_raw_awc"]
    assert "accepted" in contract["acceptance_states"]
    assert "conflict" in contract["acceptance_states"]
    assert "AWC raw/cache sources are reference hard observations and cannot emit accepted primary hard source signal" in contract["demoted_rules"]
    assert "WU/TWC current sources remain diagnostic_current_source and cannot emit awc_hard_source_signal_at" in contract["demoted_rules"]
    assert contract["emission_boundary"]["awc_hard_source_signal_at"] == "only populated when acceptance_state=accepted"


def test_classify_awc_signal_window_rejects_preparation_period_as_signal():
    window = classify_awc_signal_window(
        awc_valid_time="2026-07-03T09:00:00Z",
        signal_seen_at="2026-07-03T09:04:59Z",
    )

    assert window.state == "preparation_window"
    assert window.reason == "signal_seen_before_awc_acquire_window"


def test_build_awc_signal_candidate_accepts_amsc_primary_read_only_and_not_emitted():
    candidate = build_awc_hard_source_signal_candidate(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        observation={
            "ok": True,
            "source_id": "zuuu_metar_raw_amsc",
            "value_c": 30.0,
            "observed_at": "2026-07-03T09:30:00Z",
            "fetched_at": "2026-07-03T09:31:37Z",
            "raw": "ZUUU 030900Z ... 30/22",
        },
        candidate_seen_at="2026-07-03T09:31:38Z",
    )

    assert candidate["acceptance_state"] == "accepted"
    assert candidate["acceptance_reason"] == "primary_hard_source_signal_accepted"
    assert candidate["signal_eligible"] is True
    assert candidate["primary_hard_source"] is True
    assert candidate["accepted_signal_source"] is True
    assert candidate["awc_hard_source_signal_at"] == "2026-07-03T09:31:38Z"
    assert candidate["read_only"] is True
    assert candidate["emits_signal"] is False
    assert candidate["terminal_state"] == "skipped"
    assert candidate["terminal_reason"] == "read_only_candidate_not_emitted"
    assert candidate["signal_id"].startswith("sig_")
    assert candidate["source_event_id"].startswith("srcevt_")


def test_build_awc_signal_candidate_demotes_awc_reference_source():
    candidate = build_awc_hard_source_signal_candidate(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        observation={
            "ok": True,
            "source_id": "zuuu_metar_raw_awc",
            "value_c": 30.0,
            "observed_at": "2026-07-03T09:00:00Z",
            "fetched_at": "2026-07-03T09:05:37Z",
            "raw": "ZUUU 030900Z ... 30/22",
        },
        candidate_seen_at="2026-07-03T09:05:38Z",
    )

    assert candidate["acceptance_state"] == "demoted"
    assert candidate["acceptance_reason"] == "hard_source_reference_not_primary_signal_source"
    assert candidate["reference_hard_source"] is True
    assert candidate["primary_hard_source"] is False
    assert candidate["signal_eligible"] is False
    assert candidate["awc_hard_source_signal_at"] == ""


def test_build_awc_signal_candidate_demotes_twc_current():
    candidate = build_awc_hard_source_signal_candidate(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        observation={
            "ok": True,
            "source_id": "twc_current_icao_metric",
            "value_c": 29.0,
            "observed_at": "2026-07-03T09:35:39Z",
            "fetched_at": "2026-07-03T09:37:27Z",
        },
        candidate_seen_at="2026-07-03T09:37:27Z",
    )

    assert candidate["acceptance_state"] == "demoted"
    assert candidate["source_class"] == "diagnostic_current_source"
    assert candidate["signal_eligible"] is False
    assert candidate["awc_hard_source_signal_at"] == ""
    assert candidate["acceptance_reason"] == "twc_current_blended_not_hard_source"


def test_build_awc_signal_candidate_records_conflict_before_acceptance():
    candidate = build_awc_hard_source_signal_candidate(
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        observation={
            "ok": True,
            "source_id": "zuuu_metar_raw_amsc",
            "value_c": 30.0,
            "observed_at": "2026-07-03T09:00:00Z",
            "fetched_at": "2026-07-03T09:05:37Z",
            "conflict_reason": "newer_hard_fact_already_present",
        },
        candidate_seen_at="2026-07-03T09:05:38Z",
    )

    assert candidate["acceptance_state"] == "conflict"
    assert candidate["acceptance_reason"] == "newer_hard_fact_already_present"
    assert candidate["signal_eligible"] is False
    assert candidate["awc_hard_source_signal_at"] == ""
