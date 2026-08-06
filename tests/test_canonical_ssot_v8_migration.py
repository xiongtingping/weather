from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V7,
    FACT_TYPES_V8,
    V7_TRIGGERS,
    V8_TRIGGERS,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v7_migration import (
    TARGET_TABLE_DDL as V7_TABLE_DDL,
    TARGET_TRIGGER_DDL as V7_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v8_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    CanonicalSSOTV8MigrationError,
    migrate_canonical_ssot_fact_v8,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


def _database(tmp_path: Path, *, name: str = "canonical-v7.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V7_TABLE_DDL)
        for ddl in V7_TRIGGER_DDL:
            conn.execute(ddl)
        for index, fact_type in enumerate(FACT_TYPES_V7, start=1):
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
    return migrate_canonical_ssot_fact_v8(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v8_preview_zero_write_and_capacity_includes_sidecars(tmp_path: Path) -> None:
    path = _database(tmp_path)
    Path(str(path) + "-wal").write_bytes(b"wal")
    Path(str(path) + "-shm").write_bytes(b"shm-data")
    before = path.read_bytes(), _snapshot(path)
    expected_wal = Path(str(path) + "-wal").stat().st_size
    expected_shm = Path(str(path) + "-shm").stat().st_size

    receipt = migrate_canonical_ssot_fact_v8(path)

    assert receipt["state"] == "PREVIEW_REQUIRES_APPLY"
    assert receipt["schema_before"] == "V7"
    assert receipt["schema_after_if_applied"] == "V8"
    assert receipt["database_wal_bytes"] == expected_wal
    assert receipt["database_shm_bytes"] == expected_shm
    assert receipt["source_footprint_bytes"] == (
        receipt["database_bytes"] + expected_wal + expected_shm
    )
    assert receipt["writes_database"] is False
    assert (path.read_bytes(), _snapshot(path)) == before


def test_v8_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _snapshot(path)
    with pytest.raises(
        CanonicalSSOTV8MigrationError,
        match="canonical_ssot_v8_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v8(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV8MigrationError,
        match="canonical_ssot_v8_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v8(path, apply=True, confirmation=CONFIRMATION)
    assert _snapshot(path) == before


def test_v8_apply_preserves_v7_rows_and_v6_history(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _rows(path)
    receipt = _apply(path)

    assert receipt["state"] == "APPLIED_V8"
    assert receipt["schema_before"] == "V7"
    assert receipt["schema_after"] == "V8"
    assert receipt["rows_content_sha256_before"] == receipt["rows_content_sha256_after"]
    assert receipt["fact_type_counts_before"] == {
        value: 1 for value in FACT_TYPES_V7
    }
    assert receipt["fact_type_counts_after"] == receipt["fact_type_counts_before"]
    assert _rows(path) == before
    assert _rows(path, BACKUP_TABLE) == before
    with sqlite3.connect(path) as conn:
        active = inspect_canonical_ssot_schema(conn)
        retired = inspect_canonical_ssot_schema(
            conn, table=BACKUP_TABLE, trigger_names=RETIRED_TRIGGERS
        )
        assert active["state"] == "V8"
        assert active["fact_types"] == list(FACT_TYPES_V8)
        assert active["trigger_names"] == list(V8_TRIGGERS)
        assert retired["state"] == "V7"
        assert retired["fact_types"] == list(FACT_TYPES_V7)
        assert conn.execute(
            "SELECT marker FROM canonical_ssot_fact_v6_replaced"
        ).fetchone()[0] == "preserve-v6"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v8_faults_rollback_and_retry(tmp_path: Path, stage: str) -> None:
    path = _database(tmp_path, name=f"fault-{stage}.db")
    before_rows = _rows(path)
    before_schema = _snapshot(path)
    with pytest.raises(
        CanonicalSSOTV8MigrationError,
        match=f"canonical_ssot_v8_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == before_rows
    assert _snapshot(path) == before_schema
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V7"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0
    assert _apply(path)["state"] == "APPLIED_V8"


def test_v8_already_current_is_zero_write(tmp_path: Path) -> None:
    path = _database(tmp_path)
    assert _apply(path)["state"] == "APPLIED_V8"
    before = path.read_bytes(), _snapshot(path)
    assert _apply(path)["state"] == "ALREADY_V8_NO_WRITE"
    preview = migrate_canonical_ssot_fact_v8(path)
    assert preview["state"] == "PREVIEW_ALREADY_V8"
    assert preview["requires_writer_quiescence"] is False
    assert preview["confirmation_required"] is False
    assert (path.read_bytes(), _snapshot(path)) == before


def test_v8_invalid_fault_stage_uses_v8_prefix(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with pytest.raises(
        CanonicalSSOTV8MigrationError,
        match="canonical_ssot_v8_injected_fault_stage_invalid",
    ):
        _apply(path, inject_fault_at="wrong")
