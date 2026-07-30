from __future__ import annotations

from pathlib import Path
import json
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_bayes_basket_target_icon_gap import build_audit  # noqa: E402
from scripts.audit_bayes_basket_target_icon_gap import _launchctl_icon_jobs  # noqa: E402
from scripts.audit_bayes_basket_target_icon_gap import _latest_model_arrival_set  # noqa: E402
from scripts.audit_bayes_basket_target_icon_gap import _latest_native_decode  # noqa: E402


def test_audit_does_not_count_macos_iconservices_as_weather_scheduler() -> None:
    listing = "0\t0\tcom.apple.iconservices.iconservicesagent\n0\t0\tcom.weather.forecast_source_forward_capture\n"
    completed = type("Completed", (), {"stdout": listing})()
    with patch("scripts.audit_bayes_basket_target_icon_gap.subprocess.run", return_value=completed):
        assert _launchctl_icon_jobs() == ["com.weather.forecast_source_forward_capture"]


def test_icon_is_not_removed_but_monitor_gate_is_not_default_open() -> None:
    result = build_audit(probe_official=False)
    switches = result["switch_state"]
    assert switches["catalog_provider_present"] is True
    assert switches["target_capture_config_enabled"] is True
    assert switches["target_capture_config_includes_icon"] is True
    assert switches["target_capture_config_station_scope"] == "canonical_45_station"
    assert switches["target_capture_is_45_station_scoped"] is True
    assert switches["resident_capture_config_station_scope"] == "ZUUU"
    assert switches["model_ids_include_icon"] is True
    assert switches["provider_allowlist_include_icon"] is True
    assert switches["et_provider_set_include_icon"] is True
    assert switches["monitor_job_enabled_by_default"] is False


def test_audit_keeps_forward_preflight_out_of_default_historical_audit() -> None:
    # Keep the fixture's 2026-07-24 target day open explicitly; the test must
    # not change meaning merely because wall-clock time advances.
    result = build_audit(probe_official=False, as_of_utc="2026-07-25T00:00:00Z")
    causes = {row["id"]: row for row in result["root_causes"]}
    assert "ICON_NOT_REMOVED" in causes
    assert "NO_HISTORICAL_NATIVE_ARCHIVE_ROUTE" in causes
    assert "HISTORICAL_CALIBRATION_WINDOW_INSUFFICIENT" in causes
    assert "FORWARD_ICON_EXISTS_SAME_RUN_OPEN_DAY" in causes
    assert "FORWARD_ICON_EXISTS_BUT_RUN_MISALIGNED" not in causes
    assert "FORWARD_PREFLIGHT_AVAILABLE_NOT_PROMOTED" not in causes
    assert result["forward_preflight"]["available"] is False
    statement = result["availability_summary"]["status_statement"]
    assert statement["current_forward_icon_data"] == "PRESENT"
    assert statement["historical_closed_icon_data_for_calibration"] == "PRESENT_BUT_CALIBRATION_WINDOW_INSUFFICIENT"
    assert "当前 ICON 有数据" in statement["interpretation"]
    assert result["availability_summary"]["historical_closed"]["complete_curve_count"] == 45
    assert result["availability_summary"]["historical_closed"]["closed_et_exact_join_station_day_count"] == 45
    assert result["availability_summary"]["current_forward"]["icon_arrival_count"] == 45
    assert result["availability_summary"]["current_forward"]["icon_complete_curve_count"] == 45
    assert result["target_icon_forward_fanout"]["valid_time_count"] == 24
    archive = result["external_official_probe"]["retained_official_archive_probe"]
    # The probe set is intentionally bounded but may grow as new dated
    # candidates are added to the receipt. Assert the partition contract
    # rather than freezing an obsolete probe count.
    assert archive["exact_file_probe_count"] >= 5
    assert archive["exact_file_probe_available_count"] + archive["exact_file_probe_unavailable_count"] + archive["exact_file_probe_error_count"] == archive["exact_file_probe_count"]
    assert result["forward_preflight"]["target_local_date"] == ""
    boundary = result["forward_preflight"]["date_boundary"]
    assert boundary["must_be_excluded_from_historical_backfill"] is True
    assert boundary["historical_ssot_allowed"] is False
    assert boundary["production_calibration_allowed"] is False
    assert result["boundary"]["protected_production_line_touched"] is False


def test_receipt_coverage_excludes_future_et_rows_from_formal_counts() -> None:
    coverage = build_audit(probe_official=False)["target_icon_receipt_coverage"]
    assert not set(coverage["target_local_dates"]) & set(
        coverage["quarantined_future_target_local_dates"]
    )
    assert coverage["receipt_rows"] + coverage["quarantined_future_receipt_count"] <= coverage[
        "all_receipt_file_rows"
    ]


