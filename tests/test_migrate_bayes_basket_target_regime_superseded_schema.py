from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.regime_materialization import build_regime_artifact
from bayes_basket_target.ssot import (
    REGIME_ARTIFACT_COLUMNS,
    apply_production_ssot_schema,
    inspect_regime_superseded_schema,
    production_ssot_schema_status,
    upgrade_regime_superseded_schema,
)
import scripts.migrate_bayes_basket_target_regime_superseded_schema as migration


def _envelope(checkpoint_id: str, *, station_id="RKPK"):
    timezone_name = (
        "Asia/Seoul" if station_id == "RKPK" else "America/New_York"
    )
    return ProbabilityEnvelope.from_mapping(
        {
            "stage": "P0",
            "checkpoint_id": checkpoint_id,
            "station_id": station_id,
            "event_date": "2026-07-28",
            "settlement_timezone": timezone_name,
            "decision_at_utc": "2026-07-27T16:00:00Z",
            "fresh_until_utc": "2026-07-28T15:00:00Z",
            "bucket_ordinals": {"36": 36, "37": 37, "38": 38},
            "probabilities": {"36": 0.2, "37": 0.5, "38": 0.3},
            "posterior_version": "strict-oos-v1",
            "posterior_sha256": "2" * 64,
            "bridge_sha256": "3" * 64,
            "lineage": {
                "settlement_timezone": timezone_name,
                "model_envelope_lower_bucket": "35",
                "model_envelope_upper_bucket": "39",
                "model_envelope_span_c": "4",
            },
        }
    )


