from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.et_history import (
    TARGET_TIMEZONE,
    build_et_history_evidence,
    build_et_truth_rows,
    assert_event_day_closed,
    event_day_window,
    normalize_et_model_rows,
)
from bayes_basket_target.resource_bounds import write_bounded_json
from scripts.build_bayes_basket_target_et_history import _historical_rebuild_rows


SHA = "a" * 64


def _model(provider: str, *, event_date: str = "2026-07-24", target_timezone: str = TARGET_TIMEZONE) -> dict[str, object]:
    return {
        "station_id": "ZUUU",
        "event_date": event_date,
        "target_timezone": target_timezone,
        "provider_id": provider,
        "model_run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-24T16:00:00Z",
        "published_at_utc": "2026-07-23T07:00:00Z",
        "received_at_utc": "2026-07-23T08:00:00Z",
        "temperature_2m_c": 30.0,
        "raw_message_sha256": SHA,
        "receipt_sha256": SHA,
    }


def _observation(*, lane: str = "amsc", valid_at: str = "2026-07-24T16:00:00Z") -> dict[str, object]:
    return {
        "station_id": "ZUUU",
        "source_lane": lane,
        "report_type": "METAR",
        "valid_at_utc": valid_at,
        "reported_temperature_c": 31,
        "raw_sha256": SHA,
        "source_poll_id": "poll-1",
        "first_seen_at_utc": "2026-07-24T16:01:00Z",
        "canonical_committed_at_utc": "2026-07-24T16:02:00Z",
        "qc_status": "ACCEPTED",
        "correction_suspected": 0,
    }


def test_legacy_asia_shanghai_row_is_blocked_without_relabel() -> None:
    row = _model("ecmwf_ifs025_official", target_timezone="Asia/Shanghai")
    accepted, blocked = normalize_et_model_rows([row], as_of_utc="2026-07-24T00:00:00Z")
    assert accepted == []
    assert blocked == [{
        "row_index": "0",
        "reason": "model_target_timezone_registry_mismatch",
    }]


def test_missing_target_timezone_is_blocked_even_when_utc_date_looks_compatible() -> None:
    row = _model("ecmwf_ifs025_official")
    row.pop("target_timezone")
    accepted, blocked = normalize_et_model_rows([row], as_of_utc="2026-07-24T00:00:00Z")
    assert not accepted
    assert blocked[0]["reason"] == "target_timezone_missing"


def test_future_event_day_is_blocked_before_history_grouping() -> None:
    row = _model("ecmwf_ifs025_official", event_date="2026-07-25")
    accepted, blocked = normalize_et_model_rows(
        [row],
        as_of_utc="2026-07-24T15:00:00Z",
    )
    assert accepted == []
    assert blocked == [{"row_index": "0", "reason": "future_event_day_not_allowed"}]


def test_not_started_window_is_classified_without_admitting_model_rows() -> None:
    window = event_day_window("2026-07-25", as_of_utc="2026-07-24T15:00:00Z")
    assert window["state"] == "not_started"


def test_historical_boundary_requires_closed_et_day() -> None:
    with pytest.raises(ContractError, match="historical_event_day_not_closed"):
        assert_event_day_closed("2026-07-24", as_of_utc="2026-07-24T15:00:00Z")
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        assert_event_day_closed("2026-07-25", as_of_utc="2026-07-24T15:00:00Z")


