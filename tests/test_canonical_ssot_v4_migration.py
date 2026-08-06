from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    FACT_TYPES_V3,
    FACT_TYPES_V4,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v4_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    CanonicalSSOTV4MigrationError,
    migrate_canonical_ssot_fact_v4,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.apply_canonical_contract_schema import migrate as apply_contract_schema


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


def _db(tmp_path: Path, *, name: str = "ssot.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.executescript(V3_DDL)
        for index, fact_type in enumerate(FACT_TYPES_V3):
            value = str(index + 1) * 64
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
    return path


def _rows(path: Path, *, table: str = "canonical_ssot_fact") -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute(f'SELECT * FROM "{table}" ORDER BY fact_id'))


def _schema(path: Path, *, table: str = "canonical_ssot_fact") -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return "" if row is None else str(row[0])


def _apply(path: Path, **kwargs: object) -> dict[str, object]:
    return migrate_canonical_ssot_fact_v4(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def _assert_append_only(path: Path, table: str) -> None:
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f'UPDATE "{table}" SET scope_key=\'changed\'')
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f'DELETE FROM "{table}"')


def test_preview_is_zero_write_and_reports_exact_v3_identity(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before_stat = path.stat()
    before_rows = _rows(path)
    before_schema = _schema(path)

    receipt = migrate_canonical_ssot_fact_v4(path)

    after_stat = path.stat()
    assert receipt["state"] == "PREVIEW_REQUIRES_APPLY"
    assert receipt["schema_before"] == "V3"
    assert receipt["schema_after_if_applied"] == "V4"
    assert receipt["row_count"] == 5
    assert receipt["fact_type_counts"] == {
        "activation": 1,
        "activation_supersession": 1,
        "model": 1,
        "residual": 1,
        "truth": 1,
    }
    assert receipt["writes_database"] is False
    assert _rows(path) == before_rows
    assert _schema(path) == before_schema
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )


def test_apply_preserves_rows_triggers_and_enables_new_fact_types(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    before = _rows(path)

    receipt = _apply(path)

    assert receipt["state"] == "APPLIED_V4"
    assert receipt["schema_before"] == "V3"
    assert receipt["schema_after"] == "V4"
    assert receipt["row_count_before"] == receipt["row_count_after"] == 5
    assert receipt["rows_content_sha256_before"] == receipt[
        "rows_content_sha256_after"
    ]
    assert receipt["fact_type_counts"] == {
        "activation": 1,
        "activation_supersession": 1,
        "model": 1,
        "residual": 1,
        "truth": 1,
    }
    assert _rows(path) == before
    assert _rows(path, table=BACKUP_TABLE) == before
    assert "'ensemble_member'" in _schema(path)
    assert "'native_arrival'" in _schema(path)

    with sqlite3.connect(path) as conn:
        for index, fact_type in enumerate(("ensemble_member", "native_arrival"), 8):
            value = str(index) * 64
            with pytest.raises(
                sqlite3.OperationalError,
                match="no such function: canonical_ingress_authorized",
            ):
                conn.execute(
                    "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                    (
                        f"{fact_type}:{value}",
                        fact_type,
                        f"scope-{fact_type}",
                        value,
                        "{}",
                        "{}",
                        "2026-08-04T00:00:01Z",
                    ),
                )
        counts = dict(
            conn.execute(
                "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact GROUP BY fact_type"
            )
        )
    assert counts == {fact_type: 1 for fact_type in FACT_TYPES_V3}
    _assert_append_only(path, "canonical_ssot_fact")
    _assert_append_only(path, BACKUP_TABLE)


def test_second_apply_and_preview_are_idempotent_no_write(tmp_path: Path) -> None:
    path = _db(tmp_path)
    _apply(path)
    before_rows = _rows(path)
    before_retired = _rows(path, table=BACKUP_TABLE)
    before_schema = _schema(path)
    before_stat = path.stat()

    receipt = _apply(path)

    after_stat = path.stat()
    assert receipt["state"] == "ALREADY_V4_NO_WRITE"
    assert receipt["row_count_before"] == receipt["row_count_after"] == 5
    assert _rows(path) == before_rows
    assert _rows(path, table=BACKUP_TABLE) == before_retired
    assert _schema(path) == before_schema
    assert (after_stat.st_size, after_stat.st_mtime_ns) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )
    preview = migrate_canonical_ssot_fact_v4(path)
    assert preview["state"] == "PREVIEW_ALREADY_V4"
    assert preview["requires_writer_quiescence"] is False
    assert preview["confirmation_required"] is False


