from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    FACT_TYPES_V4,
    FACT_TYPES_V5,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v4_migration import (
    CONFIRMATION as V4_CONFIRMATION,
    migrate_canonical_ssot_fact_v4,
)
from bayes_basket_target.canonical_ssot_v5_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    CanonicalSSOTV5MigrationError,
    migrate_canonical_ssot_fact_v5,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.ssot import apply_production_ssot_schema


V3_DDL = """
CREATE TABLE canonical_ssot_fact (
 fact_id TEXT PRIMARY KEY,
 fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession')),
 scope_key TEXT NOT NULL,
 content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
 fact_json TEXT NOT NULL,
 lineage_json TEXT NOT NULL,
 ingested_at_utc TEXT NOT NULL,
 UNIQUE(fact_type,content_sha256),
 UNIQUE(fact_type,scope_key)
);
CREATE TRIGGER canonical_ssot_fact_no_update BEFORE UPDATE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER canonical_ssot_fact_no_delete BEFORE DELETE ON canonical_ssot_fact
BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def _v4_db(tmp_path: Path, *, name: str = "ssot.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.executescript(V3_DDL)
        for index, fact_type in enumerate(FACT_TYPES_V4[:5]):
            value = f"{index + 1:x}" * 64
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    f"{fact_type}:{value}",
                    fact_type,
                    f"scope-{fact_type}",
                    value,
                    '{"exact":"bytes"}',
                    '{"lineage":"kept"}',
                    "2026-08-04T00:00:00Z",
                ),
            )
    migrate_canonical_ssot_fact_v4(
        path,
        apply=True,
        confirmation=V4_CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    return path


def _rows(path: Path, *, table: str = "canonical_ssot_fact") -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute(f'SELECT * FROM "{table}" ORDER BY fact_id'))


def _apply(path: Path, **kwargs: object) -> dict[str, object]:
    return migrate_canonical_ssot_fact_v5(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v5_preview_is_zero_write_and_apply_preserves_exact_rows(
    tmp_path: Path,
) -> None:
    path = _v4_db(tmp_path)
    before = _rows(path)
    before_stat = path.stat()

    preview = migrate_canonical_ssot_fact_v5(path)

    after_stat = path.stat()
    assert preview["state"] == "PREVIEW_REQUIRES_APPLY"
    assert preview["schema_before"] == "V4"
    assert preview["schema_after_if_applied"] == "V5"
    assert preview["writes_database"] is False
    assert _rows(path) == before
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )

    receipt = _apply(path)
    assert receipt["state"] == "APPLIED_V5"
    assert receipt["rows_content_sha256_before"] == receipt[
        "rows_content_sha256_after"
    ]
    assert receipt["row_count_before"] == receipt["row_count_after"] == 5
    assert _rows(path) == before
    assert _rows(path, table=BACKUP_TABLE) == before
    with sqlite3.connect(path) as conn:
        inspection = inspect_canonical_ssot_schema(conn)
        assert inspection["state"] == "V5"
        assert inspection["fact_types"] == list(FACT_TYPES_V5)
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    "ensemble_member_arrival_binding:" + "f" * 64,
                    "ensemble_member_arrival_binding",
                    "scope-binding",
                    "f" * 64,
                    "{}",
                    "{}",
                    "2026-08-04T00:00:01Z",
                ),
            )


def test_v5_second_apply_and_preview_are_idempotent(tmp_path: Path) -> None:
    path = _v4_db(tmp_path)
    _apply(path)
    before = _rows(path)
    before_backup = _rows(path, table=BACKUP_TABLE)
    receipt = _apply(path)
    assert receipt["state"] == "ALREADY_V5_NO_WRITE"
    assert _rows(path) == before
    assert _rows(path, table=BACKUP_TABLE) == before_backup
    assert migrate_canonical_ssot_fact_v5(path)["state"] == "PREVIEW_ALREADY_V5"


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v5_faults_restore_exact_v4_then_recover(
    tmp_path: Path, stage: str
) -> None:
    path = _v4_db(tmp_path)
    before = _rows(path)
    with pytest.raises(
        CanonicalSSOTV5MigrationError,
        match=f"canonical_ssot_v5_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V4"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0
    assert _apply(path)["state"] == "APPLIED_V5"


@pytest.mark.parametrize(
    ("values", "state"),
    (
        (FACT_TYPES_V5, "V5"),
        (FACT_TYPES_V5[:-1], "V4"),
        ((*FACT_TYPES_V5, "extra"), "INVALID"),
        ((*FACT_TYPES_V4[:-1], FACT_TYPES_V5[-1], FACT_TYPES_V4[-1]), "INVALID"),
    ),
)
def test_v5_inspector_requires_exact_ordered_fact_types(
    tmp_path: Path, values: tuple[str, ...], state: str
) -> None:
    path = tmp_path / (state + str(len(values)) + ".db")
    quoted = ",".join(f"'{value}'" for value in values)
    ddl = V3_DDL.replace(
        "'model','truth','residual','activation','activation_supersession'",
        quoted,
    )
    ddl += (
        "CREATE TRIGGER canonical_ssot_fact_ingress_only "
        "BEFORE INSERT ON canonical_ssot_fact "
        "WHEN canonical_ingress_authorized()!=1 "
        "BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END;"
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(ddl)
        inspection = inspect_canonical_ssot_schema(conn)
    assert inspection["state"] == state


def test_production_schema_requires_receipted_v6_after_v5(
    tmp_path: Path,
) -> None:
    path = _v4_db(tmp_path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(
            RuntimeError, match="canonical_contract_schema_v5_migration_required"
        ):
            apply_production_ssot_schema(conn)
    _apply(path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(
            RuntimeError, match="canonical_contract_schema_v6_migration_required"
        ):
            apply_production_ssot_schema(conn)


def test_v5_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _v4_db(tmp_path)
    with pytest.raises(
        CanonicalSSOTV5MigrationError,
        match="canonical_ssot_v5_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v5(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV5MigrationError,
        match="canonical_ssot_v5_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v5(
            path, apply=True, confirmation=CONFIRMATION
        )
