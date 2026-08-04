from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.audit_bayes_basket_target_official_ingress_routes import (
    MAX_RECEIPT_BYTES,
    audit_official_ingress_routes,
)


ROOT = Path(__file__).resolve().parents[1]


def test_multi_station_official_ingress_replay_passes_all_role_and_timezone_gates() -> None:
    result = audit_official_ingress_routes()
    assert result["status"] == "PASS"
    assert result["ok"] is True
    assert result["reason_codes"] == []
    assert all(result["checks"].values())
    assert result["boundary"]["writes_database"] is False
    assert result["boundary"]["calls_order_gateway"] is False
    assert result["counts"]["station_count"] == 4
    assert result["counts"]["truth_row_count"] == 4
    assert result["checks"]["amsc_00_formal"] is True
    assert result["checks"]["amsc_30_reference_only"] is True
    assert result["checks"]["awc_hour_for_amsc_reference_only"] is True


def test_clean_bad_clean_rewrap_is_not_latched() -> None:
    result = audit_official_ingress_routes()
    assert result["checks"]["rewrap_baseline_pass"] is True
    assert result["checks"]["rewrap_adversarial_fails"] is True
    assert result["checks"]["rewrap_clean_pass"] is True


def test_custom_cross_lane_and_noncanonical_minute_fail_closed() -> None:
    lanes = {"ZBAA": "amsc", "KATL": "awc"}
    zones = {"ZBAA": "Asia/Shanghai", "KATL": "America/New_York"}
    rows = [{
        "observation_id": "bad",
        "station_id": "ZBAA",
        "source_lane": "amsc",
        "report_type": "METAR",
        "valid_at_utc": "2026-07-28T12:15:00Z",
        "expected_role": "BLOCKED",
        "expected_reason": "amsc_metar_timing_invalid",
    }]
    result = audit_official_ingress_routes(lanes, zones, rows)
    assert result["status"] == "FAIL"
    assert result["checks"]["integration_case_matches_expected"] is True
    assert "multi_station_same_day_isolated" in result["checks"]


def test_cli_receipt_is_stable_atomic_and_under_one_mib(tmp_path: Path) -> None:
    output = tmp_path / "official-ingress.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/audit_bayes_basket_target_official_ingress_routes.py"),
        "--output",
        str(output),
    ]
    first = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    second = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    assert first.stdout == second.stdout
    assert output.read_bytes() == first.stdout.encode("utf-8")
    assert output.stat().st_size < MAX_RECEIPT_BYTES
    payload = json.loads(first.stdout)
    assert payload["status"] == "PASS"
    assert list(tmp_path.glob(".official-ingress.json.tmp.*")) == []
