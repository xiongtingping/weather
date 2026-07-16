import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "phase0_schema_probe.py"
spec = importlib.util.spec_from_file_location("phase0_schema_probe", SCRIPT_PATH)
probe = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(probe)


def _create_forward_ready_schema(conn: sqlite3.Connection) -> None:
    table_columns = {
        **probe.REQUIRED_TABLE_COLUMNS,
        **probe.PLANNED_TABLE_COLUMNS,
    }
    for table, columns in table_columns.items():
        constraints = probe.TABLE_ID_CONSTRAINTS.get(table, {})
        primary_key = list(constraints.get("primary_key") or ())
        defs = []
        for col in columns:
            if len(primary_key) == 1 and col == primary_key[0]:
                col_type = "INTEGER" if col == "id" else "TEXT"
                defs.append(f"{col} {col_type} PRIMARY KEY")
            else:
                col_type = "INTEGER" if col == "id" else "TEXT"
                defs.append(f"{col} {col_type}")
        if len(primary_key) > 1:
            defs.append(f"PRIMARY KEY ({', '.join(primary_key)})")
        conn.execute(f"CREATE TABLE {table} ({', '.join(defs)})")
        for unique_key in constraints.get("unique_keys") or ():
            cols = [str(col) for col in unique_key]
            index_name = f"idx_test_{table}_{'_'.join(cols)}"
            conn.execute(f"CREATE UNIQUE INDEX {index_name} ON {table}({', '.join(cols)})")


def test_run_probe_is_read_only_and_reports_missing_lineage_columns(tmp_path):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                fact_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE event_fact_latest (
                slug TEXT,
                event_date TEXT,
                fact_id INTEGER,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                will_order INTEGER,
                details_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json, created_at) "
            "VALUES (1, '2026-07-03', 's', '2026-07-03', '2026-07-03T00:00:00Z', ?, '2026-07-03T00:00:00Z')",
            (json.dumps({"phase": "D0_midday"}),),
        )
        conn.execute(
            "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES ('s', '2026-07-03', 1, 'x')"
        )

    report = probe.run_probe(db)

    assert report["ok"] is True
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["schema_surface_ready"] is False
    assert report["schema_missing"]
    assert report["fact_json_missing"]
    assert report["phase0_ready"] is False
    assert "correlation_id" in report["tables"]["event_fact_history"]["missing_required_columns"]
    assert "source_observations" in report["fact_json_contract"]["missing_required_keys"]
    assert report["id_constraint_matrix"]["schema"] == "phase0_id_constraint_matrix_v1"
    assert report["id_constraint_matrix"]["tables"]["event_fact_history"]["target_primary_key"] == ["id"]
    assert "prepare_forward_write_lineage_columns_migration" in report["next_actions"]


def test_run_probe_accepts_forward_schema_when_all_required_items_exist(tmp_path):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _create_forward_ready_schema(conn)
        fact_payload = {key: True for key in probe.FACT_JSON_REQUIRED_KEYS}
        conn.execute(
            "INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json, created_at) "
            "VALUES (1, '2026-07-03', 's', '2026-07-03', '2026-07-03T00:00:00Z', ?, '2026-07-03T00:00:00Z')",
            (json.dumps(fact_payload),),
        )
        conn.execute(
            "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES ('s', '2026-07-03', '1', 'x')"
        )

    report = probe.run_probe(db)

    assert report["ok"] is True
    assert report["phase0_ready"] is True
    assert report["schema_surface_ready"] is True
    assert report["schema_missing"] == []
    assert report["fact_json_missing"] == []
    assert report["missing"] == []
    assert report["next_actions"] == ["phase0_schema_probe_clean"]


def test_id_constraint_matrix_reports_primary_unique_and_lineage_readiness(tmp_path):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE daily_weather_reviews (
                review_id TEXT PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                first_bad_link TEXT,
                review_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX idx_daily_reviews_review_id ON daily_weather_reviews(review_id)")
        conn.execute(
            """
            CREATE TABLE daily_calibration_records (
                daily_calibration_record_id TEXT PRIMARY KEY,
                review_id TEXT,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_snapshot_id TEXT,
                state TEXT,
                calibration_metrics_json TEXT,
                error_attribution_json TEXT,
                strata_json TEXT,
                record_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_daily_calibration_record_id "
            "ON daily_calibration_records(daily_calibration_record_id)"
        )

    report = probe.run_probe(db)
    review_constraints = report["id_constraint_matrix"]["tables"]["daily_weather_reviews"]
    calibration_constraints = report["id_constraint_matrix"]["tables"]["daily_calibration_records"]

    assert review_constraints["exists"] is True
    assert review_constraints["target_primary_key"] == ["review_id"]
    assert review_constraints["actual_primary_key"] == ["review_id"]
    assert review_constraints["missing_primary_key_columns"] == []
    assert review_constraints["missing_unique_keys"] == []
    assert review_constraints["foreign_key_policy"] == "lineage_key_contract"
    assert review_constraints["target_foreign_key_fields"] == ["review_id", "fact_snapshot_id"]
    assert review_constraints["actual_sqlite_foreign_keys"] == []
    assert review_constraints["missing_foreign_key_fields"] == []
    assert review_constraints["foreign_key_ready"] is True
    assert review_constraints["missing_lineage_fields"] == []
    assert review_constraints["constraint_ready"] is True
    assert review_constraints["write_owner"] == "daily_review_persistence_writer"
    assert calibration_constraints["exists"] is True
    assert calibration_constraints["target_primary_key"] == ["daily_calibration_record_id"]
    assert calibration_constraints["actual_primary_key"] == ["daily_calibration_record_id"]
    assert calibration_constraints["missing_unique_keys"] == []
    assert calibration_constraints["missing_lineage_fields"] == []
    assert calibration_constraints["constraint_ready"] is True


def test_main_schema_only_strict_allows_future_fact_json_gap(tmp_path, capsys):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _create_forward_ready_schema(conn)
        conn.execute(
            "INSERT INTO event_fact_history(id, run_date, slug, event_date, fact_as_of, fact_json, created_at) "
            "VALUES (1, '2026-07-03', 's', '2026-07-03', '2026-07-03T00:00:00Z', '{}', '2026-07-03T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES ('s', '2026-07-03', '1', 'x')"
        )

    schema_only_code = probe.main(["--db", str(db), "--strict", "--schema-only"])
    full_code = probe.main(["--db", str(db), "--strict"])
    out = capsys.readouterr().out

    assert schema_only_code == 0
    assert full_code == 3
    assert '"schema_surface_ready": true' in out
    assert '"phase0_ready": false' in out


def test_main_strict_exits_nonzero_when_gaps_remain(tmp_path, capsys):
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY)")

    code = probe.main(["--db", str(db), "--strict"])
    out = capsys.readouterr().out

    assert code == 3
    assert "phase0_schema_probe_v1" in out
