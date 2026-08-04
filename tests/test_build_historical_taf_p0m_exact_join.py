from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.resource_bounds import write_bounded_json
from scripts import build_historical_taf_p0m_exact_join as pipeline


SCOPE = ("TEST", "JJA", "d2", "18Z")
FORMAL_TAF_AUTHORITY = dict(pipeline.AWC_TAF_AUTHORITY)


def _archive(
    *,
    available: str = "2026-07-21T10:00:00Z",
    blockers: list[dict[str, str]] | None = None,
) -> dict:
    identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": "TEST",
        "settlement_timezone": "Etc/UTC",
        "start_date": "2026-07-22",
        "end_date": "2026-07-22",
        "provider": "NOAA Aviation Weather Center",
        "source_lane": "AWC",
        "truth_label_authority": pipeline.FORMAL_STATION_LANE_TRUTH,
        "truth_authority": FORMAL_TAF_AUTHORITY,
        "samples": [{
            "sample_id": "sample-1",
            "station_id": "TEST",
            "event_date": "2026-07-22",
            "settlement_timezone": "Etc/UTC",
            "source_lane": "AWC",
            "truth_label_authority": pipeline.FORMAL_STATION_LANE_TRUTH,
            "truth_authority": FORMAL_TAF_AUTHORITY,
            "reference_only": False,
            "decision_anchor_utc": "2026-07-21T16:00:00Z",
            "taf": {
                "issue_time_utc": "2026-07-21T09:00:00Z",
                "available_time_utc": available,
                "tx_c": 30.0,
                "tx_valid_at_utc": "2026-07-22T12:00:00Z",
                "cloud_cover_fraction": 0.25,
                "precipitation_probability": 0.0,
                "wind_speed_mps": 3.0,
                "raw_taf_sha256": "a" * 64,
            },
            "truth": {
                "source_lane": "AWC",
                "truth_label_authority": pipeline.FORMAL_STATION_LANE_TRUTH,
            },
            "receipts": {"taf_response_sha256": "b" * 64},
        }],
        "sample_count": 1,
        "excluded_samples": [],
        "excluded_sample_count": 0,
        "blockers": list(blockers or []),
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
            "historical_replay_only": True,
            "truth_days_closed": True,
            "reference_only": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "replacement_required_when_canonical_history_available": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "resource_bounds": {"max_days": 90},
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def _live_inbox() -> dict:
    identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _exact_join() -> dict:
    identity = {
        "schema": "taf_native_ensemble_strict_oos_exact_join_v1",
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        "nwp_crossfit_prediction_artifact_sha256": "c" * 64,
        "sample_count": 24,
        "samples": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _report(*, sample_n: int = 24, validated: bool = True) -> dict:
    identity = {
        "schema": "taf_nwp_transport_oos_evidence_v1",
        "state": "PASS" if validated else "NA",
        "validated": validated,
        "sample_n": sample_n,
        "gaps": [] if validated else [{
            "code": "taf_native_oos_sample_n_insufficient",
        }],
        "boundary": {
            "strict_time_split": True,
            "writes_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE calibration_historical_taf_capture "
            "(historical_taf_capture_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE sentinel (value TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    archive_path = tmp_path / "AWC_TAF_TEST_20260722_20260722.json"
    live_path = tmp_path / "live_p0.json"
    artifact_path = tmp_path / "taf_artifact.json"
    output_path = tmp_path / "manifest.json"
    write_bounded_json(archive_path, _archive())
    write_bounded_json(live_path, _live_inbox())
    write_bounded_json(artifact_path, {
        "station_id": "TEST",
        "season_bin": "JJA",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
    })
    return archive_path, live_path, artifact_path, output_path


def _patch_pipeline(monkeypatch, *, sample_n: int = 24, validated: bool = True):
    monkeypatch.setattr(pipeline, "_discover_live_scopes", lambda _inbox: [SCOPE])
    monkeypatch.setattr(
        pipeline,
        "_build_exact_join",
        lambda _conn, live_inbox, scope: _exact_join(),
    )
    monkeypatch.setattr(
        pipeline,
        "_taf_oos_report",
        lambda **_kwargs: _report(sample_n=sample_n, validated=validated),
    )


def test_read_only_does_not_ingest_or_mutate_db(tmp_path, monkeypatch) -> None:
    archive_path, live_path, artifact_path, output_path = _write_inputs(tmp_path)
    db_path = tmp_path / "target.db"
    _db(db_path)
    _patch_pipeline(monkeypatch)
    called = []
    monkeypatch.setattr(
        pipeline,
        "ingest_historical_taf_calibration_with_quarantine",
        lambda *_args: called.append(True),
    )

    result = pipeline.main([
        "--db", str(db_path),
        "--live-p0-inbox", str(live_path),
        "--archive", str(archive_path),
        "--taf-artifact", str(artifact_path),
        "--output", str(output_path),
    ])

    assert result == 0
    assert called == []
    assert json.loads(output_path.read_text())["state"] == (
        "VALIDATED_NOT_COMMITTED"
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM sentinel").fetchone() == (
            "unchanged",
        )


def test_commit_calls_atomic_ingest_once_and_is_no_live(tmp_path, monkeypatch) -> None:
    archive_path, live_path, artifact_path, output_path = _write_inputs(tmp_path)
    db_path = tmp_path / "target.db"
    _db(db_path)
    _patch_pipeline(monkeypatch)
    calls = []

    def ingest(conn, archive):
        calls.append((conn, archive["content_sha256"]))
        return {
            "capture_count": archive["sample_count"],
            "wrote_count": 1,
            "replay_count": 0,
            "skipped_after_anchor_count": 0,
        }

    monkeypatch.setattr(
        pipeline,
        "ingest_historical_taf_calibration_with_quarantine",
        ingest,
    )

    result = pipeline.main([
        "--db", str(db_path),
        "--live-p0-inbox", str(live_path),
        "--archive", str(archive_path),
        "--taf-artifact", str(artifact_path),
        "--output", str(output_path),
        "--commit",
    ])

    assert result == 0
    assert len(calls) == 1
    manifest = json.loads(output_path.read_text())
    assert manifest["state"] == "COMMITTED"
    assert manifest["boundary"]["calls_order_gateway"] is False
    assert manifest["boundary"]["orders_submitted"] == 0


def test_twenty_four_sample_gate_cannot_be_lowered(tmp_path, monkeypatch) -> None:
    archive_path, live_path, artifact_path, output_path = _write_inputs(tmp_path)
    db_path = tmp_path / "target.db"
    _db(db_path)
    _patch_pipeline(monkeypatch, sample_n=23, validated=True)
    monkeypatch.setattr(
        pipeline,
        "ingest_historical_taf_calibration_with_quarantine",
        lambda *_args: {
            "capture_count": 1,
            "wrote_count": 1,
            "replay_count": 0,
            "skipped_after_anchor_count": 0,
        },
    )

    result = pipeline.main([
        "--db", str(db_path),
        "--live-p0-inbox", str(live_path),
        "--archive", str(archive_path),
        "--taf-artifact", str(artifact_path),
        "--output", str(output_path),
        "--commit",
    ])

    assert result == 2
    manifest = json.loads(output_path.read_text())
    candidate = manifest["results"][0]["p0m_candidate"]
    assert candidate["state"] == "BLOCKED"
    assert candidate["minimum_sample_n"] == 24
    assert "taf_native_oos_sample_n_insufficient" in candidate["blockers"]


def test_strict_as_of_late_sample_is_excluded_from_candidate(tmp_path) -> None:
    source = tmp_path / "archive.json"
    write_bounded_json(source, _archive(available="2026-07-21T17:00:00Z"))
    archive = pipeline._mapping(
        pipeline.load_bounded_json(source),
        reason="unused",
    )
    metadata = pipeline._validate_archive_contract(archive, source=source)
    assert metadata["strict_asof_skipped_sample_count"] == 1
    assert metadata["candidate_blocker_count"] == 1
    assert metadata["candidate_input_complete"] is False
    assert "historical_taf_strict_asof_violation" in metadata["blocker_codes"]


def test_truth_only_awc_sample_never_becomes_a_taf_p0m_candidate(
    tmp_path: Path,
) -> None:
    archive = _archive()
    archive["samples"] = []
    archive["sample_count"] = 0
    archive["truth_only_samples"] = [{
        "truth_sample_id": "awc-truth-only-test",
        "station_id": "TEST",
        "event_date": "2026-07-22",
        "settlement_timezone": "Etc/UTC",
        "source_lane": "AWC",
        "truth_label_authority": pipeline.FORMAL_STATION_LANE_TRUTH,
        "truth_authority": FORMAL_TAF_AUTHORITY,
        "reference_only": False,
        "truth_only": True,
        "taf_p0m_eligible": False,
        "truth": {"tmax_c": 30.0},
        "truth_recovery": {
            "historical_replay_only": True,
            "automatic_ssot_commit_eligible": False,
            "requires_authoritative_review": True,
            "requires_manual_correction_review": True,
        },
    }]
    archive["truth_only_sample_count"] = 1
    identity = {
        key: value for key, value in archive.items() if key != "content_sha256"
    }
    archive["content_sha256"] = content_sha256(identity)
    source = tmp_path / "truth-only.json"
    write_bounded_json(source, archive)

    metadata = pipeline._validate_archive_contract(archive, source=source)

    assert metadata["truth_only_sample_count"] == 1
    assert metadata["truth_only_manual_review_count"] == 1
    assert metadata["candidate_input_complete"] is False


def test_preflight_rejects_retired_amsc_or_temporary_taf_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "archive.json"
    archive = _archive()
    archive["source_lane"] = "AMSC"
    identity = {
        key: value for key, value in archive.items() if key != "content_sha256"
    }
    archive["content_sha256"] = content_sha256(identity)
    write_bounded_json(source, archive)
    with pytest.raises(RuntimeError, match="awc_formal_authority_required"):
        pipeline._validate_archive_contract(archive, source=source)


def test_candidate_preflight_quarantines_issue_after_availability(
    tmp_path: Path,
) -> None:
    source = tmp_path / "archive.json"
    archive = _archive(available="2026-07-21T08:00:00Z")
    write_bounded_json(source, archive)
    with pytest.raises(RuntimeError, match="strict_asof_violation"):
        pipeline._validate_archive_contract(archive, source=source)

    metadata = pipeline._validate_archive_contract(
        archive,
        source=source,
        candidate_only=True,
    )
    assert metadata["issue_after_availability_sample_count"] == 1
    assert metadata["candidate_blocker_count"] == 1
    assert metadata["candidate_input_complete"] is False
    assert "historical_taf_issue_after_availability" in metadata["blocker_codes"]


def test_unsettled_future_day_does_not_veto_settled_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "archive.json"
    write_bounded_json(source, _archive(blockers=[{
        "event_date": "2026-08-01",
        "reason": (
            "ContractError:awc_taf_calibration_open_truth_day_forbidden"
        ),
    }]))
    archive = pipeline._mapping(
        pipeline.load_bounded_json(source),
        reason="unused",
    )
    metadata = pipeline._validate_archive_contract(archive, source=source)
    assert metadata["blocker_count"] == 1
    assert metadata["candidate_blocker_count"] == 0
    assert metadata["unsettled_future_blocker_count"] == 1
    assert metadata["candidate_input_complete"] is True

    live_path = tmp_path / "live.json"
    write_bounded_json(live_path, _live_inbox())
    db_path = tmp_path / "target.db"
    _db(db_path)
    _patch_pipeline(monkeypatch)

    result = pipeline.run_pipeline(
        db_path=db_path,
        live_p0_inbox_path=live_path,
        archives=[archive],
        archive_metadata=[metadata],
        taf_artifact_by_scope={},
        commit=False,
    )

    assert result["candidate_state"] == "READY"
    assert result["results"][0]["p0m_candidate"]["state"] == "READY"
    assert result["archive_metadata"][0]["unsettled_future_blocker_count"] == 1


def test_station_scope_is_hard_capped_at_forty_five(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "target.db"
    _db(db_path)
    live_path = tmp_path / "live.json"
    write_bounded_json(live_path, _live_inbox())
    scopes = [
        (f"S{index:02d}", "JJA", "d2", "18Z")
        for index in range(46)
    ]
    monkeypatch.setattr(pipeline, "_discover_live_scopes", lambda _inbox: scopes)
    with pytest.raises(RuntimeError, match="live_station_scope_exceeded"):
        pipeline.run_pipeline(
            db_path=db_path,
            live_p0_inbox_path=live_path,
            archives=[],
            archive_metadata=[],
            taf_artifact_by_scope={},
        )
