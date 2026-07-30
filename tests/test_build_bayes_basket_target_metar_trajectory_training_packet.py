from __future__ import annotations

import json

from scripts.build_bayes_basket_target_metar_trajectory_training_packet import (
    build_training_packet,
)
from bayes_basket_target.contracts import content_sha256


def _truth(day: int, *, committed: str) -> dict[str, object]:
    date = f"2026-07-{day:02d}"
    return {
        "truth_id": f"truth-{day}",
        "station_id": "RKPK",
        "local_date": date,
        "official_tmax_c": 35.0,
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": 0,
        "canonical_committed_at_utc": committed,
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        "content_sha256": f"{day % 10}" * 64,
    }


def _observation(
    suffix: str,
    *,
    valid_at: str,
    committed_at: str,
    temperature_c: float,
    correction: int = 0,
) -> dict[str, object]:
    return {
        "observation_id": f"obs-{suffix}",
        "source_lane": "awc",
        "station_id": "RKPK",
        "report_type": "METAR",
        "valid_at": valid_at,
        "reported_temperature_c": temperature_c,
        "raw_sha256": suffix.rjust(64, "0"),
        "canonical_committed_at": committed_at,
        "correction_suspected": correction,
    }


def _root() -> dict[str, object]:
    return {
        "stage": "P0",
        "checkpoint_id": "p0-root-25",
        "station_id": "RKPK",
        "event_date": "2026-07-25",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-25T01:00:00Z",
        "bucket_ordinals_json": json.dumps(
            {"33C": 0, "34C": 1, "35C": 2}
        ),
        "probabilities_json": json.dumps(
            {"33C": 0.2, "34C": 0.5, "35C": 0.3}
        ),
        "lineage_json": json.dumps({
            "bucket_centers_c_json": json.dumps(
                {"33C": 33.0, "34C": 34.0, "35C": 35.0}
            )
        }),
        "content_sha256": "a" * 64,
    }


def test_builder_uses_only_truth_and_observations_available_at_cutoff() -> None:
    truths = [
        _truth(20, committed="2026-07-20T15:00:00Z"),
        _truth(21, committed="2026-07-21T15:00:00Z"),
        _truth(22, committed="2026-07-22T15:00:00Z"),
        _truth(25, committed="2026-07-25T15:00:00Z"),
    ]
    observations = [
        _observation(
            "1",
            valid_at="2026-07-25T02:00:00Z",
            committed_at="2026-07-25T02:05:00Z",
            temperature_c=33.0,
        ),
        _observation(
            "2",
            valid_at="2026-07-25T03:00:00Z",
            committed_at="2026-07-25T03:05:00Z",
            temperature_c=35.0,
        ),
    ]
    packet = build_training_packet(
        truth_rows=truths,
        observation_rows=observations,
        probability_rows=[_root()],
        station_id="RKPK",
        settlement_timezone="Asia/Seoul",
        local_time_bucket="AFTERNOON",
        minimum_prior_truths=3,
        minimum_effective_sample_n=20.0,
        decided_at_utc="2026-07-30T00:00:00Z",
        valid_from_utc="2026-07-30T00:00:00Z",
        valid_until_utc="2027-07-30T00:00:00Z",
        generation=1,
    )
    assert len(packet["folds"]) == 1
    fold = packet["folds"][0]
    assert fold["target_local_date"] == "2026-07-25"
    assert fold["remaining_warming_c"] == 2.0
    assert fold["root_checkpoint_id"] == "p0-root-25"
    assert fold["truth_bucket"] == "35C"
    assert len(fold["trajectory_training_truths"]) == 3
    assert packet["content_sha256"] == content_sha256({
        key: value
        for key, value in packet.items()
        if key != "content_sha256"
    })


def test_builder_excludes_correction_day() -> None:
    truths = [
        _truth(20, committed="2026-07-20T15:00:00Z"),
        _truth(21, committed="2026-07-21T15:00:00Z"),
        _truth(22, committed="2026-07-22T15:00:00Z"),
        _truth(25, committed="2026-07-25T15:00:00Z"),
    ]
    packet = build_training_packet(
        truth_rows=truths,
        observation_rows=[_observation(
            "3",
            valid_at="2026-07-25T02:00:00Z",
            committed_at="2026-07-25T02:05:00Z",
            temperature_c=33.0,
            correction=1,
        )],
        probability_rows=[],
        station_id="RKPK",
        settlement_timezone="Asia/Seoul",
        local_time_bucket="AFTERNOON",
        minimum_prior_truths=3,
        minimum_effective_sample_n=20.0,
        decided_at_utc="2026-07-30T00:00:00Z",
        valid_from_utc="2026-07-30T00:00:00Z",
        valid_until_utc="2027-07-30T00:00:00Z",
        generation=1,
    )
    assert packet["folds"] == []
    assert any(
        row["reason"] == "correction_suspected"
        for row in packet["source_manifest"]["excluded"]
    )
