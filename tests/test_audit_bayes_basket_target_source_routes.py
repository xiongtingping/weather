from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_bayes_basket_target_source_routes import (  # noqa: E402
    CANONICAL_OPERATIONAL_TIMEZONE,
    MAX_RECEIPT_BYTES,
    MAX_STATIONS,
    audit_source_routes,
)


LANE_MAP = {
    "schema": "bayes_basket_target_station_lane_map_v1",
    "operational_timezone": CANONICAL_OPERATIONAL_TIMEZONE,
    "settlement_timezone_authority": "market_registry.settlement_timezone",
    "stations": {"ZBAA": "awc", "KATL": "awc"},
    "boundary": {
        "target_only": True,
        "allows_cross_lane_fallback": False,
        "reference_policy": {
            "awc_global_metar_speci": "formal_truth_primary_for_all_45_stations",
            "amsc_production_lane": "retired_history_only",
            "amsc_reference_evidence": "offline_audit_only",
            "reference_enters_formal_truth": False,
            "reference_enters_calibration": False,
            "temporary_awc_calibration": "retired_history_only",
        },
        "writes_production_db": False,
        "calls_order_gateway": False,
    },
}


def _routes(*rows: dict[str, object]) -> list[dict[str, object]]:
    return list(rows)


def _good_routes() -> list[dict[str, object]]:
    return _routes(
        {"station_id": "ZBAA", "truth_lane": "awc"},
        {"station_id": "KATL", "truth_lane": "awc"},
    )


def test_positive_route_audit_has_exact_one_lane_and_no_partial_output() -> None:
    result = audit_source_routes(
        LANE_MAP,
        _good_routes(),
        official_station_keys={"ZBAA", "KATL"},
    )
    assert result["status"] == "PASS"
    assert result["ok"] is True
    assert result["reason_codes"] == []
    assert result["canonical_routes"] == [
        {"station_id": "KATL", "truth_lane": "awc"},
        {"station_id": "ZBAA", "truth_lane": "awc"},
    ]
    assert result["checks"]["official_universe_exact"] is True


def test_duplicate_route_is_fail_closed_even_when_input_is_a_list() -> None:
    result = audit_source_routes(
        LANE_MAP,
        _routes(
            {"station_id": "ZBAA", "truth_lane": "awc"},
            {"station_id": "ZBAA", "truth_lane": "awc"},
            {"station_id": "KATL", "truth_lane": "awc"},
        ),
        official_station_keys={"ZBAA", "KATL"},
    )
    assert result["status"] == "FAIL"
    assert "duplicate_route" in result["reason_codes"]
    assert result["canonical_routes"] == []


def test_missing_unknown_and_wrong_lane_are_rejected() -> None:
    missing = audit_source_routes(
        LANE_MAP,
        [{"station_id": "ZBAA", "truth_lane": "amsc"}],
        official_station_keys={"ZBAA", "KATL"},
    )
    assert "missing_route" in missing["reason_codes"]

    wrong_lane = audit_source_routes(
        LANE_MAP,
        [
            {"station_id": "ZBAA", "truth_lane": "amsc"},
            {"station_id": "KATL", "truth_lane": "awc"},
        ],
        official_station_keys={"ZBAA", "KATL"},
    )
    assert "route_lane_mismatch" in wrong_lane["reason_codes"]

    unknown = audit_source_routes(
        LANE_MAP,
        _routes(
        {"station_id": "ZBAA", "truth_lane": "awc"},
            {"station_id": "KATL", "truth_lane": "awc"},
            {"station_id": "XXXX", "truth_lane": "awc"},
        ),
        official_station_keys={"ZBAA", "KATL"},
    )
    assert "unknown_route_station" in unknown["reason_codes"]


def test_illegal_lane_and_cross_lane_fallback_are_fail_closed() -> None:
    result = audit_source_routes(
        LANE_MAP,
        _routes(
            {"station_id": "ZBAA", "truth_lane": "dwd"},
            {
                "station_id": "KATL",
                "truth_lane": "awc",
                "fallback_provider_id": "global_noaa_awc_relay",
            },
        ),
        official_station_keys={"ZBAA", "KATL"},
    )
    assert "illegal_lane" in result["reason_codes"]
    assert "cross_lane_fallback_route" in result["reason_codes"]


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (
            {
                "station_id": "ZBAA",
                "truth_lane": "amsc",
                "route_role": "reference_only",
            },
            "reference_intrudes_canonical",
        ),
    ],
)
def test_reference_only_evidence_cannot_enter_canonical_routes(entry: dict[str, object], reason: str) -> None:
    rows = [entry, {"station_id": "KATL", "truth_lane": "awc"}]
    result = audit_source_routes(LANE_MAP, rows, official_station_keys={"ZBAA", "KATL"})
    assert reason in result["reason_codes"]
    assert result["canonical_routes"] == []


