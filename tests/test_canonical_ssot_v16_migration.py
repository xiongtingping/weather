from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V15,
    FACT_TYPES_V16,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v15_migration import (
    TARGET_TABLE_DDL as V15_TABLE_DDL,
    TARGET_TRIGGER_DDL as V15_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v16_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    TARGET_TABLE_DDL,
    CanonicalSSOTV16MigrationError,
    migrate_canonical_ssot_fact_v16,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


def _database(tmp_path: Path, name: str = "canonical-v15.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V15_TABLE_DDL)
        for ddl in V15_TRIGGER_DDL:
            conn.execute(ddl)
        for index, fact_type in enumerate(FACT_TYPES_V15, start=1):
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
    return migrate_canonical_ssot_fact_v16(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def test_v16_preview_apply_replay_and_preserve_v15_rows(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _rows(path)

    preview = migrate_canonical_ssot_fact_v16(path)
    assert preview["schema_before"] == "V15"
    assert preview["schema_after_if_applied"] == "V16"
    assert preview["writes_database"] is False
    assert _rows(path) == before

    assert _apply(path)["state"] == "APPLIED_V16"
    assert _rows(path) == before
    assert _rows(path, BACKUP_TABLE) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V16"
        assert inspect_canonical_ssot_schema(
            conn, table=BACKUP_TABLE, trigger_names=RETIRED_TRIGGERS
        )["state"] == "V15"
        sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='canonical_ssot_fact'"
            ).fetchone()[0]
        )
        assert all(f"'{fact_type}'" in sql for fact_type in FACT_TYPES_V16)

    assert _apply(path)["state"] == "ALREADY_V16_NO_WRITE"


@pytest.mark.parametrize(
    "new_fact_type",
    ("native_ensemble_oos_event", "native_ensemble_oos_cohort"),
)
def test_v16_allows_only_new_native_oos_types_through_ingress(
    tmp_path: Path, new_fact_type: str
) -> None:
    path = _database(tmp_path)
    _apply(path)
    with sqlite3.connect(path) as conn:
        digest = ("e" if new_fact_type.endswith("event") else "c") * 64
        row = (
            f"{new_fact_type}:{digest}",
            new_fact_type,
            f"scope:{new_fact_type}",
            digest,
            "{}",
            "{}",
            "2026-08-05T00:00:00Z",
        )
        conn.create_function("canonical_ingress_authorized", 0, lambda: 0)
        with pytest.raises(sqlite3.IntegrityError, match="canonical_ingress_only"):
            conn.execute("INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", row)

        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute("INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)", row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    "unknown:" + "a" * 64,
                    "unknown_native_oos_type",
                    "scope:unknown",
                    "a" * 64,
                    "{}",
                    "{}",
                    "2026-08-05T00:00:00Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "UPDATE canonical_ssot_fact SET fact_json='{}' WHERE fact_id=?",
                (row[0],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(f'INSERT INTO "{BACKUP_TABLE}" VALUES(?,?,?,?,?,?,?)', row)


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v16_faults_rollback_exactly(tmp_path: Path, stage: str) -> None:
    path = _database(tmp_path, f"fault-{stage}.db")
    before = _rows(path)
    with pytest.raises(
        CanonicalSSOTV16MigrationError,
        match=f"canonical_ssot_v16_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)
    assert _rows(path) == before
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V15"
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
        (*FACT_TYPES_V15, "unknown_fact_type"),
        (*FACT_TYPES_V15[:-2], FACT_TYPES_V15[-1], FACT_TYPES_V15[-2]),
    ),
)
def test_v16_rejects_noncanonical_source_fact_type_contract(
    tmp_path: Path, fact_types: tuple[str, ...]
) -> None:
    path = tmp_path / "invalid-source.db"
    values = ",".join(f"'{value}'" for value in fact_types)
    ddl = V15_TABLE_DDL.replace(
        ",".join(f"'{value}'" for value in FACT_TYPES_V15), values
    )
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(ddl)
        for trigger in V15_TRIGGER_DDL:
            conn.execute(trigger)
    with pytest.raises(
        CanonicalSSOTV16MigrationError, match="source_schema_invalid"
    ):
        migrate_canonical_ssot_fact_v16(path)


def test_v16_inspector_rejects_reordered_or_extra_target_contract(
    tmp_path: Path,
) -> None:
    for name, fact_types in (
        ("reordered", (*FACT_TYPES_V16[:-2], FACT_TYPES_V16[-1], FACT_TYPES_V16[-2])),
        ("extra", (*FACT_TYPES_V16, "unknown_fact_type")),
    ):
        path = tmp_path / f"{name}.db"
        values = ",".join(f"'{value}'" for value in fact_types)
        ddl = TARGET_TABLE_DDL.replace(
            ",".join(f"'{value}'" for value in FACT_TYPES_V16), values
        )
        with sqlite3.connect(path) as conn:
            conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
            conn.execute(ddl)
            from bayes_basket_target.canonical_ssot_v16_migration import (
                TARGET_TRIGGER_DDL,
            )

            for trigger in TARGET_TRIGGER_DDL:
                conn.execute(trigger)
            inspection = inspect_canonical_ssot_schema(conn)
        assert inspection["state"] == "INVALID"
        assert "fact_type_check_mismatch" in inspection["errors"]
