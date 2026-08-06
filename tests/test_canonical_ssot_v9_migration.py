from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V8,
    V8_TRIGGERS,
)
from bayes_basket_target.canonical_ssot_v8_migration import (
    TARGET_TABLE_DDL as V8_TABLE_DDL,
    TARGET_TRIGGER_DDL as V8_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v7_migration import (
    TARGET_TABLE_DDL as V7_TABLE_DDL,
    TARGET_TRIGGER_DDL as V7_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v9_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    FACT_TYPES_V9,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    CanonicalSSOTV9MigrationError,
    migrate_canonical_ssot_fact_v9,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


def _database(tmp_path: Path, *, name: str = "canonical-v8.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V8_TABLE_DDL)
        for ddl in V8_TRIGGER_DDL:
            conn.execute(ddl)
        for index, fact_type in enumerate(FACT_TYPES_V8, start=1):
            digest = f"{index:064x}"
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    f"{fact_type}:{digest}",
                    fact_type,
                    f"scope:{fact_type}",
                    digest,
                    json.dumps({"fact_type": fact_type}, separators=(",", ":")),
                    json.dumps({"lineage": fact_type}, separators=(",", ":")),
                    "2026-08-04T00:00:00Z",
                ),
            )
        conn.execute("CREATE TABLE canonical_ssot_fact_v7_replaced(marker TEXT)")
        conn.execute(
            "INSERT INTO canonical_ssot_fact_v7_replaced(marker) VALUES('preserve-v7')"
        )
        conn.execute("CREATE TABLE canonical_ssot_fact_v6_replaced(marker TEXT)")
        conn.execute(
            "INSERT INTO canonical_ssot_fact_v6_replaced(marker) VALUES('preserve-v6')"
        )
    return path


def _rows(path: Path, table: str = "canonical_ssot_fact") -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return list(
            conn.execute(
                f'SELECT {",".join(EXPECTED_COLUMNS)} FROM "{table}" ORDER BY fact_id'
            )
        )


def _snapshot(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return list(
            conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE type IN ('table','trigger') ORDER BY type,name"
            )
        )


def _apply(path: Path, **kwargs: object) -> dict[str, object]:
    return migrate_canonical_ssot_fact_v9(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def _fact_types(path: Path, table: str = "canonical_ssot_fact") -> tuple[str, ...]:
    with sqlite3.connect(path) as conn:
        sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
        )
    start = sql.index("CHECK(fact_type IN (") + len("CHECK(fact_type IN (")
    end = sql.index("))", start)
    return tuple(value.strip("'") for value in sql[start:end].split(","))


def test_v9_preview_zero_write_and_capacity_includes_sidecars(tmp_path: Path) -> None:
    path = _database(tmp_path)
    Path(str(path) + "-wal").write_bytes(b"wal")
    Path(str(path) + "-shm").write_bytes(b"shm-data")
    before = path.read_bytes(), _snapshot(path)
    expected_wal = Path(str(path) + "-wal").stat().st_size
    expected_shm = Path(str(path) + "-shm").stat().st_size

    receipt = migrate_canonical_ssot_fact_v9(path)

    assert receipt["state"] == "PREVIEW_REQUIRES_APPLY"
    assert receipt["schema_before"] == "V8"
    assert receipt["schema_after_if_applied"] == "V9"
    assert receipt["database_wal_bytes"] == expected_wal
    assert receipt["database_shm_bytes"] == expected_shm
    assert receipt["source_footprint_bytes"] == (
        receipt["database_bytes"] + expected_wal + expected_shm
    )
    assert receipt["writes_database"] is False
    assert (path.read_bytes(), _snapshot(path)) == before


def test_v9_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _snapshot(path)
    with pytest.raises(
        CanonicalSSOTV9MigrationError,
        match="canonical_ssot_v9_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v9(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV9MigrationError,
        match="canonical_ssot_v9_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v9(path, apply=True, confirmation=CONFIRMATION)
    assert _snapshot(path) == before


def test_v9_apply_preserves_v8_rows_and_all_retired_history(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _rows(path)
    receipt = _apply(path)

    assert receipt["state"] == "APPLIED_V9"
    assert receipt["schema_before"] == "V8"
    assert receipt["schema_after"] == "V9"
    assert receipt["rows_content_sha256_before"] == receipt["rows_content_sha256_after"]
    assert receipt["fact_type_counts_before"] == {
        value: 1 for value in FACT_TYPES_V8
    }
    assert receipt["fact_type_counts_after"] == receipt["fact_type_counts_before"]
    assert _rows(path) == before
    assert _rows(path, BACKUP_TABLE) == before
    assert _fact_types(path) == FACT_TYPES_V9
    with sqlite3.connect(path) as conn:
        trigger_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                ("canonical_ssot_fact",),
            )
        }
        retired_triggers = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (BACKUP_TABLE,),
            )
        }
        assert trigger_names == set(V8_TRIGGERS)
        assert retired_triggers == set(RETIRED_TRIGGERS)
        assert conn.execute(
            "SELECT marker FROM canonical_ssot_fact_v7_replaced"
        ).fetchone()[0] == "preserve-v7"
        assert conn.execute(
            "SELECT marker FROM canonical_ssot_fact_v6_replaced"
        ).fetchone()[0] == "preserve-v6"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v9_faults_rollback_and_retry(tmp_path: Path, stage: str) -> None:
    path = _database(tmp_path, name=f"fault-{stage}.db")
    before_rows = _rows(path)
    before_schema = _snapshot(path)
    with pytest.raises(
        CanonicalSSOTV9MigrationError,
        match=f"canonical_ssot_v9_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == before_rows
    assert _snapshot(path) == before_schema
    with sqlite3.connect(path) as conn:
        assert _fact_types(path) == FACT_TYPES_V8
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0
    assert _apply(path)["state"] == "APPLIED_V9"


def test_v9_already_current_is_zero_write(tmp_path: Path) -> None:
    path = _database(tmp_path)
    assert _apply(path)["state"] == "APPLIED_V9"
    before = path.read_bytes(), _snapshot(path)
    assert _apply(path)["state"] == "ALREADY_V9_NO_WRITE"
    preview = migrate_canonical_ssot_fact_v9(path)
    assert preview["state"] == "PREVIEW_ALREADY_V9"
    assert preview["requires_writer_quiescence"] is False
    assert preview["confirmation_required"] is False
    assert (path.read_bytes(), _snapshot(path)) == before


def test_v9_rejects_non_v8_source_and_invalid_fault_stage(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER canonical_ssot_fact_no_update")
    with pytest.raises(
        CanonicalSSOTV9MigrationError,
        match="canonical_ssot_v9_source_schema_invalid",
    ):
        migrate_canonical_ssot_fact_v9(path)
    path = _database(tmp_path, name="invalid-fault.db")
    with pytest.raises(
        CanonicalSSOTV9MigrationError,
        match="canonical_ssot_v9_injected_fault_stage_invalid",
    ):
        _apply(path, inject_fault_at="wrong")


def test_v9_rejects_a_valid_v7_source(tmp_path: Path) -> None:
    path = tmp_path / "canonical-v7.db"
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V7_TABLE_DDL)
        for ddl in V7_TRIGGER_DDL:
            conn.execute(ddl)
    with pytest.raises(
        CanonicalSSOTV9MigrationError,
        match="canonical_ssot_v9_source_schema_invalid",
    ):
        migrate_canonical_ssot_fact_v9(path)


def test_v9_allows_the_new_fact_type_and_rejects_unknown_type(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _apply(path)
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        digest = "f" * 64
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                f"republisher_no_live_activation:{digest}",
                "republisher_no_live_activation",
                "scope:republisher_no_live_activation",
                digest,
                "{}",
                "{}",
                "2026-08-04T00:00:00Z",
            ),
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_no_live_activation'"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    "unknown:" + "e" * 64,
                    "unknown",
                    "scope:unknown",
                    "e" * 64,
                    "{}",
                    "{}",
                    "2026-08-04T00:00:00Z",
                ),
            )


