from pathlib import Path

from scripts import awc_legacy_terminal_closeout_operator_runbook as runbook


def _packet(*, ok=False):
    return {
        "schema": "awc_legacy_terminal_closeout_decision_packet_v1",
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "candidate_count": 2,
        "recommended_materialize_count": 1,
        "operator_review_required_count": 1,
        "candidate_ids": ["sig_blocked", "sig_review"],
        "hard_blockers": [] if ok else ["awc_legacy_terminal_closeout_decision_pending"],
        "decision_manifest_audit": {
            "manifest_provided": ok,
            "operation_count": 2 if ok else 0,
            "operations": [] if not ok else [{"signal_id": "sig_blocked"}, {"signal_id": "sig_review"}],
            "blockers": [] if ok else ["awc_legacy_terminal_closeout_decision_pending"],
        },
        "guards": {"writes_archive_db": False},
        "next_boundary": (
            "awc_legacy_terminal_closeout_apply_contract_ready"
            if ok
            else "review_awc_legacy_terminal_closeout_manifest_before_apply_contract"
        ),
    }


def _contract(*, ok=False):
    return {
        "schema": "awc_legacy_terminal_closeout_apply_contract_v1",
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "db": "/tmp/archive.db",
        "operation_count": 2 if ok else 0,
        "operations": [] if not ok else [{"signal_id": "sig_blocked"}, {"signal_id": "sig_review"}],
        "blockers": [] if ok else ["decision_manifest_required"],
        "operator_contract": {
            "confirmation_phrase": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
            "requires_backup": True,
            "requires_writer_quiescence": True,
            "backup_path": "/tmp/archive.db.awc-legacy-terminal-closeout.bak",
            "backup_command": "sqlite3 /tmp/archive.db \".backup /tmp/archive.db.awc-legacy-terminal-closeout.bak\"",
            "verify_writer_quiescence_command": (
                "python3 scripts/controlled_apply_maintenance_window_packet.py --db /tmp/archive.db --summary"
            ),
            "post_apply_verification_commands": [
                "python3 scripts/awc_hard_source_terminal_coverage_audit.py --summary",
                "python3 scripts/hierarchical_joint_plan_coverage_audit.py --summary",
            ],
        },
        "guards": {"writes_archive_db": False},
    }


def _maintenance(*, writer_quiescent=False):
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": writer_quiescent,
        "writer_quiescent": writer_quiescent,
        "required_stop_labels": [] if writer_quiescent else ["system/com.weather.monitor.daemon"],
        "counts": {"writer_quiescence_blocker_count": 0 if writer_quiescent else 1},
        "operator_commands": {
            "suggested_pause_commands": [
                "sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon"
            ],
            "suggested_resume_commands": [
                "sudo launchctl enable system/com.weather.monitor.daemon && sudo launchctl kickstart -k system/com.weather.monitor.daemon"
            ],
            "verify_writer_quiescence": "python3 scripts/controlled_apply_maintenance_window_packet.py --summary",
        },
        "stop_authorization": {"blockers": [] if writer_quiescent else ["system_monitor_daemon_stop_requires_interactive_sudo"]},
    }


def test_awc_terminal_operator_runbook_blocks_until_manifest_and_writer_quiescence(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runbook.decision_packet, "build_packet", lambda *args, **kwargs: _packet(ok=False))
    monkeypatch.setattr(runbook.apply_contract, "build_contract", lambda *args, **kwargs: _contract(ok=False))
    monkeypatch.setattr(
        runbook.maintenance_packet,
        "build_maintenance_window_packet",
        lambda *args, **kwargs: _maintenance(writer_quiescent=False),
    )

    report = runbook.build_operator_runbook(
        tmp_path / "archive.db",
        backup_dir=tmp_path,
        generated_at="20260707T020000Z",
    )
    summary = runbook.compact_operator_runbook(report)

    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["ready_for_controlled_apply_now"] is False
    assert "awc_legacy_terminal_closeout_decision_pending" in report["start_blockers"]
    assert "writer_quiescence_not_satisfied" in report["start_blockers"]
    assert report["artifacts"]["decision_manifest_path"].endswith(
        "archive.db.awc-legacy-terminal-closeout-20260707T020000Z.decision-manifest.json"
    )
    assert "emit_decision_manifest_template" in [row["step"] for row in summary["operator_sequence"]]
    manifest_step = next(row for row in summary["operator_sequence"] if row["step"] == "emit_decision_manifest_template")
    assert "awc_legacy_terminal_closeout_review_report.py" in manifest_step["command"]
    assert "--emit-draft-manifest" in manifest_step["command"]
    apply_step = next(row for row in summary["operator_sequence"] if row["step"] == "apply_awc_legacy_terminal_closeout")
    assert apply_step["writes_archive_db"] is True
    assert apply_step["confirmation_phrase"] == "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT"


def test_awc_terminal_operator_runbook_ready_after_contract_and_quiescence(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runbook.decision_packet, "build_packet", lambda *args, **kwargs: _packet(ok=True))
    monkeypatch.setattr(runbook.apply_contract, "build_contract", lambda *args, **kwargs: _contract(ok=True))
    monkeypatch.setattr(
        runbook.maintenance_packet,
        "build_maintenance_window_packet",
        lambda *args, **kwargs: _maintenance(writer_quiescent=True),
    )

    report = runbook.build_operator_runbook(
        tmp_path / "archive.db",
        backup_dir=tmp_path,
        generated_at="20260707T020000Z",
    )

    assert report["ready_for_controlled_apply_now"] is True
    assert report["start_blockers"] == []
    assert report["awc_terminal_scope"]["contract_operation_count"] == 2
    assert report["impact_summary"]["archive_db_apply_steps"] == ["apply_awc_legacy_terminal_closeout"]
    assert report["impact_summary"]["manifest_write_steps"] == [
        "emit_decision_manifest_template",
        "operator_reviews_decision_manifest",
    ]
    assert report["confirmation_phrases"]["apply_awc_legacy_terminal_closeout"] == (
        "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT"
    )
