import json
import sqlite3

from daily_review_persistence_writer import (
    BIAS_CORRECTION_CANDIDATE_COLUMNS,
    DAILY_CALIBRATION_RECORD_COLUMNS,
    DAILY_WEATHER_REVIEW_COLUMNS,
    PARAMETER_CHANGE_EVENT_COLUMNS,
    apply_daily_review_persistence_plan,
    build_daily_review_persistence_plan,
    compact_daily_review_persistence_plan,
    daily_review_persistence_schema_status,
)
from daily_weather_review_runtime import build_daily_weather_review


def _create_table(conn: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    defs = []
    for column in columns:
        if table == "daily_weather_reviews" and column == "review_id":
            defs.append("review_id TEXT PRIMARY KEY")
        elif table == "daily_calibration_records" and column == "daily_calibration_record_id":
            defs.append("daily_calibration_record_id TEXT PRIMARY KEY")
        elif table == "bias_correction_candidates" and column == "calibration_candidate_id":
            defs.append("calibration_candidate_id TEXT PRIMARY KEY")
        elif table == "parameter_change_events" and column == "id":
            defs.append("id INTEGER PRIMARY KEY")
        else:
            defs.append(f"{column} TEXT")
    conn.execute(f"CREATE TABLE {table} ({', '.join(defs)})")


def _create_planned_tables(conn: sqlite3.Connection) -> None:
    _create_table(conn, "daily_weather_reviews", DAILY_WEATHER_REVIEW_COLUMNS)
    _create_table(conn, "daily_calibration_records", DAILY_CALIBRATION_RECORD_COLUMNS)
    _create_table(conn, "bias_correction_candidates", BIAS_CORRECTION_CANDIDATE_COLUMNS)
    _create_table(conn, "parameter_change_events", PARAMETER_CHANGE_EVENT_COLUMNS)


def _review_with_candidate():
    return build_daily_weather_review(
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        fact_snapshot={
            "fact_snapshot_id": "fact_1234567890abcdef",
            "source_observations": [{"source_event_id": "srcevt_1234567890abcdef"}],
            "posterior_ssot_fact": {
                "schema": "bayes_posterior_ssot_fact_v1",
                "state": "available",
                "fact_snapshot_id": "fact_1234567890abcdef",
                "posterior_run_id": "post_1234567890abcdef",
                "target_bucket": "30",
                "target_probability": 0.7,
                "bucket_probabilities": [
                    {"outcome": "30", "probability": 0.7},
                    {"outcome": "31", "probability": 0.3},
                ],
            },
            "posterior_target_bucket_selection": {"target_bucket": "30", "target_probability": 0.7},
            "distribution": [
                {"outcome": "30", "q_temperature_bucket_posterior": 0.7},
                {"outcome": "31", "q_temperature_bucket_posterior": 0.3},
            ],
        },
        decisions=[{"will_order": 0, "decided_state": "blocked"}],
        orders=[],
        settlement={"settlement_actual": 31.0},
        source_health={"ok": True},
    )


def test_persistence_plan_is_no_apply_and_compact_preview_has_hashes() -> None:
    review = _review_with_candidate()
    plan = build_daily_review_persistence_plan(review, created_at="2026-07-03T10:00:00Z")
    compact = compact_daily_review_persistence_plan(plan)

    assert plan["read_only"] is True
    assert plan["guards"]["writes_archive_db"] is False
    assert plan["row_counts"]["daily_weather_reviews"] == 1
    assert plan["row_counts"]["daily_calibration_records"] == 1
    assert plan["row_counts"]["bias_correction_candidates"] == 1
    assert plan["row_counts"]["parameter_change_events"] == 1
    assert compact["row_preview"]["daily_weather_reviews"][0]["json_sha256"]
    assert compact["row_preview"]["daily_calibration_records"][0]["json_sha256"]
    assert (
        compact["row_preview"]["daily_calibration_records"][0]["primary_id"]
        == review["daily_calibration_record"]["daily_calibration_record_id"]
    )
    assert "review_json" in plan["rows"]["daily_weather_reviews"][0]
    assert "calibration_metrics_json" in plan["rows"]["daily_calibration_records"][0]


def test_persistence_apply_blocks_without_allow_apply() -> None:
    conn = sqlite3.connect(":memory:")
    _create_planned_tables(conn)
    plan = build_daily_review_persistence_plan(_review_with_candidate())

    result = apply_daily_review_persistence_plan(conn, plan)

    assert result["status"] == "apply_blocked_noop"
    assert result["writes_archive_db"] is False
    assert conn.execute("SELECT COUNT(*) FROM daily_weather_reviews").fetchone()[0] == 0


def test_persistence_apply_noops_when_schema_missing() -> None:
    conn = sqlite3.connect(":memory:")
    plan = build_daily_review_persistence_plan(_review_with_candidate())

    result = apply_daily_review_persistence_plan(conn, plan, allow_apply=True)

    assert result["status"] == "schema_missing_noop"
    assert result["writes_archive_db"] is False
    assert result["schema_status"]["tables"]["daily_weather_reviews"]["exists"] is False


def test_persistence_apply_writes_temp_db_and_dedupes_parameter_event() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_planned_tables(conn)
    review = _review_with_candidate()
    plan = build_daily_review_persistence_plan(review, created_at="2026-07-03T10:00:00Z")

    first = apply_daily_review_persistence_plan(
        conn,
        plan,
        allow_apply=True,
        allow_parameter_event_apply=True,
    )
    second = apply_daily_review_persistence_plan(
        conn,
        plan,
        allow_apply=True,
        allow_parameter_event_apply=True,
    )

    assert daily_review_persistence_schema_status(conn)["ok"] is True
    assert first["status"] == "applied"
    assert first["row_counts"]["parameter_change_events"] == 1
    assert second["row_counts"]["parameter_change_events"] == 0
    review_row = conn.execute("SELECT * FROM daily_weather_reviews").fetchone()
    calibration_row = conn.execute("SELECT * FROM daily_calibration_records").fetchone()
    candidate_row = conn.execute("SELECT * FROM bias_correction_candidates").fetchone()
    event_count = conn.execute("SELECT COUNT(*) FROM parameter_change_events").fetchone()[0]
    assert review_row["review_id"] == review["review_id"]
    assert json.loads(review_row["review_json"])["target_hit"] is False
    assert calibration_row["review_id"] == review["review_id"]
    assert json.loads(calibration_row["calibration_metrics_json"])["target_hit"] is False
    assert json.loads(calibration_row["record_json"])["daily_calibration_record_id"] == review["daily_calibration_record"]["daily_calibration_record_id"]
    assert candidate_row["state"] == "proposed"
    assert event_count == 1
