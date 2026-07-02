from __future__ import annotations

import json
from pathlib import Path

from scripts import settlement_confirmation_packet as packet


def _flow_payload() -> dict:
    return {
        "generated_at": "2026-06-01T01:00:00+00:00",
        "actual_apply": {
            "candidate_count": 2,
            "ready_count": 2,
            "required_confirm_apply": "APPLY_SETTLEMENT_ACTUALS",
            "review_packet": {
                "would_insert_event_run_rows": 2,
                "write_policy": "append_only_event_runs_settlement_actual_rows",
                "confirm_command": ".venv/bin/python scripts/apply_settlement_validation_flow.py --apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS",
                "items": [
                    {
                        "city": "nyc",
                        "slug": "highest-temperature-in-nyc-on-may-31-2026",
                        "event_date": "2026-05-31",
                        "actual": 77.8,
                        "market_unit": "F",
                        "station_id": "KLGA",
                        "source_station_id": "KLGA",
                        "station_source_match": True,
                        "resolution_source": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
                        "review_checksum": "abc123",
                        "apply_eligible": True,
                        "blockers": [],
                    }
                ],
            },
        },
        "label_plan": {
            "planned_labels": 0,
            "decisions_scanned": 9009,
            "skipped": {"settlement_missing": 1778},
        },
        "after_actual_apply_simulation": {
            "actual_inserted_count": 2,
            "label_planned_after_actual_apply": 88,
            "label_apply_inserted_count": 88,
            "label_planned_after_label_apply": 0,
            "label_apply_closed_loop_ok": True,
        },
        "post_label_apply_validation": {
            "commands": {
                "rolling_validation": ".venv/bin/python scripts/paper_observation_rolling_validation.py --json"
            }
        },
    }


def test_confirmation_packet_summarizes_actuals_labels_and_commands(
    tmp_path: Path,
) -> None:
    flow_path = tmp_path / "flow.json"
    flow_path.write_text(json.dumps(_flow_payload()), encoding="utf-8")

    report = packet.build_packet(flow_path)

    assert report["ok"] is True
    assert report["actual_ready"] == 2
    assert report["would_insert_event_run_rows"] == 2
    assert report["actual_items"][0]["station_source_match"] is True
    assert report["after_actual_apply_simulation"]["label_planned_after_actual_apply"] == 88
    assert report["after_actual_apply_simulation"]["label_apply_closed_loop_ok"] is True
    assert report["write_safety"]["backup_required"] is True
    assert report["confirmation"]["actual_required_phrase"] == "APPLY_SETTLEMENT_ACTUALS"
    assert report["confirmation"]["label_required_phrase"] == "APPLY_SETTLEMENT_LABELS"
    assert report["next_action"] == "review_packet_then_confirm_actuals_labels_validate"


def test_confirmation_packet_blocks_station_mismatch(tmp_path: Path) -> None:
    payload = _flow_payload()
    payload["actual_apply"]["review_packet"]["items"][0]["station_source_match"] = False
    flow_path = tmp_path / "flow.json"
    flow_path.write_text(json.dumps(payload), encoding="utf-8")

    report = packet.build_packet(flow_path)

    assert report["ok"] is False
    assert "resolution_station_mismatch_in_actual_packet" in report["blockers"]


def test_confirmation_packet_markdown_contains_commands(tmp_path: Path) -> None:
    flow_path = tmp_path / "flow.json"
    flow_path.write_text(json.dumps(_flow_payload()), encoding="utf-8")

    report = packet.build_packet(flow_path)
    md = packet.format_markdown(report)

    assert "Settlement Confirmation Packet" in md
    assert "nyc" in md
    assert "77.8F" in md
    assert "actuals_labels_validate" in md
