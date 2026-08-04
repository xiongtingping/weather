from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bayes_basket_target.calibration_ingress import (
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
    _truth_authority,
)
from bayes_basket_target.contracts import ContractError
from scripts import build_bayes_basket_target_45station_calibration_base as target


def _sample(*, lane: str = "AWC") -> dict[str, object]:
    identity = {
        "station_id": "ZBAA",
        "event_date": "2026-06-28",
        "settlement_timezone": "Asia/Shanghai",
        "settlement_window": {
            "start_utc": "2026-06-27T16:00:00Z",
            "end_utc": "2026-06-28T16:00:00Z",
        },
        "truth": {
            "source_lane": lane,
            "source_role": HISTORICAL_AWC_REPLAY,
            "authority_scope": NO_LIVE_REPLAY,
            "tmax_c": 31.0,
            "observations": [{
                "station_id": "ZBAA",
                "source_lane": lane,
                "source_role": HISTORICAL_AWC_REPLAY,
                "authority_scope": NO_LIVE_REPLAY,
                "settlement_timezone": "Asia/Shanghai",
                "evidence_role": "FORMAL",
                "reference_only": False,
                "qc_status": "ACCEPTED",
                "report_type": "METAR",
                "valid_at_utc": "2026-06-28T05:00:00Z",
                "reported_temperature_c": 31.0,
                "raw_sha256": "a" * 64,
            }],
        },
    }
    return {
        "sample_id": "truth-" + target.content_sha256(identity)[:32],
        **identity,
    }


def test_truth_sample_is_historical_awc_replay_and_no_live(tmp_path):
    row, reference, lineage = target._truth_sample(
        station_id="ZBAA",
        timezone_name="Asia/Shanghai",
        sample=_sample(),
        label_authority=target.FORMAL_TRUTH,
        dataset_path=Path(tmp_path / "truth.json"),
        dataset_sha256="b" * 64,
        dataset_content_sha256="c" * 64,
        received_at_utc="2026-06-30T00:00:00Z",
        existing=None,
    )

    assert reference is None
    assert row is not None
    assert row["source_lane"] == "AWC"
    assert row["source_role"] == HISTORICAL_AWC_REPLAY
    assert row["authority_scope"] == NO_LIVE_REPLAY
    assert row["timestamps_complete"] is False
    assert _truth_authority(row) == NO_LIVE_REPLAY
    assert lineage["official_tmax_c"] == 31.0


def test_truth_sample_rejects_reference_authority_and_non_awc_lane(tmp_path):
    common = {
        "station_id": "ZBAA",
        "timezone_name": "Asia/Shanghai",
        "dataset_path": Path(tmp_path / "truth.json"),
        "dataset_sha256": "b" * 64,
        "dataset_content_sha256": "c" * 64,
        "received_at_utc": "2026-06-30T00:00:00Z",
        "existing": None,
    }
    with pytest.raises(ContractError, match="label_authority_invalid"):
        target._truth_sample(
            sample=_sample(),
            label_authority="REFERENCE_ONLY",
            **common,
        )
    with pytest.raises(ContractError, match="source_lane_invalid"):
        target._truth_sample(
            sample=_sample(lane="AMSC"),
            label_authority=target.FORMAL_TRUTH,
            **common,
        )

    reference = _sample()
    reference["truth"]["observations"][0]["reference_only"] = True
    reference["sample_id"] = "truth-" + target.content_sha256({
        key: value for key, value in reference.items() if key != "sample_id"
    })[:32]
    with pytest.raises(ContractError, match="observation_scope_mismatch"):
        target._truth_sample(
            sample=reference,
            label_authority=target.FORMAL_TRUTH,
            **common,
        )

    active_role = _sample()
    active_role["truth"]["source_role"] = (
        "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
    )
    active_role["sample_id"] = "truth-" + target.content_sha256({
        key: value for key, value in active_role.items() if key != "sample_id"
    })[:32]
    with pytest.raises(ContractError, match="truth_lineage_invalid"):
        target._truth_sample(
            sample=active_role,
            label_authority=target.FORMAL_TRUTH,
            **common,
        )

    rejected_qc = _sample()
    rejected_qc["truth"]["observations"][0]["qc_status"] = "REJECTED"
    rejected_qc["sample_id"] = "truth-" + target.content_sha256({
        key: value for key, value in rejected_qc.items() if key != "sample_id"
    })[:32]
    with pytest.raises(ContractError, match="observation_qc_invalid"):
        target._truth_sample(
            sample=rejected_qc,
            label_authority=target.FORMAL_TRUTH,
            **common,
        )


def test_45station_builder_owns_its_oos_materialization_helper():
    assert target._load_oos_rows.__module__ == target.__name__


