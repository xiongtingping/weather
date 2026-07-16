from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/production_probability_transaction_b_cutover_readiness.py"
spec = importlib.util.spec_from_file_location("transaction_b_cutover_readiness", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_readiness_separates_safe_default_candidate_from_production_cutover(tmp_path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    for table in module.REQUIRED_TABLES:
        conn.execute(f"CREATE TABLE {table}(id TEXT)")
    conn.close()
    _write(tmp_path / "config/production_probability_runtime_activation.json", json.dumps({"schema": "production_probability_runtime_activation_v2", "state": "disabled", "places_orders": False, "authorizes_clob": False}))
    _write(tmp_path / "production_sequential_bayes_public_ssot_writer.py", "production_transaction_b_refuses_shadow_database\nBEGIN IMMEDIATE\nrequires_no_ambient_transaction\nproduction_transaction_b_frozen_p0_context_drift\nupdate_latest=publish_latest\nlatest_repointed\n")
    _write(tmp_path / "production_sequential_bayes_transaction_b_host.py", 'value.get("candidate_mode")) != "production"\nvalue.get("shadow_only") is not False\n')
    _write(tmp_path / "production_sequential_bayes_candidate_compiler.py", 'compile_production_transaction_b_candidate\ncandidate_mode="production"\n')
    _write(tmp_path / "production_initial_posterior_checkpoint.py", "activation_prior_artifact_mismatch activation_weak_artifact_mismatch activation_artifact_mismatch")
    _write(tmp_path / "production_initial_posterior_writer.py", "BEGIN IMMEDIATE production_initial_checkpoint_requires_no_ambient_transaction validate_production_probability_activation")
    _write(tmp_path / "production_probability_activation_contract.py", "prior_model_artifact_sha256 weak_likelihood_artifact_sha256 strong_lr_artifact_sha256 bayes_formula_id composer_schema checkpoint_schema strong_update_schema public_ssot_schema runtime_manifest_sha256")
    _write(
        tmp_path / "launchd/com.weather.monitor.daemon.plist",
        "AMSC_ZUUU_APPLY scripts/polymarket_monitor_daemon.sh",
    )
    _write(
        tmp_path / "scripts/amsc_zuuu_metar_taf_poll_once.sh",
        'PRODUCTION_PROBABILITY_UPDATE_ENABLED="${AMSC_ZUUU_PRODUCTION_PROBABILITY_UPDATE_ENABLED:-1}" apply_amsc_probability_fast_lane_from_plan.py',
    )
    _write(tmp_path / "production_probability_p0_runtime.py", "build_production_p0_inputs compile_production_p0_candidate bind_full_physical_p0_prior_context_for_production bind_live_joint_weak_likelihood_for_production")
    _write(tmp_path / "production_sequential_bayes_event_runtime.py", "load_canonical_observation_event build_canonical_event_transaction_b_candidate ProductionSequentialBayesTransactionBHost")
    _write(tmp_path / "production_canonical_event_drain_runtime.py", "selected_source_id='zuuu_metar_raw_amsc' places_orders load_latest_persisted_checkpoint consumed_event_ids")
    _write(tmp_path / "production_amsc_archive_probability_ingress.py", "apply_p2_canonical_amsc_forward_append drain_production_canonical_events canonical_retained_probability_blocked")
    _write(tmp_path / "production_weak_forecast_revision_writer.py", "BEGIN IMMEDIATE weak_forecast_revision_updates places_orders")
    _write(tmp_path / "production_weak_forecast_revision_event_runtime.py", "compile_weak_forecast_revision_candidate places_orders")
    _write(tmp_path / "scripts/watch_zuuu_speci_once.py", "apply_production_amsc_archive_probability_ingress --production-probability-ingress")

    result = module.build_readiness(db, root=tmp_path)

    assert result["ok"] is True
    assert result["state"] == "pre_cutover_ready"
    assert result["production_cutover"]["ok"] is False
    assert "legacy_probability_writer_still_active" not in result["production_cutover"]["blockers"]
    assert result["source_fact_ingress"]["amsc_source_fact_writer_present"] is True
    assert result["legacy_writer"]["regular_metar_legacy_posterior_enqueue_enabled"] is False
    assert result["legacy_writer"]["speci_legacy_posterior_materializer_present"] is False


def test_enabled_orderless_activation_is_not_rejected_as_non_default(tmp_path, monkeypatch) -> None:
    test_readiness_separates_safe_default_candidate_from_production_cutover(tmp_path)
    activation_path = tmp_path / "config/production_probability_runtime_activation.json"
    activation_path.write_text(json.dumps({
        "schema": "production_probability_runtime_activation_v2",
        "state": "production_enabled",
        "places_orders": False,
        "authorizes_clob": False,
    }), encoding="utf-8")
    monkeypatch.setattr(module, "load_production_probability_activation", lambda _path: {})
    monkeypatch.setattr(module, "_runtime_monitor_deployment", lambda _root: {"ready": True})

    result = module.build_readiness(tmp_path / "archive.db", root=tmp_path)

    assert "production_probability_activation_must_be_orderless" not in result["production_cutover"]["blockers"]
    assert "production_probability_activation_not_enabled" not in result["production_cutover"]["blockers"]