def test_route_config_timezone_authority_fails_closed() -> None:
    config = json.loads(json.dumps(LANE_MAP))
    config["operational_timezone"] = "UTC"
    result = audit_source_routes(config, _good_routes(), official_station_keys={"ZBAA", "KATL"})
    assert result["status"] == "FAIL"
    assert "config_operational_timezone_mismatch" in result["reason_codes"]

    config = json.loads(json.dumps(LANE_MAP))
    config["target_timezone"] = "America/New_York"
    result = audit_source_routes(config, _good_routes(), official_station_keys={"ZBAA", "KATL"})
    assert "route_config_settlement_timezone_authority_forbidden" in result["reason_codes"]

    config = json.loads(json.dumps(LANE_MAP))
    config["settlement_timezone_authority"] = "station_lane_map.target_timezone"
    result = audit_source_routes(config, _good_routes(), official_station_keys={"ZBAA", "KATL"})
    assert "settlement_timezone_authority_mismatch" in result["reason_codes"]


def test_official_registry_values_are_not_interpreted_as_route_semantics() -> None:
    # Only keys are passed: a fallback-looking provider value is irrelevant to
    # the target route audit and cannot replace the canonical lane map.
    official_routes = {
        "ZBAA": "cn_amsc",
        "KATL": "global_noaa_awc_relay",
    }
    result = audit_source_routes(LANE_MAP, _good_routes(), official_station_keys=official_routes)
    assert result["status"] == "PASS"


def test_exact_500_is_allowed_and_501_is_fail_closed() -> None:
    stations = {f"S{i:03d}": "awc" for i in range(MAX_STATIONS)}
    rows = [{"station_id": station, "truth_lane": lane} for station, lane in stations.items()]
    config = dict(LANE_MAP)
    config["stations"] = stations
    passed = audit_source_routes(config, rows, official_station_keys=set(stations))
    assert passed["status"] == "PASS"
    assert passed["counts"]["canonical_station_count"] == MAX_STATIONS
    too_many = dict(config)
    too_many["stations"] = {**stations, "S500": "awc"}
    failed = audit_source_routes(
        too_many,
        rows + [{"station_id": "S500", "truth_lane": "awc"}],
        official_station_keys=set(too_many["stations"]),
    )
    assert failed["status"] == "FAIL"
    assert "station_count_exceeded" in failed["reason_codes"]
    assert "route_count_exceeded" in failed["reason_codes"]


def test_low_wrong_low_rewrap_does_not_latch_failure() -> None:
    low = audit_source_routes(LANE_MAP, _good_routes(), official_station_keys={"ZBAA", "KATL"})
    wrong = audit_source_routes(
        LANE_MAP,
        [{"station_id": "ZBAA", "truth_lane": "amsc"}, {"station_id": "KATL", "truth_lane": "awc"}],
        official_station_keys={"ZBAA", "KATL"},
    )
    low_again = audit_source_routes(LANE_MAP, _good_routes(), official_station_keys={"ZBAA", "KATL"})
    assert low["status"] == "PASS"
    assert wrong["status"] == "FAIL"
    assert low_again["status"] == "PASS"
    assert low_again["canonical_routes"] == low["canonical_routes"]


def test_cli_writes_stable_atomic_receipt_under_one_mib(tmp_path: Path) -> None:
    lane_path = tmp_path / "lane.json"
    official_path = tmp_path / "official.json"
    output_path = tmp_path / "receipt.json"
    lane_path.write_text(json.dumps(LANE_MAP), encoding="utf-8")
    official_path.write_text(
        json.dumps({"station_routes": {"ZBAA": "fallback-looking", "KATL": "another-provider"}}),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(ROOT / "scripts/audit_bayes_basket_target_source_routes.py"),
        "--lane-map",
        str(lane_path),
        "--official-sources",
        str(official_path),
        "--output",
        str(output_path),
    ]
    first = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    second = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    assert first.stdout == second.stdout
    assert output_path.read_bytes() == first.stdout.encode("utf-8")
    assert output_path.stat().st_size < MAX_RECEIPT_BYTES
    assert list(tmp_path.glob(".receipt.json.tmp.*")) == []
