from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from bayes_basket_target.p1_replay_source_authority import (
    build_awc_official_p1_replay_authority,
)
from scripts.audit_p1_historical_replay_authority import build_audit


EVIDENCE_SHA = "a" * 64
SNAPSHOT_SHA = "b" * 64
RAW_SHA = "c" * 64
STATION = "RKPK"
EVENT_DATE = "2026-07-27"
VALID_AT = "2026-07-27T10:00:00Z"
DECISION_AT = "2026-07-27T10:10:00Z"
RAW_TEXT = "RKPK 271000Z 00000KT CAVOK 30/20 Q1010"


def _observation(*, source_poll_id: str = "awc-poll-1") -> dict[str, object]:
    return {
        "observation_id": "awc-rkpk-1000",
        "station_id": STATION,
        "event_date_local": EVENT_DATE,
        "source_lane": "AWC",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "raw_sha256": RAW_SHA,
        "valid_at_utc": VALID_AT,
        "source_poll_id": source_poll_id,
        "correction_suspected": False,
    }


def _authority() -> dict[str, object]:
    return build_awc_official_p1_replay_authority(
        authority_id="awc-rkpk-20260727-v1",
        station_id=STATION,
        event_date=EVENT_DATE,
        canonical_evidence_sha256=EVIDENCE_SHA,
        source_snapshot_sha256=SNAPSHOT_SHA,
        observations=[
            {
                "observation_id": "awc-rkpk-1000",
                "station_id": STATION,
                "event_date_local": EVENT_DATE,
                "source_lane": "AWC",
                "report_type": "METAR",
                "raw_sha256": RAW_SHA,
                "published_at_utc": VALID_AT,
                "received_at_utc": "2026-07-27T10:05:00Z",
                "correction_status": "NONE",
                "manual_review_status": "NOT_REQUIRED",
                "manual_review_receipt_sha256": None,
            }
        ],
    )


def _write_canonical(
    path: Path,
    *,
    source_poll_id: str = "awc-poll-1",
    include_authority: bool = True,
) -> None:
    value: dict[str, object] = {
        "schema": "bayes_basket_target_canonical_observation_evidence_v1",
        "state": "EXPORTED",
        "evidence_sha256": EVIDENCE_SHA,
        "observations": [_observation(source_poll_id=source_poll_id)],
    }
    if include_authority:
        value["p1_replay_source_authority"] = _authority()
    path.write_text(json.dumps(value), encoding="utf-8")


def _init_archive(
    path: Path,
    *,
    source_poll_id: str = "awc-poll-1",
    receipt_at: str = "2026-07-27T10:05:00Z",
) -> None:
    poll = {
        "source_lane": "AWC",
        "transport": "awc_metar_cacheless_v1",
        "reports": [
            {
                "raw_text": RAW_TEXT,
                "source_report_time": VALID_AT,
            }
        ],
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE reported_trajectory_observations(
                observation_id TEXT PRIMARY KEY,
                source_lane TEXT NOT NULL,
                station_id TEXT NOT NULL,
                report_type TEXT NOT NULL,
                valid_at TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                canonical_committed_at TEXT NOT NULL,
                source_poll_id TEXT NOT NULL,
                correction_suspected INTEGER NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE reported_trajectory_source_polls(
                poll_id TEXT PRIMARY KEY,
                source_lane TEXT NOT NULL,
                response_completed_at TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                error_code TEXT,
                poll_json TEXT NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO reported_trajectory_source_polls(
                poll_id,source_lane,response_completed_at,response_status,error_code,poll_json
            ) VALUES(?,?,?,?,?,?)""",
            (source_poll_id, "awc", receipt_at, 200, None, json.dumps(poll)),
        )
        conn.execute(
            """INSERT INTO reported_trajectory_observations(
                observation_id,source_lane,station_id,report_type,valid_at,raw_sha256,
                raw_text,first_seen_at,canonical_committed_at,source_poll_id,correction_suspected
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "rtobs-rkpk-1000",
                "awc",
                STATION,
                "METAR",
                VALID_AT,
                RAW_SHA,
                RAW_TEXT,
                receipt_at,
                receipt_at,
                source_poll_id,
                0,
            ),
        )


def _audit(tmp_path: Path, **archive_kwargs: object) -> dict[str, object]:
    canonical_path = tmp_path / "canonical.json"
    archive_path = tmp_path / "archive.db"
    _write_canonical(canonical_path)
    _init_archive(archive_path, **archive_kwargs)
    return build_audit(
        canonical_evidence_path=canonical_path,
        archive_db_path=archive_path,
        station_id=STATION,
        event_date=EVENT_DATE,
        decision_at_utc=DECISION_AT,
    )


def test_late_retained_receipt_and_poll_mismatch_cannot_be_backfilled_as_authority(
    tmp_path: Path,
) -> None:
    audit = _audit(
        tmp_path,
        source_poll_id="rtpoll-late-copy",
        receipt_at="2026-07-27T10:10:01Z",
    )

    assert audit["state"] == "BLOCKED_RETAINED_AWC_SOURCE_RECEIPT_UNBINDABLE"
    assert audit["strict_asof_bindable_observation_count"] == 0
    assert audit["p1_replay_authority_eligible"] is False
    assert audit["authority_validation"]["state"] == "AUTHORITY_OBJECT_VALID"
    assert audit["blockers"] == [
        "canonical_source_poll_identity_mismatch",
        "source_receipt_after_decision",
    ]


def test_timely_matching_retained_source_still_blocks_without_authority_object(
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical.json"
    archive_path = tmp_path / "archive.db"
    _write_canonical(canonical_path, include_authority=False)
    _init_archive(archive_path)

    audit = build_audit(
        canonical_evidence_path=canonical_path,
        archive_db_path=archive_path,
        station_id=STATION,
        event_date=EVENT_DATE,
        decision_at_utc=DECISION_AT,
    )

    assert audit["state"] == "BLOCKED_AUTHORITY_OBJECT_MISSING"
    assert audit["strict_asof_bindable_observation_count"] == 1
    assert audit["p1_replay_authority_eligible"] is False
    assert audit["authority_validation"] == {
        "state": "AUTHORITY_OBJECT_MISSING",
        "blockers": ["authority_object_missing"],
    }
    assert audit["blockers"] == ["authority_object_missing"]


def test_timely_exact_retained_awc_source_and_valid_authority_are_eligible(
    tmp_path: Path,
) -> None:
    audit = _audit(tmp_path)

    assert audit["state"] == "FORMAL_AWC_HISTORICAL_REPLAY_AUTHORITY_VALID"
    assert audit["formal_observation_count"] == 1
    assert audit["strict_asof_bindable_observation_count"] == 1
    assert audit["all_retained_source_rows_bindable"] is True
    assert audit["p1_replay_authority_eligible"] is True
    assert audit["blockers"] == []