def test_truth_uses_et_day_and_enforces_lane_ownership() -> None:
    row = _observation(valid_at="2026-07-25T02:00:00Z")
    row["first_seen_at_utc"] = "2026-07-25T02:01:00Z"
    row["canonical_committed_at_utc"] = "2026-07-25T02:02:00Z"
    truth, blocked = build_et_truth_rows(
        [row],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert not blocked
    assert truth[0]["event_date"] == "2026-07-24"
    assert truth[0]["target_timezone"] == TARGET_TIMEZONE

    truth, blocked = build_et_truth_rows(
        [_observation(lane="awc", valid_at="2026-07-24T16:15:00Z")],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert truth == []
    assert blocked[0]["reason"] == "truth_source_lane_cross_fallback_forbidden"


def test_awc_hourly_for_amsc_station_is_reference_only_and_nonfatal() -> None:
    truth, blocked = build_et_truth_rows(
        [_observation(lane="awc", valid_at="2026-07-24T16:00:00Z")],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert truth == []
    assert blocked == [{
        "row_index": "0",
        "reason": "awc_hourly_reference_only_for_amsc_station",
    }]


def test_amsc_half_hour_is_reference_only() -> None:
    row = _observation(valid_at="2026-07-24T16:30:00Z")
    truth, blocked = build_et_truth_rows(
        [row],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert truth == []
    assert blocked == [{"row_index": "0", "reason": "amsc_half_hour_reference_only"}]


def test_amsc_half_hour_on_awc_station_is_cross_lane_blocked() -> None:
    row = _observation(valid_at="2026-07-24T16:30:00Z")
    truth, blocked = build_et_truth_rows(
        [row],
        station_lane_map={"ZUUU": "awc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert truth == []
    assert blocked == [{"row_index": "0", "reason": "truth_source_lane_cross_fallback_forbidden"}]


def test_amsc_non_hourly_metar_is_blocked_but_speci_and_awc_53_are_formal() -> None:
    invalid = _observation(valid_at="2026-07-24T16:15:00Z")
    truth, blocked = build_et_truth_rows(
        [invalid],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert truth == []
    assert blocked == [{"row_index": "0", "reason": "amsc_metar_timing_invalid"}]

    speci = _observation(valid_at="2026-07-24T16:00:42Z")
    speci["report_type"] = "SPECI"
    speci["first_seen_at_utc"] = "2026-07-24T16:16:00Z"
    speci["canonical_committed_at_utc"] = "2026-07-24T16:17:00Z"
    truth, blocked = build_et_truth_rows(
        [speci],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert not blocked
    assert truth[0]["official_tmax_c"] == 31

    awc = _observation(lane="awc", valid_at="2026-07-24T16:53:00Z")
    awc["first_seen_at_utc"] = "2026-07-24T16:54:00Z"
    awc["canonical_committed_at_utc"] = "2026-07-24T16:55:00Z"
    truth, blocked = build_et_truth_rows(
        [awc],
        station_lane_map={"ZUUU": "awc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert not blocked
    assert truth[0]["official_tmax_c"] == 31


def test_truth_event_date_uses_explicit_settlement_timezone_not_operational_default() -> None:
    row = _observation(valid_at="2026-07-27T16:00:00Z")
    row["first_seen_at_utc"] = "2026-07-27T16:01:00Z"
    row["canonical_committed_at_utc"] = "2026-07-27T16:02:00Z"
    truth, blocked = build_et_truth_rows(
        [row],
        station_lane_map={"ZUUU": "amsc"},
        station_timezone_map={"ZUUU": "Asia/Shanghai"},
        as_of_utc="2026-07-29T00:00:00Z",
    )
    assert not blocked
    assert truth[0]["event_date"] == "2026-07-28"
    assert truth[0]["target_timezone"] == "Asia/Shanghai"


def test_reference_only_rows_do_not_make_history_fatal() -> None:
    truth, blocked = build_et_truth_rows(
        [_observation(valid_at="2026-07-24T16:30:00Z")],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )
    assert truth == []
    assert blocked[0]["reason"] == "amsc_half_hour_reference_only"

    models = [_model(provider, event_date="2026-07-24") for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[_observation(valid_at="2026-07-24T16:30:00Z")],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-25T04:00:00Z",
    )
    assert evidence["input_counts"]["fatal_truth_rows"] == 0
    assert evidence["reference_only"]["row_count"] == 1


def test_revision_and_noncanonical_truth_never_become_labels() -> None:
    revised = _observation()
    revised["correction_suspected"] = 1
    truth, blocked = build_et_truth_rows(
        [revised], station_lane_map={"ZUUU": "amsc"}, as_of_utc="2026-07-26T00:00:00Z"
    )
    assert truth == []
    assert blocked[0]["reason"] == "truth_revision_or_quarantine"

    truth, blocked = build_et_truth_rows(
        [_observation()], station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z", canonical_ingress=False
    )
    assert truth == []
    assert blocked[0]["reason"] == "truth_not_canonical_ingress"

    first = _observation()
    second = _observation()
    second["valid_at_utc"] = first["valid_at_utc"]
    second["raw_sha256"] = "b" * 64
    truth, blocked = build_et_truth_rows(
        [first, second], station_lane_map={"ZUUU": "amsc"}, as_of_utc="2026-07-26T00:00:00Z"
    )
    assert truth == []
    assert any(item["reason"] == "truth_same_slot_revision" for item in blocked)


def test_revision_isolates_only_slot_and_keeps_unrelated_station_day() -> None:
    first = _observation(valid_at="2026-07-24T08:00:00Z")
    second = dict(first)
    second["raw_sha256"] = "b" * 64
    second["reported_temperature_c"] = 32
    unrelated_speci = _observation(valid_at="2026-07-24T08:00:00Z")
    unrelated_speci["report_type"] = "SPECI"
    unrelated_speci["raw_sha256"] = "c" * 64
    unrelated_speci["reported_temperature_c"] = 33
    unrelated_later = _observation(valid_at="2026-07-24T13:00:00Z")
    unrelated_later["raw_sha256"] = "d" * 64
    unrelated_later["reported_temperature_c"] = 34

    truth, blocked = build_et_truth_rows(
        [first, second, unrelated_speci, unrelated_later],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-26T00:00:00Z",
    )

    assert len(truth) == 1
    assert truth[0]["event_date"] == "2026-07-24"
    assert truth[0]["official_tmax_c"] == 34
    assert truth[0]["report_count"] == 2
    assert any(item["reason"] == "truth_same_slot_revision" for item in blocked)


def test_history_receipt_accepts_two_model_calibration_scope_and_never_authorizes() -> None:
    models = [_model("ecmwf_ifs025_official"), _model("gfs_0p25_official")]
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[_observation()],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-25T04:00:00Z",
    )
    assert evidence["state"] == "READY_FOR_STRICT_ASOF"
    assert evidence["checks"]["multi_model_join_positive"] is True
    assert evidence["checks"]["all_joined_model_sets_at_least_two"] is True
    assert evidence["checks"]["promotion_allowed"] is False
    assert evidence["boundary"]["writes_target_db"] is False
    assert evidence["boundary"]["calls_order_gateway"] is False
    assert not any(
        item["reason"] == "multi_model_provider_set_incomplete"
        for item in evidence["blockers"]
    )


@pytest.mark.parametrize(
    ("as_of_utc", "expected_state"),
    [
        ("2026-07-24T12:00:00Z", "open"),
        ("2026-07-25T04:00:00Z", "closed"),
    ],
)
def test_truth_missing_carries_explicit_et_event_day_window(
    as_of_utc: str,
    expected_state: str,
) -> None:
    models = [_model(provider) for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc=as_of_utc,
    )

    blocker = next(item for item in evidence["blockers"] if item["reason"] == "truth_missing")
    assert blocker["truth_window_state"] == expected_state
    assert blocker["target_day_start_utc"] == "2026-07-24T04:00:00Z"
    assert blocker["target_day_end_utc"] == "2026-07-25T04:00:00Z"
    assert evidence["event_day_windows"]["by_station_event_date"][
        "ZUUU|2026-07-24"
    ]["state"] == expected_state


def test_expected_quarantine_isolated_without_blocking_unrelated_join() -> None:
    models = [_model(provider) for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    quarantined = _observation(valid_at="2026-07-25T16:00:00Z")
    quarantined["first_seen_at_utc"] = "2026-07-25T16:01:00Z"
    quarantined["canonical_committed_at_utc"] = "2026-07-25T16:02:00Z"
    quarantined["correction_suspected"] = 1
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[_observation(), quarantined],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-25T04:00:00Z",
    )
    assert evidence["state"] == "READY_FOR_STRICT_ASOF"
    assert evidence["input_counts"]["joined_station_days"] == 1
    assert evidence["input_counts"]["quarantined_truth_rows"] == 1
    assert evidence["checks"]["truth_rows_canonical_et_compatible"] is True
    assert evidence["checks"]["truth_quarantine_isolated"] is True
    assert evidence["quarantine"]["reason_counts"] == {"truth_revision_or_quarantine": 1}


def test_historical_rebuild_event_date_filter_excludes_forward_overlap(tmp_path) -> None:
    path = tmp_path / "historical.json"
    row = {
        "capture_mode": "historical_rebuild",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "target_timezone": "America/New_York",
        "provider_id": "gfs_0p25_official",
        "production_calibration_eligible": False,
    }
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_historical_native_rebuild_evidence_v1",
            "model_rows": [row, {**row, "event_date": "2026-07-24"}],
        },
    )

    rows = _historical_rebuild_rows(
        path,
        event_dates={"2026-07-23"},
        station_timezone_map={"ZUUU": "America/New_York"},
    )

    assert [item["event_date"] for item in rows] == ["2026-07-23"]


def test_historical_rebuild_defaults_to_closed_et_days_before_grouping(tmp_path) -> None:
    path = tmp_path / "historical.json"
    row = {
        "capture_mode": "historical_rebuild",
        "station_id": "ZUUU",
        "target_timezone": "America/New_York",
        "provider_id": "gfs_0p25_official",
        "production_calibration_eligible": False,
    }
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_historical_native_rebuild_evidence_v1",
            "model_rows": [
                {**row, "event_date": "2026-07-23"},
                {**row, "event_date": "2026-07-24"},
                {**row, "event_date": "2026-07-25"},
            ],
        },
    )

    excluded: set[str] = set()
    rows = _historical_rebuild_rows(
        path,
        as_of_utc="2026-07-25T00:00:00Z",
        excluded_event_dates=excluded,
        station_timezone_map={"ZUUU": "America/New_York"},
    )

    assert [item["event_date"] for item in rows] == ["2026-07-23"]
    assert excluded == {"2026-07-24", "2026-07-25"}


def test_explicit_historical_date_cannot_bypass_et_closure(tmp_path) -> None:
    path = tmp_path / "historical.json"
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_historical_native_rebuild_evidence_v1",
            "model_rows": [{
                "station_id": "ZUUU",
                "event_date": "2026-07-24",
                "target_timezone": "America/New_York",
                "provider_id": "gfs_0p25_official",
            }],
        },
    )

    rows = _historical_rebuild_rows(
        path,
        event_dates={"2026-07-24"},
        as_of_utc="2026-07-25T00:00:00Z",
        station_timezone_map={"ZUUU": "America/New_York"},
    )

    assert rows == []


def test_icon_retained_raw_rebuild_uses_common_historical_row_envelope(tmp_path) -> None:
    path = tmp_path / "icon-retained.json"
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_icon_retained_raw_rebuild_evidence_v1",
            "reconstructed_at_utc": "2026-07-24T20:00:00Z",
            "model_points": [{
                "station_id": "ZUUU",
                "event_date": "2026-07-23",
                "target_timezone": "America/New_York",
                "provider_id": "icon_global_official",
                "published_at_utc": "",
                "received_at_utc": "2026-07-23T12:00:00Z",
                "raw_message_sha256": SHA,
                "receipt_sha256": SHA,
            }],
        },
    )

    rows = _historical_rebuild_rows(
        path,
        event_dates={"2026-07-23"},
        station_timezone_map={"ZUUU": "America/New_York"},
    )

    assert rows[0]["capture_mode"] == "historical_rebuild"
    assert rows[0]["reconstructed_at_utc"] == "2026-07-24T20:00:00Z"
    assert rows[0]["historical_replay_eligible"] is True
    assert rows[0]["prospective_strict_asof_eligible"] is False


def test_icon_retained_raw_rebuild_uses_complete_curves_only(tmp_path) -> None:
    path = tmp_path / "icon-retained-complete-only.json"
    point = {
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "target_timezone": "America/New_York",
        "provider_id": "icon_global_official",
        "model_run_initialization_utc": "2026-07-22T00:00:00Z",
        "valid_time_utc": "2026-07-23T16:00:00Z",
        "temperature_2m_c": 31.0,
        "raw_message_sha256": SHA,
        "receipt_sha256": SHA,
        "published_at_utc": "2026-07-22T07:00:00Z",
        "received_at_utc": "",
    }
    write_bounded_json(
        path,
        {
            "schema": "bayes_basket_target_icon_retained_raw_rebuild_evidence_v1",
            "reconstructed_at_utc": "2026-07-24T20:00:00Z",
            "model_points": [{**point, "temperature_2m_c": 99.0}],
            "curves": {
                "complete_curve_count": 1,
                "eligible_model_rows": [{
                    "station_id": "ZUUU",
                    "event_date": "2026-07-23",
                    "provider_id": "icon_global_official",
                    "model_run_initialization_utc": "2026-07-22T00:00:00Z",
                    "temperature_points": [point],
                }],
            },
        },
    )

    rows = _historical_rebuild_rows(
        path,
        event_dates={"2026-07-23"},
        station_timezone_map={"ZUUU": "America/New_York"},
    )

    assert len(rows) == 1
    assert rows[0]["temperature_2m_c"] == 31.0


def test_historical_rebuild_cannot_admit_a_future_et_day() -> None:
    row = _model("gfs_0p25_official", event_date="2026-07-25")
    row["valid_time_utc"] = "2026-07-25T16:00:00Z"
    row["model_run_initialization_utc"] = "2026-07-24T00:00:00Z"
    row["published_at_utc"] = "2026-07-24T07:00:00Z"
    row["reconstructed_at_utc"] = "2026-07-25T03:00:00Z"
    row["capture_mode"] = "historical_rebuild"

    accepted, blocked = normalize_et_model_rows(
        [row],
        as_of_utc="2026-07-25T04:00:00Z",
        history_only=True,
    )

    assert accepted == []
    assert blocked == [{
        "row_index": "0",
        "reason": "historical_rebuild_event_day_not_closed",
    }]


def test_historical_rebuild_without_arrival_timestamps_is_replay_only() -> None:
    row = _model("gfs_0p25_official")
    row.update({
        "capture_mode": "historical_rebuild",
        "reconstructed_at_utc": "2026-07-24T20:00:00Z",
        "published_at_utc": "",
        "received_at_utc": "",
    })
    accepted, blocked = normalize_et_model_rows(
        [row],
        as_of_utc="2026-07-25T04:00:00Z",
        history_only=True,
    )
    assert not blocked
    assert accepted[0]["historical_replay_eligible"] is True
    assert accepted[0]["prospective_strict_asof_eligible"] is False


def test_history_only_keeps_open_day_pending_and_aggregates_daily_max() -> None:
    models = []
    for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    ):
        first = _model(provider, event_date="2026-07-23")
        first["valid_time_utc"] = "2026-07-23T16:00:00Z"
        first["temperature_2m_c"] = 20.0
        second = dict(first)
        second["valid_time_utc"] = "2026-07-24T03:00:00Z"
        second["temperature_2m_c"] = 31.0
        models.extend([first, second])
        current = _model(provider, event_date="2026-07-24")
        current["temperature_2m_c"] = 29.0
        models.append(current)
    old_truth = _observation(valid_at="2026-07-23T16:00:00Z")
    old_truth["first_seen_at_utc"] = "2026-07-23T16:01:00Z"
    old_truth["canonical_committed_at_utc"] = "2026-07-23T16:02:00Z"
    current_truth = _observation(valid_at="2026-07-24T16:00:00Z")
    current_truth["first_seen_at_utc"] = "2026-07-24T16:01:00Z"
    current_truth["canonical_committed_at_utc"] = "2026-07-24T16:02:00Z"
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[old_truth, current_truth],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-24T12:00:00Z",
        history_only=True,
    )
    assert evidence["input_counts"]["joined_station_days"] == 1
    assert evidence["joined_station_days"][0]["forecasts"]["ecmwf_ifs025_official"] == 31.0
    assert evidence["input_counts"]["pending_event_days"] == 1
    assert not any(
        item["reason"] == "event_day_not_closed_in_settlement_timezone"
        for item in evidence["blockers"]
    )


