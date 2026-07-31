from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target.calibration_evidence import build_calibration_evidence
from bayes_basket_target.contracts import ContractError, content_sha256


SHA = "a" * 64


def _rows() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    models: list[dict[str, str]] = []
    observations: list[dict[str, str]] = []
    for index in range(1, 7):
        target = date(2026, 7, index)
        day = target.isoformat()
        run_day = (target - timedelta(days=1)).isoformat()
        observations.append({
            "station_id": "ZBAA",
            "event_date": day,
            "valid_time_utc": f"{day}T12:00:00Z",
            "temperature_c": str(30 + index),
            "raw_sha256": SHA,
            "source_raw_sha256": SHA,
        })
        for provider, forecast in (
            ("ecmwf_ifs025_official", 29 + index),
            ("gfs_0p25_official", 28 + index),
            ("icon_global_official", 27 + index),
        ):
            models.append({
                "station_id": "ZBAA",
                "city": "Beijing",
                "event_date": day,
                "target_timezone": "America/New_York",
                "season": "summer",
                "provider_id": provider,
                "model_run_initialization_utc": f"{run_day}T00:00:00Z",
                "forecast_lead_hours": "24",
                "valid_time_utc": f"{day}T12:00:00Z",
                "temperature_2m_c": str(forecast),
                "raw_message_sha256": SHA,
                "receipt_sha256": SHA,
            })
    return models, observations


def _canonical(
    observations: list[dict[str, str]],
    *,
    committed_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    committed_overrides = committed_overrides or {}
    canonical_observations: list[dict[str, object]] = []
    truth_rows: list[dict[str, str]] = []
    for index, row in enumerate(observations, start=1):
        event_date = row["event_date"]
        truth_id = f"truth-ZBAA-{event_date}"
        first_seen = f"{event_date}T12:01:00Z"
        canonical_observations.append({
            "observation_id": f"obs-{index}",
            "source_lane": "amsc",
            "station_id": "ZBAA",
            "event_date_local": event_date,
            "valid_at_utc": row["valid_time_utc"],
            "reported_temperature_c": row["temperature_c"],
            "raw_sha256": SHA,
            "first_seen_at_utc": first_seen,
            "canonical_committed_at_utc": committed_overrides.get(
                event_date,
                f"{event_date}T12:01:01Z",
            ),
            "qc_status": "ACCEPTED",
        })
        truth_rows.append({
            "truth_id": truth_id,
            "source_lane": "amsc",
            "station_id": "ZBAA",
            "event_date": event_date,
        })
    return {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "state": "EXPORTED",
        "checks": {"strict_timestamp_ordering": True},
        "observations": canonical_observations,
        "truth_rows": truth_rows,
    }


def test_evidence_builder_is_deterministic_and_never_promotes() -> None:
    models, observations = _rows()
    first = build_calibration_evidence(models, observations, as_of_utc="2026-07-07T00:00:00Z", target_timezone="America/New_York")
    second = build_calibration_evidence(models, observations, as_of_utc="2026-07-07T00:00:00Z", target_timezone="America/New_York")
    assert first == second
    assert first["checks"]["promotion_allowed"] is False
    assert first["checks"]["writes_production_db"] is False
    assert first["checks"]["calls_order_gateway"] is False
    assert first["checks"]["candidate_artifact_count_positive"] is True
    assert first["calibration_window"]["state"] == "CANDIDATE_SAMPLE_READY"
    assert first["calibration_window"]["closed_settlement_event_day_count"] == 5
    assert all(item["promoted"] is False for item in first["calibration_artifacts"] + first["ensemble_artifacts"])
    assert len(first["ensemble_artifacts"][0]["candidate_models"]) >= 2
    assert all(
        len(weights) >= 2
        for weights in first["ensemble_artifacts"][0]["supported_masks"].values()
    )
    assert len(first["ensemble_selection_reports"][0]["selected_models"]) >= 2
    assert len(first["ensemble_selection_reports"][0]["candidate_scores"]) == 7
    assert all(
        row["formal_selection_eligible"] is False
        and row["candidate_role"] == "single_model_ablation"
        for row in first["ensemble_selection_reports"][0]["candidate_scores"]
        if len(row["models"]) == 1
    )
    assert first["promotion_decisions"]
    assert {item["decision"] for item in first["promotion_decisions"]} == {"REJECTED"}
    assert all(item["production_authority"] is False for item in first["promotion_decisions"])


def test_evidence_builder_hash_binds_source_content() -> None:
    models, observations = _rows()
    first = build_calibration_evidence(models, observations, as_of_utc="2026-07-07T00:00:00Z", target_timezone="America/New_York")
    models[0] = {**models[0], "temperature_2m_c": "99"}
    second = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )
    assert first["receipt_sha256"] != second["receipt_sha256"]


