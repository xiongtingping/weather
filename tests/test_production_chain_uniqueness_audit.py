import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "production_chain_uniqueness_audit.py"
spec = importlib.util.spec_from_file_location("production_chain_uniqueness_audit", SCRIPT_PATH)
audit_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(audit_mod)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_clean_backend(root: Path) -> None:
    _write(
        root / "production_probability_v6_activation_contract.py",
        '''
BAYES_FORMULA_ID = "p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_then_quantized_robust_time_conditioned_all_weather_forecast_regime_conditioned_canonical_joint_emission_then_regular_metar_hard_bound_then_reported_x_projection_v4"
COMPOSER_SCHEMA = "physical_state_bayes_v6_pre_event_predict_update"
PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v4_reported_x"
CHECKPOINT_SCHEMA = "posterior_checkpoint_v4_physical_state_bayes_v6"
        ''',
    )
    _write(
        root / "production_probability_v6_amsc_archive_ingress.py",
        '''
from production_probability_v6_canonical_event_drain_runtime import drain_v6_canonical_events
def apply_v6_amsc_archive_probability_ingress():
    return {"canonical_fact_commits_before_probability": True, "v5_fallback_forbidden": True}
        ''',
    )
    _write(
        root / "production_probability_v6_canonical_event_drain_runtime.py",
        "def pending_v6_canonical_event_ids(): pass\ndef commit_v6_public_physical_transaction(): pass\n",
    )
    _write(
        root / "production_probability_v6_weak_revision_event_runtime.py",
        "def apply_v6_taf_revision_from_source_observations(): pass\ndef commit_v6_weak_revision_transaction(): pass\n",
    )
    _write(
        root / "production_probability_v6_public_ssot_writer.py",
        '''
def validate_v6_activation(): pass
def commit_v6_public_physical_transaction(): pass
BEGIN = "BEGIN IMMEDIATE"
        ''',
    )
    _write(
        root / "production_probability_v6_event_runtime.py",
        'predict_to_event = True\nbuild_joint_observation_emission = True\nbuild_v6_transaction_b_candidate = True\n',
    )
    _write(
        root / "production_probability_v6_candidate_compiler.py",
        'update_with_observation = True\nproject_reported_x = True\nmeta = {"no_post_emission_state_transition": True}\n',
    )
    _write(
        root / "physical_state_bayes_v6" / "core.py",
        'def predict_to_event(): pass\ndef update_with_observation():\n    return {"no_post_emission_state_transition": True}\n',
    )
    _write(
        root / "weather_fact_snapshot_writer.py",
        """
POSTERIOR_SSOT_FACT_SCHEMA = "bayes_posterior_ssot_fact_v1"
def build_fact_snapshot_write_plan(): pass
def apply_fact_snapshot_write_plan(): pass
def build_hierarchical_posterior_fact_payload(): pass
def build_hierarchical_posterior_fact_snapshot_write_plan(): pass
posterior_ssot_fact = {}
production_identity = {
    "production_model_family": "joint_weak_physical_strong",
    "production_probability_policy": "unique_joint_weak_physical_strong_only_fail_closed",
    "legacy_model_fallback_allowed": False,
}
        """,
    )
    _write(
        root / "posterior_pipeline_entry_runtime.py",
        """
from hierarchical_posterior_pipeline_runtime import SCHEMA_VERSION as HIERARCHICAL_PIPELINE_VERSION
from hierarchical_posterior_pipeline_runtime import build_hierarchical_posterior_pipeline
meta = {"single_hierarchical_entrypoint": True}
        """,
    )
    _write(
        root / "hierarchical_posterior_pipeline_runtime.py",
        """
from sequential_remaining_warming_bayes_runtime import build_sequential_remaining_warming_posterior
SCHEMA_VERSION = "hierarchical_posterior_pipeline_runtime_v1"
def build_hierarchical_posterior_pipeline():
    build_sequential_remaining_warming_posterior()
    return {
        "single_read_only_pipeline_entry": True,
        "legacy_bucket_product_posterior_not_used": True,
        "live_primary_posterior_composer": "joint_weak_physical_strong_sequential_composer_v1",
        "legacy_model_fallback_allowed": False,
    }
        """,
    )
    _write(
        root / "sequential_remaining_warming_bayes_runtime.py",
        """
SCHEMA_VERSION = "sequential_remaining_warming_bayes_runtime_v31"
def build_sequential_remaining_warming_posterior():
    return {
        "live_primary_composer": True,
        "legacy_bucket_product_used": False,
        "continuous_before_bucket_integration": True,
        "metar_valid_times_update_state_once": True,
    }
        """,
    )
    _write(root / "phase_weighting_runtime.py", "DEFAULT_PHASE_BETA_SCHEDULE = []\ndef build_phase_weighting_snapshot(): pass\n")
    _write(root / "forecast_weak_likelihood_runtime.py", "DEFAULT_GROUP_CONFIG = {}\ndef build_forecast_weak_likelihood(): pass\n")
    _write(root / "metar_strong_likelihood_runtime.py", "def build_metar_strong_likelihood(): pass\n")
    _write(root / "ui" / "index.html", "const snapshot = await fetch('/api/ui_snapshot');\n")
    _write(
        root / "scripts" / "check_launchd_weather.py",
        """
MONITOR_SIGNAL_LOG = "MONITOR_SIGNAL_LOG"
POLY_SIGNAL_PATH = "POLY_SIGNAL_PATH"
monitor_signal_log_equals_poly_signal_path = False
monitor_signal_log_pointing_to_poly_signal_path = "blocker"
        """,
    )
    _write(
        root / "launchd" / "com.weather.monitor.daemon.plist",
        "<plist><dict><key>Label</key><string>com.weather.monitor.daemon</string></dict></plist>",
    )
    _write(
        root / "scripts" / "auto_trade_watcher.py",
        """
def load_sms_sender():
    return None, False, ""
def read_new_lines():
    return []
        """,
    )
    _write(
        root / "scripts" / "live_signal_order_watch.py",
        """
import sqlite3
def _connect_ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        """,
    )
    _write(
        root / "launchd" / "com.weather.auto_trade_watcher.plist",
        "<plist><dict><key>Label</key><string>com.weather.auto_trade_watcher</string></dict></plist>",
    )
    _write(root / "scripts" / "clob_order_service.mjs", "function placeOrder(payload) { return payload; }\n")
    _write(root / "scripts" / "clob_order_service.sh", "exec node scripts/clob_order_service.mjs\n")
    _write(root / "scripts" / "polymarket_order.mjs", "console.log(JSON.stringify({ok:true}));\n")
    _write(
        root / "scripts" / "update_order_status.py",
        """
from auto_order_ledger_writer import update_auto_order_status_by_order_id
def main():
    return update_auto_order_status_by_order_id
        """,
    )
    _write(
        root / "launchd" / "com.weather.clob_order_service.plist",
        "<plist><dict><key>Label</key><string>com.weather.clob_order_service</string></dict></plist>",
    )
    _write(
        root / "scripts" / "trade_engine_loop.sh",
        """
python scan_weather_market.py --trade-sync-only
python scan_weather_market.py --auto-trade-job-worker --once
        """,
    )
    _write(
        root / "launchd" / "com.weather.trade_engine.plist",
        "<plist><dict><key>Label</key><string>com.weather.trade_engine</string></dict></plist>",
    )
    _write(
        root / "tests" / "test_bayesian_probability_runtime.py",
        """
class TestBayes:
    def test_hierarchical_runtime(self):
        assert True
        """,
    )


