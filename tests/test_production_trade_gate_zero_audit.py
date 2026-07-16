from scripts.production_trade_gate_zero_audit import run_audit


def test_production_trade_gate_zero_audit_passes() -> None:
    payload = run_audit()
    assert payload["ok"] is True, payload
    assert payload["blockers"] == []
    assert payload["checks"]["physical_report_to_market_bucket_direct_mapping_retired"] is True
