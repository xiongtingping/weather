from __future__ import annotations

import json
from pathlib import Path

from scripts.persist_official_nwp_daily_peak_revision import build_contract


def _point(run: str, valid: str, value: float, suffix: str) -> dict:
    return {
        "schema": "official_nwp_normalized_snapshot_v1",
        "provider_id": "gfs",
        "run_at_utc": run,
        "valid_time_utc": valid,
        "response_completed_at": "2026-07-16T01:00:00Z",
        "requested_at": "2026-07-16T00:59:58Z",
        "response_started_at": "2026-07-16T00:59:59Z",
        "response_headers_observed": True,
        "raw_sha256": f"raw-{suffix}",
        "raw_payload_sha256s": [f"payload-{suffix}"],
        "raw_object_ref": f"/raw/{suffix}",
        "snapshot_sha256": f"snapshot-{suffix}",
        "normalized_features": {"temperature_2m_c": value},
    }


def test_build_contract_uses_only_requested_model_run(tmp_path: Path) -> None:
    root = tmp_path / "curves"
    history = root / "gfs" / "history"
    history.mkdir(parents=True)
    target_run = "2026-07-16T00:00:00Z"
    older_run = "2026-07-15T18:00:00Z"
    valid_times = [f"2026-07-16T{hour:02d}:00:00Z" for hour in range(16, 24)] + [
        f"2026-07-17T{hour:02d}:00:00Z" for hour in range(0, 16)
    ]
    for index, valid in enumerate(valid_times):
        for run, label in ((target_run, "target"), (older_run, "older")):
            point = _point(run, valid, 30.0 + index / 10.0, f"{label}-{index}")
            (history / f"{label}-{index}.json").write_text(json.dumps(point), encoding="utf-8")
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "official_nwp_daily_curve_run_contract_v1",
                "target_timezone": "Asia/Shanghai",
                "providers": {
                    "gfs": {
                        "allowed_run_hours_utc": [0, 6, 12, 18],
                        "curve_time_resolution_hours": 1,
                        "maximum_planned_lead_hours": 120,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = build_contract(
        provider="gfs",
        run_at_utc=target_run,
        target_event_date="2026-07-17",
        curve_root=root,
        contract_path=contract_path,
    )

    assert result["ok"] is True
    assert result["revision"]["run_at_utc"] == target_run
    assert "official_nwp_point_duplicate_valid_time_conflict" not in result["revision"].get("block_reasons", [])
