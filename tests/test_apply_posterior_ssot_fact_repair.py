import json
import sqlite3
from pathlib import Path

from scripts import apply_posterior_ssot_fact_repair as repair
from tests.test_posterior_ssot_rematerialization_plan import (
    _amsc_source_only_payload,
    _posterior_context_payload,
)
from ui import server


def _insert_fact(conn: sqlite3.Connection, payload: dict, *, fact_as_of: str, composer_version: str) -> int:
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.get("run_date") or payload["event_date"],
            payload["slug"],
            payload["event_date"],
            fact_as_of,
            composer_version,
            json.dumps(payload, ensure_ascii=False),
            fact_as_of,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _create_source_only_latest_db(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    server.ensure_fact_tables(conn)
    context = _posterior_context_payload()
    source_only = _amsc_source_only_payload()
    _insert_fact(
        conn,
        context,
        fact_as_of="2026-07-05T14:00:00Z",
        composer_version="unit_context_posterior",
    )
    source_fact_id = _insert_fact(
        conn,
        source_only,
        fact_as_of="2026-07-05T16:00:14Z",
        composer_version="amsc_zuuu_metar_taf_fetcher_v1",
    )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            source_only["slug"],
            source_only["event_date"],
            source_fact_id,
            "2026-07-05T16:00:14Z",
        ),
    )
    conn.commit()
    conn.close()
    return db_path, source_fact_id


def _create_posterior_latest_with_newer_source_history_db(tmp_path: Path) -> tuple[Path, int, int]:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    server.ensure_fact_tables(conn)
    context = _posterior_context_payload()
    source_only = _amsc_source_only_payload()
    context_fact_id = _insert_fact(
        conn,
        context,
        fact_as_of="2026-07-05T15:05:55Z",
        composer_version="unit_context_posterior",
    )
    source_fact_id = _insert_fact(
        conn,
        source_only,
        fact_as_of="2026-07-05T16:00:14Z",
        composer_version="amsc_zuuu_metar_taf_fetcher_v1",
    )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            context["slug"],
            context["event_date"],
            context_fact_id,
            "2026-07-05T15:05:55Z",
        ),
    )
    conn.commit()
    conn.close()
    return db_path, context_fact_id, source_fact_id


def _quiet_maintenance_packet(db_path: Path) -> dict:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": True,
        "writer_quiescent": True,
        "db": str(db_path),
        "counts": {
            "db_holder_process_count": 0,
            "writer_quiescence_blocker_count": 0,
            "reader_warning_count": 0,
            "write_capable_handle_process_count": 0,
        },
        "required_stop_labels": [],
        "blockers": [],
        "stop_authorization": {"blockers": [], "can_pause_required_services_now": True},
        "guards": {"writes_archive_db": False},
    }


def _blocked_maintenance_packet(db_path: Path) -> dict:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": False,
        "writer_quiescent": False,
        "db": str(db_path),
        "counts": {
            "db_holder_process_count": 1,
            "writer_quiescence_blocker_count": 1,
            "reader_warning_count": 0,
            "write_capable_handle_process_count": 1,
        },
        "required_stop_labels": ["system/com.weather.monitor.daemon"],
        "blockers": [
            {
                "owner_class": "hard_source_direct_poller",
                "service_label": "system/com.weather.monitor.daemon",
                "block_reason": "write_capable_archive_db_handle",
            }
        ],
        "stop_authorization": {
            "blockers": ["system_monitor_daemon_stop_requires_interactive_sudo"],
            "can_pause_required_services_now": False,
        },
        "guards": {"writes_archive_db": False},
        "next_boundary": "obtain_interactive_sudo_for_system_monitor_pause_then_rerun_packet",
    }


def test_apply_posterior_ssot_fact_repair_requires_explicit_confirmation(tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=False,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is False
    assert result["block_reason"] == "allow_apply_false"
    assert result["guards"]["writes_archive_db"] is False
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
        assert conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0] == source_fact_id
    finally:
        conn.close()


def test_apply_posterior_ssot_fact_repair_requires_writer_quiescence(tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=False,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is False
    assert result["block_reason"] == "writer_quiescent_false"
    assert result["writer_quiescent"] is False
    assert result["guards"]["writes_archive_db"] is False
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
        assert conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0] == source_fact_id
    finally:
        conn.close()


def test_apply_posterior_ssot_fact_repair_requires_maintenance_window_quiescence(
    tmp_path: Path,
) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=tmp_path / "backups",
        maintenance_window_packet=_blocked_maintenance_packet(db_path),
    )

    assert result["ok"] is False
    assert result["block_reason"] == "maintenance_window_not_quiescent"
    assert result["writer_quiescent"] is True
    assert result["maintenance_window"]["writer_quiescent"] is False
    assert result["maintenance_window"]["required_stop_labels"] == ["system/com.weather.monitor.daemon"]
    assert result["guards"]["writes_archive_db"] is False
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
        assert conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0] == source_fact_id
    finally:
        conn.close()


