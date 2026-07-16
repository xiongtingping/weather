from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from resolution_final_source_runtime import (
    STRICT_AMSC_READER_SCHEMA,
    build_amsc_physical_final_source_observation,
    build_wunderground_settlement_final_source_observation,
)
from resolution_final_source_shadow_writer import (
    SOURCE_TABLE,
    initialize_isolated_resolution_final_source_staging_schema,
    persist_final_source_observation_shadow,
)
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError
from zuuu_metar_reporting_schedule import (
    EXPECTED_REGULAR_SLOT_COUNT,
    SCHEDULE_ID,
    SCHEDULE_SCHEMA,
    expected_regular_metar_slots_utc,
)


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())
DAY = "2026-07-03"


def _strict_result() -> dict:
    rows = []
    for index, valid_text in enumerate(expected_regular_metar_slots_utc(DAY)):
        valid = datetime.fromisoformat(valid_text.replace("Z", "+00:00"))
        rows.append(
            {
                "source_id": "zuuu_metar_raw_amsc",
                "role": "metar_hard_observation_primary",
                "report_type": "METAR",
                "station_id": "ZUUU",
                "event_date": DAY,
                "valid_time": valid.isoformat().replace("+00:00", "Z"),
                "temperature_c": 24.0 + (index % 13),
                "source_observation_id": f"amsc-{index:02d}",
                "raw_sha256": f"{index:064x}",
            }
        )
    return {"schema": STRICT_AMSC_READER_SCHEMA, "ok": True, "event_date": DAY, "source_observations": rows}


def _coverage() -> dict:
    rows = _strict_result()["source_observations"]
    return {
        "event_date": DAY,
        "station_id": "ZUUU",
        "day_state": "forward_arrival_complete_pending_label",
        "distinct_valid_time_count": EXPECTED_REGULAR_SLOT_COUNT,
        "event_raw_object_count": EXPECTED_REGULAR_SLOT_COUNT,
        "correction_count": 0,
        "late_arrival_count": 0,
        "missing_first_seen_count": 0,
        "raw_archive_integrity": {"ok": True},
        "coverage_sha256": "a" * 64,
        "raw_object_sha256_list": [row["raw_sha256"] for row in rows],
        "reporting_schedule_schema": SCHEDULE_SCHEMA,
        "reporting_schedule_id": SCHEDULE_ID,
        "expected_regular_metar_slot_count": EXPECTED_REGULAR_SLOT_COUNT,
        "expected_regular_metar_slots_utc": expected_regular_metar_slots_utc(DAY),
        "missing_regular_metar_slots_utc": [],
        "unexpected_regular_metar_slots_utc": [],
    }


def test_physical_x_requires_closed_complete_strict_half_hour_day() -> None:
    result = build_amsc_physical_final_source_observation(
        event_date=DAY,
        strict_amsc_reader_result=_strict_result(),
        coverage_manifest=_coverage(),
        coverage_ref="coverage_v2/2026/07/03/" + "a" * 64 + ".json",
        finalized_at="2026-07-03T16:01:00Z",
        authority_contract=AUTHORITY,
    )
    assert result["ok"] is True
    observation = result["source_observation"]
    assert observation["payload"]["final_physical_high_c"] == 36.0
    assert observation["payload"]["strict_asof_training_eligible"] is True
    assert observation["payload"]["physical_x_provenance_schema"] == "amsc_station_day_raw_membership_v1"
    assert len(observation["payload"]["physical_source_observation_ids"]) == EXPECTED_REGULAR_SLOT_COUNT

    incomplete = _coverage()
    incomplete["distinct_valid_time_count"] = 47
    blocked = build_amsc_physical_final_source_observation(
        event_date=DAY,
        strict_amsc_reader_result=_strict_result(),
        coverage_manifest=incomplete,
        coverage_ref="coverage.json",
        finalized_at="2026-07-03T16:01:00Z",
        authority_contract=AUTHORITY,
    )
    assert blocked["ok"] is False
    assert "physical_x_scheduled_coverage_not_exactly_expected" in blocked["block_reasons"]


def test_physical_x_keeps_complete_late_day_but_marks_it_non_strict_training() -> None:
    coverage = _coverage()
    coverage["day_state"] = "historical_window_or_late_arrival_only"
    coverage["late_arrival_count"] = 1
    reader = _strict_result()
    for row in reader["source_observations"]:
        row["strict_asof_replay_eligible"] = False

    result = build_amsc_physical_final_source_observation(
        event_date=DAY,
        strict_amsc_reader_result=reader,
        coverage_manifest=coverage,
        coverage_ref="coverage.json",
        finalized_at="2026-07-03T16:01:00Z",
        authority_contract=AUTHORITY,
    )

    assert result["ok"] is True
    payload = result["source_observation"]["payload"]
    assert payload["final_physical_high_c"] == 36.0
    assert payload["strict_asof_training_eligible"] is False
    assert payload["physical_x_label_quality"] == "complete_schedule_late_or_non_strict_arrival"


def test_settlement_s_requires_raw_extraction_binding_and_finality() -> None:
    raw = '{"observations":[{"valid_time_gmt":1783058400,"temp":31}]}'
    capture = {
        "station_id": "ZUUU",
        "event_date": DAY,
        "source_url": "https://api.weather.com/v1/location/ZUUU%3A9%3ACN/observations/historical.json?apiKey=REDACTED&units=m&startDate=20260703&endDate=20260703",
        "raw_payload": raw,
        "raw_ref": "wunderground/2026/07/03/history.json",
        "fetched_at": "2026-07-03T17:00:00Z",
        "response_completed_at": "2026-07-03T16:59:59Z",
        "parsed_at": "2026-07-03T17:00:01Z",
        "daily_high_c": 31.0,
        "extractor_version": "wunderground_station_history_observation_parser_v2",
        "extraction_evidence": '{"valid_time_gmt":1783058400,"temp":31}',
        "market_resolution_finality_attestation": {
            "market_resolution_final": True,
            "event_date": DAY,
            "attested_at": "2026-07-03T18:00:00Z",
            "market_rule_snapshot_sha256": "b" * 64,
        },
    }
    result = build_wunderground_settlement_final_source_observation(
        event_date=DAY, capture=capture, authority_contract=AUTHORITY
    )
    assert result["ok"] is True
    assert result["source_observation"]["payload"]["settlement_scalar_c"] == 31.0

    capture["market_resolution_finality_attestation"]["market_resolution_final"] = False
    blocked = build_wunderground_settlement_final_source_observation(
        event_date=DAY, capture=capture, authority_contract=AUTHORITY
    )
    assert blocked["ok"] is False
    assert "settlement_s_market_resolution_not_final" in blocked["block_reasons"]


def test_final_source_rows_are_isolated_append_only() -> None:
    physical = build_amsc_physical_final_source_observation(
        event_date=DAY,
        strict_amsc_reader_result=_strict_result(),
        coverage_manifest=_coverage(),
        coverage_ref="coverage.json",
        finalized_at="2026-07-03T16:01:00Z",
        authority_contract=AUTHORITY,
    )["source_observation"]
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_final_source_staging_schema(conn)
    assert persist_final_source_observation_shadow(conn, physical)["idempotent"] is False
    assert persist_final_source_observation_shadow(conn, physical)["idempotent"] is True
    assert conn.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE}").fetchone()[0] == 1
    changed = dict(physical)
    changed["source_observation_id"] = "different-observation-id"
    with pytest.raises(ShadowTransactionIntegrityError, match="identity_drift"):
        persist_final_source_observation_shadow(conn, changed)