OLD_DDL = """
CREATE TABLE basket_regime_artifact (
  artifact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  checkpoint_id TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  probability_sha256 TEXT NOT NULL CHECK(length(probability_sha256)=64),
  as_of_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  artifact_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
  created_at TEXT NOT NULL,
  CHECK(valid_from_utc<valid_until_utc),
  UNIQUE(station_id,event_date,stage,checkpoint_id,probability_sha256)
);
CREATE INDEX basket_regime_artifact_scope_idx
ON basket_regime_artifact(
  station_id,event_date,stage,checkpoint_id,probability_sha256,
  state,valid_from_utc,valid_until_utc
);
CREATE TRIGGER basket_regime_artifact_no_update
BEFORE UPDATE ON basket_regime_artifact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER basket_regime_artifact_no_delete
BEFORE DELETE ON basket_regime_artifact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def _insert_old(
    conn: sqlite3.Connection,
    checkpoint_id: str,
    *,
    station_id="RKPK",
    state="COMMITTED",
) -> dict:
    artifact = build_regime_artifact(
        _envelope(checkpoint_id, station_id=station_id),
        stability_threshold=0.005,
    )
    payload = artifact.to_dict()
    conn.execute(
        "INSERT INTO basket_regime_artifact VALUES("
        + ",".join("?" for _ in REGIME_ARTIFACT_COLUMNS)
        + ")",
        (
            artifact.artifact_id,
            artifact.station_id,
            artifact.event_date,
            artifact.stage,
            artifact.checkpoint_id,
            artifact.settlement_timezone,
            artifact.probability_sha256,
            artifact.as_of_utc,
            artifact.valid_from_utc,
            artifact.valid_until_utc,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            artifact.content_sha256,
            state,
            "2026-07-27T16:00:00Z",
        ),
    )
    return payload


def _old_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_DDL)
        _insert_old(conn, "rkpk-old")
        _insert_old(
            conn,
            "katl-old",
            station_id="KATL",
            state="QUARANTINED",
        )
        conn.commit()


def _rows(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM basket_regime_artifact ORDER BY artifact_id"
    ).fetchall()


def test_authoritative_ddl_allows_only_committed_to_superseded():
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        assert inspect_regime_superseded_schema(conn)["ok"] is True
        assert production_ssot_schema_status(conn)["regime_schema_ok"] is True
        old = _insert_old(conn, "current-checkpoint")

        with pytest.raises(sqlite3.IntegrityError, match="regime_update_forbidden"):
            conn.execute(
                "UPDATE basket_regime_artifact SET state='QUARANTINED'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="regime_update_forbidden"):
            conn.execute(
                "UPDATE basket_regime_artifact SET station_id='OTHER',"
                "state='SUPERSEDED'"
            )
        conn.execute(
            "UPDATE basket_regime_artifact SET state='SUPERSEDED'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="regime_update_forbidden"):
            conn.execute(
                "UPDATE basket_regime_artifact SET state='COMMITTED'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM basket_regime_artifact")

        replacement = {
            **old,
            "artifact_id": "replacement-artifact",
            "content_sha256": "f" * 64,
        }
        conn.execute(
            "INSERT INTO basket_regime_artifact VALUES("
            + ",".join("?" for _ in REGIME_ARTIFACT_COLUMNS)
            + ")",
            (
                replacement["artifact_id"],
                replacement["station_id"],
                replacement["event_date"],
                replacement["stage"],
                replacement["checkpoint_id"],
                replacement["settlement_timezone"],
                replacement["probability_sha256"],
                replacement["as_of_utc"],
                replacement["valid_from_utc"],
                replacement["valid_until_utc"],
                json.dumps(replacement),
                replacement["content_sha256"],
                "COMMITTED",
                "2026-07-27T18:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO basket_regime_artifact SELECT "
                "'duplicate',station_id,event_date,stage,checkpoint_id,"
                "settlement_timezone,probability_sha256,as_of_utc,"
                "valid_from_utc,valid_until_utc,artifact_json,"
                "'e'||substr(content_sha256,2),state,created_at "
                "FROM basket_regime_artifact WHERE state='COMMITTED'"
            )


def test_upgrade_preserves_rows_is_idempotent_and_failure_rolls_back(tmp_path):
    db = tmp_path / "old.db"
    _old_database(db)
    with sqlite3.connect(db) as conn:
        before = _rows(conn)
        first = upgrade_regime_superseded_schema(conn)
        assert first["state"] == "migrated"
        assert _rows(conn) == before
        assert inspect_regime_superseded_schema(conn)["ok"] is True
        second = upgrade_regime_superseded_schema(conn)
        assert second["state"] == "already_current"
        assert _rows(conn) == before

    rollback_db = tmp_path / "rollback.db"
    _old_database(rollback_db)
    with sqlite3.connect(rollback_db) as conn:
        before = _rows(conn)

        def authorizer(action, arg1, _arg2, _db_name, _trigger):
            if (
                action == sqlite3.SQLITE_DROP_TABLE
                and arg1 == "basket_regime_artifact_superseded_source_v1"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            upgrade_regime_superseded_schema(conn)
        conn.set_authorizer(None)
        assert _rows(conn) == before
        assert inspect_regime_superseded_schema(conn)["ok"] is False
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_rehearsal_runs_forward_rollback_forward_without_source_write(tmp_path):
    db = tmp_path / "source.db"
    _old_database(db)
    before = db.read_bytes()
    receipt = migration.run_rehearsal(source_db=db)
    assert receipt["status"] == "PASS"
    assert receipt["mode"] == "REHEARSAL"
    assert receipt["rollback_exact"] is True
    assert receipt["forward"]["state"] == "migrated"
    assert receipt["second_forward"]["state"] == "already_current"
    assert receipt["temporary_clone_removed"] is True
    assert receipt["boundary"]["writes_source_database"] is False
    assert db.read_bytes() == before


def test_explicit_apply_uses_quiescence_backup_and_temp_database_only(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "apply.db"
    backup = tmp_path / "before.db"
    _old_database(db)
    monkeypatch.setattr(
        migration,
        "isolated_target_writer_quiescence_receipt",
        lambda path: synthetic_writer_quiescence_receipt(path),
    )
    receipt = migration.apply_migration(
        db_path=db,
        backup_path=backup,
    )
    assert receipt["status"] == "PASS"
    assert receipt["migration"]["state"] == "migrated"
    assert backup.is_file()
    with sqlite3.connect(db) as conn:
        assert inspect_regime_superseded_schema(conn)["ok"] is True
    with sqlite3.connect(backup) as conn:
        assert inspect_regime_superseded_schema(conn)["ok"] is False


def test_active_writer_receipt_and_resource_overflow_fail_before_write(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "blocked.db"
    backup = tmp_path / "must-not-exist.db"
    _old_database(db)
    before = db.read_bytes()
    receipt = synthetic_writer_quiescence_receipt(db)
    receipt["writer_process_hits"] = ["reported_trajectory_no_daemon.py:999"]
    receipt["quiescent"] = False
    identity = {
        key: value
        for key, value in receipt.items()
        if key != "receipt_sha256"
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    monkeypatch.setattr(
        migration,
        "isolated_target_writer_quiescence_receipt",
        lambda _path: receipt,
    )
    with pytest.raises(
        ValueError,
        match="writer_quiescence_receipt_not_quiescent",
    ):
        migration.apply_migration(db_path=db, backup_path=backup)
    assert not backup.exists()
    assert db.read_bytes() == before

    monkeypatch.setattr(migration, "MAX_TARGET_DB_BYTES", 1)
    with pytest.raises(
        migration.MigrationError,
        match="database_bytes_exceeded",
    ):
        migration.run_rehearsal(source_db=db)
    assert db.read_bytes() == before
