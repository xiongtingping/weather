"""Charter 176: the native P0 compile's two slow reads get the index they need.

One compile ran ~311 statements, 89% of its time SQL, and two statements
were 75% of that: a scope's settlement timezone DISTINCTed off ~880 wide
registry rows through table lookups (75ms), and the activations valid now
for one (station, season, lead) found by reading every activation the scope
ever had, because run_cycle sat between the lead bucket and the validity
window in the only index (72ms).  Both are pinned here by plan, on a fresh
store built from the production DDL -- the live store has no sqlite_stat
tables, so the planner there reads the same schema this does.
"""

import importlib.util
import sqlite3
from pathlib import Path

from bayes_basket_target.market_registry import apply_target_market_registry_schema
from bayes_basket_target.ssot import apply_production_ssot_schema, declared_index_names

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_TZ = "basket_target_market_registry_scope_tz_idx"
WINDOW = "calibration_native_activation_window_idx"
SCOPE_LANE = "basket_target_market_registry_scope_lane_idx"  # charter 179
GENERATION = "calibration_native_activation_generation_idx"  # charter 185
EVIDENCE_A = "calibration_native_activation_evidence_idx"  # 2026-10-02
EVIDENCE_R = "calibration_cross_native_artifact_evidence_idx"  # 2026-10-02

TIMEZONE_READS = {
    # native_ensemble_p0 (the compile)
    "compile": "SELECT DISTINCT settlement_timezone FROM basket_target_market_registry"
               " WHERE station_id=? AND event_date=? AND settlement_timezone<>''",
    # delta_v2_settlement_label, forward_observation_store,
    # forward_training_validation, native_ensemble_training_facts
    "scope": "SELECT DISTINCT settlement_timezone FROM basket_target_market_registry"
             " WHERE station_id=? AND event_date=?",
}
LEAD_BUCKET = (
    "SELECT r.ensemble_system_ids_json,a.run_cycle_by_system_json "
    "FROM calibration_native_ensemble_activation a "
    "JOIN calibration_cross_native_ensemble_artifact r "
    "ON r.cross_ensemble_artifact_id=a.artifact_id "
    "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
    "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
    "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
    "AND a.valid_from_utc<=? AND +a.valid_until_utc>? "
    "ORDER BY a.generation DESC,a.activation_id LIMIT 256"
)


def _store(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    return conn


def _plan(conn, sql):
    return " | ".join(
        str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, ("x",) * sql.count("?"))
    )


def test_both_indexes_are_declared_where_the_resident_reads_its_schema() -> None:
    declared = declared_index_names()
    assert declared[WINDOW] == "calibration_native_ensemble_activation"


def test_every_scope_timezone_read_is_answered_from_the_index_alone(tmp_path) -> None:
    conn = _store(tmp_path)
    for name, sql in TIMEZONE_READS.items():
        plan = _plan(conn, sql)
        assert f"COVERING INDEX {REGISTRY_TZ}" in plan, (name, plan)


def test_the_lead_bucket_read_streams_in_generation_order(tmp_path) -> None:
    # Charter 185: the window seek (charter 176) kept every still-valid
    # activation and sorted them; the generation index hands them over in
    # the walk's own order.
    conn = _store(tmp_path)
    plan = _plan(conn, LEAD_BUCKET)
    assert GENERATION in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_the_compile_still_asks_exactly_these_questions() -> None:
    # If the compile's SQL moves, the plans above stop proving anything.
    source = (ROOT / "bayes_basket_target" / "native_ensemble_p0.py").read_text(encoding="utf-8")
    flat = " ".join(source.split())
    assert "AND a.valid_from_utc<=? AND +a.valid_until_utc>? " in flat
    assert "WHERE station_id=? AND event_date=? " in flat


