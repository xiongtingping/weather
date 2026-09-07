from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_v3_migration import (
    CONFIRMATION,
    CanonicalSSOTV3MigrationError,
    migrate_canonical_ssot_fact_v3,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    production_ssot_schema_status,
)
from scripts.apply_canonical_contract_schema import migrate as apply_contract_schema


OLD_DDL = """
CREATE TABLE canonical_ssot_fact (
 fact_id TEXT PRIMARY KEY,
 fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation')),
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


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "ssot.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(OLD_DDL)
        for index, fact_type in enumerate(("model", "truth", "residual", "activation")):
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


def _rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute("SELECT * FROM canonical_ssot_fact ORDER BY fact_id"))


def _schema(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
            ).fetchone()[0]
        )


def test_preview_is_zero_write_and_reports_exact_identity(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before_stat = path.stat()
    before_rows = _rows(path)
    before_schema = _schema(path)
    receipt = migrate_canonical_ssot_fact_v3(path)
    after_stat = path.stat()
    assert receipt["state"] == "PREVIEW_REQUIRES_APPLY"
    assert receipt["row_count"] == 4
    assert receipt["fact_type_counts"] == {
        "activation": 1,
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


def test_apply_preserves_rows_triggers_and_allows_supersession(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = _rows(path)
    receipt = migrate_canonical_ssot_fact_v3(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    assert receipt["state"] == "APPLIED_V3"
    assert receipt["rows_content_sha256_before"] == receipt["rows_content_sha256_after"]
    assert receipt["row_count_before"] == receipt["row_count_after"] == 4
    assert _rows(path) == before
    assert "'activation_supersession'" in _schema(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact_v2_replaced"
        ).fetchone()[0] == 4
        value = "f" * 64
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                "activation_supersession:" + value,
                "activation_supersession",
                "scope-supersession",
                value,
                "{}",
                "{}",
                "2026-08-04T00:00:01Z",
            ),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("UPDATE canonical_ssot_fact SET scope_key='changed'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM canonical_ssot_fact")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM canonical_ssot_fact_v2_replaced")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "UPDATE canonical_ssot_fact_v2_replaced SET scope_key='changed'"
            )


def test_second_apply_is_idempotent_no_write(tmp_path: Path) -> None:
    path = _db(tmp_path)
    migrate_canonical_ssot_fact_v3(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    before = _rows(path)
    receipt = migrate_canonical_ssot_fact_v3(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    assert receipt["state"] == "ALREADY_V3_NO_WRITE"
    assert receipt["row_count_before"] == receipt["row_count_after"] == 4
    assert _rows(path) == before


def test_incoming_foreign_key_rejects_without_schema_change(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = _rows(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE canonical_consumer("
            "id TEXT PRIMARY KEY,"
            "fact_id TEXT REFERENCES canonical_ssot_fact(fact_id))"
        )
    with pytest.raises(
        CanonicalSSOTV3MigrationError,
        match="canonical_ssot_v3_incoming_foreign_key_present",
    ):
        migrate_canonical_ssot_fact_v3(
            path,
            apply=True,
            confirmation=CONFIRMATION,
            writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        )
    assert _rows(path) == before
    assert "'activation_supersession'" not in _schema(path)


def test_extra_fact_type_schema_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "invalid.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            OLD_DDL.replace(
                "'model','truth','residual','activation'",
                "'model','truth','residual','activation','activation_supersession','extra'",
            )
        )
    with pytest.raises(
        CanonicalSSOTV3MigrationError,
        match="canonical_ssot_v3_schema_invalid",
    ):
        migrate_canonical_ssot_fact_v3(path)


def test_lookalike_literal_does_not_masquerade_as_v3(tmp_path: Path) -> None:
    path = tmp_path / "lookalike.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            OLD_DDL.replace(
                "CHECK(fact_type IN ('model','truth','residual','activation'))",
                "CHECK(fact_type IN ('model','truth','residual','activation')) "
                "CHECK(scope_key!='activation_supersession')",
            )
        )
    assert migrate_canonical_ssot_fact_v3(path)["state"] == "PREVIEW_REQUIRES_APPLY"


def test_production_schema_apply_rejects_v2_before_any_other_ddl(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        before_schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        before_tables = list(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
        with pytest.raises(
            RuntimeError,
            match="canonical_contract_schema_v14_migration_required",
        ):
            apply_production_ssot_schema(conn)
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == before_schema_version
        assert list(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ) == before_tables
        assert production_ssot_schema_status(conn)[
            "canonical_contract_schema_ok"
        ] is False


def test_the_retired_one_shot_schema_entry_fails_closed(tmp_path: Path) -> None:
    """The tombstone raises on preview as well as on apply.

    This used to read a MIGRATION_REQUIRED preview off the one-shot entry
    and then check that applying refused.  The command could create a V10
    fact table outside the receipted V13-to-V14 migration, so it was
    retired to a fail-closed tombstone that does not resolve, open or
    inspect the path at all -- preview included, deliberately, because a
    preview that opened the database would already be the ingress the
    retirement exists to close.
    """

    path = _db(tmp_path)
    for apply in (False, True):
        with pytest.raises(
            RuntimeError,
            match="canonical_contract_schema_legacy_apply_retired",
        ):
            apply_contract_schema(path, apply=apply)

def test_symlink_database_is_rejected(tmp_path: Path) -> None:
    path = _db(tmp_path)
    link = tmp_path / "ssot-link.db"
    link.symlink_to(path)
    with pytest.raises(
        CanonicalSSOTV3MigrationError,
        match="canonical_ssot_v3_database_path_invalid",
    ):
        migrate_canonical_ssot_fact_v3(link)


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_injected_fault_rolls_back_entire_migration(
    tmp_path: Path, stage: str
) -> None:
    path = _db(tmp_path)
    before = _rows(path)
    with pytest.raises(
        CanonicalSSOTV3MigrationError,
        match=f"canonical_ssot_v3_injected_fault:{stage}",
    ):
        migrate_canonical_ssot_fact_v3(
            path,
            apply=True,
            confirmation=CONFIRMATION,
            writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
            inject_fault_at=stage,
        )
    assert _rows(path) == before
    assert "'activation_supersession'" not in _schema(path)
    with sqlite3.connect(path) as conn:
        leftovers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('canonical_ssot_fact_v3_new','canonical_ssot_fact_v2_replaced')"
        ).fetchone()[0]
        assert leftovers == 0
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM canonical_ssot_fact")


def test_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with pytest.raises(
        CanonicalSSOTV3MigrationError,
        match="canonical_ssot_v3_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v3(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV3MigrationError,
        match="canonical_ssot_v3_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v3(
            path, apply=True, confirmation=CONFIRMATION
        )
