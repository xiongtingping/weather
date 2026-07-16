from scripts.live_offline_evidence_isolation_audit import audit_live_offline_evidence_isolation


def test_default_live_surfaces_do_not_import_offline_sample_or_oos_gates():
    report = audit_live_offline_evidence_isolation()
    assert report["ok"] is True, report["violations"]
    assert report["boundary"]["live_amsc_ingest_not_blocked_by_offline_evidence"] is True


def test_detects_offline_gate_import_in_live_surface(tmp_path):
    path = tmp_path / "bad_live.py"
    path.write_text("from scripts.v2_training_data_readiness_audit import main\n", encoding="utf-8")
    report = audit_live_offline_evidence_isolation([path])
    assert report["ok"] is False
    assert report["violations"][0]["offline_gate_imports"] == ["v2_training_data_readiness_audit"]
