from scripts.forecast_source_weight_plan_completion_audit import build_completion_audit


def test_completion_audit_does_not_confuse_engineering_files_with_real_external_evidence():
    report = build_completion_audit()
    assert report["ok"] is False
    assert "dod_1_external_immutable_training_evidence" in report["blockers"]
    assert "dod_6_contextual_strict_oos" in report["blockers"]
    assert report["guards"]["changes_parameters"] is False
