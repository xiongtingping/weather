from __future__ import annotations

import json
import sqlite3

from amsc_raw_history_archive import archive_amsc_poll, raw_text_sha256
from source_observation_runtime import build_source_observation
from strict_amsc_hard_observation_reader import load_strict_amsc_primary_hard_observations


def _initialize_source_observations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT PRIMARY KEY, source_id TEXT, source_class TEXT,
            station_id TEXT, event_date TEXT, valid_time TEXT, fetched_at TEXT,
            temperature_c REAL, raw_ref TEXT, raw_sha256 TEXT, parser_version TEXT,
            observation_json TEXT, created_at TEXT, source_observation_id TEXT UNIQUE,
            role TEXT, valid_time_local TEXT, parsed_at TEXT,
            response_completed_at TEXT, response_duration_ms REAL, dewpoint_c REAL,
            observed_today_max_c REAL, forecast_peak_c REAL, hard_eligible INTEGER,
            demotion_reason TEXT, quality_flags_json TEXT
        )
        """
    )


def _insert(conn: sqlite3.Connection, observation: dict) -> None:
    columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(source_observations)")]
    values = {
        **observation,
        "created_at": observation["fetched_at"],
        "quality_flags_json": json.dumps(observation.get("quality_flags") or []),
        "observation_json": json.dumps(observation, ensure_ascii=False, sort_keys=True),
    }
    conn.execute(
        f"INSERT INTO source_observations({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [values.get(column) for column in columns],
    )


def _strict_primary(tmp_path, *, report_type: str = "METAR") -> dict:
    raw_text = (
        "METAR ZUUU 111600Z 02005MPS 9999 SCT050 27/22 Q1003 NOSIG="
        if report_type == "METAR"
        else "SPECI ZUUU 111622Z 02005MPS 9999 SCT050 27/22 Q1003 NOSIG="
    )
    valid_time = "2026-07-11T16:00:00Z" if report_type == "METAR" else "2026-07-11T16:22:00Z"
    first_seen = "2026-07-11T16:00:55Z" if report_type == "METAR" else "2026-07-11T16:22:55Z"
    archived = archive_amsc_poll(
        archive_dir=tmp_path,
        source_payloads=[
            {
                "source_id": "zuuu_metar_raw_amsc",
                "station_id": "ZUUU",
                "report_type": report_type,
                "raw_text": raw_text,
                "verified_metar_valid_time": valid_time,
                "valid_time_local": "2026-07-12T00:00:00+08:00",
            }
        ],
        poll_started_at="2026-07-11T16:00:50Z",
        response_started_at="2026-07-11T16:00:52Z",
        response_completed_at=first_seen,
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="amsc_zuuu_metar_taf_fetcher_v1",
    )
    raw_sha = raw_text_sha256(raw_text)
    payload = {
        "report_type": report_type,
        "raw_text": raw_text,
        "raw_object_ref": archived["raw_objects_by_sha256"][raw_sha]["raw_object_ref"],
        "first_seen_at": first_seen,
        "as_of_eligible_at": first_seen,
        "arrival_time_quality": "observed_first_seen",
        "transport": {
            "raw_archive_manifest_ref": archived["manifest_ref"],
            "raw_archive_manifest_sha256": archived["manifest_sha256"],
        },
    }
    return build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-12",
        valid_time=valid_time,
        valid_time_local="2026-07-12T00:00:00+08:00",
        fetched_at=first_seen,
        parsed_at=first_seen,
        response_completed_at=first_seen,
        temperature_c=27.0,
        dewpoint_c=22.0,
        raw_ref=raw_text,
        raw_payload=raw_text,
        raw_sha256_override=raw_sha,
        parser_version="amsc_zuuu_metar_taf_fetcher_v1",
        payload=payload,
    ).as_dict()


def test_reader_returns_only_raw_object_and_manifest_bound_primary_arrival(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    observation = _strict_primary(tmp_path)
    _insert(conn, observation)

    result = load_strict_amsc_primary_hard_observations(conn, event_date="2026-07-12", archive_dir=tmp_path)

    assert result["ok"] is True, result
    assert result["source_observation_count"] == 1
    source = result["source_observations"][0]
    assert source["report_type"] == "METAR"
    assert source["first_transport_seen_at"] == "2026-07-11T16:00:55Z"
    assert source["first_actionable_seen_at"] == "2026-07-11T16:00:55Z"


def test_reader_quarantines_raw_archive_drift_without_leaking_the_row(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    observation = _strict_primary(tmp_path)
    observation["payload"]["raw_text"] = "tampered"
    _insert(conn, observation)

    result = load_strict_amsc_primary_hard_observations(conn, event_date="2026-07-12", archive_dir=tmp_path)

    assert result["ok"] is False
    assert result["source_observations"] == []
    assert "amsc_payload_raw_text_binding_mismatch" in result["blocked"][0]["block_reasons"]


def test_reader_recovers_speci_identity_from_the_raw_archive(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    observation = _strict_primary(tmp_path, report_type="SPECI")
    observation["payload"].pop("report_type")
    _insert(conn, observation)

    result = load_strict_amsc_primary_hard_observations(conn, event_date="2026-07-12", archive_dir=tmp_path)

    assert result["ok"] is True, result
    assert result["source_observations"][0]["report_type"] == "SPECI"


def test_reader_validates_only_requested_incremental_source_observations(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    observation = _strict_primary(tmp_path)
    _insert(conn, observation)

    selected = load_strict_amsc_primary_hard_observations(
        conn,
        event_date="2026-07-12",
        archive_dir=tmp_path,
        source_observation_ids=[observation["source_observation_id"]],
    )
    missing = load_strict_amsc_primary_hard_observations(
        conn,
        event_date="2026-07-12",
        archive_dir=tmp_path,
        source_observation_ids=["srcobs_not_present"],
    )

    assert selected["ok"] is True
    assert selected["source_observation_count"] == 1
    assert selected["source_observation_id_filter_applied"] is True
    assert selected["requested_source_observation_count"] == 1
    assert missing["ok"] is False
    assert missing["source_observations"] == []