def test_unexpected_truth_shape_remains_fatal_with_quarantine_present() -> None:
    models = [_model(provider) for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    malformed = _observation(valid_at="2026-07-25T16:00:00Z")
    malformed["first_seen_at_utc"] = "2026-07-25T16:01:00Z"
    malformed["canonical_committed_at_utc"] = "2026-07-25T16:02:00Z"
    malformed.pop("raw_sha256")
    quarantined = _observation(valid_at="2026-07-25T17:00:00Z")
    quarantined["first_seen_at_utc"] = "2026-07-25T17:01:00Z"
    quarantined["canonical_committed_at_utc"] = "2026-07-25T17:02:00Z"
    quarantined["correction_suspected"] = 1
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[_observation(), malformed, quarantined],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-25T04:00:00Z",
    )
    assert evidence["state"] == "BLOCKED_EVIDENCE"
    assert evidence["input_counts"]["fatal_truth_rows"] == 1
    assert evidence["checks"]["truth_rows_canonical_et_compatible"] is False
    assert any(item["reason"] == "raw_sha256_missing" for item in evidence["blockers"])


def test_exact_three_model_et_join_is_ready_only_for_next_strict_asof_gate() -> None:
    models = [_model(provider) for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[_observation()],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-25T04:00:00Z",
    )
    assert evidence["state"] == "READY_FOR_STRICT_ASOF"
    assert evidence["input_counts"]["joined_station_days"] == 1