def test_production_chain_uniqueness_audit_passes_clean_backend_fixture(tmp_path):
    _write_clean_backend(tmp_path)

    report = audit_mod.build_production_chain_uniqueness_audit(tmp_path, db_path=None, max_samples_per_pattern=10)

    assert report["schema"] == "production_chain_uniqueness_audit_v1"
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["ok"] is True
    assert report["checks"] == {
        "legacy_evidence_not_production": True,
        "ui_no_frontend_recompute": True,
        "ui_read_path_no_archive_or_execution_side_effect": True,
        "fact_writer_single_canonical_owner": True,
        "v6_single_production_mainline": True,
        "monitor_signal_no_bot_signal_alias": True,
        "readonly_watcher_no_external_weather_or_posterior": True,
        "order_submitter_no_weather_fact_or_posterior_write": True,
        "trade_engine_only_sync_or_job_worker": True,
        "trajectory_breakout_diagnostic_only": True,
        "v6_runtime_identity_aligned": True,
        "document_authority_aligned": True,
    }


def test_production_chain_uniqueness_audit_blocks_ui_trace_market_refresh_side_effect(tmp_path):
    _write_clean_backend(tmp_path)
    _write(
        tmp_path / "ui" / "server.py",
        '''
def auto_trade_trace():
    def _compute_payload():
        _auto_trade_cleanup_stale_runs()
    def _refresh_async():
        _trace_refresh_market_ticks_for_blocked("2026-07-15", {})
        start_auto_trade_run("2026-07-15", force=True)
    now_ts = time.time()

def compute_global_order_list():
    pass
        ''',
    )

    report = audit_mod.build_production_chain_uniqueness_audit(
        tmp_path, db_path=None, max_samples_per_pattern=10
    )
    blocker_ids = {row["blocker"] for row in report["blockers"]}

    assert report["ok"] is False
    assert report["checks"]["ui_read_path_no_archive_or_execution_side_effect"] is False
    assert "ui_read_path_has_archive_or_execution_side_effect" in blocker_ids


