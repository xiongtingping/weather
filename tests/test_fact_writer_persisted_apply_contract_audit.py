from scripts import fact_writer_persisted_apply_contract_audit as audit


def test_fact_writer_persisted_apply_contract_audit_covers_success_and_blocked_paths():
    report = audit.build_fact_writer_persisted_apply_contract_audit()

    assert report["schema"] == audit.SCHEMA_VERSION
    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["writes_facts_sqlite"] is False
    assert report["guards"]["places_orders"] is False
    assert report["guards"]["uses_temporary_sqlite_fixture"] is True
    assert report["coverage"]["checked_fixture_count"] == 2
    assert report["blockers"] == []

    fixtures = {item["fixture"]: item for item in report["fixtures"]}
    persisted = fixtures["persisted_verified"]
    blocked = fixtures["blocked_before_persist"]

    assert persisted["ok"] is True
    assert persisted["writer_result_ok"] is True
    assert persisted["writer_result_applied"] is True
    assert persisted["persisted_row_count"] == 1
    assert persisted["contract"]["target_bucket"] == "31"
    assert persisted["contract"]["post_apply_persisted_verification_ok"] is True
    assert persisted["contract"]["posterior_ssot_apply_contract_ok"] is True

    assert blocked["ok"] is True
    assert blocked["writer_result_ok"] is False
    assert blocked["writer_result_applied"] is False
    assert blocked["persisted_row_count"] == 0
    assert blocked["contract"]["post_apply_persisted_verification_ok"] is False
    assert blocked["contract"]["posterior_ssot_apply_contract_ok"] is False