def test_station_registry_validation_has_explicit_awc_role_dependency():
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE calibration_station_registry ("
            "station_id TEXT, icao_code TEXT, city_label TEXT, "
            "timezone_name TEXT, source_lane TEXT, settlement_definition TEXT, "
            "registry_version TEXT, source_role TEXT, content_sha256 TEXT)"
        )
        for number in range(target.EXPECTED_STATION_COUNT):
            station = f"T{number:03d}"
            row = {
                "station_id": station,
                "icao_code": station,
                "city_label": station,
                "timezone_name": "Asia/Shanghai",
                "source_lane": "AWC",
                "settlement_definition": target.SETTLEMENT_DEFINITION,
                "registry_version": "test-v1",
                "source_role": (
                    target.CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1
                ),
            }
            conn.execute(
                "INSERT INTO calibration_station_registry VALUES "
                "(:station_id, :icao_code, :city_label, :timezone_name, "
                ":source_lane, :settlement_definition, :registry_version, "
                ":source_role, :content_sha256)",
                {**row, "content_sha256": target.content_sha256(row)},
            )

        rows, timezones = target._station_rows(conn)

    assert len(rows) == target.EXPECTED_STATION_COUNT
    assert timezones["T000"] == "Asia/Shanghai"


def test_truth_dataset_loader_rechecks_no_live_boundary(tmp_path):
    zone = ZoneInfo("Asia/Shanghai")
    samples = []
    for offset in range(target.EXPECTED_DAY_COUNT):
        event_day = date(2026, 6, 1) + timedelta(days=offset)
        start = datetime.combine(event_day, time.min, tzinfo=zone).astimezone(
            timezone.utc
        )
        end = datetime.combine(
            event_day + timedelta(days=1), time.min, tzinfo=zone
        ).astimezone(timezone.utc)
        raw_sha = f"{offset:064x}"
        observation = {
            "station_id": "ZBAA",
            "source_lane": "AWC",
            "source_role": HISTORICAL_AWC_REPLAY,
            "authority_scope": NO_LIVE_REPLAY,
            "settlement_timezone": "Asia/Shanghai",
            "evidence_role": "FORMAL",
            "reference_only": False,
            "qc_status": "ACCEPTED",
            "report_type": "METAR",
            "valid_at_utc": (
                datetime.combine(event_day, time(5), tzinfo=timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            ),
            "reported_temperature_c": 31.0,
            "raw_sha256": raw_sha,
        }
        identity = {
            "station_id": "ZBAA",
            "event_date": event_day.isoformat(),
            "settlement_window": {
                "start_utc": start.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "end_utc": end.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
            },
            "settlement_timezone": "Asia/Shanghai",
            "truth": {
                "source_lane": "AWC",
                "source_role": HISTORICAL_AWC_REPLAY,
                "authority_scope": NO_LIVE_REPLAY,
                "tmax_c": 31.0,
                "observations": [observation],
            },
        }
        samples.append({
            "sample_id": "truth-" + target.content_sha256(identity)[:32],
            **identity,
        })
    identity = {
        "schema": "bayes_basket_station_calibration_dataset_v1",
        "station_id": "ZBAA",
        "settlement_timezone": "Asia/Shanghai",
        "source_lane": "AWC",
        "source_role": HISTORICAL_AWC_REPLAY,
        "authority_scope": NO_LIVE_REPLAY,
        "label_authority": target.FORMAL_TRUTH,
        "start_date": "2026-06-01",
        "end_date": "2026-06-29",
        "as_of_utc": "2026-06-30T00:00:00Z",
        "samples": samples,
        "missing_dates": [],
        "input_evidence_sha256s": ["d" * 64],
        "checks": {
            "closed_days_only": True,
            "formal_awc_truth_only": True,
            "awc_hourly_metar_and_speci_only": True,
            "historical_awc_replay_only": True,
            "input_evidence_content_hashes_verified": True,
            "settlement_timezone_exact_match": True,
            "reference_only_excluded": True,
            "complete_date_coverage": True,
        },
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
    }
    payload = {
        **identity,
        "content_sha256": target.content_sha256(identity),
    }
    path = tmp_path / "truth.json"
    path.write_text(json.dumps(payload))

    rows, references, index, metadata = target._load_station_truth(
        path=path,
        station_id="ZBAA",
        timezone_name="Asia/Shanghai",
        existing={},
    )

    assert len(rows) == target.EXPECTED_DAY_COUNT
    assert references == []
    assert len(index) == target.EXPECTED_DAY_COUNT
    assert metadata["authority_scope"] == NO_LIVE_REPLAY

    payload["boundary"]["enters_p1"] = True
    payload["content_sha256"] = target.content_sha256({
        key: value for key, value in payload.items() if key != "content_sha256"
    })
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match="truth_dataset_invalid"):
        target._load_station_truth(
            path=path,
            station_id="ZBAA",
            timezone_name="Asia/Shanghai",
            existing={},
        )
