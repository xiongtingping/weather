from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    FACT_TYPES_V4,
    FACT_TYPES_V5,
    FACT_TYPES_V6,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v4_migration import (
    CONFIRMATION as V4_CONFIRMATION,
    migrate_canonical_ssot_fact_v4,
)
from bayes_basket_target.canonical_ssot_v5_migration import (
    BACKUP_TABLE as V5_BACKUP_TABLE,
    CONFIRMATION as V5_CONFIRMATION,
    migrate_canonical_ssot_fact_v5,
)
from bayes_basket_target.canonical_ssot_v6_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    CanonicalSSOTV6MigrationError,
    migrate_canonical_ssot_fact_v6,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    production_ssot_schema_status,
)
from scripts.apply_canonical_contract_schema import migrate as install_schema


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


def _rows(path: Path, *, table: str = "canonical_ssot_fact") -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute(f'SELECT * FROM "{table}" ORDER BY fact_id'))


def _history(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    return {
        table: _rows(path, table=table)
        for table in (V5_BACKUP_TABLE, "canonical_ssot_fact_v3_replaced")
        if table in tables
    }


def _v5_db(tmp_path: Path, *, name: str = "ssot.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.executescript(V3_DDL)
        for index, fact_type in enumerate(FACT_TYPES_V4[:5]):
            digest = f"{index + 1:x}" * 64
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    f"{fact_type}:{digest}",
                    fact_type,
                    f"scope-{fact_type}",
                    digest,
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
    migrate_canonical_ssot_fact_v5(
        path,
        apply=True,
        confirmation=V5_CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V5"
    return path


def _apply(path: Path, **kwargs: object) -> dict[str, object]:
    return migrate_canonical_ssot_fact_v6(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v6_preview_apply_idempotency_and_history_identity(
    tmp_path: Path,
) -> None:
    path = _v5_db(tmp_path)
    before = _rows(path)
    before_history = _history(path)
    before_stat = path.stat()

    preview = migrate_canonical_ssot_fact_v6(path)

    assert preview["state"] == "PREVIEW_REQUIRES_APPLY"
    assert preview["schema_before"] == "V5"
    assert preview["schema_after_if_applied"] == "V6"
    assert preview["row_count"] == len(before)
    assert preview["v5_legacy_binding_count"] == 0
    assert preview["v5_legacy_binding_rows_copied"] == 0
    assert preview["writes_database"] is False
    assert _rows(path) == before
    assert _history(path) == before_history
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )

    receipt = _apply(path)

    assert receipt["state"] == "APPLIED_V6"
    assert receipt["row_count_before"] == receipt["row_count_after"] == len(before)
    assert receipt["rows_content_sha256_before"] == receipt[
        "rows_content_sha256_after"
    ]
    assert receipt["fact_type_counts_before"] == receipt["fact_type_counts_after"]
    assert receipt["v5_legacy_binding_count"] == 0
    assert receipt["v5_legacy_binding_rows_copied"] == 0
    assert _rows(path) == before
    assert _rows(path, table=BACKUP_TABLE) == before
    assert _history(path) == before_history
    with sqlite3.connect(path) as conn:
        inspection = inspect_canonical_ssot_schema(conn)
        assert inspection["state"] == "V6"
        assert inspection["fact_types"] == list(FACT_TYPES_V6)
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (NEW_TABLE,),
        ).fetchone()[0] == 0

    second = _apply(path)
    assert second["state"] == "ALREADY_V6_NO_WRITE"
    assert _rows(path) == before
    assert _rows(path, table=BACKUP_TABLE) == before
    assert _history(path) == before_history
    assert migrate_canonical_ssot_fact_v6(path)["state"] == "PREVIEW_ALREADY_V6"


def test_v6_rejects_v5_binding_in_preview_and_apply(tmp_path: Path) -> None:
    path = _v5_db(tmp_path)
    digest = "f" * 64
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                f"ensemble_member_arrival_binding:{digest}",
                "ensemble_member_arrival_binding",
                "legacy-binding-scope",
                digest,
                "{}",
                "{}",
                "2026-08-04T00:00:01Z",
            ),
        )
    before = _rows(path)
    before_history = _history(path)

    with pytest.raises(
        CanonicalSSOTV6MigrationError,
        match="canonical_ssot_v6_legacy_v5_binding_present",
    ):
        migrate_canonical_ssot_fact_v6(path)
    with pytest.raises(
        CanonicalSSOTV6MigrationError,
        match="canonical_ssot_v6_legacy_v5_binding_present",
    ):
        _apply(path)

    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V5"
    assert _rows(path) == before
    assert _history(path) == before_history


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v6_faults_restore_v5_and_all_prior_history(
    tmp_path: Path, stage: str
) -> None:
    path = _v5_db(tmp_path, name=f"fault-{stage}.db")
    before = _rows(path)
    before_history = _history(path)

    with pytest.raises(
        CanonicalSSOTV6MigrationError,
        match=f"canonical_ssot_v6_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)

    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V5"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0
    assert _rows(path) == before
    assert _history(path) == before_history


@pytest.mark.parametrize(
    ("values", "state"),
    (
        (FACT_TYPES_V6, "V6"),
        (FACT_TYPES_V5, "V5"),
        ((*FACT_TYPES_V6, "extra"), "INVALID"),
        ((*FACT_TYPES_V5, FACT_TYPES_V6[-1], FACT_TYPES_V5[-1]), "INVALID"),
    ),
)
def test_v6_inspector_requires_exact_ordered_fact_types(
    tmp_path: Path, values: tuple[str, ...], state: str
) -> None:
    path = tmp_path / f"{state}-{len(values)}.db"
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


def test_v6_installers_create_or_require_exact_v6(tmp_path: Path) -> None:
    path = tmp_path / "installer.db"
    with sqlite3.connect(path):
        pass
    receipt = install_schema(path, apply=True)
    assert receipt["state"] == "APPLIED"
    assert receipt["canonical_contract_schema_ok"] is True
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V6"
        assert apply_production_ssot_schema(conn)[
            "canonical_contract_schema_ok"
        ] is True
        assert production_ssot_schema_status(conn)[
            "canonical_contract_schema_ok"
        ] is True

    v5_path = _v5_db(tmp_path, name="installer-v5.db")
    with pytest.raises(
        RuntimeError, match="canonical_contract_schema_v6_migration_required"
    ):
        install_schema(v5_path, apply=True)