def test_apply_posterior_ssot_fact_repair_materializes_source_only_latest(tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T16:01:00Z",
        maintenance_window_packet=_quiet_maintenance_packet(db_path),
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["writer_quiescent"] is True
    assert result["maintenance_window"]["writer_quiescent"] is True
    assert result["candidate_fact_id"] == source_fact_id
    assert result["new_fact_id"] != source_fact_id
    assert result["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert result["writer_apply_result_summary"]["target_bucket"] == "37°C"
    assert result["writer_apply_result_summary"]["target_probability"] == 1.0
    assert result["backup"]["created"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 3
        latest = conn.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            ("highest-temperature-in-chengdu-on-july-5-2026", "2026-07-05"),
        ).fetchone()
        assert int(latest["fact_id"]) == int(result["new_fact_id"])
        row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id=?", (result["new_fact_id"],)).fetchone()
        payload = json.loads(row["fact_json"])
    finally:
        conn.close()

    assert payload["posterior_ssot_fact"]["state"] == "available"
    assert payload["posterior_ssot_fact"]["target_bucket"] == "37°C"
    assert payload["posterior_ssot_fact"]["target_probability"] == 1.0
    assert payload["prior_distribution"]["37°C"] == 0.6
    assert payload["metar_strong_likelihood"]["37°C"] == 1.0
    assert payload["posterior_projection_context"]["does_not_inherit_previous_observations"] is True


def test_apply_posterior_ssot_fact_repair_can_reuse_existing_backup(tmp_path: Path) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_path = backup_dir / "archive.db.shared.bak"
    backup_path.write_bytes(db_path.read_bytes())

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=backup_dir,
        backup_path=backup_path,
        generated_at="2026-07-05T16:01:00Z",
        maintenance_window_packet=_quiet_maintenance_packet(db_path),
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["backup"]["reused"] is True
    assert result["backup"]["created"] is False
    assert result["backup"]["path"] == str(backup_path.resolve())
    assert result["backup"]["candidate_verification"]["ok"] is True
    assert result["backup_verification"]["ok"] is True
    assert result["backup_verification"]["candidate_fact_id"] == source_fact_id
    assert not list(backup_dir.glob("archive.db.posterior-ssot-repair-apply-*.bak"))


def test_apply_posterior_ssot_fact_repair_rejects_backup_that_does_not_match_candidate(
    tmp_path: Path,
) -> None:
    db_path, source_fact_id = _create_source_only_latest_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup_path = backup_dir / "archive.db.shared.bak"
    backup_path.write_bytes(db_path.read_bytes())
    with sqlite3.connect(backup_path) as conn:
        conn.execute("DELETE FROM event_fact_latest")

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        fact_id=source_fact_id,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=backup_dir,
        backup_path=backup_path,
        generated_at="2026-07-05T16:01:00Z",
        maintenance_window_packet=_quiet_maintenance_packet(db_path),
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["block_reason"] == "backup_verification_failed"
    assert result["backup_verification"]["ok"] is False
    assert result["backup_verification"]["reason"] == "backup_candidate_mismatch"
    assert result["backup_verification"]["checks"]["latest_pointer_matches"] is False
    assert result["guards"]["writes_archive_db"] is False
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
        assert conn.execute("SELECT fact_id FROM event_fact_latest").fetchone()[0] == source_fact_id


def test_apply_posterior_ssot_fact_repair_materializes_newer_amsc_history_when_posterior_is_latest(
    tmp_path: Path,
) -> None:
    db_path, context_fact_id, source_fact_id = _create_posterior_latest_with_newer_source_history_db(tmp_path)

    result = repair.apply_posterior_ssot_fact_repair(
        db_path=db_path,
        slug="highest-temperature-in-chengdu-on-july-5-2026",
        event_date="2026-07-05",
        allow_apply=True,
        confirm=repair.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-05T16:01:00Z",
        maintenance_window_packet=_quiet_maintenance_packet(db_path),
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["candidate_fact_id"] == context_fact_id
    assert result["new_fact_id"] not in {context_fact_id, source_fact_id}
    assert result["projection_mode"] == "contextual_hierarchical_posterior_projection"
    assert result["writer_apply_result_summary"]["target_bucket"] == "37°C"
    assert result["writer_apply_result_summary"]["target_probability"] == 1.0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            ("highest-temperature-in-chengdu-on-july-5-2026", "2026-07-05"),
        ).fetchone()
        assert int(latest["fact_id"]) == int(result["new_fact_id"])
        row = conn.execute("SELECT fact_json FROM event_fact_history WHERE id=?", (result["new_fact_id"],)).fetchone()
        payload = json.loads(row["fact_json"])
    finally:
        conn.close()

    source_context = payload["posterior_projection_context"]["source_observation_context"]
    assert source_context["source_history_fact_ids"] == [source_fact_id]
    assert source_context["source_history_merged_observation_count"] == 2
    assert payload["posterior_ssot_fact"]["target_bucket"] == "37°C"
    assert payload["posterior_ssot_fact"]["target_probability"] == 1.0
    assert payload["metar_strong_likelihood"]["37°C"] == 1.0
    assert {row["source_id"] for row in payload["source_observations"]} >= {
        "zuuu_metar_raw_amsc",
        "zuuu_taf_forecast_amsc",
    }
