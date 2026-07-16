from __future__ import annotations

from scripts import awc_signal_timing_contract_audit as audit


def test_awc_signal_timing_contract_audit_passes():
    report = audit.build_audit()

    assert report["ok"] is True
    assert report["checks"]["contract_has_awc_valid_time"] is True
    assert report["checks"]["contract_has_ssot_times"] is True
    assert report["checks"]["contract_has_decision_job_times"] is True
    assert report["checks"]["contract_has_order_times"] is True
    assert report["checks"]["missing_awc_valid_time_blocks"] is True
    assert report["checks"]["missing_order_execution_time_blocks"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["fetches_orderbook"] is False
    assert report["guards"]["places_orders"] is False


def test_awc_signal_timing_contract_blocks_missing_valid_time_contract(monkeypatch):
    base = audit.replay_audit.ssot_chain_replay_input_contract()
    slo = dict(base["awc_signal_to_order_slo"])
    slo["required_timing_keys"] = [
        key for key in slo["required_timing_keys"] if key != "awc_valid_time"
    ]
    base["awc_signal_to_order_slo"] = slo
    monkeypatch.setattr(audit.replay_audit, "ssot_chain_replay_input_contract", lambda: base)

    report = audit.build_audit()

    assert report["ok"] is False
    assert "awc_timing_contract_required_keys_missing" in report["blockers"]
    assert report["checks"]["contract_has_awc_valid_time"] is False


def test_awc_signal_timing_fixture_blocks_missing_job_time():
    timing = audit._without_keys("job_enqueued_at")

    report = audit.timing_fixture_report(timing)

    assert report["ok"] is False
    assert "submitted_signal_execution_safety_coverage_incomplete" in report["blockers"]
    assert "awc_signal_timing_required_keys_incomplete" in report["blockers"]


def test_awc_signal_timing_contract_blocks_missing_aggregate_policy(monkeypatch):
    base = audit.replay_audit.ssot_chain_replay_input_contract()
    policy = dict(base["missing_policy"])
    policy.pop("accepted_signal_base_timing_incomplete", None)
    base["missing_policy"] = policy
    monkeypatch.setattr(audit.replay_audit, "ssot_chain_replay_input_contract", lambda: base)

    report = audit.build_audit()

    assert report["ok"] is False
    assert "awc_timing_missing_policy_keys_missing" in report["blockers"]
    assert report["checks"]["missing_policy_covers_aggregate_blockers"] is False
