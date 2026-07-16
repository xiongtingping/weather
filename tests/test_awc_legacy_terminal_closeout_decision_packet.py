from scripts import awc_legacy_terminal_closeout_decision_packet as packet


def _replay_report():
    return {
        "schema": "ssot_chain_replay_audit_v1",
        "ok": False,
        "read_only": True,
        "since": "2026-07-04T00:00:00+00:00",
        "production_coverage": {
            "terminal_coverage": {
                "non_final_terminal_signal_ids": ["sig_blocked", "sig_fact_only"],
            },
            "ledger_coverage": {
                "missing_ledger_signal_ids": ["sig_blocked", "sig_fact_only"],
            },
        },
        "signal_details": [
            {
                "signal_id": "sig_blocked",
                "timings": [
                    {
                        "source": "auto_trade_jobs",
                        "meta": {"job_id": 1},
                        "timing": {
                            "signal_id": "sig_blocked",
                            "acceptance_state": "accepted",
                            "awc_hard_source_signal_at": "2026-07-04T10:06:36Z",
                            "terminal_state": "job_enqueued",
                            "terminal_reason": "awaiting_order_submit",
                        },
                    }
                ],
            },
            {
                "signal_id": "sig_fact_only",
                "timings": [
                    {
                        "source": "event_fact_history",
                        "meta": {"fact_id": 2},
                        "timing": {
                            "signal_id": "sig_fact_only",
                            "acceptance_state": "accepted",
                            "awc_hard_source_signal_at": "2026-07-04T10:06:52Z",
                            "terminal_state": "fact_written",
                            "terminal_reason": "awaiting_decision_write",
                        },
                    }
                ],
            },
        ],
    }


def test_awc_legacy_terminal_packet_requires_manifest(monkeypatch):
    monkeypatch.setattr(
        packet,
        "_query_decision_evidence",
        lambda *args, **kwargs: {
            "sig_blocked": [
                {
                    "decision_row_id": 10,
                    "created_at": "2026-07-04T10:06:48Z",
                    "timing": {
                        "signal_id": "sig_blocked",
                        "terminal_state": "blocked",
                        "terminal_reason": "order_already_live",
                        "awc_hard_source_signal_at": "2026-07-04T10:06:36Z",
                    },
                }
            ],
            "sig_fact_only": [],
        },
    )

    result = packet.build_packet(replay_report=_replay_report())

    assert result["ok"] is False
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["candidate_count"] == 2
    assert result["recommended_materialize_count"] == 1
    assert result["operator_review_required_count"] == 1
    assert "awc_legacy_terminal_closeout_manifest_missing" in result["hard_blockers"]
    by_id = {row["signal_id"]: row for row in result["candidates"]}
    assert by_id["sig_blocked"]["recommended_decision"] == "materialize_existing_final_terminal"
    assert by_id["sig_fact_only"]["recommended_decision"] == "undecided"


def test_awc_legacy_terminal_packet_builds_operations_from_reviewed_manifest(monkeypatch):
    monkeypatch.setattr(
        packet,
        "_query_decision_evidence",
        lambda *args, **kwargs: {
            "sig_blocked": [
                {
                    "decision_row_id": 10,
                    "created_at": "2026-07-04T10:06:48Z",
                    "timing": {
                        "signal_id": "sig_blocked",
                        "terminal_state": "blocked",
                        "terminal_reason": "order_already_live",
                        "awc_hard_source_signal_at": "2026-07-04T10:06:36Z",
                    },
                }
            ],
            "sig_fact_only": [],
        },
    )
    baseline = packet.build_packet(replay_report=_replay_report())
    manifest = baseline["decision_manifest_template"]
    for row in manifest["decisions"]:
        if row["signal_id"] == "sig_blocked":
            row["decision"] = "materialize_existing_final_terminal"
        else:
            row["decision"] = "mark_reviewed_no_action"
            row["review_reason"] = "Legacy accepted signal wrote fact but no decision/job was emitted."

    result = packet.build_packet(replay_report=_replay_report(), decision_manifest=manifest)
    audit = result["decision_manifest_audit"]

    assert result["ok"] is True
    assert audit["ok"] is True
    assert audit["operation_count"] == 2
    operations = {item["signal_id"]: item for item in audit["operations"]}
    assert operations["sig_blocked"]["terminal_state"] == "blocked"
    assert operations["sig_fact_only"]["terminal_state"] == "no_action"
    assert operations["sig_blocked"]["write_targets"] == [
        "awc_hard_source_signals",
        "signal_to_order_timing",
    ]


def test_awc_legacy_terminal_packet_rejects_materialize_without_final_evidence(monkeypatch):
    monkeypatch.setattr(packet, "_query_decision_evidence", lambda *args, **kwargs: {})
    baseline = packet.build_packet(replay_report=_replay_report())
    manifest = baseline["decision_manifest_template"]
    for row in manifest["decisions"]:
        row["decision"] = "materialize_existing_final_terminal"

    result = packet.build_packet(replay_report=_replay_report(), decision_manifest=manifest)

    assert result["ok"] is False
    assert "awc_legacy_terminal_closeout_existing_final_evidence_missing" in result["hard_blockers"]
