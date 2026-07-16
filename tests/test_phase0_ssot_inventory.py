import importlib.util
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase0_ssot_inventory.py"
spec = importlib.util.spec_from_file_location("phase0_ssot_inventory", SCRIPT_PATH)
inventory_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(inventory_mod)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_ssot_inventory_detects_write_surfaces_and_legacy_evidence(tmp_path):
    _write(
        tmp_path / "ui" / "server.py",
        """
def ensure_fact_tables(conn): pass
def _insert_event_fact_history(conn): pass
def ui_snapshot(): pass
conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")
conn.execute("INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)")
row.get("q_bayes_temperature_trajectory_evidence")
        """,
    )
    _write(
        tmp_path / "scripts" / "poller.py",
        """
def _insert_decision(conn): pass
conn.execute("INSERT INTO event_fact_history(slug) VALUES (?)")
conn.execute("INSERT INTO auto_trade_decisions(run_date, will_order) VALUES (?, ?)")
def _refresh_awc_fact(): pass
        """,
    )
    _write(
        tmp_path / "ui" / "index.html",
        """
const p = componentProbability('model', 'q_bayes_model_evidence');
const w = row.will_order;
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)

    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    fact_writers = report["table_write_inventory"]["event_fact_history"]["writer_files"]
    assert "ui/server.py" in fact_writers
    assert "scripts/poller.py" in fact_writers
    assert any(risk["risk"] == "fact_snapshot_write_not_single_owner" for risk in report["fork_risks"])
    assert any(risk["risk"] == "legacy_additive_evidence_usage_gate_blocked" for risk in report["fork_risks"])
    assert any(row["layer"] == "ui_snapshot" and "ui/index.html" in row["current_files"] for row in report["ssot_matrix"])
    assert report["production_chain_map"][0]["layer"] == "market_contract"
    assert any(item["id"] == "write_event_fact_history" for item in report["entrypoint_inventory"])
    assert report["cleanup_inventory"]["item_count"] >= 1
    assert report["development_checklist"]["gap_count"] >= 1
    assert report["source_acquisition_catalog"]["source_count"] >= 1
    assert any(item["id"] == "source_acquisition_catalog" for item in report["development_checklist"]["items"])
    assert report["awc_hard_source_acceptance_contract"]["accepted_source_ids"] == ["zuuu_metar_raw_amsc"]
    assert any(item["id"] == "awc_hard_source_acceptance_contract" for item in report["development_checklist"]["items"])
    assert len(report["daily_review_input_contract"]["required_input_layers"]) == 7
    assert any(item["id"] == "daily_review_input_contract" for item in report["development_checklist"]["items"])
    assert report["phase_weighting_contract"]["schema"] == "phase_weighting_contract_v1"
    assert "beta_weak" in report["phase_weighting_contract"]["required_output_fields"]
    assert any(item["id"] == "phase_weighting_contract" for item in report["development_checklist"]["items"])
    assert report["ssot_chain_replay_input_contract"]["awc_signal_to_order_slo"]["slo_ms"] == 3000
    assert any(item["id"] == "ssot_chain_replay_input_contract" for item in report["development_checklist"]["items"])
    assert report["parameter_change_governance_contract"]["state_machine"]["apply_allowed_in_state"] == "approved"
    assert any(item["id"] == "parameter_change_governance_contract" for item in report["development_checklist"]["items"])
    assert (
        report["awc_signal_to_order_contract"]["optimization_target"]
        == "accepted_awc_hard_source_signal_to_order_submit_within_3000ms"
    )
    assert "extract_fact_snapshot_writer_dry_run_boundary" in report["next_actions"]
    assert "replace_ui_local_derivation_with_backend_ui_snapshot_or_retired_compat" in report["next_actions"]


def test_ssot_inventory_main_summary_prints_json(tmp_path, capsys):
    _write(
        tmp_path / "scripts" / "decision.py",
        'conn.execute("INSERT INTO auto_trade_decisions(run_date, will_order) VALUES (?, ?)")\n',
    )

    code = inventory_mod.main(["--root", str(tmp_path), "--no-db", "--summary"])
    out = capsys.readouterr().out

    assert code == 0
    assert "phase0_ssot_inventory_v1" in out
    assert '"writes_archive_db": false' in out
    assert "auto_trade_decisions" in out
    assert "production_chain_map" in out
    assert "awc_signal_to_order_contract" in out
    assert "development_checklist" in out
    assert "legacy_evidence_usage_gate" in out
    assert "ui_local_derivation_gate" in out
    assert "source_acquisition_catalog" in out
    assert "awc_hard_source_acceptance_contract" in out
    assert "daily_review_input_contract" in out
    assert "phase_weighting_contract" in out
    assert "ssot_chain_replay_input_contract" in out
    assert "parameter_change_governance_contract" in out
    assert "posterior_consumption_inventory" in out
    assert "identity_field_inventory" in out


def test_ssot_inventory_falls_back_from_immutable_probe_on_live_wal_read_error(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    db_path.write_bytes(b"not-a-real-db-for-monkeypatched-probe")
    (tmp_path / "archive.db-wal").write_bytes(b"wal")
    calls = []

    def fake_run_probe(path, *, immutable=False):
        calls.append(bool(immutable))
        if immutable:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return {
            "phase0_ready": True,
            "missing": [],
            "schema_surface_ready": True,
            "next_actions": [],
        }

    monkeypatch.setattr(inventory_mod.phase0_schema_probe, "run_probe", fake_run_probe)

    report = inventory_mod.build_inventory(
        tmp_path,
        db_path=db_path,
        immutable=True,
        max_samples_per_pattern=10,
    )

    assert calls == [True, False]
    assert report["schema_probe"]["checked"] is True
    assert report["schema_probe"]["phase0_ready"] is True
    assert report["schema_probe"]["immutable_fallback_used"] is True
    assert report["schema_probe"]["immutable_fallback_wal_exists"] is True
    assert "database disk image is malformed" in report["schema_probe"]["immutable_fallback_reason"]


def test_ssot_inventory_accepts_auto_trade_decision_writer_as_single_owner(tmp_path):
    _write(
        tmp_path / "auto_trade_decision_writer.py",
        """
def insert_auto_trade_decision(conn): pass
conn.execute("INSERT INTO auto_trade_decisions(run_date, will_order) VALUES (?, ?)")
conn.execute("UPDATE auto_trade_decisions SET run_id = ? WHERE id = ?")
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    decisions = report["table_write_inventory"]["auto_trade_decisions"]
    decision_layer = [row for row in report["ssot_matrix"] if row["layer"] == "auto_trade_decisions"][0]

    assert decisions["target_owner"] == "auto_trade_decision_writer"
    assert decisions["writer_files"] == ["auto_trade_decision_writer.py"]
    assert decision_layer["target_write_owner"] == "auto_trade_decision_writer"
    assert decision_layer["status"] == "observed"
    assert not any(
        risk["risk"] == "decision_write_not_single_owner"
        for risk in report["fork_risks"]
    )


def test_ssot_inventory_lists_posterior_consumers_and_identity_fields(tmp_path):
    _write(
        tmp_path / "auto_trade_decision_writer.py",
        """
def insert_auto_trade_decision(conn, fact):
    ssot = fact["posterior_ssot_fact"]
    target_bucket = ssot["target_bucket"]
    target_probability = ssot["target_probability"]
    fact_snapshot_id = ssot["fact_snapshot_id"]
    posterior_run_id = ssot["posterior_run_id"]
    source_observation_id = ssot["lineage"]["source_observation_ids"][0]
    decision_id = "dec_fixture"
    signal_id = "sig_fixture"
    order_intent_id = "intent_fixture"
    conn.execute("INSERT INTO auto_trade_decisions(decision_id, signal_id, order_intent_id) VALUES (?, ?, ?)")
        """,
    )
    _write(
        tmp_path / "scripts" / "auto_trade_watcher.py",
        """
def read_decision(row):
    return row["decision_id"], row["posterior_run_id"]
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=20)

    posterior = report["posterior_consumption_inventory"]
    assert posterior["schema"] == "posterior_consumption_inventory_v1"
    chain = posterior["decision_posterior_field_chain"]
    assert chain["status"] == "ready"
    assert chain["missing_required_fields"] == []
    assert chain["legacy_compat_blocking_fields"] == []
    assert chain["legacy_compat_blocking_reference_count"] == 0
    assert "auto_trade_decision_writer.py" in chain["files"]
    assert "posterior_ssot_fact" in posterior["roles"]["auto_trade_decision_writer"]["fields"]
    watcher_contract = [
        item for item in posterior["role_contracts"] if item["role"] == "readonly_watcher"
    ][0]
    assert watcher_contract["observed"] is True
    assert watcher_contract["boundary"].startswith("observe decisions/orders")

    identity = report["identity_field_inventory"]
    assert identity["schema"] == "identity_field_inventory_v1"
    for field in (
        "source_observation_id",
        "fact_snapshot_id",
        "posterior_run_id",
        "decision_id",
        "signal_id",
        "order_intent_id",
    ):
        assert field in identity["available_identity_fields"]
        assert identity["fields"][field]["reference_count"] > 0


def test_ssot_inventory_blocks_legacy_distribution_as_decision_ssot(tmp_path):
    _write(
        tmp_path / "scripts" / "hard_source_direct_poller.py",
        """
def bad_decision_target(target_source):
    decision_ssot = "posterior_temperature_distribution"
    target_probability_field = "posterior_temperature_distribution"
    return decision_ssot, target_probability_field
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=20)

    chain = report["posterior_consumption_inventory"]["decision_posterior_field_chain"]
    gate = report["posterior_consumption_inventory"]["decision_legacy_compat_gate"]
    assert chain["status"] == "gap"
    assert "posterior_temperature_distribution" in chain["legacy_compat_blocking_fields"]
    assert gate["ok"] is False
    reasons = {
        reason
        for sample in gate["blocking_samples"]
        for reason in sample["reasons"]
    }
    assert reasons == {
        "legacy_distribution_as_decision_ssot",
        "legacy_distribution_as_probability_field_fallback",
    }


def test_ssot_inventory_accepts_auto_trade_job_writer_as_single_owner(tmp_path):
    _write(
        tmp_path / "auto_trade_job_writer.py",
        """
def insert_auto_trade_job(conn): pass
conn.execute("INSERT INTO auto_trade_jobs(run_date, state) VALUES (?, ?)")
conn.execute("UPDATE auto_trade_jobs SET state = ? WHERE id = ?")
conn.execute("DELETE FROM auto_trade_jobs WHERE state = 'success'")
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    jobs = report["table_write_inventory"]["auto_trade_jobs"]

    assert jobs["target_owner"] == "auto_trade_job_writer"
    assert jobs["writer_files"] == ["auto_trade_job_writer.py"]
    assert not any(
        risk["risk"] == "execution_job_write_not_single_owner"
        for risk in report["fork_risks"]
    )


def test_ssot_inventory_accepts_auto_order_ledger_writer_as_single_owner(tmp_path):
    _write(
        tmp_path / "auto_order_ledger_writer.py",
        """
def insert_auto_order_ignore(conn): pass
conn.execute("INSERT OR IGNORE INTO auto_orders(order_id) VALUES (?)")
conn.execute("UPDATE auto_orders SET order_status = ? WHERE order_id = ?")
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    orders = report["table_write_inventory"]["auto_orders"]

    assert orders["target_owner"] == "auto_order_ledger_writer"
    assert orders["writer_files"] == ["auto_order_ledger_writer.py"]
    assert not any(
        risk["risk"] == "order_ledger_write_not_single_owner"
        for risk in report["fork_risks"]
    )


def test_ssot_inventory_detects_posterior_fact_snapshot_adapter(tmp_path):
    _write(
        tmp_path / "weather_fact_snapshot_writer.py",
        """
def build_hierarchical_posterior_fact_snapshot_write_plan(): pass
def build_hierarchical_posterior_fact_payload(): pass
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    fact_snapshot = [row for row in report["ssot_matrix"] if row["layer"] == "fact_snapshot"][0]

    assert "weather_fact_snapshot_writer.py" in fact_snapshot["current_files"]


def test_ssot_inventory_excludes_read_only_fact_migration_plan(tmp_path):
    _write(
        tmp_path / "scripts" / "fact_write_migration_plan.py",
        """
TARGET_OWNER = "weather_fact_snapshot_writer.py"
blocked_operations = ["append_event_fact_history", "repoint_event_fact_latest"]
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    fact_snapshot = [row for row in report["ssot_matrix"] if row["layer"] == "fact_snapshot"][0]

    assert "scripts/fact_write_migration_plan.py" not in fact_snapshot["current_files"]


def test_ssot_inventory_marks_archive_rebuild_candidate_copy_as_non_live_writer(tmp_path):
    _write(
        tmp_path / "scripts" / "archive_db_rebuild_build.py",
        """
def build_archive_rebuild_candidate_copy_boundary():
    return {
        "execution_mode": "candidate_db_table_copy_not_fact_snapshot_mutation",
        "guards": {
            "writes_candidate_archive_db": True,
            "replaces_source_db": False,
        },
        "fact_writer_integration": {"status": "not_applicable_for_candidate_copy"},
    }
conn.execute("INSERT INTO main.event_fact_history SELECT * FROM src.event_fact_history")
        """,
    )
    _write(
        tmp_path / "weather_fact_snapshot_writer.py",
        "def build_fact_snapshot_write_plan(): pass\n",
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    fact_history = report["table_write_inventory"]["event_fact_history"]

    assert "scripts/archive_db_rebuild_build.py" not in fact_history["writer_files"]
    assert "scripts/archive_db_rebuild_build.py" in fact_history["non_live_writer_files"]
    assert report["file_boundaries"]["scripts/archive_db_rebuild_build.py"]["candidate_archive_copy_boundary"] is True
    assert not any(
        risk["risk"] == "fact_snapshot_write_not_single_owner"
        for risk in report["fork_risks"]
    )


def test_ssot_inventory_marks_temporary_drill_db_writes_as_non_live(tmp_path):
    _write(
        tmp_path / "scripts" / "posterior_target_direct_latency_drill.py",
        """
def build_posterior_target_direct_latency_drill_boundary():
    return {
        "execution_mode": "temporary_db_copy_fake_live_drill_not_production_writer",
        "guards": {
            "writes_temporary_archive_db_copy": True,
            "writes_temporary_auto_trade_decisions": True,
            "uses_fake_order_runner": True,
        },
    }
conn.execute("INSERT INTO auto_trade_decisions(run_date, will_order) VALUES (?, ?)")
conn.execute("DELETE FROM auto_trade_jobs WHERE source = 'posterior_target_direct'")
conn.execute("UPDATE edge_trades SET state = 'exit' WHERE slug = ?")
conn.execute("UPDATE event_fact_latest SET updated_at = ? WHERE slug = ?")
        """,
    )
    _write(
        tmp_path / "scan_weather_market.py",
        'conn.execute("INSERT INTO auto_trade_decisions(run_date, will_order) VALUES (?, ?)")\n',
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)

    for table in ("auto_trade_decisions", "auto_trade_jobs", "edge_trades", "event_fact_latest"):
        inventory = report["table_write_inventory"][table]
        assert "scripts/posterior_target_direct_latency_drill.py" not in inventory["writer_files"]
        assert "scripts/posterior_target_direct_latency_drill.py" in inventory["non_live_writer_files"]
    assert "scan_weather_market.py" in report["table_write_inventory"]["auto_trade_decisions"]["writer_files"]
    assert report["file_boundaries"]["scripts/posterior_target_direct_latency_drill.py"]["temporary_drill_db_boundary"] is True


def test_ssot_inventory_exposes_awc_signal_to_order_timing_contract(tmp_path):
    _write(
        tmp_path / "signal_to_order_ledger_writer.py",
        'SIGNAL_TO_ORDER_TIMING_TABLE = "signal_to_order_timing"\n',
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    contract = report["awc_signal_to_order_contract"]
    checklist = report["development_checklist"]

    assert contract["timing_table"] == "signal_to_order_timing"
    assert "awc_hard_source_signal_at" in contract["required_timing_columns"]
    assert "order_submitted_at" in contract["required_timing_columns"]
    assert "pre_signal_prepare_missing_components" in contract["required_timing_json_fields"]
    assert "submitted" in contract["terminal_states"]
    assert any(item["id"] == "awc_signal_to_order_contract" and item["status"] == "ready" for item in checklist["items"])


def test_ssot_inventory_does_not_classify_backend_decision_fields_as_ui_local_derivation(tmp_path):
    _write(
        tmp_path / "auto_trade_decision_writer.py",
        """
conn.execute("INSERT INTO auto_trade_decisions(will_order, reason_codes_json) VALUES (?, ?)")
payload = {"will_order": 1, "reason_codes_json": "[]"}
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    ui_pattern = [item for item in report["entrypoint_inventory"] if item["id"] == "ui_local_derivation"][0]

    assert ui_pattern["observed"] is False
    assert not any(risk["risk"] == "ui_local_or_compat_derivation_surface_present" for risk in report["fork_risks"])


def test_ssot_inventory_allows_ui_retired_compat_derivation_symbols(tmp_path):
    _write(
        tmp_path / "ui" / "index.html",
        """
function bayesChainForDistributionRow(row) { return row.bayes_chain; }
const chain = bayesChainForDistributionRow(row);
function legacyPhysicalLikelihoodSnapshotCompatFromItem(item, snapshot) { return null; }
const compat = legacyPhysicalLikelihoodSnapshotCompatFromItem(item, snapshot);
function legacyDistributionLikelihoodCompatForRow(row) { return null; }
const likelihood = legacyDistributionLikelihoodCompatForRow(row);
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    gate = report["ui_local_derivation_gate"]
    ui_layer = [row for row in report["ssot_matrix"] if row["layer"] == "ui_snapshot"][0]

    assert gate["ok"] is True
    assert gate["blocking_reference_count"] == 0
    assert gate["allowed_reference_count"] == 6
    assert not any(risk["risk"] == "ui_local_or_compat_derivation_surface_present" for risk in report["fork_risks"])
    assert "ui_legacy_or_local_derivation_surface_present" not in ui_layer["gaps"]


def test_ssot_inventory_uses_legacy_evidence_registry_when_present(tmp_path):
    _write(
        tmp_path / "legacy_evidence_migration_registry.py",
        """
LEGACY_EVIDENCE_MIGRATIONS = ()
def classify_legacy_evidence_field(field): return {}
        """,
    )
    _write(
        tmp_path / "bayesian_probability_runtime.py",
        """
row.get("q_bayes_temperature_trajectory_evidence")
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)

    assert "migrate_or_retire_legacy_evidence_fields_according_to_registry" in report["next_actions"]
    assert "classify_legacy_evidence_fields_as_retired_compat_or_migrate" not in report["next_actions"]


def test_ssot_inventory_does_not_treat_legacy_diagnostic_references_as_fork_risk(tmp_path):
    _write(
        tmp_path / "legacy_evidence_migration_registry.py",
        'FIELD = "q_bayes_model_evidence"\n',
    )
    _write(
        tmp_path / "scripts" / "backtest_probe.py",
        'row.get("q_bayes_temperature_trajectory_evidence")\n',
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    hierarchical = [row for row in report["ssot_matrix"] if row["layer"] == "hierarchical_posterior"][0]

    assert report["legacy_evidence_usage_gate"]["checked"] is True
    assert report["legacy_evidence_usage_gate"]["ok"] is True
    assert not any(
        risk["risk"] in {
            "legacy_additive_evidence_fields_still_present",
            "legacy_additive_evidence_usage_gate_blocked",
        }
        for risk in report["fork_risks"]
    )
    assert "legacy_evidence_fields_still_present" not in hierarchical["gaps"]
    assert "migrate_or_retire_legacy_evidence_fields_according_to_registry" not in report["next_actions"]


def test_ssot_inventory_detects_phase_weighting_runtime(tmp_path):
    _write(
        tmp_path / "phase_weighting_runtime.py",
        """
DEFAULT_PHASE_BETA_SCHEDULE = ()
def build_phase_weighting_snapshot(): pass
def awc_release_window_for_time(): pass
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    hierarchical = [row for row in report["ssot_matrix"] if row["layer"] == "hierarchical_posterior"][0]

    assert "phase_weighting_runtime.py" in hierarchical["current_files"]


def test_ssot_inventory_detects_forecast_weak_likelihood_runtime(tmp_path):
    _write(
        tmp_path / "forecast_weak_likelihood_runtime.py",
        """
DEFAULT_GROUP_CONFIG = {}
def build_forecast_weak_likelihood(): pass
forecast_weak_likelihood_map = {}
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    hierarchical = [row for row in report["ssot_matrix"] if row["layer"] == "hierarchical_posterior"][0]

    assert "forecast_weak_likelihood_runtime.py" in hierarchical["current_files"]


def test_ssot_inventory_detects_metar_strong_likelihood_runtime(tmp_path):
    _write(
        tmp_path / "metar_strong_likelihood_runtime.py",
        """
def build_metar_strong_likelihood(): pass
def infer_environment_state_probs(): pass
state_probs = {}
remaining_warming_by_state = []
dedupe_trace = []
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    hierarchical = [row for row in report["ssot_matrix"] if row["layer"] == "hierarchical_posterior"][0]

    assert "metar_strong_likelihood_runtime.py" in hierarchical["current_files"]


def test_ssot_inventory_detects_hierarchical_posterior_pipeline_runtime(tmp_path):
    _write(
        tmp_path / "hierarchical_posterior_pipeline_runtime.py",
        """
single_read_only_pipeline_entry = True
old_additive_evidence_pool_not_used = True
def build_hierarchical_posterior_pipeline(): pass
        """,
    )

    report = inventory_mod.build_inventory(tmp_path, db_path=None, max_samples_per_pattern=10)
    hierarchical = [row for row in report["ssot_matrix"] if row["layer"] == "hierarchical_posterior"][0]

    assert "hierarchical_posterior_pipeline_runtime.py" in hierarchical["current_files"]
