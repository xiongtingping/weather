import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "legacy_evidence_migration_audit.py"
spec = importlib.util.spec_from_file_location("legacy_evidence_migration_audit", SCRIPT_PATH)
audit_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit_mod)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_legacy_evidence_audit_classifies_source_references(tmp_path):
    _write(
        tmp_path / "bayesian_probability_runtime.py",
        """
score = q_bayes_model_evidence + q_bayes_temperature_trajectory_evidence
row["q_bayes_physical_likelihood_evidence_scale_details"] = {}
row["q_bayes_climo_regime_evidence_raw"] = 1
        """,
    )
    _write(
        tmp_path / "ui" / "index.html",
        """
const risk = row.q_bayes_post_cool_rebound_risk_evidence;
const taf = row.q_bayes_taf_peak_evidence;
        """,
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["schema"] == "legacy_evidence_migration_audit_v2"
    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["guards"]["writes_auto_trade_decisions"] is False
    assert report["field_reference_count"] == 6
    assert report["unclassified_fields"] == []
    assert report["production_allowed_fields"] == []
    assert report["ui_primary_allowed_fields"] == []
    assert report["usage_gate"]["ok"] is False
    assert report["usage_gate"]["production_blocking_reference_count"] == 0
    assert report["usage_gate"]["ui_primary_blocking_reference_count"] == 2
    assert report["usage_gate"]["production_blocking_files"] == []
    assert report["usage_gate"]["ui_primary_blocking_files"] == ["ui/index.html"]
    assert report["usage_gate"]["production_blocking_fields"] == []
    assert "q_bayes_taf_peak_evidence" in report["usage_gate"]["ui_primary_blocking_fields"]
    assert report["by_target_layer"]["forecast_weak_likelihood"] == 2
    assert report["by_target_layer"]["metar_strong_likelihood_internal"] == 2
    assert report["by_target_layer"]["root_prior_weather_type_adjustment"] == 1
    assert report["by_target_layer"]["diagnostic_only"] == 1
    physical = [row for row in report["fields"] if row["field"] == "q_bayes_physical_likelihood_evidence"][0]
    assert physical["samples"][0]["field"] == "q_bayes_physical_likelihood_evidence_scale_details"
    assert physical["samples"][0]["usage_role"] == "production_compat_surface"
    assert physical["usage"]["production_blocking_reference_count"] == 0


def test_legacy_evidence_audit_main_summary_prints_json(tmp_path, capsys):
    _write(
        tmp_path / "ui" / "server.py",
        'row.get("q_bayes_wu_forecast_evidence")\n',
    )

    code = audit_mod.main(["--root", str(tmp_path), "--summary", "--max-samples-per-field", "1"])
    out = capsys.readouterr().out

    assert code == 0
    assert "legacy_evidence_migration_audit_v2" in out
    assert '"writes_archive_db": false' in out
    assert "forecast_weak_likelihood" in out
    assert "legacy_evidence_usage_gate_v1" in out
    assert "ui_primary_or_api_surface_references_legacy_evidence" in out


def test_legacy_evidence_audit_allows_migration_and_diagnostic_references(tmp_path):
    _write(
        tmp_path / "legacy_evidence_migration_registry.py",
        'FIELD = "q_bayes_model_evidence"\n',
    )
    _write(
        tmp_path / "scripts" / "backtest_probe.py",
        'row.get("q_bayes_temperature_trajectory_evidence")\n',
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["ok"] is True
    assert report["usage_gate"]["ok"] is True
    assert report["usage_gate"]["production_blocking_reference_count"] == 0
    assert report["usage_gate"]["ui_primary_blocking_reference_count"] == 0
    assert report["usage_gate"]["allowed_reference_count"] == 2
    assert report["usage_gate"]["by_usage_role"]["migration_governance"] == 1
    assert report["usage_gate"]["by_usage_role"]["diagnostic_or_backtest"] == 1


def test_legacy_evidence_audit_demotes_ui_server_retired_compat_scope(tmp_path):
    _write(
        tmp_path / "ui" / "server.py",
        """
def _bayes_chain_for_distribution_row(row):
    return row.get("q_bayes_model_evidence")

def _legacy_bayes_chain_for_distribution_row(row):
    return row.get("q_bayes_wu_forecast_evidence")

def current_primary_ui_payload(row):
    return row.get("q_bayes_taf_peak_evidence")
        """,
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["ok"] is False
    assert report["usage_gate"]["by_usage_role"]["ui_retired_compat_surface"] == 2
    assert report["usage_gate"]["by_usage_role"]["ui_primary_surface"] == 1
    assert report["usage_gate"]["ui_primary_blocking_reference_count"] == 1
    assert report["usage_gate"]["warning_reference_count"] == 2
    warning_scopes = {row["scope"] for row in report["usage_gate"]["warning_samples"]}
    assert warning_scopes == {"_bayes_chain_for_distribution_row", "_legacy_bayes_chain_for_distribution_row"}
    blocker = report["usage_gate"]["blocker_samples"][0]
    assert blocker["scope"] == "current_primary_ui_payload"


def test_legacy_evidence_audit_demotes_ui_server_compat_status_scopes(tmp_path):
    _write(
        tmp_path / "ui" / "server.py",
        """
def _ui_snapshot_soft_forecast_channels(payload, distribution):
    return row0.get("q_bayes_wu_forecast_evidence")

def _ui_temperature_trajectory_status_from_payload(payload, distribution):
    return row0.get("q_bayes_temperature_trajectory_evidence")

def render_primary(payload):
    return payload.get("q_bayes_observed_nowcast_evidence")
        """,
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["ok"] is False
    assert report["usage_gate"]["by_usage_role"]["ui_retired_compat_surface"] == 2
    assert report["usage_gate"]["by_usage_role"]["ui_primary_surface"] == 1
    assert report["usage_gate"]["ui_primary_blocking_reference_count"] == 1
    assert report["usage_gate"]["warning_reference_count"] == 2
    warning_scopes = {row["scope"] for row in report["usage_gate"]["warning_samples"]}
    assert warning_scopes == {
        "_ui_snapshot_soft_forecast_channels",
        "_ui_temperature_trajectory_status_from_payload",
    }


def test_legacy_evidence_audit_demotes_ui_server_drop_only_constant(tmp_path):
    _write(
        tmp_path / "ui" / "server.py",
        """
def _attach_physical_likelihood_audit_fields_from_features(row):
    return row

_FAST_TRADE_ROW_DROP_KEYS = {
    "q_bayes_physical_likelihood_evidence_scale_details",
}

def render_primary(payload):
    return payload.get("q_bayes_physical_likelihood_evidence")
        """,
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["ok"] is False
    assert report["usage_gate"]["by_usage_role"]["ui_retired_compat_surface"] == 1
    assert report["usage_gate"]["by_usage_role"]["ui_primary_surface"] == 1
    assert report["usage_gate"]["ui_primary_blocking_reference_count"] == 1
    assert report["usage_gate"]["warning_reference_count"] == 1
    warning = report["usage_gate"]["warning_samples"][0]
    assert warning["scope"] == "_FAST_TRADE_ROW_DROP_KEYS"
    blocker = report["usage_gate"]["blocker_samples"][0]
    assert blocker["scope"] == "render_primary"


def test_legacy_evidence_audit_allows_hard_source_drop_only_compaction_scope(tmp_path):
    _write(
        tmp_path / "scripts" / "hard_source_direct_poller.py",
        """
def _compact_hard_source_fact_payload_for_writer(payload):
    for key in ("q_bayes_physical_likelihood_evidence_scale_details",):
        payload.pop(key, None)
        """,
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["ok"] is True
    assert report["usage_gate"]["ok"] is True
    assert report["usage_gate"]["by_usage_role"]["production_compat_surface"] == 1
    assert report["usage_gate"]["warning_reference_count"] == 0
    assert report["usage_gate"]["warning_files"] == []
    assert report["fields"][0]["samples"][0]["usage_reason"].startswith(
        "production_drop_only_legacy_field_compaction_scope"
    )


def test_legacy_evidence_audit_demotes_ui_index_retired_compat_scope(tmp_path):
    _write(
        tmp_path / "ui" / "index.html",
        """
function bayesChainForDistributionRow(row) {
  return row.q_bayes_model_evidence;
}

function firstPhysicalLikelihoodRow(rows) {
  return rows[0].q_bayes_physical_likelihood_evidence;
}

function legacyPhysicalLikelihoodSnapshotCompatFromItem(item) {
  return item.distribution[0].q_bayes_independent_physics_residual_evidence;
}

function legacyDistributionLikelihoodCompatForRow(row) {
  return row.q_bayes_temperature_trajectory_evidence;
}

function renderPrimaryPanel(row) {
  return row.q_bayes_taf_peak_evidence;
}
        """,
    )

    report = audit_mod.build_legacy_evidence_migration_audit(tmp_path, max_samples_per_field=5)

    assert report["ok"] is False
    assert report["usage_gate"]["by_usage_role"]["ui_retired_compat_surface"] == 4
    assert report["usage_gate"]["by_usage_role"]["ui_primary_surface"] == 1
    assert report["usage_gate"]["ui_primary_blocking_reference_count"] == 1
    assert report["usage_gate"]["warning_reference_count"] == 4
    warning_scopes = {row["scope"] for row in report["usage_gate"]["warning_samples"]}
    assert warning_scopes == {
        "bayesChainForDistributionRow",
        "firstPhysicalLikelihoodRow",
        "legacyPhysicalLikelihoodSnapshotCompatFromItem",
        "legacyDistributionLikelihoodCompatForRow",
    }
    blocker = report["usage_gate"]["blocker_samples"][0]
    assert blocker["scope"] == "renderPrimaryPanel"
