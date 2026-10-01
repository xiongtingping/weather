from __future__ import annotations

import json

import pytest

from scripts import (
    build_bayes_basket_target_metar_trajectory_training_packet as packet_builder,
)
from scripts.build_bayes_basket_target_metar_trajectory_training_packet import (
    build_training_packet,
)
from bayes_basket_target.contracts import content_sha256


@pytest.fixture(autouse=True)
def _no_archive_reconstruction(tmp_path, monkeypatch):
    """Keep these cases off the real ``data/runtime`` history files.

    The builder falls back to an archive-reconstructed root whenever the
    supplied root carries no complete future curve, which every fixture root
    here does.  Reading the live directory made the outcome depend on which
    stations had been backfilled: these cases passed only while RKPK happened
    to have no reconstruction on disk, and started asserting the wrong root
    the moment one appeared.  Point the lookup at an empty directory so the
    fixtures alone decide.
    """

    monkeypatch.setattr(
        packet_builder, "POSTERIOR_HISTORY_DIR", tmp_path / "posteriors"
    )
    monkeypatch.setattr(
        packet_builder, "CURVE_HISTORY_DIR", tmp_path / "curves"
    )
    packet_builder._HISTORICAL_CACHE.clear()
    yield
    packet_builder._HISTORICAL_CACHE.clear()


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
    assert packet["source_manifest"]["v3_eligible_fold_count"] == 0
    assert packet["source_manifest"][
        "v3_incomplete_future_curve_fold_count"
    ] == 1
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


def test_builder_keeps_forward_rows_as_observational_candidates_only() -> None:
    truths = [
        _truth(20, committed="2026-07-20T15:00:00Z"),
        _truth(21, committed="2026-07-21T15:00:00Z"),
        _truth(22, committed="2026-07-22T15:00:00Z"),
        # The target day is closed by the evaluation cut, but its truth was
        # committed after the cut: it is a future truth candidate, not OOS.
        _truth(24, committed="2026-08-01T15:00:00Z"),
        # The settlement day itself remains open at the evaluation cut.
        _truth(30, committed="2026-07-30T00:00:00Z"),
    ]
    packet = build_training_packet(
        truth_rows=truths,
        observation_rows=[],
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
    manifest = packet["source_manifest"]
    assert manifest["evaluation_as_of_utc"] == "2026-07-30T00:00:00.000Z"
    assert manifest["observational_candidate_count"] == 2
    candidates = {
        row["event_date"]: row for row in manifest["observational_candidates"]
    }
    assert candidates["2026-07-24"]["reason"] == "future_truth_not_oos"
    assert candidates["2026-07-30"]["reason"] == "future_event_not_oos"
    assert all(
        row["evidence_role"] == "observational_candidate_not_oos"
        for row in manifest["observational_candidates"]
    )


def test_paired_reports_for_one_valid_time_collapse_like_settled_truth():
    """26 of 45 stations publish two reports for one valid time.

    The path-state compiler requires strictly increasing observation times, so
    an undeduplicated prefix raised ``metar_path_observation_time_rewind`` and
    took the station's whole packet down -- over half the fleet could not build
    a training packet at all.  Settled truth already answers this case: reports
    agreeing on temperature state the same fact, so either may be kept.
    """

    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_packet_builder_dedupe",
        root / "scripts"
        / "build_bayes_basket_target_metar_trajectory_training_packet.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    agreeing = [
        {"valid_at": "2026-07-18T13:13:00.000Z", "reported_temperature_c": 24.0},
        {"valid_at": "2026-07-18T13:13:00.000Z", "reported_temperature_c": 24.0},
        {"valid_at": "2026-07-18T14:00:00.000Z", "reported_temperature_c": 25.0},
    ]
    rows, ok = module._deduplicate_prefix(agreeing)
    assert ok is True
    assert [row["valid_at"] for row in rows] == [
        "2026-07-18T13:13:00.000Z",
        "2026-07-18T14:00:00.000Z",
    ]

    # A genuine disagreement is not resolvable, and skipping the fold is the
    # bounded response -- the packet keeps every other day.
    conflicting = [
        {"valid_at": "2026-07-18T13:13:00.000Z", "reported_temperature_c": 24.0},
        {"valid_at": "2026-07-18T13:13:00.000Z", "reported_temperature_c": 26.0},
    ]
    rows, ok = module._deduplicate_prefix(conflicting)
    assert ok is False
    assert rows == []


