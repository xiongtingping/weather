from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from scripts.build_bayes_basket_target_temporary_awc_calibration_base import (
    DEFAULT_AUDIT_ROOT,
    DEFAULT_MARKET_REGISTRY,
    DEFAULT_STATION_LANE_MAP,
    TEMPORARY_AWC_LABEL,
    build_temporary_awc_calibration_base_packet,
    main,
)
from bayes_basket_target.contracts import content_sha256


def test_builds_9_station_packet_with_canonical_amsc_registry_and_temporary_awc_truth() -> None:
    packet = build_temporary_awc_calibration_base_packet()

    assert packet["schema"] == "bayes_basket_target_calibration_evidence_v1"
    assert packet["stats"]["station_count"] == 9
    assert packet["stats"]["truth_row_count"] == 214
    assert packet["stats"]["model_row_count"] == 2140
    assert packet["stats"]["residual_row_count"] == 2140
    assert packet["stats"]["scope_count"] == 428
    assert packet["stats"]["min_models_per_scope"] == 5
    assert packet["stats"]["max_models_per_scope"] == 5
    assert packet["scope"]["included_station_ids"] == [
        "RCSS",
        "ZBAA",
        "ZGGG",
        "ZGSZ",
        "ZHHH",
        "ZSPD",
        "ZSQD",
        "ZUCK",
        "ZUUU",
    ]
    assert {row["source_lane"] for row in packet["station_registry"]} == {"AMSC"}
    assert {row["source_role"] for row in packet["station_registry"]} == {
        "CANONICAL_AMSC_AWC_INGRESS"
    }

    assert packet["checks"]["truth_source_canonical_ingress"] is False
    assert packet["checks"]["temporary_awc_calibration_label_explicit"] is True
    assert packet["boundary"]["enters_realtime_truth"] is False
    assert packet["boundary"]["enters_p1"] is False
    assert packet["boundary"]["enters_hard_no"] is False

    truth_rows = packet["truth_evidence"]
    assert truth_rows
    assert {row["source_lane"] for row in truth_rows} == {"AWC"}
    assert {row["source_role"] for row in truth_rows} == {TEMPORARY_AWC_LABEL}
    assert all(row["whole_hour_observations_only"] is True for row in truth_rows)
    assert all(len(row["observation_ids"]) == len(row["observation_raw_sha256s"]) for row in truth_rows)
    assert all(len(row["content_sha256"]) == 64 for row in truth_rows)

    model_rows = packet["calibration_model_rows"]
    residual_rows = packet["residual_evidence"]
    assert model_rows
    assert len(model_rows) == len(residual_rows)
    assert {row["source_class"] for row in model_rows} == {"aggregator_republisher"}
    assert {row["asof_status"] for row in model_rows} == {"HISTORICAL_REPLAY_ONLY"}
    assert {row["prospective_strict_asof_eligible"] for row in model_rows} == {False}
    assert {row["historical_replay_eligible"] for row in model_rows} == {True}
    assert all(row["capture_response_sha256"] == row["source_object_sha256"] for row in model_rows)
    assert all("#captures/" in row["raw_object_ref"] for row in model_rows)

    scope_counts = Counter(
        (
            row["station_id"],
            row["target_local_date"],
            row["lead_bucket"],
            row["maturity_key"],
        )
        for row in model_rows
    )
    assert min(scope_counts.values()) >= 2
    assert packet["stats"]["min_models_per_scope"] >= 2


def test_packet_is_content_addressed_and_ingress_validates_without_write() -> None:
    packet = build_temporary_awc_calibration_base_packet()

    assert packet["content_sha256"]
    assert packet["packet_id"] == (
        f"temp-awc-calibration-base-{packet['content_sha256'][:16]}"
    )
    assert packet["content_sha256"] == content_sha256({
        key: value
        for key, value in packet.items()
        if key not in {"content_sha256", "packet_id"}
    })
    assert packet["validation_preview"]["accepted"] is True
    assert packet["validation_preview"]["state"] == "VALIDATED_NO_WRITE"
    assert packet["validation_preview"]["validated_counts"] == {
        "stations": 9,
        "truths": 214,
        "truth_references": 0,
        "model_runs": 2140,
        "model_references": 0,
        "residuals": 2140,
    }

    provider_ids = {row["provider_id"] for row in packet["calibration_model_rows"]}
    assert provider_ids == {
        "cma_grapes_global_open_meteo",
        "ecmwf_ifs_open_meteo_previous_runs",
        "gem_global_open_meteo",
        "gfs_global_open_meteo_previous_runs",
        "icon_global_open_meteo",
    }


def test_cli_writes_output_only_under_requested_path(tmp_path: Path, capsys) -> None:
    output_path = tmp_path / "temporary_awc_calibration_base.json"

    rc = main(
        [
            "--audit-root",
            str(DEFAULT_AUDIT_ROOT),
            "--station-lane-map",
            str(DEFAULT_STATION_LANE_MAP),
            "--market-registry",
            str(DEFAULT_MARKET_REGISTRY),
            "--output",
            str(output_path),
        ]
    )

    assert rc == 0
    assert output_path.is_file()
    payload = json.loads(output_path.read_text())
    assert payload["validation_preview"]["state"] == "VALIDATED_NO_WRITE"
    assert payload["scope"]["authority_scope_intent"] == "NO_LIVE_REPLAY_CANDIDATE"

    stdout = json.loads(capsys.readouterr().out)
    assert stdout["output"] == str(output_path.resolve())
    assert stdout["validation_blocker"] is None
    assert stdout["live_order_authorized"] is False