def test_v9_cli_preview_and_apply_paths(tmp_path: Path) -> None:
    path = _database(tmp_path)
    preview_output = tmp_path / "preview-receipt.json"
    preview = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/migrate_canonical_ssot_fact_v9.py"),
            "--database",
            str(path),
            "--receipt-output",
            str(preview_output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert preview.returncode == 0, preview.stderr
    preview_receipt = json.loads(preview_output.read_text(encoding="utf-8"))
    assert preview_receipt["state"] == "PREVIEW_REQUIRES_APPLY"
    assert _fact_types(path) == FACT_TYPES_V8

    quiescence_path = tmp_path / "writer-quiescence.json"
    quiescence_path.write_text(
        json.dumps(synthetic_writer_quiescence_receipt(path)), encoding="utf-8"
    )
    apply_output = tmp_path / "apply-receipt.json"
    applied = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/migrate_canonical_ssot_fact_v9.py"),
            "--database",
            str(path),
            "--receipt-output",
            str(apply_output),
            "--writer-quiescence-receipt",
            str(quiescence_path),
            "--apply-production-no-live-v9",
            "--confirm",
            CONFIRMATION,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert applied.returncode == 0, applied.stderr
    apply_receipt = json.loads(apply_output.read_text(encoding="utf-8"))
    assert apply_receipt["state"] == "APPLIED_V9"
    assert _fact_types(path) == FACT_TYPES_V9