def test_new_ensemble_is_parent_linked_and_weight_delta_bounded() -> None:
    models, observations = _rows()
    first = build_calibration_evidence(
        models, observations, as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )
    previous = dict(first["ensemble_artifacts"][0])
    previous.update({
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-05-20",
        "recent_start_date": "2026-05-14",
        "recent_end_date": "2026-05-20",
        "holdout_start_date": "2026-05-21",
        "holdout_end_date": "2026-05-31",
        "valid_from_utc": "2026-06-01T00:00:00Z",
        "valid_until_utc": "2026-07-31T00:00:00Z",
        "effective_sample_n": 20.0,
    })
    previous.pop("content_sha256")
    previous["content_sha256"] = content_sha256(previous)
    second = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
        previous_ensemble_artifacts=[previous],
    )
    current = second["ensemble_artifacts"][0]
    selection = second["ensemble_selection_evidence"][0]
    assert current["previous_ensemble_artifact_sha256"] == previous["content_sha256"]
    assert selection["previous_ensemble_artifact_sha256"] == previous["content_sha256"]
    assert selection["actual_max_weight_delta"] <= selection["max_weight_delta"]
    assert current["selection_evidence_sha256"] == selection["content_sha256"]


def test_overlapping_previous_holdout_cannot_constrain_a_reused_holdout() -> None:
    models, observations = _rows()
    first = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )
    previous = []
    for raw in first["ensemble_artifacts"]:
        item = dict(raw)
        item["effective_sample_n"] = 20.0
        item["train_start_date"] = "2026-06-20"
        item["recent_start_date"] = "2026-06-26"
        item["train_end_date"] = "2026-06-28"
        item["recent_end_date"] = "2026-06-28"
        item["holdout_start_date"] = "2026-06-29"
        item["holdout_end_date"] = "2026-07-06"
        item.pop("content_sha256")
        item["content_sha256"] = content_sha256(item)
        previous.append(item)

    with pytest.raises(ContractError, match="previous_ensemble_holdout_overlap"):
        build_calibration_evidence(
            models,
            observations,
            as_of_utc="2026-07-07T00:00:00Z",
            target_timezone="America/New_York",
            previous_ensemble_artifacts=previous,
        )


def test_residual_pairing_waits_for_settlement_close_not_model_initialization() -> None:
    models, observations = _rows()
    evidence = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )

    first = next(
        row for row in evidence["residual_evidence"]
        if row["model_id"] == "ECMWF" and row["station_id"] == "ZBAA"
    )
    assert first["paired_asof_utc"] == "2026-07-02T04:00:00Z"
    assert first["paired_asof_utc"] != "2026-06-30T00:00:00Z"
    report = evidence["oos_reports"][0]
    assert report["strict_asof_train_holdout_disjoint"] is True
    assert report["trained_through_utc"] <= report["training_cutoff_utc"]
    assert report["train_end_date"] < report["holdout_start_date"]
    assert evidence["checks"]["residual_pairing_strict_asof"] is True
    assert evidence["checks"]["train_holdout_disjoint"] is True


def test_model_availability_after_asof_fails_closed_even_without_receipt_pair() -> None:
    models, observations = _rows()
    models[0]["published_at_utc"] = "2026-07-08T00:00:00Z"

    with pytest.raises(ContractError, match="calibration_model_available_after_asof"):
        build_calibration_evidence(
            models,
            observations,
            as_of_utc="2026-07-07T00:00:00Z",
            target_timezone="America/New_York",
        )


