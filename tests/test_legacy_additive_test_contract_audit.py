import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "legacy_additive_test_contract_audit.py"
spec = importlib.util.spec_from_file_location("legacy_additive_test_contract_audit", SCRIPT_PATH)
audit_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit_mod)


def test_legacy_additive_test_contract_audit_classifies_old_test_contracts(tmp_path):
    test_file = tmp_path / "test_bayes.py"
    test_file.write_text(
        """
class LegacyTests:
    def test_old_runtime(self):
        assert meta["source"] == "continuous_temperature_posterior_v1"
        self.assertTrue(meta["old_additive_evidence_pool_used"])

    def test_old_channels(self):
        assert meta["wu_forecast_enabled"]
        assert meta["physical_likelihood_evidence"] == 12
        assert meta["hard_source_posterior_override_applied"]
        assert meta["post_cool_rebound_risk_evidence"] == 0
        """,
        encoding="utf-8",
    )

    report = audit_mod.build_legacy_additive_test_contract_audit(test_file)

    assert report["schema"] == "legacy_additive_test_contract_audit_v1"
    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["blocking_test_count"] == 2
    assert report["by_pattern"]["legacy_runtime_source"] == 1
    assert report["by_pattern"]["old_additive_bridge_active"] == 1
    assert report["by_pattern"]["forecast_weak_old_channels"] == 1
    assert report["by_pattern"]["metar_strong_old_channels"] == 1
    assert report["by_pattern"]["hard_source_old_override_meta"] == 1
    assert report["by_pattern"]["post_cool_rebound_old_channels"] == 1
    assert "migrate_legacy_additive_tests_to_hierarchical_contracts" in report["next_actions"]


def test_legacy_additive_test_contract_audit_passes_new_contracts(tmp_path):
    test_file = tmp_path / "test_bayes.py"
    test_file.write_text(
        """
class NewTests:
    def test_new_runtime(self):
        assert meta["source"] == "hierarchical_posterior_pipeline_runtime_v1"
        assert meta["layer_contributions"]["forecast_weak_likelihood"]
        assert meta["layer_contributions"]["metar_strong_likelihood"]
        """,
        encoding="utf-8",
    )

    report = audit_mod.build_legacy_additive_test_contract_audit(test_file)

    assert report["ok"] is True
    assert report["blocking_test_count"] == 0
    assert report["next_actions"] == []
