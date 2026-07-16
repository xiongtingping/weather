import json
import sqlite3
from pathlib import Path

from scripts import posterior_ssot_apply_confirmation_packet as packet


def _payload() -> dict:
    return {
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "fact_snapshot_id": "fact_stale_surface",
        "posterior_run_id": "post_stale_surface",
        "phase": "D0_hard",
        "phase_reason": "unit_test_final_lock",
        "beta_weak": 0.0,
        "beta_strong": 1.0,
        "hard_mask_reason": "final_bucket_lock",
        "weather_likelihood_features": {
            "observed_today_max_c": 37.0,
            "observed_today_max_final": True,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "settlement_actual": 37.0,
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_stale_surface",
            "posterior_run_id": "post_stale_surface",
            "target_bucket": "37°C",
            "target_probability": 1.0,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "boundaries": {
                "frontend_recompute_allowed": False,
                "decision_recompute_allowed": False,
                "hard_mask_is_constraint_not_weight": True,
            },
            "layer_chain": {"schema": "bayes_layer_chain_summary_v1"},
            "bucket_probabilities": [
                {"bucket": "36°C", "probability": 0.0, "hard_zero": True},
                {"bucket": "37°C", "probability": 1.0, "final_bucket_lock_target": True},
                {"bucket": "38°C", "probability": 0.0, "hard_zero": True},
            ],
            "ranked_bucket_probabilities": [
                {"bucket": "37°C", "probability": 1.0, "final_bucket_lock_target": True},
            ],
        },
        "posterior_target_bucket_selection": {
            "ssot_source": "posterior_ssot_fact",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "selection_runtime_recomputed": False,
            "target_bucket": "37°C",
            "target_probability": 1.0,
            "fact_snapshot_id": "fact_stale_surface",
            "posterior_run_id": "post_stale_surface",
        },
        "target_bucket_selection": {
            "ssot_source": "posterior_ssot_fact",
            "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "selection_runtime_recomputed": False,
            "target_bucket": "37°C",
            "target_probability": 1.0,
            "fact_snapshot_id": "fact_stale_surface",
            "posterior_run_id": "post_stale_surface",
        },
        "posterior_temperature_distribution": {
            "bucket_probabilities": [
                {"bucket": "36°C", "probability": 0.035},
                {"bucket": "37°C", "probability": 0.027},
                {"bucket": "38°C", "probability": 0.018},
            ]
        },
        "posterior_temperature_distribution_map": {
            "36°C": 0.035,
            "37°C": 0.027,
            "38°C": 0.018,
        },
        "distribution": [
            {"outcome": "36°C", "q_temperature_bucket_posterior": 0.035},
            {"outcome": "37°C", "q_temperature_bucket_posterior": 0.027},
            {"outcome": "38°C", "q_temperature_bucket_posterior": 0.018},
        ],
    }


def _create_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "archive.db"
    payload = _payload()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            composer_version TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO event_fact_history(
            id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            payload["run_date"],
            payload["slug"],
            payload["event_date"],
            "2026-07-05T14:06:28Z",
            "weather_fact_snapshot_writer",
            json.dumps(payload),
            "2026-07-05T14:06:29Z",
        ),
    )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (payload["slug"], payload["event_date"], 1, "2026-07-05T14:06:29Z"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_confirmation_packet_is_read_only_and_shows_surface_probability_diff(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)

    result = packet.build_confirmation_packet(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        latest_only=True,
        limit=5,
        max_items_output=5,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T15:00:00Z",
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["projected_ok"] is True
    assert candidate["writer_plan_apply_supported"] is True
    assert candidate["writer_plan_write_latest"] is True
    assert "posterior_surface_probability_mismatch_posterior_ssot_fact" in candidate["current_issues"]
    diffs = candidate["surface_probability_diff"]
    assert any(
        row["path"] == "posterior_temperature_distribution_map"
        and row["bucket"] == "38°C"
        and row["before_probability"] == 0.018
        and row["after_probability"] == 0.0
        for row in diffs
    )
    assert result["backup_exists"] is False
    assert result["apply_boundary"]["generic_apply_command_emitted"] is True
    assert candidate["generic_posterior_repair_route"]["available"] is True
    assert "--confirm APPLY_POSTERIOR_SSOT_FACT_REPAIR" in candidate["generic_posterior_repair_route"]["apply_command"]
    assert "--writer-quiescent" in candidate["generic_posterior_repair_route"]["apply_command"]
    assert any(step["step"] == "create_fresh_archive_backup" for step in result["operator_preflight"])


def test_confirmation_packet_fact_ids_scope_keeps_history_governance_separate_from_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    payload = _payload()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            composer_version TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        );
        """
    )
    for fact_id, fact_as_of in (
        (1, "2026-07-05T13:30:00Z"),
        (2, "2026-07-05T14:06:28Z"),
    ):
        row_payload = dict(payload)
        conn.execute(
            """
            INSERT INTO event_fact_history(
                id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_id,
                row_payload["run_date"],
                row_payload["slug"],
                row_payload["event_date"],
                fact_as_of,
                "weather_fact_snapshot_writer",
                json.dumps(row_payload),
                fact_as_of,
            ),
        )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (payload["slug"], payload["event_date"], 2, "2026-07-05T14:06:29Z"),
    )
    conn.commit()
    conn.close()

    result = packet.build_confirmation_packet(
        db_path,
        fact_ids=[1, 2],
        latest_only=True,
        limit=5,
        max_items_output=5,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T15:00:00Z",
    )
    summary = packet.compact_confirmation_packet(result)

    assert result["ok"] is True
    assert result["scope"]["latest_only_requested"] is True
    assert result["scope"]["latest_only_effective"] is False
    assert result["candidate_count"] == 2
    by_id = {row["fact_id"]: row for row in result["candidates"]}
    assert by_id[1]["is_latest_fact"] is False
    assert by_id[1]["latest_fact_id"] == 2
    assert by_id[1]["generic_posterior_repair_route"]["available"] is False
    assert by_id[1]["generic_posterior_repair_route"]["apply_command"] == ""
    assert by_id[1]["final_bucket_lock_route"]["available"] is False
    assert by_id[2]["is_latest_fact"] is True
    assert by_id[2]["generic_posterior_repair_route"]["available"] is True
    assert "--fact-id 2" in by_id[2]["generic_posterior_repair_route"]["apply_command"]
    assert summary["schema"] == "posterior_ssot_apply_confirmation_packet_v1_summary"
    assert summary["candidate_fact_ids"] == [2, 1]
    assert summary["current_latest_candidate_count"] == 1
    assert summary["historical_only_candidate_count"] == 1
    assert summary["apply_ready_candidate_count"] == 1
    item_by_id = {row["fact_id"]: row for row in summary["candidate_items"]}
    assert item_by_id[1]["generic_apply_route_available"] is False
    assert item_by_id[2]["generic_apply_route_available"] is True
