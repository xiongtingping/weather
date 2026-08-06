from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V9,
    FACT_TYPES_V10,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v9_migration import (
    TARGET_TABLE_DDL as V9_TABLE_DDL,
    TARGET_TRIGGER_DDL as V9_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v10_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    CanonicalSSOTV10MigrationError,
    migrate_canonical_ssot_fact_v10,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


def _database(tmp_path: Path, name: str = "canonical-v9.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V9_TABLE_DDL)
        for ddl in V9_TRIGGER_DDL:
            conn.execute(ddl)
        for index, fact_type in enumerate(FACT_TYPES_V9, start=1):
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
        for version in ("v8", "v7", "v6"):
            conn.execute(
                f"CREATE TABLE canonical_ssot_fact_{version}_replaced(marker TEXT)"
            )
            conn.execute(
                f"INSERT INTO canonical_ssot_fact_{version}_replaced VALUES(?)",
                (f"preserve-{version}",),
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
    return migrate_canonical_ssot_fact_v10(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v10_preview_and_apply_preserve_rows_and_history(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _rows(path)
    preview = migrate_canonical_ssot_fact_v10(path)
    assert preview["state"] == "PREVIEW_REQUIRES_APPLY"
    assert preview["schema_before"] == "V9"
    assert preview["schema_after_if_applied"] == "V10"
    assert preview["writes_database"] is False

    receipt = _apply(path)
    assert receipt["state"] == "APPLIED_V10"
    assert receipt["rows_content_sha256_before"] == receipt["rows_content_sha256_after"]
    assert _rows(path) == before
    assert _rows(path, BACKUP_TABLE) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V10"
        retired = inspect_canonical_ssot_schema(
            conn, table=BACKUP_TABLE, trigger_names=RETIRED_TRIGGERS
        )
        assert retired["state"] == "V9"
        assert retired["errors"] == []
        for version in ("v8", "v7", "v6"):
            assert conn.execute(
                f"SELECT marker FROM canonical_ssot_fact_{version}_replaced"
            ).fetchone()[0] == f"preserve-{version}"
        table_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='canonical_ssot_fact'"
            ).fetchone()[0]
        )
        assert all(f"'{fact_type}'" in table_sql for fact_type in FACT_TYPES_V10)


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v10_faults_rollback_exactly(tmp_path: Path, stage: str) -> None:
    path = _database(tmp_path, f"fault-{stage}.db")
    rows_before = _rows(path)
    schema_before = _snapshot(path)
    with pytest.raises(
        CanonicalSSOTV10MigrationError,
        match=f"canonical_ssot_v10_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == rows_before
    assert _snapshot(path) == schema_before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V9"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0


def test_v10_confirmation_quiescence_and_already_current(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with pytest.raises(
        CanonicalSSOTV10MigrationError,
        match="canonical_ssot_v10_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v10(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV10MigrationError,
        match="canonical_ssot_v10_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v10(path, apply=True, confirmation=CONFIRMATION)
    assert _apply(path)["state"] == "APPLIED_V10"
    before = _snapshot(path)
    assert _apply(path)["state"] == "ALREADY_V10_NO_WRITE"
    assert migrate_canonical_ssot_fact_v10(path)["state"] == "PREVIEW_ALREADY_V10"
    assert _snapshot(path) == before
