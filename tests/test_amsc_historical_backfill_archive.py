from __future__ import annotations

import gzip
import json
from datetime import timedelta
from pathlib import Path

from amsc_historical_backfill_archive import (
    audit_amsc_historical_backfill_archive,
    build_amsc_historical_daily_coverage,
    historical_station_day_query_contract,
    persist_amsc_historical_backfill,
    prepare_amsc_historical_backfill,
)
from zuuu_metar_reporting_schedule import EXPECTED_REGULAR_SLOT_COUNT, expected_regular_metar_slots_utc


def _payload_for_station_day(query: dict) -> dict:
    from datetime import datetime

    rows = []
    for index, valid_text in enumerate(expected_regular_metar_slots_utc("2025-07-05")):
        valid_at = datetime.fromisoformat(valid_text.replace("Z", "+00:00"))
        rows.append(
            {
                "cccc": "ZUUU",
                "airportName": "成都双流机场",
                "content": (
                    f"METAR ZUUU {valid_at.day:02d}{valid_at.hour:02d}{valid_at.minute:02d}Z "
                    f"00000MPS CAVOK {20 + index % 10:02d}/20 Q1000 NOSIG="
                ),
            }
        )
    # A same-day special report must be retained as H but never used to fill
    # one of the scheduled regular-METAR primary slots.
    rows.append({"cccc": "ZUUU", "content": "SPECI ZUUU 050601Z 00000MPS 9999 25/20 Q1000="})
    return {"code": 200, "msg": "操作成功", "data": rows}


def test_station_day_query_uses_utc_window_for_shanghai_physical_date():
    query = historical_station_day_query_contract(event_date="2025-07-05")

    assert query["ok"] is True
    assert query["utc_start"] == "2025-07-04T16:00:00Z"
    assert query["utc_end"] == "2025-07-05T15:59:59Z"
    assert query["canonical_t1_eligible"] is False
    assert query["p10_latency_eligible"] is False


def test_historical_backfill_binds_old_report_year_to_query_not_download_time(tmp_path: Path):
    query = historical_station_day_query_contract(event_date="2025-07-05")
    prepared = prepare_amsc_historical_backfill(
        payload={
            "code": 200,
            "data": [
                {"cccc": "ZUUU", "content": "METAR ZUUU 050600Z 00000MPS CAVOK 25/20 Q1000 NOSIG="},
            ],
        },
        query=query,
        query_received_at="2026-07-12T06:00:01Z",
    )

    assert prepared["ok"] is True
    assert prepared["records"][0]["verified_valid_time_utc"] == "2025-07-05T06:00:00Z"
    assert prepared["records"][0]["first_historical_query_received_at"] == "2026-07-12T06:00:01Z"
    assert "first_seen_at" not in prepared["records"][0]
    assert prepared["boundary"]["creates_canonical_t1"] is False


def test_historical_backfill_is_append_only_complete_and_not_live_evidence(tmp_path: Path):
    query = historical_station_day_query_contract(event_date="2025-07-05")
    prepared = prepare_amsc_historical_backfill(
        payload=_payload_for_station_day(query),
        query=query,
        query_received_at="2026-07-12T06:00:01Z",
    )
    assert prepared["ok"] is True
    assert prepared["returned_raw_count"] == EXPECTED_REGULAR_SLOT_COUNT + 1

    first = persist_amsc_historical_backfill(archive_dir=tmp_path, prepared=prepared)
    second = persist_amsc_historical_backfill(archive_dir=tmp_path, prepared=prepared)
    audit = audit_amsc_historical_backfill_archive(tmp_path)
    coverage = build_amsc_historical_daily_coverage(tmp_path, event_date="2025-07-05")

    assert first["objects_written"] == EXPECTED_REGULAR_SLOT_COUNT + 1
    assert second["objects_written"] == 0
    assert second["objects_reused"] == EXPECTED_REGULAR_SLOT_COUNT + 1
    assert audit["ok"] is True
    assert coverage["state"] == "complete"
    assert coverage["offline_physical_sequence_eligible"] is True
    assert coverage["strict_asof_or_live_replay_eligible"] is False
    assert coverage["strict_asof_or_live_replay_na_reason"] == "historical_query_retrieval_has_no_forward_arrival_evidence"

    object_path = next((tmp_path / "objects").glob("**/*.json.gz"))
    with gzip.open(object_path, "rt", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["historical_data_class"] == "H"
    assert stored["canonical_t1_eligible"] is False
    assert stored["p10_latency_eligible"] is False
    assert "first_seen_at" not in stored
    assert "as_of_eligible_at" not in stored


def test_historical_backfill_rejects_credential_bearing_query_and_does_not_prepare():
    query = historical_station_day_query_contract(event_date="2025-07-05")
    query["token"] = "must-never-be-persisted"
    prepared = prepare_amsc_historical_backfill(
        payload={"code": 200, "data": [{"cccc": "ZUUU", "content": "METAR ZUUU 050600Z 00000MPS CAVOK 25/20 Q1000="}]},
        query=query,
        query_received_at="2026-07-12T06:00:01Z",
    )

    assert prepared["ok"] is False
    assert "historical_query_sensitive_key_forbidden" in prepared["block_reasons"]
