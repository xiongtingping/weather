from scripts import awc_legacy_terminal_closeout_review_report as report


def _closeout_packet():
    return {
        "schema": "awc_legacy_terminal_closeout_decision_packet_v1",
        "ok": False,
        "read_only": True,
        "dry_run": True,
        "candidate_count": 2,
        "recommended_materialize_count": 1,
        "operator_review_required_count": 1,
        "candidates": [
            {
                "signal_id": "sig_blocked",
                "source_event_id": "source_event_1",
                "awc_valid_time": "2026-07-04T10:00:00Z",
                "awc_hard_source_signal_at": "2026-07-04T10:06:36Z",
                "recommended_decision": "materialize_existing_final_terminal",
                "requires_operator_review": False,
                "current_terminal_state": "blocked",
                "current_terminal_reason": "order_already_live",
                "best_evidence_source": "auto_trade_decisions",
                "source_evidence_ref": "auto_trade_decisions.id=10",
                "best_evidence": {
                    "source": "auto_trade_decisions",
                    "terminal_state": "blocked",
                    "terminal_reason": "order_already_live",
                    "meta": {"decision_row_id": 10, "created_at": "2026-07-04T10:06:48Z"},
                    "timing": {
                        "signal_id": "sig_blocked",
                        "awc_hard_source_signal_at": "2026-07-04T10:06:36Z",
                        "decision_written_at": "2026-07-04T10:06:44Z",
                        "terminal_state": "blocked",
                        "terminal_reason": "order_already_live",
                    },
                },
            },
            {
                "signal_id": "sig_fact_only",
                "source_event_id": "source_event_1",
                "awc_valid_time": "2026-07-04T10:00:00Z",
                "awc_hard_source_signal_at": "2026-07-04T10:06:52Z",
                "recommended_decision": "undecided",
                "requires_operator_review": True,
                "current_terminal_state": "fact_written",
                "current_terminal_reason": "awaiting_decision_write",
                "best_evidence_source": "event_fact_history",
                "source_evidence_ref": "event_fact_history:sig_fact_only",
                "best_evidence": {
                    "source": "event_fact_history",
                    "terminal_state": "fact_written",
                    "terminal_reason": "awaiting_decision_write",
                    "meta": {"fact_id": 2, "fact_snapshot_id": "fact_1"},
                    "timing": {
                        "signal_id": "sig_fact_only",
                        "awc_hard_source_signal_at": "2026-07-04T10:06:52Z",
                        "fact_written_at": "2026-07-04T10:06:52Z",
                        "terminal_state": "fact_written",
                        "terminal_reason": "awaiting_decision_write",
                    },
                },
            },
        ],
        "decision_manifest_template": {
            "schema": "awc_legacy_terminal_closeout_decision_manifest_v1",
            "read_only_template": True,
            "decision_count": 2,
            "decisions": [
                {
                    "signal_id": "sig_blocked",
                    "recommended_decision": "materialize_existing_final_terminal",
                    "decision": "undecided",
                    "allowed_decisions": [
                        "undecided",
                        "materialize_existing_final_terminal",
                        "mark_reviewed_no_action",
                    ],
                    "review_reason": "",
                    "operator": "",
                    "reviewed_at": "",
                    "source_evidence_ref": "auto_trade_decisions.id=10",
                },
                {
                    "signal_id": "sig_fact_only",
                    "recommended_decision": "undecided",
                    "decision": "undecided",
                    "allowed_decisions": [
                        "undecided",
                        "materialize_existing_final_terminal",
                        "mark_reviewed_no_action",
                    ],
                    "review_reason": "",
                    "operator": "",
                    "reviewed_at": "",
                    "source_evidence_ref": "event_fact_history:sig_fact_only",
                },
            ],
        },
        "hard_blockers": ["awc_legacy_terminal_closeout_manifest_missing"],
        "guards": {"writes_archive_db": False},
    }


def test_awc_terminal_review_report_keeps_operator_required_candidate_undecided():
    result = report.build_review_report(closeout_packet=_closeout_packet())
    draft_by_id = {row["signal_id"]: row for row in result["draft_manifest"]["decisions"]}
    item_by_id = {row["signal_id"]: row for row in result["review_items"]}

    assert result["ok"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["recommended_materialize_count"] == 1
    assert result["operator_review_required_count"] == 1
    assert result["materialize_ready_signal_ids"] == ["sig_blocked"]
    assert result["review_required_signal_ids"] == ["sig_fact_only"]
    assert draft_by_id["sig_blocked"]["decision"] == "materialize_existing_final_terminal"
    assert draft_by_id["sig_fact_only"]["decision"] == "undecided"
    assert item_by_id["sig_blocked"]["evidence_strength"] == "final_terminal_evidence"
    assert item_by_id["sig_fact_only"]["evidence_strength"] == "fact_only_non_final_evidence"


def test_awc_terminal_review_report_compact_summary_limits_items():
    result = report.build_review_report(closeout_packet=_closeout_packet(), max_items_output=2)
    summary = report.compact_review_report(result, max_items_output=1)

    assert summary["candidate_count"] == 2
    assert len(summary["review_items"]) == 1
    assert summary["review_required_signal_ids"] == ["sig_fact_only"]
    assert summary["materialize_ready_signal_ids"] == ["sig_blocked"]

