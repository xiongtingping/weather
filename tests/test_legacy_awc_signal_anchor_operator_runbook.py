from pathlib import Path

from scripts import legacy_awc_signal_anchor_decision_packet as decision_packet
from scripts import legacy_awc_signal_anchor_operator_runbook as runbook


def _decision_packet_fixture() -> dict:
    replay_resolution = {
        "items": [
            {
                "fact_id": 97059,
                "slug": "highest-temperature-in-chengdu-on-june-18-2026",
                "event_date": "2026-06-18",
                "hard_source_signal_expected": True,
                "signal_id": "",
                "override": {
                    "source": "zuuu_metar_raw_awc",
                    "constraint_mode": "hard_source_lower_bound_constraint",
                    "target_bucket": "31°C",
                    "value_c": 31.0,
                },
                "no_apply_probe_command": (
                    "python3 scripts/ssot_chain_replay_audit.py --fact-id 97059 --max-signals-output 3"
                ),
            },
            {
                "fact_id": 97062,
                "slug": "highest-temperature-in-chengdu-on-june-21-2026",
                "event_date": "2026-06-21",
                "hard_source_signal_expected": True,
                "signal_id": "",
                "override": {
                    "source": "zuuu_metar_raw_awc",
                    "constraint_mode": "hard_source_lower_bound_constraint",
                    "target_bucket": "25°C or below",
                    "value_c": 24.0,
                },
                "no_apply_probe_command": (
                    "python3 scripts/ssot_chain_replay_audit.py --fact-id 97062 --max-signals-output 3"
                ),
            },
        ],
    }
    return decision_packet.build_decision_packet_from_replay_resolution(
        replay_resolution,
        max_items_output=10,
    )


def test_operator_runbook_builds_reviewed_no_signal_manifest_candidate_without_writing(tmp_path: Path) -> None:
    result = runbook.build_operator_runbook_from_packet(
        _decision_packet_fixture(),
        db_path=tmp_path / "archive.db",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
        max_items_output=5,
    )

    assert result["schema"] == "legacy_awc_signal_anchor_operator_runbook_v1"
    assert result["read_only"] is True
    assert result["dry_run"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_manifest_file"] is False
    assert result["boundary"]["reviewed_no_signal_manifest_is_candidate_only"] is True
    assert result["candidate_count"] == 2
    assert result["manifest_output_exists"] is False
    assert str(tmp_path / "backups") in result["manifest_output_path"]

    reviewed = result["reviewed_no_signal_manifest_candidate"]
    assert reviewed["schema"] == "legacy_awc_signal_anchor_decision_manifest_v1"
    assert reviewed["operator_review_required"] is True
    assert {row["decision"] for row in reviewed["decisions"]} == {
        "mark_reviewed_legacy_no_signal_anchor"
    }
    assert all(row["review_reason"] for row in reviewed["decisions"])
    assert result["reviewed_no_signal_would_clear_legacy_start_blocker"] is True
    assert result["reviewed_no_signal_manifest_audit"]["hard_blockers"] == []

    steps = {row["step"]: row for row in result["checklist"]}
    assert steps["materialize_reviewed_no_signal_manifest_if_operator_accepts"]["writes_manifest_file"] is True
    assert steps["materialize_reviewed_no_signal_manifest_if_operator_accepts"]["writes_archive_db"] is False
    assert "--emit-reviewed-no-signal-manifest-json" in steps["materialize_reviewed_no_signal_manifest_if_operator_accepts"]["command"]
    assert "--legacy-awc-anchor-decision-manifest" in steps["recheck_daily_review_production_preflight"]["command"]


def test_operator_runbook_reports_existing_manifest_readiness(tmp_path: Path) -> None:
    base_packet = _decision_packet_fixture()
    existing_manifest = runbook.build_reviewed_no_signal_manifest(
        base_packet,
        operator="unit-test",
        reviewed_at="2026-07-06T00:00:00Z",
    )

    result = runbook.build_operator_runbook_from_packet(
        base_packet,
        db_path=tmp_path / "archive.db",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
        existing_decision_manifest=existing_manifest,
    )

    assert result["existing_manifest_clears_legacy_start_blocker"] is True
    assert result["existing_decision_manifest_audit"]["acceptance_exception_complete"] is True
    assert result["existing_decision_manifest_audit"]["hard_blockers"] == []


def test_compact_operator_runbook_keeps_commands_and_guard_summary(tmp_path: Path) -> None:
    full = runbook.build_operator_runbook_from_packet(
        _decision_packet_fixture(),
        db_path=tmp_path / "archive.db",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )
    compact = runbook.compact_operator_runbook(full)

    assert compact["schema"] == "legacy_awc_signal_anchor_operator_runbook_v1_summary"
    assert compact["read_only"] is True
    assert compact["guards"]["writes_archive_db"] is False
    assert compact["candidate_count"] == 2
    assert compact["reviewed_no_signal_would_clear_legacy_start_blocker"] is True
    assert any(row["step"] == "recheck_legacy_awc_decision_packet" for row in compact["checklist"])
