from __future__ import annotations

from datetime import datetime

from amsc_historical_backfill_archive import (
    historical_station_day_query_contract,
    persist_amsc_historical_backfill,
    prepare_amsc_historical_backfill,
)
from amsc_historical_physical_x_samples import build_amsc_historical_physical_x_samples
from zuuu_metar_reporting_schedule import EXPECTED_REGULAR_SLOT_COUNT, expected_regular_metar_slots_utc


def _complete_payload(event_date: str) -> dict:
    rows = []
    for index, valid_text in enumerate(expected_regular_metar_slots_utc(event_date)):
        valid = datetime.fromisoformat(valid_text.replace("Z", "+00:00"))
        rows.append({
            "cccc": "ZUUU",
            "content": (
                f"METAR ZUUU {valid.day:02d}{valid.hour:02d}{valid.minute:02d}Z "
                f"00000MPS CAVOK {20 + index % 10:02d}/20 Q1000 NOSIG="
            ),
        })
    return {"code": 200, "data": rows}


def test_complete_h_sequence_builds_auxiliary_physical_x_samples_only(tmp_path):
    event_date = "2025-07-05"
    query = historical_station_day_query_contract(event_date=event_date)
    prepared = prepare_amsc_historical_backfill(
        payload=_complete_payload(event_date),
        query=query,
        query_received_at="2026-07-12T06:00:01Z",
    )
    persist_amsc_historical_backfill(archive_dir=tmp_path, prepared=prepared)

    result = build_amsc_historical_physical_x_samples(tmp_path, event_date=event_date)

    assert result["ok"] is True
    assert result["state"] == "available_auxiliary_only"
    assert result["sample_count"] == EXPECTED_REGULAR_SLOT_COUNT
    assert result["samples"][0]["label"]["physical_x_c"] == 29.0
    assert result["samples"][-1]["label"]["remaining_new_high_c"] == 0.0
    assert result["samples"][1]["features"]["pre_event_observation_count"] == 1
    assert all(sample["eligibility"]["strict_asof_training_eligible"] is False for sample in result["samples"])
    assert all(sample["eligibility"]["p10_latency_evaluation_eligible"] is False for sample in result["samples"])
    assert "physical_x_c" not in result["samples"][0]["features"]


def test_incomplete_h_sequence_cannot_become_auxiliary_physical_x_samples(tmp_path):
    event_date = "2025-07-05"
    query = historical_station_day_query_contract(event_date=event_date)
    payload = _complete_payload(event_date)
    payload["data"] = payload["data"][:-1]
    prepared = prepare_amsc_historical_backfill(
        payload=payload,
        query=query,
        query_received_at="2026-07-12T06:00:01Z",
    )
    persist_amsc_historical_backfill(archive_dir=tmp_path, prepared=prepared)

    result = build_amsc_historical_physical_x_samples(tmp_path, event_date=event_date)

    assert result["ok"] is False
    assert "historical_primary_metar_schedule_coverage_not_complete" in result["block_reasons"]
