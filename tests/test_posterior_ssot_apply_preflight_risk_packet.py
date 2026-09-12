import json
import sqlite3
from pathlib import Path

from scripts import posterior_ssot_apply_preflight_risk_packet as risk
from tests.test_posterior_ssot_apply_confirmation_packet import _payload
from ui import server


def _create_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "archive.db"
    payload = _payload()
    payload["weather_likelihood_features"]["hard_source_final_daily_max"] = True
    payload["weather_likelihood_features"]["hard_source_settlement_final"] = True
    conn = sqlite3.connect(db_path)
    server.ensure_fact_tables(conn)
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, model_snapshot_id, market_snapshot_id,
            composer_version, best_outcome, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["run_date"],
            payload["slug"],
            payload["event_date"],
            "2026-07-05T14:06:28Z",
            1001,
            2001,
            "weather_fact_snapshot_writer",
            "37°C",
            json.dumps(payload),
            "2026-07-05T14:06:29Z",
        ),
    )
    fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (payload["slug"], payload["event_date"], fact_id, "2026-07-05T14:06:29Z"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_preflight_risk_packet_rebuilds_final_lock_apply_route_without_writing(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)

    packet = risk.build_preflight_risk_packet(
        db_path,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        # No age window.  The fixture's fact is dated 2026-07-05 and the
        # default since_hours is 168 counted from now, so this scanned an
        # empty set and reported zero candidates for every run made more than
        # a week after that date -- a test that rots on the calendar rather
        # than on the code.
        since_hours=0,
        latest_only=True,
        limit=5,
        max_items_output=5,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T15:00:00Z",
    )

    assert packet["ok"] is True
    assert packet["read_only"] is True
    assert packet["guards"]["writes_archive_db"] is False
    assert packet["risk_level"] == "controlled_low_after_operator_confirmation"
    assert packet["candidate_count"] == 1
    assert packet["expected_write_summary"]["tables"] == [
        "event_fact_history",
        "event_fact_latest",
        "event_fact_summary_cache",
    ]
    assert packet["expected_write_summary"]["append_event_fact_history_count"] == 1
    assert packet["expected_write_summary"]["repoint_event_fact_latest_count"] == 1
    assert packet["expected_write_summary"]["upsert_event_fact_summary_cache_count"] == 1
    route = packet["route_projections"][0]
    assert route["ok"] is True
    assert route["current_latest_fact_id"] == 1
    assert route["current_history_count"] == 1
    assert route["posterior_ssot_apply_contract"]["ok"] is True
    quiescence_step = next(row for row in packet["operator_preflight"] if row["step"] == "assert_writer_quiescence")
    assert "controlled_apply_maintenance_window_packet.py" in quiescence_step["command"]
    assert quiescence_step["pass_condition"] == "writer_quiescent == true"
    apply_step = next(row for row in packet["operator_preflight"] if row["step"] == "apply_posterior_repair_route_after_confirmation")
    assert "--confirm APPLY_POSTERIOR_SSOT_FACT_REPAIR" in apply_step["command"]
    assert "--writer-quiescent" in apply_step["command"]
    assert apply_step["writer_quiescent_flag_required"] is True
    assert apply_step["maintenance_window_hard_check"] is True
    assert apply_step["maintenance_window_block_reason"] == "maintenance_window_not_quiescent"
    assert packet["rollback"]["backup_path"].endswith("archive.db.posterior-ssot-preflight-20260705T150000Z.bak")

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
        assert conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0] == 1
    finally:
        conn.close()


def test_preflight_risk_packet_blocks_multi_candidate_scope_without_apply_command(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    payload = _payload()
    payload["weather_likelihood_features"]["hard_source_final_daily_max"] = True
    payload["weather_likelihood_features"]["hard_source_settlement_final"] = True
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_fact_history(
                run_date, slug, event_date, fact_as_of, model_snapshot_id, market_snapshot_id,
                composer_version, best_outcome, fact_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["run_date"],
                payload["slug"],
                payload["event_date"],
                "2026-07-05T14:16:28Z",
                1002,
                2002,
                "weather_fact_snapshot_writer",
                "37°C",
                json.dumps(payload),
                "2026-07-05T14:16:29Z",
            ),
        )
        second_fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            UPDATE event_fact_latest
            SET fact_id = ?, updated_at = ?
            WHERE slug = ? AND event_date = ?
            """,
            (
                second_fact_id,
                "2026-07-05T14:16:29Z",
                payload["slug"],
                payload["event_date"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    packet = risk.build_preflight_risk_packet(
        db_path,
        fact_ids=[1, second_fact_id],
        latest_only=True,
        limit=5,
        max_items_output=5,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T15:00:00Z",
    )

    assert packet["ok"] is False
    assert packet["risk_level"] == "blocked"
    assert "expected_exactly_one_repair_candidate" in packet["blockers"]
    assert "posterior_repair_route_not_available" in packet["blockers"]
    apply_step = next(row for row in packet["operator_preflight"] if row["step"] == "apply_posterior_repair_route_after_confirmation")
    assert apply_step["command"] == ""
    assert apply_step["required"] is False