# --------------------------------------------------------------------------
# 2026-10-01 (user: 「上线」): the packet had trained nothing since 09-01.
# --------------------------------------------------------------------------


def _afternoon_packet(root: dict[str, object], *, method: str) -> dict:
    truths = [
        _truth(20, committed="2026-07-20T15:00:00Z"),
        _truth(21, committed="2026-07-21T15:00:00Z"),
        _truth(22, committed="2026-07-22T15:00:00Z"),
        _truth(25, committed="2026-07-25T15:00:00Z"),
    ]
    observations = [
        _observation("1", valid_at="2026-07-25T02:00:00Z", committed_at="2026-07-25T02:05:00Z",
                     temperature_c=33.0),
        _observation("2", valid_at="2026-07-25T03:00:00Z", committed_at="2026-07-25T03:05:00Z",
                     temperature_c=35.0),
    ]
    return build_training_packet(
        truth_rows=truths, observation_rows=observations, probability_rows=[root],
        station_id="RKPK", settlement_timezone="Asia/Seoul", local_time_bucket="AFTERNOON",
        minimum_prior_truths=3, minimum_effective_sample_n=20.0,
        decided_at_utc="2026-07-30T00:00:00Z", valid_from_utc="2026-07-30T00:00:00Z",
        valid_until_utc="2027-07-30T00:00:00Z", generation=1, method=method,
    )


def test_the_physical_method_does_not_read_the_root_curve() -> None:
    """Serving builds the physical path state with no curve; so does the packet.

    A physical fold reading the curve failed every evening whose curve had run
    out of future points (``future_temperature_curve_future_points_insufficient``).
    """

    root = _root()
    lineage = json.loads(root["lineage_json"])
    lineage["conditional_future_temperature_curve_json"] = "not a curve"
    root["lineage_json"] = json.dumps(lineage)
    packet = _afternoon_packet(root, method="physical")
    assert len(packet["folds"]) == 1
    assert packet["folds"][0]["root_checkpoint_id"] == "p0-root-25"
    with pytest.raises(ValueError):
        _afternoon_packet(root, method="hourly_curve")


def test_lineage_references_are_put_back_before_the_packet_reads_them(tmp_path) -> None:
    """Since 926dfa29 the curve is stored as ``@ref/<sha>``; json.loads of that
    reference was the failure that left the packet without folds."""

    import hashlib
    import sqlite3

    from bayes_basket_target.contracts import ContractError
    from bayes_basket_target.materialization import LINEAGE_DOCUMENT_DDL

    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(LINEAGE_DOCUMENT_DDL)
    body = json.dumps({"points": [1, 2, 3]})
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    columns = [r[1] for r in conn.execute("PRAGMA table_info(basket_probability_lineage_document)")]
    values = {"document_sha256": digest, "document": body}
    values.update({c: "2026-10-01T00:00:00Z" for c in columns if c not in values})
    conn.execute(
        f"INSERT INTO basket_probability_lineage_document({','.join(values)})"
        f" VALUES({','.join('?' * len(values))})", tuple(values.values()),
    )
    conn.commit()
    ref = {"conditional_future_temperature_curve_json": f"@ref/{digest}", "kept": 1}
    (row,) = packet_builder._rehydrated(conn, [{"lineage_json": json.dumps(ref)}])
    assert json.loads(row["lineage_json"]) == {
        "conditional_future_temperature_curve_json": body, "kept": 1,
    }
    missing = {"conditional_future_temperature_curve_json": "@ref/" + "0" * 64}
    with pytest.raises(ContractError):
        packet_builder._rehydrated(conn, [{"lineage_json": json.dumps(missing)}])
