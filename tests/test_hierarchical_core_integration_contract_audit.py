from scripts import hierarchical_core_integration_contract_audit as audit


def test_hierarchical_core_integration_contract_passes() -> None:
    report = audit.build_audit()

    assert report["ok"] is True
    assert report["read_only"] is True
    assert all(report["checks"].values())
    assert report["posterior_ssot_fact"]["state"] == "available"
    assert report["posterior_ssot_fact"]["target_bucket"] == "31°C"
    assert report["fact_writer"]["posterior_ssot_apply_contract"]["ok"] is True
    assert report["auto_trade_candidate_contract"]["details"]["decision_ssot"] == "posterior_ssot_fact"
    assert report["daily_review_contract"]["first_bad_link"] == "none"
    assert report["daily_review_contract"]["daily_calibration_state"] == "no_apply"
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["writes_auto_trade_decisions"] is False
    assert report["guards"]["places_orders"] is False


def test_hierarchical_core_integration_contract_blocks_ui_target_fork() -> None:
    def mutate_ui_item(ui_item):
        ui_item["posterior_ssot_fact"]["target_bucket"] = "32°C"

    report = audit.build_audit(mutate_ui_item=mutate_ui_item)

    assert report["ok"] is False
    assert "ui_snapshot_lineage:ui_snapshot_posterior_ssot_fact_target_bucket_mismatch" in report["blockers"]
    assert report["checks"]["ui_snapshot_reads_posterior_ssot"] is False


def test_hierarchical_core_integration_contract_blocks_auto_trade_target_fork() -> None:
    def mutate_decision_payload(decision_payload):
        decision_payload["outcome"] = "32°C"
        decision_payload["details"]["posterior_settlement_target_bucket"] = "32°C"

    report = audit.build_audit(mutate_decision_payload=mutate_decision_payload)

    assert report["ok"] is False
    assert "auto_trade_candidate_row_target_matches_ssot_failed" in report["blockers"]
    assert "auto_trade_candidate_details_target_matches_ssot_failed" in report["blockers"]
    assert report["checks"]["auto_trade_candidate_reads_posterior_ssot"] is False