def test_production_chain_uniqueness_audit_reports_second_chain_blockers(tmp_path):
    _write_clean_backend(tmp_path)
    _write(
        tmp_path / "ui" / "index.html",
        "const p = componentProbability('model', 'q_bayes_model_evidence');\n",
    )
    _write(
        tmp_path / "scripts" / "bad_fact_writer.py",
        'conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")\n',
    )
    _write(tmp_path / "legacy_additive_bayesian_probability_runtime.py", "# legacy runtime should be absent\n")
    _write(tmp_path / "bayesian_probability_runtime.py", "meta = {}\n")
    _write(
        tmp_path / "launchd" / "com.polymarket.monitor.daemon.plist",
        "<plist><dict><key>Label</key><string>com.polymarket.monitor.daemon</string></dict></plist>",
    )
    _write(
        tmp_path / "scripts" / "auto_trade_watcher.py",
        """
from scan_weather_market import SMS_RECIPIENT
from hierarchical_posterior_pipeline_runtime import build_hierarchical_posterior_pipeline
        """,
    )
    _write(
        tmp_path / "scripts" / "clob_order_service.mjs",
        """
// submitter must not write weather facts
const sql = "INSERT INTO event_fact_history(fact_json) VALUES (?)";
        """,
    )
    _write(
        tmp_path / "scripts" / "trade_engine_loop.sh",
        """
python scan_weather_market.py --scan-weather-today --auto-trade-enqueue
        """,
    )
    _write(
        tmp_path / "scripts" / "hard_source_direct_poller.py",
        """
posterior_target_path_diagnostic_only = False
        """,
    )

    report = audit_mod.build_production_chain_uniqueness_audit(tmp_path, db_path=None, max_samples_per_pattern=10)
    blocker_ids = {row["blocker"] for row in report["blockers"]}

    assert report["ok"] is False
    assert "ui_frontend_recomputes_bayes_or_evidence" in blocker_ids
    assert "fact_table_live_writer_not_canonical" in blocker_ids
    assert "unknown_fact_writer_surface_present" in blocker_ids
    assert "legacy_additive_runtime_file_present" in blocker_ids
    assert "retired_bayesian_probability_runtime_file_present" in blocker_ids
    assert "weather_repo_references_forbidden_polymarket_monitor_daemon" in blocker_ids
    assert "readonly_watcher_reads_external_weather_or_recomputes_posterior" in blocker_ids
    assert "order_submitter_writes_weather_facts_or_recomputes_posterior" in blocker_ids
    assert "trade_engine_runs_weather_scan_or_recomputes_posterior" in blocker_ids
    assert "trajectory_breakout_or_path_follow_not_diagnostic_only" in blocker_ids


def test_production_chain_uniqueness_audit_blocks_trajectory_breakout_entry_source(tmp_path):
    _write_clean_backend(tmp_path)
    _write(
        tmp_path / "scan_weather_market.py",
        """
out["posterior_target_direct_path_follow_diagnostic_only"] = False
selection = {"trajectory_candidate_applied_to_target": True}
        """,
    )

    report = audit_mod.build_production_chain_uniqueness_audit(tmp_path, db_path=None, max_samples_per_pattern=10)
    blocker_ids = {row["blocker"] for row in report["blockers"]}

    assert report["ok"] is False
    assert report["checks"]["trajectory_breakout_diagnostic_only"] is False
    assert "trajectory_breakout_or_path_follow_not_diagnostic_only" in blocker_ids
    gate = report["gates"]["trajectory_breakout_diagnostic_only"]
    assert gate["forbidden_sample_count"] >= 1


def test_production_chain_uniqueness_audit_blocks_retired_resolution_capture_daemon(tmp_path):
    _write_clean_backend(tmp_path)
    _write(
        tmp_path / "launchd" / "com.weather.target_rule_shadow_capture.plist",
        "<plist><dict><key>Label</key><string>com.weather.target_rule_shadow_capture</string></dict></plist>",
    )

    report = audit_mod.build_production_chain_uniqueness_audit(tmp_path, db_path=None, max_samples_per_pattern=10)

    blocker_ids = {row["blocker"] for row in report["blockers"]}
    assert "retired_resolution_shadow_capture_launchdaemon_present" in blocker_ids
