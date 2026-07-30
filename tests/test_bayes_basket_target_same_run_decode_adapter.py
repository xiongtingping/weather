from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from scripts.build_bayes_basket_target_native_decode_from_same_run_preflight import build  # noqa: E402
from scripts.rebuild_bayes_basket_target_same_run_from_retained_raw import (  # noqa: E402
    _dedupe_model_points,
)


def _preflight() -> dict[str, object]:
    point = {
        "station_id": "ZUUU",
        "event_date": "2026-07-25",
        "provider_id": "icon_global_official",
        "model_run_initialization_utc": "2026-07-24T00:00:00Z",
        "valid_time_utc": "2026-07-25T04:00:00Z",
        "temperature_2m_c": 20.0,
        "raw_message_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
        "published_at_utc": "2026-07-24T01:00:00Z",
        "received_at_utc": "2026-07-24T01:01:00Z",
        "coordinate_lineage": {"station_id": "ZUUU"},
    }
    eligible = [{
        "station_id": "ZUUU",
        "event_date": "2026-07-25",
        "provider_id": "icon_global_official",
        "model_run_initialization_utc": "2026-07-24T00:00:00Z",
        "temperature_points": [point],
    }]
    return {
        "schema": "bayes_basket_target_same_run_retained_raw_preflight_v1",
        "decision_at_utc": "2026-07-24T02:00:00Z",
        "target_local_date": "2026-07-25",
        "target_timezone": "America/New_York",
        "market_registry_snapshot_sha256": "c" * 64,
        "model_points": [point],
        "blockers": [],
        "curves": {"complete_curve_count": 1, "eligible_model_rows": eligible},
        "checks": {
            "one_run_only": True,
            "official_provider_only": True,
            "raw_sha256_verified": True,
            "complete_curve_count_positive": True,
            "exact_three_model_join_positive": True,
            "model_arrival_index_complete": True,
            "all_model_arrivals_prospective": True,
            "all_model_arrival_publication_sources_native": True,
        },
        "boundary": {
            "writes_target_database": False,
            "writes_shared_live_database": False,
            "calls_order_gateway": False,
        },
    }


def test_adapter_preserves_lineage_and_non_authority(tmp_path: Path) -> None:
    source = tmp_path / "preflight.json"
    output = tmp_path / "decode.json"
    source.write_text(json.dumps(_preflight()), encoding="utf-8")

    result = build(input_path=source, output_path=output)

    assert result["schema"] == "bayes_basket_target_native_model_arrival_decode_evidence_v1"
    assert result["decoded_point_count"] == 1
    assert result["checks"]["promotion_allowed"] is False
    assert result["checks"]["writes_target_db"] is False
    assert result["boundary"]["protected_production_line_touched"] is False
    assert result["input_artifacts"][0]["path"] == str(source.resolve())
    assert result["target_local_date"] == "2026-07-25"
    assert result["market_registry_snapshot_sha256"] == "c" * 64
    assert result["date_boundary"]["event_day_state"] == "not_started"
    assert result["date_boundary"]["scope"] == "FORWARD_DIAGNOSTIC_ONLY"
    assert result["date_boundary"]["formal_decision_allowed"] is False
    assert result["checks"]["decoded_points_have_coordinate_lineage"] is True
    assert result["decoded_points"][0]["coordinate_lineage"]["station_id"] == "ZUUU"


def test_adapter_rejects_failed_preflight(tmp_path: Path) -> None:
    source = tmp_path / "preflight.json"
    payload = _preflight()
    payload["checks"]["raw_sha256_verified"] = False
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ContractError, match="same_run_preflight_required_check_failed"):
        build(input_path=source, output_path=tmp_path / "decode.json")


def test_adapter_rejects_pre_timezone_preflight(tmp_path: Path) -> None:
    source = tmp_path / "preflight.json"
    payload = _preflight()
    payload.pop("market_registry_snapshot_sha256")
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ContractError,
        match="same_run_preflight_market_registry_hash_missing",
    ):
        build(input_path=source, output_path=tmp_path / "decode.json")


def test_retained_raw_rebuilder_has_no_global_et_target_timezone() -> None:
    text = (
        ROOT
        / "scripts"
        / "rebuild_bayes_basket_target_same_run_from_retained_raw.py"
    ).read_text(encoding="utf-8")
    assert 'TARGET_TIMEZONE = "America/New_York"' not in text
    assert "--market-registry-snapshot" in text
    assert "registry_scope_settlement_timezone" in text


def test_identical_global_and_station_model_points_fold_deterministically() -> None:
    base = {
        "provider_id": "gfs_0p25_official",
        "station_id": "RKSI",
        "event_date": "2026-07-27",
        "model_run_initialization_utc": "2026-07-25T18:00:00Z",
        "valid_time_utc": "2026-07-27T03:00:00Z",
        "raw_message_sha256": "a" * 64,
        "temperature_2m_c": 32.5,
        "receipt_sha256": "f" * 64,
    }
    rows, folded = _dedupe_model_points([
        base,
        {**base, "receipt_sha256": "b" * 64},
    ])
    assert folded == 1
    assert len(rows) == 1
    assert rows[0]["receipt_sha256"] == "b" * 64


def test_conflicting_same_key_model_points_fail_closed() -> None:
    base = {
        "provider_id": "gfs_0p25_official",
        "station_id": "RKSI",
        "event_date": "2026-07-27",
        "model_run_initialization_utc": "2026-07-25T18:00:00Z",
        "valid_time_utc": "2026-07-27T03:00:00Z",
        "raw_message_sha256": "a" * 64,
        "temperature_2m_c": 32.5,
        "receipt_sha256": "f" * 64,
    }
    with pytest.raises(ContractError, match="same_run_model_point_conflict"):
        _dedupe_model_points([
            base,
            {**base, "raw_message_sha256": "c" * 64},
        ])
