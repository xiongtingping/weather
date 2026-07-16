import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase0_apply_confirmation_packet.py"
spec = importlib.util.spec_from_file_location("phase0_apply_confirmation_packet", SCRIPT_PATH)
packet_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(packet_mod)


def _create_legacy_archive_db(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE event_fact_history (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, fact_as_of TEXT, fact_json TEXT, created_at TEXT)"
        )
        conn.execute("CREATE TABLE event_fact_latest (slug TEXT, event_date TEXT, fact_id INTEGER, updated_at TEXT)")
        conn.execute("CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY, run_date TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, will_order INTEGER, details_json TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE auto_trade_jobs (id INTEGER PRIMARY KEY, run_date TEXT, source TEXT, state TEXT, payload_json TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE auto_orders (id INTEGER PRIMARY KEY, order_id TEXT, slug TEXT, event_date TEXT, outcome TEXT, side TEXT, action TEXT, created_at TEXT)")


def _create_legacy_archive_db_with_settled_legacy_fact(db: Path) -> None:
    slug = "highest-temperature-in-chengdu-on-july-4-2026"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                city TEXT,
                station_id TEXT,
                settlement_actual REAL,
                rounding_rule TEXT,
                market_closed INTEGER,
                event_end_utc TEXT,
                created_at TEXT
            );
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                fact_json TEXT,
                created_at TEXT
            );
            CREATE TABLE event_fact_latest (slug TEXT, event_date TEXT, fact_id INTEGER, updated_at TEXT);
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                reasons_json TEXT,
                details_json TEXT,
                created_at TEXT
            );
            CREATE TABLE auto_trade_jobs (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                source TEXT,
                state TEXT,
                payload_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE auto_orders (
                id INTEGER PRIMARY KEY,
                order_id TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                price REAL,
                size REAL,
                strategy TEXT,
                created_at TEXT,
                order_status TEXT
            );
            """
        )
        fact_payload = {
            "slug": slug,
            "event_date": "2026-07-04",
            "run_date": "2026-07-04",
            "source_observations": [{"source_event_id": "srcevt_packet_1"}],
            "distribution": [
                {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
                {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
            ],
            "posterior_target_bucket_selection": {
                "target_bucket": "30",
                "target_probability": 0.8,
            },
        }
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', 'chengdu', 'ZUUU', 30.0,
                   'whole_degrees', 1, '2026-07-04T16:00:00Z', '2026-07-05T00:10:00Z')
            """,
            (slug,),
        )
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
            """,
            (slug, json.dumps(fact_payload)),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
                (id, run_id, run_date, slug, event_date, outcome, side, action,
                 decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at)
            VALUES(1, 7, '2026-07-04', ?, '2026-07-04', '30', 'YES', 'entry',
                   'entry', 1, '[]', '[]', '{}', '2026-07-04T09:05:39Z')
            """,
            (slug,),
        )
        conn.execute(
            """
            INSERT INTO auto_orders
                (id, order_id, slug, event_date, outcome, side, action, price,
                 size, strategy, created_at, order_status)
            VALUES(1, 'ord_packet_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (slug,),
        )


def _table_names(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _gates_by_name(packet: dict) -> dict[str, dict]:
    return {
        str(item["gate"]): item
        for item in packet["operator_apply_gate_summary"]["gates"]
    }


def test_phase0_confirmation_packet_is_no_apply_and_has_operator_commands(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup_dir = tmp_path / "backups"
    _create_legacy_archive_db(db)
    tables_before = _table_names(db)

    packet = packet_mod.build_confirmation_packet(
        db,
        backup_dir=backup_dir,
        generated_at="2026-07-04T00:00:00Z",
    )

    assert packet["schema"] == "phase0_apply_confirmation_packet_v1"
    assert packet["read_only"] is True
    assert packet["dry_run"] is True
    assert packet["execution_mode"] == "confirmation_packet_no_apply"
    assert packet["guards"]["executes_sql"] is False
    assert packet["guards"]["writes_archive_db"] is False
    assert packet["guards"]["creates_backup"] is False
    assert packet["needs_apply"] is True
    assert packet["ddl_summary"]["create_unique_index_count"] > 0
    assert "lineage_forward_unique_indexes" in packet["ddl_summary"]["apply_order_groups"]
    assert packet["backup_exists"] is False
    assert Path(packet["backup_path"]).parent == backup_dir
    assert Path(packet["backup_path"]).exists() is False
    assert packet["apply_confirmation_phrase"] == "APPLY_PHASE0_FORWARD_SCHEMA"
    assert packet["review_persistence_writer_contract"]["writer_schema"] == "daily_review_persistence_writer_v1"
    assert packet["review_batch_plan_summary"]["schema"] == "phase0_apply_review_batch_plan_summary_v1"
    assert packet["review_batch_plan_summary"]["read_only"] is True
    assert packet["lineage_repair_review_packet"]["schema"] == "phase0_lineage_repair_review_packet_v1"
    assert packet["lineage_repair_review_packet"]["can_auto_apply"] is False
    gate_summary = packet["operator_apply_gate_summary"]
    gates = _gates_by_name(packet)
    assert gate_summary["schema"] == "phase0_operator_apply_gate_summary_v1"
    assert gate_summary["read_only"] is True
    assert gate_summary["ready_now"] is False
    assert gates["phase0_schema_apply"]["ready_after_operator_preflight"] is True
    assert gates["daily_review_persistence_apply"]["blocked"] is True
    assert "daily_review_persistence_schema_missing" in gates["daily_review_persistence_apply"]["blockers_now"]
    assert "no_settled_events_in_scope" in gates["daily_review_persistence_apply"]["blockers_now"]
    assert "fresh_archive_db_backup" in packet["manifest_apply_blockers"]
    assert "confirmation_phrase" in packet["apply_contract_blockers_without_confirmation"]
    assert "writer_quiescence" in packet["apply_contract_blockers_without_confirmation"]
    commands = []
    for item in packet["operator_packet"]["preflight"]:
        commands.append(item["command"])
    for item in packet["operator_packet"]["execution"]:
        commands.append(item["command"])
    assert any(".backup" in command for command in commands)
    assert any("--confirm APPLY_PHASE0_FORWARD_SCHEMA" in command for command in commands)
    assert any("--writer-quiescent" in command for command in commands)
    assert packet["operator_packet"]["verification"][0]["step"] == "schema_surface_strict"
    assert any(
        item["step"] == "review_persistence_writer_contract"
        and "test_daily_review_persistence_writer.py" in item["command"]
        and "test_daily_review_persistence_batch_plan.py" in item["command"]
        and "test_daily_review_coverage_audit.py" in item["command"]
        and "test_daily_review_post_apply_acceptance_audit.py" in item["command"]
        and "test_posterior_ssot_fact_coverage_audit.py" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "daily_review_batch_plan_before_persistence"
        and item["required"] is False
        and "daily_review_persistence_batch_plan.py" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "dry_run_daily_review_persistence_apply_entrypoint"
        and item["required"] is True
        and "daily_review_persistence_apply.py" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "review_persistence_writer_contract"
        and "test_daily_review_persistence_apply.py" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "prior_layer_contract_after_forward_writes"
        and item["required"] is False
        and "prior_layer_contract_audit.py" in item["command"]
        and "--latest-only" in item["command"]
        and "--active-only" in item["command"]
        and "--project-forward" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "forecast_weak_likelihood_contract_after_forward_writes"
        and item["required"] is False
        and "forecast_weak_likelihood_contract_audit.py" in item["command"]
        and "--latest-only" in item["command"]
        and "--active-only" in item["command"]
        and "--project-forward" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "metar_strong_likelihood_contract_after_forward_writes"
        and item["required"] is False
        and "metar_strong_likelihood_contract_audit.py" in item["command"]
        and "--latest-only" in item["command"]
        and "--active-only" in item["command"]
        and "--project-forward" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "posterior_ssot_fact_coverage_after_forward_writes"
        and item["required"] is False
        and "posterior_ssot_fact_coverage_audit.py" in item["command"]
        and "--latest-only" in item["command"]
        and "--project-forward" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "daily_review_coverage_audit_after_persistence"
        and item["required"] is False
        and "daily_review_coverage_audit.py" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert any(
        item["step"] == "daily_review_post_apply_acceptance_after_persistence"
        and item["required"] is False
        and "daily_review_post_apply_acceptance_audit.py" in item["command"]
        for item in packet["operator_packet"]["verification"]
    )
    assert packet["operator_packet"]["rollback"][0]["requires_manual_confirmation"] is True
    assert _table_names(db) == tables_before


def test_phase0_confirmation_packet_immutable_probe_does_not_block_future_apply_gate(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup_dir = tmp_path / "backups"
    _create_legacy_archive_db(db)

    packet = packet_mod.build_confirmation_packet(
        db,
        immutable=True,
        backup_dir=backup_dir,
        generated_at="2026-07-04T00:00:00Z",
    )

    gates = _gates_by_name(packet)
    schema_gate = gates["phase0_schema_apply"]
    assert "immutable_mode" not in schema_gate["blockers_now"]
    assert schema_gate["unexpected_blockers"] == []
    assert schema_gate["ready_after_operator_preflight"] is True


def test_phase0_confirmation_packet_surfaces_lineage_repair_candidates(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup_dir = tmp_path / "backups"
    _create_legacy_archive_db_with_settled_legacy_fact(db)
    tables_before = _table_names(db)

    packet = packet_mod.build_confirmation_packet(
        db,
        backup_dir=backup_dir,
        generated_at="2026-07-04T00:00:00Z",
    )

    summary = packet["review_batch_plan_summary"]
    repair_packet = packet["lineage_repair_review_packet"]
    assert summary["settled_event_count"] == 1
    assert summary["planned_review_count"] == 1
    assert summary["planned_calibration_record_count"] == 1
    assert summary["aggregate_row_counts"]["daily_calibration_records"] == 1
    assert summary["lineage_repair_candidate_count"] == 1
    assert summary["lineage_readiness_state"]["ready_for_confirmed_apply"] is False
    readiness = summary["lineage_decision_apply_readiness"]
    assert readiness["schema"] == "daily_review_lineage_decision_apply_readiness_v1"
    assert readiness["operator_decision_counts"]["undecided"] == 1
    assert readiness["daily_review_persistence"]["ready_after_explicit_confirmation"] is False
    assert readiness["next_action"] == "complete_lineage_decision_manifest_or_exclude_samples"
    gates = _gates_by_name(packet)
    assert gates["lineage_repair_apply"]["applicable"] is True
    assert gates["lineage_repair_apply"]["blocked"] is True
    assert "lineage_decision_manifest_missing_or_incomplete" in gates["lineage_repair_apply"]["blockers_now"]
    assert gates["daily_review_persistence_apply"]["blocked"] is True
    assert "batch_plan_lineage_incomplete" in summary["apply_blockers"]
    assert repair_packet["candidate_count"] == 1
    assert repair_packet["candidate_type_counts"] == {"legacy_fact_snapshot_lineage_repair": 1}
    assert repair_packet["candidates"][0]["repair_type"] == "legacy_fact_snapshot_lineage_repair"
    assert repair_packet["candidates"][0]["guards"]["writes_archive_db"] is False
    assert repair_packet["decision_manifest_audit"]["schema"] == "daily_review_lineage_repair_decision_manifest_audit_v1"
    assert repair_packet["decision_manifest_audit"]["missing_decision_count"] == 1
    assert repair_packet["repair_apply_contract"]["schema"] == "daily_review_lineage_repair_apply_contract_v1"
    assert repair_packet["repair_apply_contract"]["repair_item_count"] == 0
    assert repair_packet["apply_readiness"]["schema"] == "daily_review_lineage_decision_apply_readiness_v1"
    assert repair_packet["apply_readiness"]["operator_decision_counts"]["undecided"] == 1
    operator_packet = repair_packet["operator_decision_packet"]
    assert operator_packet["schema"] == "daily_review_lineage_repair_operator_packet_v1"
    assert operator_packet["daily_review_persistence_apply_gate"]["blocked"] is True
    assert operator_packet["items"][0]["allowed_decisions"] == [
        "confirm_repair",
        "exclude_sample",
        "wait_for_new_fact",
        "manual_rebuild_fact_snapshot",
    ]
    assert operator_packet["items"][0]["decision_options"][0]["guards"]["writes_archive_db"] is False
    assert any(
        item["step"] == "review_lineage_repair_candidates"
        and item["required"] is True
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "generate_lineage_decision_manifest_template"
        and item["required"] is True
        and "--emit-lineage-decision-manifest-template" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "review_lineage_repair_final_confirmation_packet"
        and item["required"] is True
        and "daily_review_lineage_repair_confirmation_packet.py" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "review_lineage_repair_operator_runbook"
        and item["required"] is True
        and "daily_review_lineage_repair_operator_runbook.py" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "recheck_lineage_decision_manifest_scope"
        and item["required"] is True
        and "--lineage-decision-manifest <manifest.json>" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "review_lineage_repair_apply_contract"
        and item["required"] is True
        and "--emit-lineage-repair-apply-contract" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "review_lineage_repair_risk_audit"
        and item["required"] is True
        and "daily_review_lineage_repair_risk_audit.py" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert any(
        item["step"] == "dry_run_lineage_repair_apply_entrypoint"
        and item["required"] is True
        and "daily_review_lineage_repair_apply.py" in item["command"]
        for item in packet["operator_packet"]["preflight"]
    )
    assert "review_lineage_repair_candidates_before_daily_review_apply" in packet["next_actions"]
    assert _table_names(db) == tables_before


def test_phase0_confirmation_packet_reuses_prebuilt_review_batch_plan(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_legacy_archive_db(db)
    prebuilt_plan = {
        "schema": "daily_review_persistence_batch_plan_v1",
        "ok": True,
        "settled_event_count": 2,
        "effective_settled_event_count": 2,
        "excluded_settled_event_count": 0,
        "planned_review_count": 2,
        "raw_planned_review_count": 2,
        "planned_calibration_record_count": 2,
        "raw_planned_calibration_record_count": 2,
        "planned_candidate_count": 0,
        "planned_parameter_event_count": 0,
        "aggregate_row_counts": {"daily_weather_reviews": 2},
        "raw_aggregate_row_counts": {"daily_weather_reviews": 2},
        "can_apply_after_schema_and_confirmation": True,
        "daily_review_persistence_schema_status": {"ok": True},
        "lineage_readiness_state": {},
        "lineage_repair_candidate_count": 0,
        "effective_lineage_repair_candidate_count": 0,
        "lineage_repair_candidates": [],
        "lineage_decision_apply_readiness": {
            "schema": "daily_review_lineage_decision_apply_readiness_v1",
            "operator_decision_counts": {},
            "daily_review_persistence": {
                "ready_after_explicit_confirmation": True,
                "remaining_hard_blockers": [],
                "apply_blockers": [],
            },
            "lineage_repair": {},
            "gates": [],
        },
        "guards": {"writes_archive_db": False},
    }

    packet = packet_mod.build_confirmation_packet(
        db,
        generated_at="2026-07-04T00:00:00Z",
        prebuilt_review_batch_plan=prebuilt_plan,
    )
    summary = packet_mod.compact_confirmation_packet(packet)

    assert packet["review_batch_plan_summary"]["batch_plan_source"] == "prebuilt_batch_plan"
    assert packet["review_batch_plan_summary"]["settled_event_count"] == 2
    assert packet["review_batch_plan_summary"]["planned_review_count"] == 2
    assert summary["review_batch_plan_summary"]["batch_plan_source"] == "prebuilt_batch_plan"
    assert summary["review_batch_plan_summary"]["planned_review_count"] == 2


def test_phase0_confirmation_packet_accepts_lineage_decision_manifest_scope(
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    backup_dir = tmp_path / "backups"
    _create_legacy_archive_db_with_settled_legacy_fact(db)
    first_packet = packet_mod.build_confirmation_packet(
        db,
        backup_dir=backup_dir,
        generated_at="2026-07-04T00:00:00Z",
    )
    repair_id = first_packet["lineage_repair_review_packet"]["candidates"][0]["repair_correlation_id"]
    manifest = {
        "schema": "daily_review_lineage_repair_decision_manifest_v1",
        "decisions": [
            {
                "repair_correlation_id": repair_id,
                "decision": "exclude_sample",
                "reason": "operator excludes first legacy sample from current scope",
            }
        ],
    }

    packet = packet_mod.build_confirmation_packet(
        db,
        backup_dir=backup_dir,
        generated_at="2026-07-04T00:00:00Z",
        lineage_decision_manifest=manifest,
    )

    summary = packet["review_batch_plan_summary"]
    audit = packet["lineage_repair_review_packet"]["decision_manifest_audit"]
    assert summary["settled_event_count"] == 1
    assert summary["effective_settled_event_count"] == 0
    assert summary["excluded_settled_event_count"] == 1
    assert summary["planned_calibration_record_count"] == 0
    gates = _gates_by_name(packet)
    assert gates["lineage_repair_apply"]["applicable"] is False
    assert gates["lineage_repair_apply"]["blocked"] is False
    assert gates["daily_review_persistence_apply"]["blocked"] is True
    assert "no_effective_settled_events_after_exclusions" in gates["daily_review_persistence_apply"]["blockers_now"]
    assert audit["manifest_provided"] is True
    assert audit["excluded_candidate_count"] == 1
    assert audit["blocked_candidate_count"] == 0
    assert "batch_plan_lineage_incomplete" not in summary["apply_blockers"]
    assert "no_effective_settled_events_after_exclusions" in summary["apply_blockers"]


def test_phase0_confirmation_packet_main_prints_json(tmp_path: Path, capsys) -> None:
    db = tmp_path / "archive.db"
    _create_legacy_archive_db(db)

    code = packet_mod.main(["--db", str(db), "--backup-dir", str(tmp_path / "backups")])
    out = capsys.readouterr().out

    assert code == 0
    assert "phase0_apply_confirmation_packet_v1" in out
    assert '"execution_mode": "confirmation_packet_no_apply"' in out
    assert '"creates_backup": false' in out


def test_phase0_confirmation_packet_summary_is_compact_and_no_apply(tmp_path: Path, capsys) -> None:
    db = tmp_path / "archive.db"
    _create_legacy_archive_db_with_settled_legacy_fact(db)

    code = packet_mod.main(
        [
            "--db",
            str(db),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--summary",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert code == 0
    assert payload["schema"] == "phase0_apply_confirmation_packet_v1_summary"
    assert payload["source_schema"] == "phase0_apply_confirmation_packet_v1"
    assert payload["read_only"] is True
    assert payload["dry_run"] is True
    assert payload["guards"]["writes_archive_db"] is False
    assert payload["ddl_summary"]["create_table_count"] >= 4
    assert payload["operator_apply_gate_summary"]["schema"] == "phase0_operator_apply_gate_summary_v1"
    assert payload["review_batch_plan_summary"]["lineage_repair_candidate_count"] == 1
    assert payload["lineage_repair_review_summary"]["candidate_count"] == 1
    assert "lineage_repair_review_packet" not in payload
    assert "candidates" not in payload["lineage_repair_review_summary"]
    assert any(item["step"] == "apply_schema_only" for item in payload["execution_steps"])
    assert any(item["step"] == "schema_surface_strict" for item in payload["required_verification_steps"])
    assert _table_names(db) == {
        "event_runs",
        "event_fact_history",
        "event_fact_latest",
        "auto_trade_decisions",
        "auto_trade_jobs",
        "auto_orders",
    }
