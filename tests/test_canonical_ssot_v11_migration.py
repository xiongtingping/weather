from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V10,
    FACT_TYPES_V11,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v10_migration import (
    TARGET_TABLE_DDL as V10_TABLE_DDL,
    TARGET_TRIGGER_DDL as V10_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v11_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    CanonicalSSOTV11MigrationError,
    migrate_canonical_ssot_fact_v11,
)
from bayes_basket_target.migration_quiescence import synthetic_writer_quiescence_receipt


def _database(tmp_path: Path, name: str = "canonical-v10.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V10_TABLE_DDL)
        for ddl in V10_TRIGGER_DDL:
            conn.execute(ddl)
        for index, fact_type in enumerate(FACT_TYPES_V10, start=1):
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
    return path


def _rows(path: Path, table: str = "canonical_ssot_fact") -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return list(
            conn.execute(
                f'SELECT {",".join(EXPECTED_COLUMNS)} FROM "{table}" ORDER BY fact_id'
            )
        )


def _apply(path: Path, **kwargs: object) -> dict[str, object]:
    return migrate_canonical_ssot_fact_v11(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v11_preview_apply_and_idempotence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _rows(path)
    preview = migrate_canonical_ssot_fact_v11(path)
    assert preview["schema_before"] == "V10"
    assert preview["schema_after_if_applied"] == "V11"
    receipt = _apply(path)
    assert receipt["state"] == "APPLIED_V11"
    assert _rows(path) == before
    assert _rows(path, BACKUP_TABLE) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V11"
        assert inspect_canonical_ssot_schema(
            conn, table=BACKUP_TABLE, trigger_names=RETIRED_TRIGGERS
        )["state"] == "V10"
        sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='canonical_ssot_fact'"
            ).fetchone()[0]
        )
        assert all(f"'{fact_type}'" in sql for fact_type in FACT_TYPES_V11)
    assert _apply(path)["state"] == "ALREADY_V11_NO_WRITE"


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v11_faults_rollback_exactly(tmp_path: Path, stage: str) -> None:
    path = _database(tmp_path, f"fault-{stage}.db")
    before = _rows(path)
    with pytest.raises(
        CanonicalSSOTV11MigrationError,
        match=f"canonical_ssot_v11_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V10"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0


def test_v11_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with pytest.raises(CanonicalSSOTV11MigrationError, match="confirmation_required"):
        migrate_canonical_ssot_fact_v11(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV11MigrationError,
        match="writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v11(path, apply=True, confirmation=CONFIRMATION)
