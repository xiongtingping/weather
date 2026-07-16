import json
import sqlite3
from pathlib import Path

from scripts import daily_review_controlled_apply_confirmation_packet as master_packet


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"


def _create_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "archive.db"
    fact_payload = {
        "slug": SLUG,
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "source_observations": [{"source_event_id": "srcevt_master_1"}],
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.8,
        },
    }
    with sqlite3.connect(db_path) as conn:
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
            CREATE TABLE event_fact_latest (
                slug TEXT,
                event_date TEXT,
                fact_id INTEGER,
                updated_at TEXT
            );
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
                order_status TEXT,
                order_checked_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', 'chengdu', 'ZUUU', 30.0,
                   'whole_degrees', 1, '2026-07-04T16:00:00Z', '2026-07-05T00:10:00Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
            """,
            (SLUG, json.dumps(fact_payload)),
        )
        conn.execute(
            """
            INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
            VALUES(?, '2026-07-04', 1, '2026-07-04T09:05:39Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
                (id, run_id, run_date, slug, event_date, outcome, side, action,
                 decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at)
            VALUES(1, 7, '2026-07-04', ?, '2026-07-04', '30', 'YES', 'entry',
                   'entry', 1, '[]', '[]', '{}', '2026-07-04T09:05:39Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO auto_orders
                (id, order_id, slug, event_date, outcome, side, action, price,
                 size, strategy, created_at, order_status)
            VALUES(1, 'ord_master_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (SLUG,),
        )
    return db_path


def _add_settled_event_without_fact(db_path: Path) -> None:
    slug = "highest-temperature-in-chengdu-on-july-5-2026"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(2, '2026-07-05', ?, '2026-07-05', 'chengdu', 'ZUUU', 31.0,
                   'whole_degrees', 1, '2026-07-05T16:00:00Z', '2026-07-06T00:10:00Z')
            """,
            (slug,),
        )


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _steps_by_name(packet: dict) -> dict[str, dict]:
    return {str(row["step"]): row for row in packet["control_sequence"]}


def test_master_packet_is_no_apply_and_uses_one_backup_for_all_writes(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    tables_before = _table_names(db_path)

    packet = master_packet.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    assert packet["schema"] == "daily_review_controlled_apply_confirmation_packet_v1"
    assert packet["read_only"] is True
    assert packet["dry_run"] is True
    assert packet["guards"]["executes_sql"] is False
    assert packet["guards"]["writes_archive_db"] is False
    assert packet["guards"]["writes_manifest_file"] is False
    assert packet["guards"]["creates_backup"] is False
    assert Path(packet["backup_path"]).exists() is False
    assert Path(packet["manifest_output_path"]).exists() is False
    assert _table_names(db_path) == tables_before

    steps = _steps_by_name(packet)
    backup_path = packet["backup_path"]
    assert "--confirm APPLY_PHASE0_FORWARD_SCHEMA" in steps["apply_phase0_schema"]["command"]
    assert f"--backup {backup_path}" in steps["apply_phase0_schema"]["command"]
    assert "--confirm APPLY_DAILY_REVIEW_LINEAGE_REPAIR" in steps["apply_lineage_repair"]["command"]
    assert f"--backup {backup_path}" in steps["apply_lineage_repair"]["command"]
    assert "--confirm APPLY_DAILY_REVIEW_PERSISTENCE" in steps["apply_daily_review_persistence"]["command"]
    assert f"--backup {backup_path}" in steps["apply_daily_review_persistence"]["command"]
    assert "sqlite3" in steps["create_fresh_backup"]["command"]
    assert ".backup" in steps["create_fresh_backup"]["command"]
    assert backup_path in steps["create_fresh_backup"]["command"]
    assert "--emit-proposed-manifest-json" in steps["materialize_reviewed_manifest"]["command"]
    assert "controlled_apply_maintenance_window_packet.py" in steps["assert_writer_quiescence"]["command"]
    assert "writer_quiescent == true" in steps["assert_writer_quiescence"]["pass_condition"]
    assert steps["materialize_reviewed_manifest"]["writes_archive_db"] is False
    assert steps["post_apply_acceptance"]["writes"] is False
    assert (
        packet["daily_review_persistence_apply_contract_summary"]["batch_plan_source"]
        == "prebuilt_batch_plan"
    )
    assert (
        packet["phase0_summary"]["review_batch_plan_summary"]["batch_plan_source"]
        == "prebuilt_batch_plan"
    )
    post_apply_commands = steps["post_apply_acceptance"]["commands"]
    assert any(
        "hierarchical_joint_plan_coverage_audit.py --summary --limit 100 --max-items-output 5" in command
        for command in post_apply_commands
    )
    assert not any(
        "hierarchical_joint_plan_coverage_audit.py --summary --limit 20" in command
        for command in post_apply_commands
    )


def test_master_packet_blocks_broken_backup_symlink(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    backup_link = tmp_path / "backups"
    missing_external_target = tmp_path / "missing-volume" / "backups"
    backup_link.symlink_to(missing_external_target)

    packet = master_packet.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        backup_dir=backup_link,
        generated_at="2026-07-06T00:00:00Z",
    )

    steps = _steps_by_name(packet)

    assert packet["ok"] is False
    assert packet["backup_command_blocked_reason"] == "backup_dir_symlink_target_missing"
    assert packet["backup_dir_state"]["is_symlink"] is True
    assert packet["backup_dir_state"]["symlink_target_exists"] is False
    assert steps["create_fresh_backup"]["command"] == ""
    assert steps["create_fresh_backup"]["backup_command_blocked_reason"] == (
        "backup_dir_symlink_target_missing"
    )
    assert packet["guards"]["creates_backup"] is False
    assert _table_names(db_path)


def test_master_packet_excludes_missing_fact_sample_by_default(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    _add_settled_event_without_fact(db_path)

    packet = master_packet.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        max_items_output=10,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    assert packet["missing_fact_decision_policy"] == "exclude_sample"
    assert packet["proposed_manifest_decision_counts"]["confirm_repair"] == 1
    assert packet["proposed_manifest_decision_counts"]["exclude_sample"] == 1
    decisions_by_type = {
        row["repair_type"]: row["decision"]
        for row in packet["proposed_manifest_json"]["decisions"]
    }
    assert decisions_by_type["legacy_fact_snapshot_lineage_repair"] == "confirm_repair"
    assert decisions_by_type["missing_fact_snapshot_for_settled_event"] == "exclude_sample"
    persistence = packet["daily_review_persistence_apply_contract_summary"]
    assert persistence["effective_settled_event_count"] == 1
    assert persistence["excluded_settled_event_count"] == 1


def test_master_packet_summary_is_compact_and_preserves_apply_boundary(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    packet = master_packet.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    summary = master_packet.compact_confirmation_packet(packet)

    assert summary["schema"] == "daily_review_controlled_apply_confirmation_packet_v1_summary"
    assert summary["read_only"] is True
    assert summary["guards"]["writes_archive_db"] is False
    assert "proposed_manifest_json" not in summary
    assert summary["phase0"]["ddl_summary"]["ddl_count"] > 0
    assert summary["lineage"]["risk_summary"]["risk_level"] == "low"
    assert summary["confirmation_phrases"]["phase0_schema_apply"] == "APPLY_PHASE0_FORWARD_SCHEMA"
    steps = {str(row["step"]): row for row in summary["control_sequence"]}
    assert steps["apply_phase0_schema"]["writes_archive_db"] is True
    assert steps["apply_lineage_repair"]["confirmation_phrase"] == "APPLY_DAILY_REVIEW_LINEAGE_REPAIR"
    assert steps["apply_daily_review_persistence"]["confirmation_phrase"] == "APPLY_DAILY_REVIEW_PERSISTENCE"
    assert steps["post_apply_acceptance"]["writes"] is False
    assert any(
        "hierarchical_joint_plan_coverage_audit.py --summary --limit 100 --max-items-output 5" in command
        for command in steps["post_apply_acceptance"]["command"]
    )
