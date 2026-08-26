from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scripts.bayes_basket_target_migration_rehearsal as migration_rehearsal
import scripts.bayes_basket_target_production_ssot_apply as ssot_apply
from scripts.bayes_basket_target_production_ssot_apply import (
    MigrationError,
    PAUSE_MARKER_TOKEN,
    _backup_database,
    apply_controlled_migration,
    inspect_cash_phase_authority_manifest_schema,
    inspect_migration,
)
from scripts.bayes_basket_target_migration_rehearsal import run_rehearsal
from bayes_basket_target.migration_quiescence import synthetic_writer_quiescence_receipt
from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.atomic_ledger import target_ledger_schema_status
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    production_ssot_schema_status,
)


LEGACY_EMPTY_EXECUTION_VECTOR_DDL = """
CREATE TABLE basket_target_execution_version_vectors (
  order_intent_id TEXT PRIMARY KEY,
  posterior_version TEXT NOT NULL,
  posterior_sha256 TEXT NOT NULL CHECK(length(posterior_sha256)=64),
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
  quote_version TEXT NOT NULL,
  inventory_version TEXT NOT NULL,
  order_state_version TEXT NOT NULL,
  target_price_id TEXT NOT NULL,
  quote_expires_at TEXT NOT NULL,
  vector_sha256 TEXT NOT NULL CHECK(length(vector_sha256)=64),
  created_at TEXT NOT NULL
)
"""


def _legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE auto_trade_decisions(id INTEGER PRIMARY KEY, will_order INTEGER, details_json TEXT)"
        )
        conn.execute("INSERT INTO auto_trade_decisions VALUES(1,0,'legacy')")
        conn.commit()


def test_empty_pre_vector_policy_table_upgrades_without_inventing_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(LEGACY_EMPTY_EXECUTION_VECTOR_DDL)

        result = apply_production_ssot_schema(conn)

        assert result["execution_vector_upgrade"] == {
            "state": "migrated_empty_legacy",
            "rows_migrated": 0,
        }
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(basket_target_execution_version_vectors)"
            )
        }
        assert "vector_policy" in columns
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 0


def test_populated_pre_vector_policy_table_requires_offline_rebuild(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute(LEGACY_EMPTY_EXECUTION_VECTOR_DDL)
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors VALUES("
            "?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-intent",
                "legacy-posterior",
                "a" * 64,
                "b" * 64,
                "legacy-quote",
                "legacy-inventory",
                "legacy-order",
                "legacy-price",
                "2026-07-27T00:00:00Z",
                "c" * 64,
                "2026-07-26T00:00:00Z",
            ),
        )
        with pytest.raises(
            RuntimeError,
            match="legacy_execution_vectors_require_offline_rebuild:"
            "vector_policy_missing",
        ):
            apply_production_ssot_schema(conn)


def test_timezone_upgrade_retires_populated_pre_timezone_probability_table(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        envelope = ProbabilityEnvelope.from_mapping({
            "stage": "P0",
            "checkpoint_id": "pre-timezone-p0",
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-26T14:00:00Z",
            "fresh_until_utc": "2026-07-26T18:00:00Z",
            "bucket_ordinals": {"29°C": 0},
            "probabilities": {"29°C": 1.0},
            "posterior_version": "pre-timezone-v1",
            "posterior_sha256": "a" * 64,
            "bridge_sha256": "b" * 64,
            "lineage": {"settlement_timezone": "Asia/Seoul"},
        })
        insert_probability_envelope(
            conn,
            envelope,
            created_at_utc=envelope.decision_at_utc,
        )
        conn.execute("DROP TRIGGER basket_probability_envelope_no_update")
        conn.execute("DROP TRIGGER basket_probability_envelope_no_delete")
        # Two indexes carry settlement_timezone, and SQLite refuses to drop a
        # column an index reads.  A pre-timezone image did not have them
        # either, so dropping them is part of modelling that image rather
        # than a concession to the check -- and the upgrade under test
        # rebuilds the table from the DDL, which puts both back.
        conn.execute(
            "DROP INDEX basket_probability_envelope_committed_scope_idx"
        )
        conn.execute(
            "DROP INDEX basket_probability_envelope_publisher_scope_idx"
        )
        conn.execute(
            "ALTER TABLE basket_probability_envelope "
            "DROP COLUMN settlement_timezone"
        )
        conn.commit()

        result = apply_production_ssot_schema(conn)

        assert result["settlement_timezone_upgrade"]["retired_tables"] == [{
            "formal_table": "basket_probability_envelope",
            "audit_table": (
                "basket_probability_envelope_legacy_"
                "no_settlement_timezone_v1"
            ),
            "row_count": 1,
        }]
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM "
            "basket_probability_envelope_legacy_no_settlement_timezone_v1"
        ).fetchone()[0] == 1
        assert production_ssot_schema_status(conn)["ok"] is True


