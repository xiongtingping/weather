from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import (
    EXPECTED_COLUMNS,
    FACT_TYPES_V6,
    FACT_TYPES_V7,
    V6_TRIGGERS,
    V7_TRIGGERS,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v7_migration import (
    BACKUP_TABLE,
    CONFIRMATION,
    NEW_TABLE,
    RETIRED_TRIGGERS,
    CanonicalSSOTV7MigrationError,
    migrate_canonical_ssot_fact_v7,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


def _v6_ddl() -> str:
    fact_types = ",".join(f"'{fact_type}'" for fact_type in FACT_TYPES_V6)
    return f"""
    CREATE TABLE canonical_ssot_fact (
      fact_id TEXT PRIMARY KEY,
      fact_type TEXT NOT NULL CHECK(fact_type IN ({fact_types})),
      scope_key TEXT NOT NULL,
      content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
      fact_json TEXT NOT NULL,
      lineage_json TEXT NOT NULL,
      ingested_at_utc TEXT NOT NULL,
      UNIQUE(fact_type, content_sha256),
      UNIQUE(fact_type, scope_key)
    );
    CREATE TRIGGER canonical_ssot_fact_no_update
    BEFORE UPDATE ON canonical_ssot_fact
    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
    CREATE TRIGGER canonical_ssot_fact_no_delete
    BEFORE DELETE ON canonical_ssot_fact
    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
    CREATE TRIGGER canonical_ssot_fact_ingress_only
    BEFORE INSERT ON canonical_ssot_fact
    WHEN canonical_ingress_authorized() != 1
    BEGIN SELECT RAISE(ABORT, 'canonical_ingress_only'); END;
    """


def _v6_database(tmp_path: Path, *, name: str = "canonical-v6.db") -> Path:
    path = tmp_path / name
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.executescript(_v6_ddl())
        for index, fact_type in enumerate(FACT_TYPES_V6, start=1):
            digest = f"{index:064x}"
            conn.execute(
                "INSERT INTO canonical_ssot_fact "
                "(fact_id,fact_type,scope_key,content_sha256,fact_json,"
                "lineage_json,ingested_at_utc) VALUES(?,?,?,?,?,?,?)",
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
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V6"
    return path


def _rows(path: Path, *, table: str = "canonical_ssot_fact") -> list[tuple[object, ...]]:
    columns = ",".join(EXPECTED_COLUMNS)
    with sqlite3.connect(path) as conn:
        return list(conn.execute(f'SELECT {columns} FROM "{table}" ORDER BY fact_id'))


def _schema_snapshot(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[str, str, str]]]:
    with sqlite3.connect(path) as conn:
        master = list(
            conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE type IN ('table','trigger') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type,name"
            )
        )
    return _rows(path), [(str(kind), str(name), str(sql)) for kind, name, sql in master]


def _apply(path: Path, **kwargs: object) -> dict[str, object]:
    return migrate_canonical_ssot_fact_v7(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
        **kwargs,
    )


def _resign_quiescence_receipt(receipt: dict[str, object]) -> dict[str, object]:
    identity = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return receipt


def test_v7_preview_is_zero_write_and_reports_v6_upgrade(tmp_path: Path) -> None:
    path = _v6_database(tmp_path)
    before_bytes = path.read_bytes()
    before = _schema_snapshot(path)

    preview = migrate_canonical_ssot_fact_v7(path)

    assert preview["state"] == "PREVIEW_REQUIRES_APPLY"
    assert preview["schema_before"] == "V6"
    assert preview["schema_after_if_applied"] == "V7"
    assert preview["row_count"] == len(FACT_TYPES_V6)
    assert preview["fact_type_counts"] == {fact_type: 1 for fact_type in FACT_TYPES_V6}
    assert preview["trigger_names"] == sorted(V6_TRIGGERS)
    assert preview["writes_database"] is False
    assert preview["requires_writer_quiescence"] is True
    assert preview["confirmation_required"] is True
    assert path.read_bytes() == before_bytes
    assert _schema_snapshot(path) == before


def test_v7_apply_requires_confirmation_and_valid_quiescence(tmp_path: Path) -> None:
    path = _v6_database(tmp_path)
    before = _schema_snapshot(path)

    with pytest.raises(
        CanonicalSSOTV7MigrationError,
        match="canonical_ssot_v7_confirmation_required",
    ):
        migrate_canonical_ssot_fact_v7(path, apply=True)
    with pytest.raises(
        CanonicalSSOTV7MigrationError,
        match="canonical_ssot_v7_writer_quiescence_receipt_required",
    ):
        migrate_canonical_ssot_fact_v7(path, apply=True, confirmation=CONFIRMATION)

    invalid_receipt = synthetic_writer_quiescence_receipt(path)
    invalid_receipt["quiescent"] = False
    with pytest.raises(
        CanonicalSSOTV7MigrationError,
        match="canonical_ssot_v7_writer_quiescence_invalid",
    ):
        migrate_canonical_ssot_fact_v7(
            path,
            apply=True,
            confirmation=CONFIRMATION,
            writer_quiescence_receipt=_resign_quiescence_receipt(invalid_receipt),
        )

    assert _schema_snapshot(path) == before


def test_v7_apply_preserves_rows_types_fingerprint_foreign_keys_and_triggers(
    tmp_path: Path,
) -> None:
    path = _v6_database(tmp_path)
    before_rows = _rows(path)
    writer_receipt = synthetic_writer_quiescence_receipt(path)

    receipt = migrate_canonical_ssot_fact_v7(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=writer_receipt,
    )

    expected_counts = {fact_type: 1 for fact_type in FACT_TYPES_V6}
    assert receipt["state"] == "APPLIED_V7"
    assert receipt["schema_before"] == "V6"
    assert receipt["schema_after"] == "V7"
    assert receipt["writes_database"] is True
    assert receipt["row_count_before"] == receipt["row_count_after"] == len(before_rows)
    assert receipt["fact_type_counts_before"] == expected_counts
    assert receipt["fact_type_counts_after"] == expected_counts
    assert receipt["rows_content_sha256_before"] == receipt["rows_content_sha256_after"]
    assert receipt["writer_quiescence_receipt_sha256"] == writer_receipt["receipt_sha256"]
    assert receipt["trigger_names_before"] == sorted(V6_TRIGGERS)
    assert receipt["trigger_names_after"] == sorted(V7_TRIGGERS)
    assert _rows(path) == before_rows
    assert _rows(path, table=BACKUP_TABLE) == before_rows

    with sqlite3.connect(path) as conn:
        active = inspect_canonical_ssot_schema(conn)
        retired = inspect_canonical_ssot_schema(
            conn, table=BACKUP_TABLE, trigger_names=RETIRED_TRIGGERS
        )
        assert active["state"] == "V7"
        assert active["fact_types"] == list(FACT_TYPES_V7)
        assert active["trigger_names"] == list(V7_TRIGGERS)
        assert retired["state"] == "V6"
        assert retired["fact_types"] == list(FACT_TYPES_V6)
        assert retired["trigger_names"] == list(RETIRED_TRIGGERS)
        assert list(conn.execute("PRAGMA foreign_key_list(canonical_ssot_fact)")) == []
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (NEW_TABLE,),
        ).fetchone()[0] == 0
        fact_id = str(before_rows[0][0])
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                "UPDATE canonical_ssot_fact SET scope_key=scope_key WHERE fact_id=?",
                (fact_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM canonical_ssot_fact WHERE fact_id=?", (fact_id,))
        conn.create_function("canonical_ingress_authorized", 0, lambda: 0)
        with pytest.raises(sqlite3.IntegrityError, match="canonical_ingress_only"):
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    "republisher_oos_artifact:" + "f" * 64,
                    "republisher_oos_artifact",
                    "scope:republisher_oos_artifact",
                    "f" * 64,
                    "{}",
                    "{}",
                    "2026-08-04T00:00:01Z",
                ),
            )


