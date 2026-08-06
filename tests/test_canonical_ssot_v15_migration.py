from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V14,
    FACT_TYPES_V15,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v14_migration import (
    TARGET_TABLE_DDL as V14_TABLE_DDL,
    TARGET_TRIGGER_DDL as V14_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v15_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    CanonicalSSOTV15MigrationError,
    migrate_canonical_ssot_fact_v15,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


def _database(tmp_path: Path, name: str = "canonical-v14.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V14_TABLE_DDL)
        for ddl in V14_TRIGGER_DDL:
            conn.execute(ddl)
        for index, fact_type in enumerate(FACT_TYPES_V14, start=1):
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
                    "2026-08-05T00:00:00Z",
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
    return migrate_canonical_ssot_fact_v15(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v15_preview_apply_replay_and_preserve_v14_rows(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _rows(path)

    preview = migrate_canonical_ssot_fact_v15(path)
    assert preview["schema_before"] == "V14"
    assert preview["schema_after_if_applied"] == "V15"
    assert preview["writes_database"] is False
    assert _rows(path) == before

    assert _apply(path)["state"] == "APPLIED_V15"
    assert _rows(path) == before
    assert _rows(path, BACKUP_TABLE) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V15"
        assert inspect_canonical_ssot_schema(
            conn, table=BACKUP_TABLE, trigger_names=RETIRED_TRIGGERS
        )["state"] == "V14"
        sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='canonical_ssot_fact'"
            ).fetchone()[0]
        )
        assert all(f"'{fact_type}'" in sql for fact_type in FACT_TYPES_V15)

    assert _apply(path)["state"] == "ALREADY_V15_NO_WRITE"


def test_v15_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with pytest.raises(CanonicalSSOTV15MigrationError, match="confirmation_required"):
        migrate_canonical_ssot_fact_v15(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV15MigrationError, match="writer_quiescence_receipt_required"
    ):
        migrate_canonical_ssot_fact_v15(
            path, apply=True, confirmation=CONFIRMATION
        )


def test_v15_active_and_retired_tables_remain_append_only(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _apply(path)
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 0)
        digest = "f" * 64
        row = (
            f"truth_authority_supersession:{digest}",
            "truth_authority_supersession",
            "scope:new",
            digest,
            "{}",
            "{}",
            "2026-08-05T00:00:00Z",
        )
        with pytest.raises(sqlite3.IntegrityError, match="canonical_ingress_only"):
            conn.execute("INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", row)
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f'INSERT INTO "{BACKUP_TABLE}" VALUES(?,?,?,?,?,?,?)', row)


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v15_faults_rollback_exactly(tmp_path: Path, stage: str) -> None:
    path = _database(tmp_path, f"fault-{stage}.db")
    before = _rows(path)
    with pytest.raises(
        CanonicalSSOTV15MigrationError,
        match=f"canonical_ssot_v15_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V14"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN (?,?)",
                (NEW_TABLE, BACKUP_TABLE),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "fact_types",
    (
        (*FACT_TYPES_V14, "unknown_fact_type"),
        (*FACT_TYPES_V14[:-2], FACT_TYPES_V14[-1], FACT_TYPES_V14[-2]),
    ),
)
def test_v15_rejects_noncanonical_source_fact_type_contract(
    tmp_path: Path, fact_types: tuple[str, ...]
) -> None:
    path = tmp_path / "invalid-source.db"
    values = ",".join(f"'{value}'" for value in fact_types)
    ddl = V14_TABLE_DDL.replace(
        ",".join(f"'{value}'" for value in FACT_TYPES_V14), values
    )
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(ddl)
        for trigger in V14_TRIGGER_DDL:
            conn.execute(trigger)
    with pytest.raises(
        CanonicalSSOTV15MigrationError, match="source_schema_invalid"
    ):
        migrate_canonical_ssot_fact_v15(path)