def test_timezone_upgrade_retires_populated_pre_timezone_decision_ledger(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        conn.execute(
            "INSERT INTO basket_target_execution_cycles("
            "cycle_id,plan_id,station_id,event_date,settlement_timezone,"
            "strategy_lane,plan_sha256,requested_reserve_usdc,state,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-cycle", "legacy-plan", "RKPK", "2026-07-27",
                "Asia/Seoul", "basket", "a" * 64, 0.0, "prepared",
                "2026-07-26T16:00:00Z", "2026-07-26T16:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions("
            "decision_id,cycle_id,plan_id,station_id,event_date,"
            "settlement_timezone,strategy_lane,decided_state,will_order,"
            "reason_codes_json,details_json,decision_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-decision", "legacy-cycle", "legacy-plan", "RKPK",
                "2026-07-27", "Asia/Seoul", "basket", "blocked", 0,
                '["legacy"]', "{}", "b" * 64,
                "2026-07-26T16:00:00Z",
            ),
        )
        conn.execute("DROP TRIGGER auto_trade_decisions_no_update")
        conn.execute("DROP TRIGGER auto_trade_decisions_no_delete")
        conn.execute(
            "ALTER TABLE basket_target_execution_cycles "
            "DROP COLUMN settlement_timezone"
        )
        conn.execute(
            "ALTER TABLE auto_trade_decisions "
            "DROP COLUMN settlement_timezone"
        )
        conn.commit()

        result = apply_production_ssot_schema(conn)

        retired = {
            item["formal_table"]: item
            for item in result["settlement_timezone_upgrade"][
                "retired_tables"
            ]
        }
        assert retired["basket_target_execution_cycles"]["row_count"] == 1
        assert retired["auto_trade_decisions"]["row_count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM "
            "basket_target_execution_cycles_legacy_no_settlement_timezone_v1"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM "
            "auto_trade_decisions_legacy_no_settlement_timezone_v1"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_cycles"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 0
        assert target_ledger_schema_status(conn)["ok"] is True
        assert production_ssot_schema_status(conn)["ok"] is True


def test_migration_plan_is_read_only_and_exposes_legacy_collision(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _legacy_db(db)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        plan = inspect_migration(conn)
    assert plan["legacy_decision_table_collision"] is True
    assert plan["legacy_decision_row_count"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


def test_mutation_requires_pause_and_confirmation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(MigrationError, match="confirmation_required"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=tmp_path / "backup.db",
                pause_file=tmp_path / "paused.flag",
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=False,
            )
        with pytest.raises(MigrationError, match="pause_marker_missing"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=tmp_path / "backup.db",
                pause_file=tmp_path / "paused.flag",
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
            )

        invalid_pause = tmp_path / "invalid.flag"
        invalid_pause.write_text("manual-order-complete\n", encoding="utf-8")
        with pytest.raises(MigrationError, match="pause_marker_invalid"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=tmp_path / "backup-invalid.db",
                pause_file=invalid_pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
            )


def test_mutation_requires_writer_quiescence(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _legacy_db(db)
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\n", encoding="utf-8")
    with sqlite3.connect(db) as conn:
        with pytest.raises(MigrationError, match="writer_quiescence_receipt_required"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=tmp_path / "backup.db",
                pause_file=pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=None,
            )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


def test_failed_schema_upgrade_rolls_back_legacy_rename(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.backup"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\n", encoding="utf-8")
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        # A non-empty pre-release probability table forces the stage-constraint
        # upgrade to fail after the legacy rename point.
        conn.execute(
            "CREATE TABLE basket_probability_envelope(envelope_id TEXT PRIMARY KEY, stage TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO basket_probability_envelope VALUES('old','P0')")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=backup,
                pause_file=pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
            )
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "auto_trade_decisions" in tables
        assert "auto_trade_decisions_legacy" not in tables
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 1
    assert backup.exists()


def test_backup_path_cannot_escape_local_database_directory(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\n", encoding="utf-8")
    _legacy_db(db)
    outside = tmp_path.parent / "archive-outside.backup"
    with sqlite3.connect(db) as conn:
        with pytest.raises(MigrationError, match="backup_path_outside_database_directory"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=outside,
                pause_file=pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
            )
    assert not outside.exists()


def test_controlled_migration_preserves_legacy_rows_and_installs_target_authority(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.backup"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\ntest pause\n", encoding="utf-8")
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        receipt = apply_controlled_migration(
            conn,
            db_path=db,
            backup_path=backup,
            pause_file=pause,
            legacy_archive_table="auto_trade_decisions_legacy",
            confirmed=True,
            writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
        )
    assert receipt["state"] == "migrated"
    assert receipt["backup"]["quick_check"] == "ok"
    assert receipt["backup"]["bytes"] > 0
    assert receipt["resource_before"]["ok"] is True
    assert receipt["resource_after"]["ok"] is True
    assert receipt["cash_phase_authority_manifest_schema"]["ok"] is True
    assert receipt["cash_phase_authority_manifest_schema"]["index_ok"] is True
    assert receipt["cash_phase_authority_manifest_schema"]["triggers_ok"] is True
    assert backup.exists()
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "auto_trade_decisions" in tables
        assert "auto_trade_decisions_legacy" in tables
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions_legacy").fetchone()[0] == 1
        assert {row[1] for row in conn.execute("PRAGMA table_info(auto_trade_decisions)")} >= {
            "cycle_id", "plan_id", "decision_sha256"
        }
        assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_target_capital_reservations").fetchone()[0] == 0
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


def test_controlled_migration_manifest_contract_is_append_only(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.backup"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\n", encoding="utf-8")
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        apply_controlled_migration(
            conn,
            db_path=db,
            backup_path=backup,
            pause_file=pause,
            legacy_archive_table="auto_trade_decisions_legacy",
            confirmed=True,
            writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
        )
        contract = inspect_cash_phase_authority_manifest_schema(conn)
        assert contract["ok"] is True
        assert contract["columns_ok"] is True
        assert contract["table_constraints_ok"] is True
        assert contract["index_columns"] == [
            "station_id",
            "authority_scope",
            "valid_from_utc",
            "valid_until_utc",
        ]
        conn.execute(
            "INSERT INTO basket_cash_phase_authority_manifest VALUES(?,?,?,?,?,?,?,?)",
            (
                "a" * 64,
                "RKPK",
                "cash-v1",
                "b" * 64,
                "Asia/Seoul",
                "2026-07-26T15:00:00Z",
                "2026-07-27T15:00:00Z",
                "PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "UPDATE basket_cash_phase_authority_manifest "
                "SET station_id='RJTT' WHERE manifest_sha256=?",
                ("a" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "DELETE FROM basket_cash_phase_authority_manifest "
                "WHERE manifest_sha256=?",
                ("a" * 64,),
            )


def test_backup_is_removed_when_backup_size_exceeds_hard_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.backup"
    _legacy_db(db)
    monkeypatch.setattr(ssot_apply, "MAX_TARGET_DB_BYTES", 1)
    with pytest.raises(MigrationError, match="backup_database_budget_exceeded"):
        _backup_database(db, backup)
    assert not backup.exists()


def test_controlled_migration_fails_before_backup_when_working_space_is_low(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.backup"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\n", encoding="utf-8")
    _legacy_db(db)

    monkeypatch.setattr(
        ssot_apply,
        "inspect_target_resource_budget",
        lambda _path: {
            "ok": True,
            "violations": [],
            "database_bytes": 4096,
            "wal_bytes": 0,
            "free_bytes": 1,
        },
    )
    monkeypatch.setattr(
        ssot_apply,
        "assert_process_memory_budget",
        lambda **_kwargs: 0,
    )
    with sqlite3.connect(db) as conn:
        with pytest.raises(MigrationError, match="working_space_insufficient"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=backup,
                pause_file=pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(
                    db
                ),
            )
    assert not backup.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions"
        ).fetchone()[0] == 1


def test_controlled_migration_refuses_shared_live_owner(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\n", encoding="utf-8")
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE basket_strategy_activation_state("
            "singleton INTEGER PRIMARY KEY,strategy_id TEXT,generation INTEGER,"
            "state TEXT,activation_json TEXT,activation_sha256 TEXT)"
        )
        conn.execute(
            "INSERT INTO basket_strategy_activation_state VALUES(1,?,?,?,?,?)",
            (
                "weather_basket_plus_hard_no_v2",
                7,
                "enabled",
                json.dumps({
                    "execution_mode": "production_live",
                    "live_submit_authorized": True,
                    "places_orders": True,
                    "calls_order_gateway": True,
                }),
                "c" * 64,
            ),
        )
        with pytest.raises(MigrationError, match="shared_live_activation_owner_conflict"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=tmp_path / "backup.db",
                pause_file=pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
            )
    assert not (tmp_path / "backup.db").exists()


def test_capital_pool_initialization_is_explicit_and_paired(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.db.backup"
    pause = tmp_path / "live_paused.flag"
    pause.write_text(f"{PAUSE_MARKER_TOKEN}\ntest pause\n", encoding="utf-8")
    _legacy_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(MigrationError, match="amount_and_epoch"):
            apply_controlled_migration(
                conn,
                db_path=db,
                backup_path=backup,
                pause_file=pause,
                legacy_archive_table="auto_trade_decisions_legacy",
                confirmed=True,
                writer_quiescence_receipt=synthetic_writer_quiescence_receipt(db),
                capital_balance_usdc=30.0,
            )


def test_real_legacy_shape_rehearsal_is_read_only_to_source(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    _legacy_db(source)
    result = run_rehearsal(
        source_db=source,
        capital_balance_usdc=30.0,
        capital_epoch_id="rehearsal-epoch",
    )
    assert result["state"] == "passed"
    assert result["source_opened_read_only"] is True
    assert result["source_before"]["row_count"] == 1
    assert result["target_ledger"]["legacy_archive_rows"][0] == 1
    assert result["target_ledger"]["target_decision_rows"] == 0
    assert result["target_ledger"]["order_intents"] == 0
    assert result["target_ledger"]["capital_pool"]["capital_epoch_id"] == "rehearsal-epoch"
    assert result["boundary"]["writes_source_database"] is False
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 1


def test_real_legacy_shape_rehearsal_selects_renamed_legacy_table(tmp_path: Path) -> None:
    source = tmp_path / "archive.db"
    _legacy_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "ALTER TABLE auto_trade_decisions RENAME TO auto_trade_decisions_legacy_20260723"
        )
        conn.execute(
            "CREATE TABLE auto_trade_decisions("
            "decision_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, plan_id TEXT NOT NULL, "
            "station_id TEXT NOT NULL, event_date TEXT NOT NULL, strategy_lane TEXT NOT NULL, "
            "decided_state TEXT NOT NULL, will_order INTEGER NOT NULL, reason_codes_json TEXT NOT NULL, "
            "details_json TEXT NOT NULL, decision_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.commit()
    result = run_rehearsal(
        source_db=source,
        capital_balance_usdc=30.0,
        capital_epoch_id="renamed-legacy-rehearsal",
    )
    assert result["state"] == "passed"
    assert result["source_before"]["legacy_table"] == "auto_trade_decisions_legacy_20260723"
    assert result["source_before"]["row_count"] == 1
    assert result["target_ledger"]["legacy_archive_rows"][0] == 1


def test_real_legacy_shape_rehearsal_rejects_source_over_resource_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "archive.db"
    _legacy_db(source)
    monkeypatch.setattr(migration_rehearsal, "MAX_SOURCE_DATABASE_BYTES", 0)
    with pytest.raises(RuntimeError, match="source_database_budget_exceeded"):
        migration_rehearsal.run_rehearsal(
            source_db=source,
            capital_balance_usdc=30.0,
            capital_epoch_id="bounded-rehearsal",
        )