@pytest.mark.parametrize(
    "stage", ("after_create", "after_copy", "after_swap", "before_commit")
)
def test_v7_faults_fully_rollback_and_allow_clean_retry(
    tmp_path: Path, stage: str
) -> None:
    path = _v6_database(tmp_path, name=f"fault-{stage}.db")
    before = _schema_snapshot(path)

    with pytest.raises(
        CanonicalSSOTV7MigrationError,
        match=f"canonical_ssot_v7_injected_fault:{stage}",
    ):
        _apply(path, inject_fault_at=stage)

    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V6"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name IN (?,?)",
            (NEW_TABLE, BACKUP_TABLE),
        ).fetchone()[0] == 0
        assert {
            str(name)
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='canonical_ssot_fact'"
            )
        } == set(V6_TRIGGERS)
    assert _schema_snapshot(path) == before
    assert _apply(path)["state"] == "APPLIED_V7"


def test_v7_already_migrated_apply_and_preview_are_zero_write(tmp_path: Path) -> None:
    path = _v6_database(tmp_path)
    assert _apply(path)["state"] == "APPLIED_V7"
    before_bytes = path.read_bytes()
    before = _schema_snapshot(path)

    receipt = _apply(path)
    preview = migrate_canonical_ssot_fact_v7(path)

    assert receipt["state"] == "ALREADY_V7_NO_WRITE"
    assert receipt["schema_before"] == receipt["schema_after"] == "V7"
    assert receipt["writes_database"] is False
    assert preview["state"] == "PREVIEW_ALREADY_V7"
    assert preview["requires_writer_quiescence"] is False
    assert preview["confirmation_required"] is False
    assert preview["writes_database"] is False
    assert path.read_bytes() == before_bytes
    assert _schema_snapshot(path) == before