def test_v4_requires_v5_for_standard_schema_apply_and_empty_install_is_v10(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    _apply(path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(
            RuntimeError, match="canonical_contract_schema_v5_migration_required"
        ):
            apply_production_ssot_schema(conn)
        assert inspect_canonical_ssot_schema(conn)["state"] == "V4"

    empty = tmp_path / "empty-v4.db"
    empty.touch()
    assert apply_contract_schema(empty, apply=False)["state"] == "MIGRATION_REQUIRED"
    assert apply_contract_schema(empty, apply=True)["state"] == "APPLIED"
    current = apply_contract_schema(empty, apply=False)
    assert current["state"] == "CURRENT_NO_WRITE"
    with sqlite3.connect(empty) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V10"


@pytest.mark.parametrize(
    ("values", "expected_state"),
    (
        (FACT_TYPES_V4, "V4"),
        (FACT_TYPES_V4[:-1], "INVALID"),
        ((*FACT_TYPES_V3, "native_arrival", "ensemble_member"), "INVALID"),
        ((*FACT_TYPES_V4, "extra"), "INVALID"),
    ),
)
def test_inspector_accepts_only_exact_ordered_v4_fact_types(
    tmp_path: Path, values: tuple[str, ...], expected_state: str
) -> None:
    path = tmp_path / ("-".join(values[-2:]) + ".db")
    quoted = ",".join(f"'{value}'" for value in values)
    ddl = V3_DDL.replace(
        "'model','truth','residual','activation','activation_supersession'", quoted
    )
    ddl += (
        "\nCREATE TRIGGER canonical_ssot_fact_ingress_only "
        "BEFORE INSERT ON canonical_ssot_fact "
        "WHEN canonical_ingress_authorized()!=1 "
        "BEGIN SELECT RAISE(ABORT,'canonical_ingress_only'); END;"
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(ddl)
        inspection = inspect_canonical_ssot_schema(conn)
    assert inspection["state"] == expected_state
    assert inspection["ok"] is (expected_state == "V4")


def test_unknown_fact_type_schema_is_invalid_for_migration(tmp_path: Path) -> None:
    path = tmp_path / "invalid.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            V3_DDL.replace(
                "'model','truth','residual','activation','activation_supersession'",
                "'model','truth','residual','activation','activation_supersession','ensemble_member','native_arrival','extra'",
            )
        )
    with pytest.raises(
        CanonicalSSOTV4MigrationError, match="canonical_ssot_v4_schema_invalid"
    ):
        migrate_canonical_ssot_fact_v4(path)


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_injected_fault_rolls_back_then_recovers_and_is_idempotent(
    tmp_path: Path, stage: str
) -> None:
    path = _db(tmp_path)
    before_rows = _rows(path)
    before_schema = _schema(path)

    with pytest.raises(
        CanonicalSSOTV4MigrationError,
        match=f"canonical_ssot_v4_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)

    assert _rows(path) == before_rows
    assert _schema(path) == before_schema
    with sqlite3.connect(path) as conn:
        leftovers = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchall()
        assert leftovers == []
        assert inspect_canonical_ssot_schema(conn)["state"] == "V3"
    _assert_append_only(path, "canonical_ssot_fact")

    assert _apply(path)["state"] == "APPLIED_V4"
    assert _rows(path) == before_rows
    assert _rows(path, table=BACKUP_TABLE) == before_rows
    assert _apply(path, inject_fault_at=stage)["state"] == "ALREADY_V4_NO_WRITE"


def test_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with pytest.raises(
        CanonicalSSOTV4MigrationError,
        match="canonical_ssot_v4_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v4(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV4MigrationError,
        match="canonical_ssot_v4_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v4(
            path, apply=True, confirmation=CONFIRMATION
        )
    assert inspect_schema(path) == "V3"


def inspect_schema(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(inspect_canonical_ssot_schema(conn)["state"])


def test_incoming_foreign_key_rejects_without_schema_or_row_change(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    before_rows = _rows(path)
    before_schema = _schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE canonical_consumer("
            "id TEXT PRIMARY KEY,"
            "fact_id TEXT REFERENCES canonical_ssot_fact(fact_id))"
        )

    with pytest.raises(
        CanonicalSSOTV4MigrationError,
        match="canonical_ssot_v4_incoming_foreign_key_present",
    ):
        _apply(path)

    assert _rows(path) == before_rows
    assert _schema(path) == before_schema
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0


def test_symlink_database_is_rejected(tmp_path: Path) -> None:
    path = _db(tmp_path)
    link = tmp_path / "ssot-link.db"
    link.symlink_to(path)
    with pytest.raises(
        CanonicalSSOTV4MigrationError,
        match="canonical_ssot_v4_database_path_invalid",
    ):
        migrate_canonical_ssot_fact_v4(link)


def test_leftover_table_rejects_and_rolls_back(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {NEW_TABLE}(sentinel TEXT)")
    before_rows = _rows(path)
    with pytest.raises(
        CanonicalSSOTV4MigrationError,
        match="canonical_ssot_v4_leftover_table_present",
    ):
        _apply(path)
    assert _rows(path) == before_rows
    assert inspect_schema(path) == "V3"
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {NEW_TABLE}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "ddl",
    (
        V3_DDL.replace("RAISE(ABORT,'append_only')", "RAISE(ABORT,'mutable')", 1),
        V3_DDL.replace(
            ",\n UNIQUE(fact_type,scope_key)\n);", "\n);"
        ),
    ),
)
def test_malformed_trigger_or_missing_unique_key_is_invalid(
    tmp_path: Path, ddl: str
) -> None:
    path = tmp_path / "malformed.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(ddl)
    with pytest.raises(
        CanonicalSSOTV4MigrationError, match="canonical_ssot_v4_schema_invalid"
    ):
        migrate_canonical_ssot_fact_v4(path)