def _migration():
    spec = importlib.util.spec_from_file_location(
        "replay_compile_indexes",
        ROOT / "scripts" / "apply_bayes_basket_target_replay_compile_indexes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store_without_the_new_indexes(tmp_path: Path) -> Path:
    path = tmp_path / "live.db"
    conn = sqlite3.connect(path)
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    conn.execute(f"DROP INDEX {REGISTRY_TZ}")
    conn.execute(f"DROP INDEX {WINDOW}")
    conn.execute(f"DROP INDEX {SCOPE_LANE}")
    conn.execute(f"DROP INDEX {GENERATION}")
    conn.execute(f"DROP INDEX {EVIDENCE_A}")
    conn.execute(f"DROP INDEX {EVIDENCE_R}")
    # Rows only need to exist for the index counts; the snapshot parent
    # rows the foreign key names are beside the point here.
    conn.execute("PRAGMA foreign_keys=OFF")
    columns = [row[1] for row in conn.execute("PRAGMA table_info(basket_target_market_registry)")]
    for i in range(30):
        row = {c: f"v{i}" for c in columns}
        row.update(station_id=f"S{i % 3}", event_date=f"2026-09-2{i % 2}",
                   settlement_timezone="" if i == 7 else "Asia/Shanghai",
                   snapshot_sha256=f"{i:064x}", source_lane="awc", source_eligible=1,
                   market_active=1, market_accepting_orders=1, settlement_unit="C",
                   rules_sha256="r" * 64, slug=f"slug-{i}")
        conn.execute(
            f"INSERT INTO basket_target_market_registry({','.join(columns)})"
            f" VALUES({','.join('?' for _ in columns)})",
            tuple(row[c] for c in columns),
        )
    conn.commit()
    conn.close()
    return path


def test_the_migration_adds_both_and_proves_them_against_their_tables(tmp_path) -> None:
    path = _store_without_the_new_indexes(tmp_path)
    receipt = _migration().migrate(db_path=path, confirmed_writer_stopped=False)
    assert receipt["state"] == "migrated"
    assert receipt["indexes_created"] == sorted([REGISTRY_TZ, WINDOW, SCOPE_LANE, GENERATION, EVIDENCE_A, EVIDENCE_R])
    by_name = {item["index"]: item for item in receipt["index_verification"]}
    # 3 stations x 2 days = 6 scopes, each with one non-blank timezone.
    assert by_name[REGISTRY_TZ]["rows"] == 6
    # ... and the blank timezone (row 7) is one more (scope, tz, lane).
    assert by_name[SCOPE_LANE]["rows"] == 7
    assert all(item["planner_used_index"] for item in receipt["index_verification"])
    again = _migration().migrate(db_path=path, confirmed_writer_stopped=False)
    assert again["indexes_created"] == []
    assert sorted(again["indexes_already_present"]) == sorted(
        [REGISTRY_TZ, WINDOW, SCOPE_LANE, GENERATION, EVIDENCE_A, EVIDENCE_R]
    )


EVIDENCE_PREFILTER = (
    "SELECT a.activation_id,r.selection_evidence_sha256,r.run_cycle,"
    "a.run_cycle_by_system_json,a.run_cycle_by_system_sha256,"
    "a.available_system_set_hash_or_star,r.ensemble_system_ids_json,"
    "a.valid_until_utc "
    "FROM calibration_native_ensemble_activation a "
    "JOIN calibration_cross_native_ensemble_artifact r"
    " INDEXED BY calibration_cross_native_artifact_evidence_idx "
    "ON r.cross_ensemble_artifact_id=a.artifact_id "
    "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
    "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
    "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
    "AND a.valid_from_utc<=? AND a.valid_until_utc>?"
)


def test_the_compile_asks_its_evidence_test_off_two_covering_indexes(tmp_path) -> None:
    """2026-10-02: 2,667 valid re-mints a scope, each a random page of both
    tables, 65 s a cycle.  The evidence test must touch neither table."""

    import inspect

    from bayes_basket_target import native_ensemble_p0

    source = inspect.getsource(native_ensemble_p0)
    assert all(part in source for part in (
        '"SELECT a.activation_id,r.selection_evidence_sha256,r.run_cycle,"',
        '" INDEXED BY calibration_cross_native_artifact_evidence_idx "',
        '"a.valid_until_utc "',
        '"AND a.valid_from_utc<=? AND a.valid_until_utc>?"',
    ))
    plan = _plan(_store(tmp_path), EVIDENCE_PREFILTER)
    assert "COVERING INDEX calibration_native_activation_evidence_idx" in plan, plan
    assert "COVERING INDEX calibration_cross_native_artifact_evidence_idx" in plan, plan


def test_the_admitted_rows_are_read_by_id_not_by_walking_the_scope_again(tmp_path) -> None:
    """2026-10-02, first cycles on the evidence index: the second read (the
    admitted rows, whole) went through the window index and walked every
    valid row of the scope again to filter the ids."""

    import inspect

    from bayes_basket_target import native_ensemble_p0 as P0

    source = inspect.getsource(P0)
    start = source.index("activation_sql = (")
    end = source.index(")\n", source.index("ORDER BY a.generation DESC", start))
    activation_sql = eval(source[start + len("activation_sql = "):end + 1])  # noqa: S307
    # A hundred ids, as the compile sends them: with two the planner took the
    # primary key even when the artifact_kind index was still open to it.
    sql = P0._ADMITTED_ROWS_SQL(activation_sql).replace(
        "ORDER BY", "AND a.activation_id IN (" + ",".join("?" * 100) + ") ORDER BY"
    )
    plan = _plan(_store(tmp_path), sql)
    assert "activation_window_idx" not in plan, plan
    assert "(activation_id=?)" in plan, plan
    assert "artifact_kind=?" not in plan, plan
