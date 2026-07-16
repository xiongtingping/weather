from __future__ import annotations

from t1_execution_activation_audit import (
    build_t1_execution_activation_audit,
    source_t1_contract_probe,
)


def _record(*, state: str) -> dict:
    return {
        "fact_id": 1,
        "execution_contract_observation_t1_gate": {
            "state": state,
            "observation_trigger_at": "2026-07-12T00:00:00Z",
            "observation_trigger_time_source": "source_to_posterior_timing.latest_source_first_actionable_seen_at",
            "observation_to_order_hard_deadline_at": "2026-07-12T00:00:05Z",
        },
    }


def _poller(*, started_at_epoch: float) -> dict:
    return {
        "pid": "42",
        "started_at_epoch": started_at_epoch,
        "command": "/usr/bin/python3 /Users/xiong/weather/scripts/hard_source_direct_poller.py --loop",
    }


def test_source_probe_requires_stale_observation_fail_closed() -> None:
    probe = source_t1_contract_probe()

    assert probe["ok"] is True
    assert probe["checks"] == {
        "fresh_contract_trade_eligible": True,
        "fresh_contract_carries_t1_fields": True,
        "stale_observation_fail_closed": True,
    }


def test_activation_requires_source_process_and_archived_forward_t1_evidence() -> None:
    result = build_t1_execution_activation_audit(
        execution_records=[_record(state="present")],
        process_rows=[_poller(started_at_epoch=101.0)],
        required_code_mtime_epoch=100.0,
    )

    assert result["ok"] is True
    assert result["state"] == "activation_evidence_ready"


def test_activation_refuses_old_poller_and_old_archived_contract() -> None:
    result = build_t1_execution_activation_audit(
        execution_records=[_record(state="missing")],
        process_rows=[_poller(started_at_epoch=90.0)],
        required_code_mtime_epoch=100.0,
    )

    assert result["ok"] is False
    assert result["state"] == "runtime_not_loaded"
    assert result["blockers"] == [
        "runtime_poller_loaded_after_t1_code",
        "forward_archive_contract_carries_t1_gate",
    ]
