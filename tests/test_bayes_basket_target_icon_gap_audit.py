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
from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.model_arrival_capture_config import EXPECTED_SCOPE


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
    assert switches["target_capture_config_station_scope"] == EXPECTED_SCOPE
    assert switches["target_capture_is_45_station_scoped"] is True
    assert switches["resident_capture_config_station_scope"] == "ZUUU"
    assert switches["model_ids_include_icon"] is True
    assert switches["provider_allowlist_include_icon"] is True
    assert switches["et_provider_set_include_icon"] is True
    assert switches["monitor_job_enabled_by_default"] is False


def test_receipt_coverage_excludes_future_et_rows_from_formal_counts() -> None:
    coverage = build_audit(probe_official=False)["target_icon_receipt_coverage"]
    assert not set(coverage["target_local_dates"]) & set(
        coverage["quarantined_future_target_local_dates"]
    )
    assert coverage["receipt_rows"] + coverage["quarantined_future_receipt_count"] <= coverage[
        "all_receipt_file_rows"
    ]


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
    write_decode(complete, "2026-07-25T08:00:00Z", expected_station_count())
    write_decode(incomplete, "2026-07-25T09:00:00Z", 0)

    payload, selected = _latest_native_decode()
    assert payload["curves"]["complete_curve_count"] == expected_station_count()
    assert selected == complete