def test_delayed_truth_is_purged_from_training_before_holdout_cutoff() -> None:
    models, observations = _rows()
    evidence = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
        canonical_observation_evidence=_canonical(
            observations,
            # July 3 truth was committed after the July 5 holdout began.
            committed_overrides={"2026-07-03": "2026-07-05T05:00:00Z"},
        ),
        station_lane_map={"ZBAA": "amsc"},
    )

    assert {report["train_n"] for report in evidence["oos_reports"]} == {3}
    assert all(
        report["trained_through_utc"] <= report["training_cutoff_utc"]
        for report in evidence["oos_reports"]
    )
    assert evidence["ensemble_selection_reports"][0][
        "strict_asof_train_holdout_disjoint"
    ] is True


def test_canonical_truth_committed_after_asof_fails_closed() -> None:
    models, observations = _rows()
    canonical = _canonical(
        observations,
        committed_overrides={"2026-07-01": "2026-07-08T00:00:00Z"},
    )

    with pytest.raises(
        ContractError,
        match="canonical_observation_available_after_asof",
    ):
        build_calibration_evidence(
            models,
            observations,
            as_of_utc="2026-07-07T00:00:00Z",
            target_timezone="America/New_York",
            canonical_observation_evidence=canonical,
            station_lane_map={"ZBAA": "amsc"},
        )


def test_prospective_arrival_evidence_is_bound_to_model_rows() -> None:
    models, observations = _rows()
    arrivals: list[dict[str, object]] = []
    for row in models:
        run = row["model_run_initialization_utc"]
        published = datetime.fromisoformat(str(run).replace("Z", "+00:00")) + timedelta(hours=1)
        received = published + timedelta(minutes=5)
        provider = row["provider_id"]
        model = {
            "ecmwf_ifs025_official": "ECMWF",
            "gfs_0p25_official": "GFS",
            "icon_global_official": "ICON",
        }[provider]
        arrivals.append({
            "arrival_id": f"arrival-{provider}-{row['event_date']}",
            "provider_id": provider,
            "model_id": model,
            "station_id": row["station_id"],
            "target_local_date": row["event_date"],
            "target_timezone": "America/New_York",
            "run_initialization_utc": run,
            "valid_time_utc": row["valid_time_utc"],
            "published_at_utc": published.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "received_at_utc": received.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "decision_at_utc": "2026-07-07T00:00:00Z",
            "publication_time_source": "provider_http_last_modified",
            "source_url": "https://example.invalid/native-model.grib2",
            "raw_payload_sha256s": [SHA],
            "receipt_sha256": SHA,
            "capture_mode": "prospective",
            "historical_arrival_time_observed": True,
            "aggregator_or_republisher": False,
            "open_meteo": False,
        })

    evidence = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
        model_arrival_evidence={"arrivals": arrivals},
    )

    assert evidence["checks"]["published_receive_timestamps_complete"] is True
    assert {row["asof_status"] for row in evidence["calibration_model_rows"]} == {"PROSPECTIVE_STRICT_ASOF"}
    assert all(row["prospective_strict_asof_eligible"] for row in evidence["calibration_model_rows"])
    assert evidence["source_quality"]["model_arrival_source"] == "PROSPECTIVE_NATIVE_PROVIDER_RECEIPTS"


def test_three_model_join_does_not_require_equal_provider_publication_times() -> None:
    models, observations = _rows()
    publication_by_provider = {
        "ecmwf_ifs025_official": ("2026-07-01T01:00:00Z", "2026-07-01T01:05:00Z"),
        "gfs_0p25_official": ("2026-07-01T02:00:00Z", "2026-07-01T02:05:00Z"),
        "icon_global_official": ("2026-07-01T03:00:00Z", "2026-07-01T03:05:00Z"),
    }
    for row in models:
        published, received = publication_by_provider[row["provider_id"]]
        row["published_at_utc"] = published
        row["received_at_utc"] = received

    evidence = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )

    assert evidence["checks"]["model_provider_set_complete"] is True
    assert evidence["checks"]["multi_model_available"] is True
    assert evidence["checks"]["published_receive_timestamps_complete"] is True
    first_day = {
        row["model_id"]: (row["published_at_utc"], row["received_at_utc"])
        for row in evidence["calibration_model_rows"]
        if row["target_local_date"] == "2026-07-01"
    }
    assert first_day == {
        "ECMWF": ("2026-07-01T01:00:00Z", "2026-07-01T01:05:00Z"),
        "GFS": ("2026-07-01T02:00:00Z", "2026-07-01T02:05:00Z"),
        "ICON": ("2026-07-01T03:00:00Z", "2026-07-01T03:05:00Z"),
    }