def test_same_run_open_day_is_reported_as_prospective_not_misaligned() -> None:
    result = build_audit(
        probe_official=False,
        as_of_utc="2026-07-25T00:00:00Z",
        forward_preflight_path=ROOT / "output/audits/bayes_basket_target_same_run_retained_raw_preflight_20260724_00z.json",
    )
    causes = {row["id"]: row for row in result["root_causes"]}
    assert "FORWARD_ICON_EXISTS_SAME_RUN_OPEN_DAY" in causes
    assert "FORWARD_ICON_EXISTS_BUT_RUN_MISALIGNED" not in causes
    assert "TARGET_EXACT_JOIN_ZERO_MIXED_RUN" not in causes
    assert result["forward_preflight"]["exact_three_model_join_count"] == 45
    assert result["forward_preflight"]["date_boundary"]["et_window_state"] == "open"
    assert result["forward_preflight"]["date_boundary"]["historical_ssot_allowed"] is False


def test_latest_arrival_set_merges_current_provider_receipts_over_stale_combined_receipt(tmp_path: Path, monkeypatch) -> None:
    import scripts.audit_bayes_basket_target_icon_gap as module

    monkeypatch.setattr(module, "DEFAULT_OUTPUT", tmp_path / "icon_gap.json")

    def write_receipt(path: Path, provider: str, event_date: str, as_of: str) -> None:
        row = {
            "station_id": "TEST",
            "target_local_date": event_date,
            "provider_id": provider,
            "run_initialization_utc": "2026-07-25T00:00:00Z",
        }
        payload = {
            "schema": "bayes_basket_target_model_arrival_evidence_v1",
            "as_of_utc": as_of,
            "checks": {
                "promotion_allowed": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
            },
            "boundary": {
                "live_order_authorized": False,
                "target_only": True,
            },
            "arrivals": [row],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    write_receipt(
        tmp_path / "bayes_basket_target_model_arrival_evidence.json",
        "ecmwf_ifs025_official",
        "2026-07-24",
        "2026-07-24T08:00:00Z",
    )
    for provider in ("ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official"):
        write_receipt(
            tmp_path / f"bayes_basket_target_model_arrival_evidence_{provider}.json",
            provider,
            "2026-07-25",
            "2026-07-25T08:30:00Z",
        )

    payload, _ = _latest_model_arrival_set()
    assert payload["_exact_join_coverage"]["exact_three_model_join_count"] == 1
    assert {row["provider_id"] for row in payload["arrivals"]} == {
        "ecmwf_ifs025_official",
        "gfs_0p25_official",
        "icon_global_official",
    }
    assert sorted(payload["_selected_paths"]) == sorted(
        str(tmp_path / f"bayes_basket_target_model_arrival_evidence_{provider}.json")
        for provider in ("ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official")
    )


def test_latest_arrival_set_does_not_replace_complete_scope_with_newer_partial_scope(tmp_path: Path, monkeypatch) -> None:
    import scripts.audit_bayes_basket_target_icon_gap as module

    monkeypatch.setattr(module, "DEFAULT_OUTPUT", tmp_path / "icon_gap.json")

    def write_set(path: Path, stations: list[str], event_date: str, as_of: str) -> None:
        rows = [
            {
                "station_id": station,
                "target_local_date": event_date,
                "provider_id": provider,
                "run_initialization_utc": "2026-07-25T00:00:00Z",
            }
            for station in stations
            for provider in (
                "ecmwf_ifs025_official",
                "gfs_0p25_official",
                "icon_global_official",
            )
        ]
        payload = {
            "schema": "bayes_basket_target_model_arrival_evidence_v1",
            "as_of_utc": as_of,
            "checks": {
                "promotion_allowed": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
            },
            "boundary": {"live_order_authorized": False, "target_only": True},
            "arrivals": rows,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    write_set(
        tmp_path / "bayes_basket_target_model_arrival_evidence_complete.json",
        ["A", "B"],
        "2026-07-25",
        "2026-07-25T08:00:00Z",
    )
    write_set(
        tmp_path / "bayes_basket_target_model_arrival_evidence_partial.json",
        ["A"],
        "2026-07-25",
        "2026-07-25T09:00:00Z",
    )

    payload, _ = _latest_model_arrival_set()
    assert payload["_exact_join_coverage"]["exact_three_model_join_count"] == 2
    assert payload["_selected_paths"] == [
        str(tmp_path / "bayes_basket_target_model_arrival_evidence_complete.json")
    ]


def test_latest_native_decode_does_not_replace_complete_decode_with_newer_incomplete_decode(tmp_path: Path, monkeypatch) -> None:
    import scripts.audit_bayes_basket_target_icon_gap as module

    audit_dir = tmp_path / "output" / "audits"
    audit_dir.mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)

    def write_decode(path: Path, as_of: str, complete_curve_count: int) -> None:
        payload = {
            "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
            "as_of_utc": as_of,
            "checks": {
                "promotion_allowed": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
            },
            "boundary": {"live_order_authorized": False},
            "curves": {
                "complete_curve_count": complete_curve_count,
                "curves": [],
                "eligible_model_rows": [],
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    complete = audit_dir / "bayes_basket_target_native_model_arrival_decode_complete.json"
    incomplete = audit_dir / "bayes_basket_target_native_model_arrival_decode_incomplete.json"
    write_decode(complete, "2026-07-25T08:00:00Z", 45)
    write_decode(incomplete, "2026-07-25T09:00:00Z", 0)

    payload, selected = _latest_native_decode()
    assert payload["curves"]["complete_curve_count"] == 45
    assert selected == complete
