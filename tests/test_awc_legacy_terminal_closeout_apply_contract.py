from scripts import awc_legacy_terminal_closeout_apply_contract as contract


def _closeout_packet(*, ok=True, bad_operation=False):
    operations = [
        {
            "signal_id": "sig_blocked",
            "terminal_state": "blocked",
            "terminal_reason": "order_already_live",
            "payload": {
                "signal_id": "sig_blocked",
                "signal_to_order_timing": {
                    "signal_id": "sig_blocked",
                    "terminal_state": "blocked",
                    "terminal_reason": "order_already_live",
                    "terminal_at": "2026-07-04T10:06:48Z",
                },
            },
            "write_targets": ["awc_hard_source_signals", "signal_to_order_timing"],
            "uses_writers": ["upsert_awc_hard_source_signal", "upsert_signal_to_order_timing"],
        },
        {
            "signal_id": "sig_no_action",
            "terminal_state": "no_action",
            "terminal_reason": "reviewed legacy fact-only signal had no decision output",
            "payload": {
                "signal_id": "sig_no_action",
                "signal_to_order_timing": {
                    "signal_id": "sig_no_action",
                    "terminal_state": "no_action",
                    "terminal_reason": "reviewed legacy fact-only signal had no decision output",
                    "terminal_at": "2026-07-04T10:06:52Z",
                },
            },
            "write_targets": ["awc_hard_source_signals", "signal_to_order_timing"],
            "uses_writers": ["upsert_awc_hard_source_signal", "upsert_signal_to_order_timing"],
        },
    ]
    if bad_operation:
        operations[0]["write_targets"] = ["auto_trade_decisions"]
    return {
        "schema": "awc_legacy_terminal_closeout_decision_packet_v1",
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "candidate_count": 2,
        "hard_blockers": [] if ok else ["awc_legacy_terminal_closeout_manifest_missing"],
        "decision_manifest_audit": {
            "manifest_provided": ok,
            "ok": ok,
            "operation_count": len(operations) if ok else 0,
            "operations": operations if ok else [],
            "blockers": [] if ok else ["awc_legacy_terminal_closeout_manifest_missing"],
        },
        "guards": {
            "writes_archive_db": False,
            "places_orders": False,
        },
    }


def test_awc_legacy_terminal_apply_contract_blocks_missing_manifest():
    report = contract.build_contract(closeout_packet=_closeout_packet(ok=False))

    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert "awc_legacy_terminal_closeout_packet_not_ready" in report["blockers"]
    assert "decision_manifest_required" in report["blockers"]
    assert "no_apply_operations" in report["blockers"]


def test_awc_legacy_terminal_apply_contract_projects_writer_operations():
    report = contract.build_contract(closeout_packet=_closeout_packet(ok=True))
    summary = contract.compact_contract(report, max_items_output=1)

    assert report["ok"] is True
    assert report["operation_count"] == 2
    assert report["operator_contract"]["confirmation_phrase"] == contract.CONFIRMATION_PHRASE
    assert report["operator_contract"]["requires_backup"] is True
    assert report["operator_contract"]["requires_writer_quiescence"] is True
    assert "awc_legacy_terminal_closeout_apply.py" in report["operator_contract"]["apply_command_template"]
    assert report["operations"][0]["uses_writers"] == [
        "upsert_awc_hard_source_signal",
        "upsert_signal_to_order_timing",
    ]
    assert summary["operation_count"] == 2
    assert len(summary["operations"]) == 1
    assert summary["operations_truncated"] is True


def test_awc_legacy_terminal_apply_contract_rejects_wrong_write_target():
    report = contract.build_contract(closeout_packet=_closeout_packet(ok=True, bad_operation=True))

    assert report["ok"] is False
    assert "apply_operations_not_safe" in report["blockers"]
    assert report["operation_blockers"][0]["signal_id"] == "sig_blocked"
    assert "write_targets_not_exactly_awc_signal_and_timing_ledgers" in report["operation_blockers"][0]["blockers"]