def test_future_truth_cannot_enter_strict_asof_join() -> None:
    models = [_model(provider) for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[_observation()],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-24T12:00:00Z",
    )
    assert evidence["state"] == "BLOCKED_EVIDENCE"
    assert evidence["input_counts"]["joined_station_days"] == 0
    assert any(item["reason"] == "truth_committed_after_asof" for item in evidence["blockers"])


def test_open_et_day_cannot_enter_exact_join_even_when_truth_exists() -> None:
    models = [_model(provider) for provider in (
        "ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"
    )]
    observation = _observation(valid_at="2026-07-24T06:00:00Z")
    observation["first_seen_at_utc"] = "2026-07-24T06:01:00Z"
    observation["canonical_committed_at_utc"] = "2026-07-24T06:02:00Z"
    evidence = build_et_history_evidence(
        model_rows=models,
        observations=[observation],
        station_lane_map={"ZUUU": "amsc"},
        as_of_utc="2026-07-24T12:00:00Z",
    )
    assert evidence["state"] == "BLOCKED_EVIDENCE"
    assert evidence["input_counts"]["joined_station_days"] == 0
    assert any(
        item["reason"] == "event_day_not_closed_in_settlement_timezone"
        for item in evidence["blockers"]
    )
    assert evidence["checks"]["promotion_allowed"] is False