def test_model_arrival_lineage_mismatch_fails_closed() -> None:
    models, observations = _rows()
    arrival = {
        "arrival_id": "arrival-bad-lineage",
        "provider_id": "ecmwf_ifs025_official",
        "model_id": "ECMWF",
        "station_id": "ZBAA",
        "target_local_date": "2026-07-01",
        "target_timezone": "America/New_York",
        "run_initialization_utc": "2026-06-30T00:00:00Z",
        "valid_time_utc": "2026-07-01T12:00:00Z",
        "published_at_utc": "2026-07-01T01:00:00Z",
        "received_at_utc": "2026-07-01T01:05:00Z",
        "decision_at_utc": "2026-07-07T00:00:00Z",
        "publication_time_source": "provider_http_last_modified",
        "source_url": "https://example.invalid/native-model.grib2",
        "raw_payload_sha256s": ["b" * 64],
        "receipt_sha256": SHA,
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
    }
    with pytest.raises(ContractError, match="model_arrival_raw_payload_lineage_mismatch"):
        build_calibration_evidence(
                models,
                observations,
                as_of_utc="2026-07-07T00:00:00Z",
                target_timezone="America/New_York",
                model_arrival_evidence=[arrival],
        )


def test_open_et_day_is_quarantined_even_when_utc_date_has_advanced() -> None:
    models, observations = _rows()
    evidence = build_calibration_evidence(
        models,
        observations,
        # 03:30Z is still 23:30 ET on July 6; July 6 has not closed.
        as_of_utc="2026-07-07T03:30:00Z",
        target_timezone="America/New_York",
    )
    assert all(row["target_local_date"] != "2026-07-06" for row in evidence["calibration_model_rows"])
    boundary = evidence["temporal_boundary"]
    assert boundary["closed_settlement_event_days_only"] is True
    assert any(
        item["event_date"] == "2026-07-06" and item["state"] == "open"
        for item in boundary["quarantined_non_closed_model_days"]
    )


def test_single_closed_day_is_explicitly_blocked_as_calibration_window_insufficient() -> None:
    models, observations = _rows()
    models = [row for row in models if row["event_date"] == "2026-07-01"]
    observations = [row for row in observations if row["event_date"] == "2026-07-01"]
    evidence = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )
    window = evidence["calibration_window"]
    assert window["state"] == "INSUFFICIENT_CLOSED_SETTLEMENT_HISTORY"
    assert window["closed_settlement_event_day_count"] == 1
    assert window["joined_station_day_count"] == 1
    assert window["insufficient_scope_count"] == 3
    assert evidence["checks"]["calibration_window_sufficient"] is False
    assert evidence["calibration_artifacts"] == []


def test_two_model_history_fits_formal_unpromoted_ensemble_candidate() -> None:
    models, observations = _rows()
    models = [row for row in models if row["provider_id"] != "icon_global_official"]
    evidence = build_calibration_evidence(
        models,
        observations,
        as_of_utc="2026-07-07T00:00:00Z",
        target_timezone="America/New_York",
    )

    assert evidence["calibration_window"]["state"] == "CANDIDATE_SAMPLE_READY"
    assert evidence["calibration_window"]["eligible_scope_count"] == 2
    assert {row["model_id"] for row in evidence["calibration_artifacts"]} == {"ECMWF", "GFS"}
    assert len(evidence["ensemble_artifacts"]) == 1
    assert evidence["ensemble_artifacts"][0]["candidate_models"] == [
        "ECMWF",
        "GFS",
    ]
    assert evidence["ensemble_artifacts"][0]["promoted"] is False
    assert evidence["checks"]["candidate_artifact_count_positive"] is True
    assert evidence["checks"]["model_provider_set_complete"] is True
    assert evidence["checks"]["strict_asof_replay_pass"] is True
    assert evidence["checks"]["promotion_allowed"] is False
