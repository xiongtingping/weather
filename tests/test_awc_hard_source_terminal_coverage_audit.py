from scripts import awc_hard_source_terminal_coverage_audit as audit


def _replay_report(*, ok=True):
    coverage_blockers = [] if ok else [
        "accepted_signal_terminal_coverage_incomplete",
        "accepted_signal_missing_signal_to_order_ledger",
    ]
    replay_blockers = [] if ok else ["signal_terminal_non_final"]
    terminal_state_counts = {"submitted": 1} if ok else {"job_enqueued": 1}
    return {
        "schema": "ssot_chain_replay_audit_v1",
        "ok": ok,
        "read_only": True,
        "signal_count": 1,
        "blockers": replay_blockers,
        "warnings": [],
        "candidate_scope_counts": {"accepted_awc_hard_source_signal": 1},
        "terminal_state_counts": terminal_state_counts,
        "source_counts": {"signal_to_order_timing": 1} if ok else {},
        "blocker_counts": {item: 1 for item in replay_blockers},
        "warning_counts": {},
        "production_coverage": {
            "schema": "signal_to_order_production_coverage_v1",
            "read_only": True,
            "ok": ok,
            "coverage_blockers": coverage_blockers,
            "accepted_signal_count": 1,
            "accepted_signal_ids": ["sig_awc"],
            "submitted_signal_count": 1 if ok else 0,
            "submitted_signal_ids": ["sig_awc"] if ok else [],
            "terminal_coverage": {
                "ok": ok,
                "final_terminal_states": ["blocked", "error", "no_action", "skipped", "stale", "submitted"],
                "terminal_state_counts": terminal_state_counts,
                "missing_terminal_signal_ids": [],
                "non_final_terminal_signal_ids": [] if ok else ["sig_awc"],
                "missing_terminal_reason_signal_ids": [],
            },
            "ledger_coverage": {
                "ok": ok,
                "missing_ledger_signal_ids": [] if ok else ["sig_awc"],
            },
        },
    }


def test_awc_terminal_coverage_passes_when_replay_proves_final_terminal():
    report = audit.build_audit(replay_report=_replay_report(ok=True))

    assert report["ok"] is True
    assert report["accepted_signal_count"] == 1
    assert report["terminal_state_counts"] == {"submitted": 1}
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["places_orders"] is False


def test_awc_terminal_coverage_blocks_non_final_and_missing_ledger():
    report = audit.build_audit(replay_report=_replay_report(ok=False))
    summary = audit.compact_audit(report)

    assert report["ok"] is False
    assert "accepted_signal_terminal_coverage_incomplete" in report["blockers"]
    assert "accepted_signal_missing_signal_to_order_ledger" in report["blockers"]
    assert "signal_terminal_non_final" in report["blockers"]
    assert report["non_final_terminal_signal_ids"] == ["sig_awc"]
    assert report["missing_ledger_signal_ids"] == ["sig_awc"]
    assert summary["ok"] is False
    assert summary["non_final_terminal_signal_ids"] == ["sig_awc"]


def test_awc_terminal_coverage_can_require_sample():
    report = audit.build_audit(
        replay_report={
            "schema": "ssot_chain_replay_audit_v1",
            "ok": False,
            "read_only": True,
            "blockers": ["no_awc_hard_source_signals_found"],
            "warnings": [],
            "production_coverage": {
                "accepted_signal_count": 0,
                "terminal_coverage": {"terminal_state_counts": {}},
            },
        },
        require_signal_sample=True,
    )

    assert report["ok"] is False
    assert "awc_terminal_coverage_no_accepted_signal_sample" in report["blockers"]
