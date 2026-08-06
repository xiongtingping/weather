from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_ssot_schema import inspect_canonical_ssot_schema
from bayes_basket_target.canonical_ssot_v13_migration import (
    TARGET_TABLE_DDL as V13_TABLE_DDL,
    TARGET_TRIGGER_DDL as V13_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v14_migration import (
    TARGET_TABLE_DDL as V14_TABLE_DDL,
    TARGET_TRIGGER_DDL as V14_TRIGGER_DDL,
)
from bayes_basket_target.ssot import (
    CANONICAL_V14_RELEASE_STATE,
    SCHEMA_VERSION,
    apply_production_ssot_schema,
    production_ssot_schema_status,
)
from scripts.apply_canonical_contract_schema import RETIRED_MESSAGE, migrate


def _install_v13(conn: sqlite3.Connection) -> None:
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(V13_TABLE_DDL)
    for ddl in V13_TRIGGER_DDL:
        conn.execute(ddl)


def test_nonempty_v13_requires_receipted_v14_migration_without_rebuild(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v13.db"
    with sqlite3.connect(database) as conn:
        _install_v13(conn)
        digest = "a" * 64
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            ("model:one", "model", "station:RKPK", digest, "{}", "{}",
             "2026-08-05T00:00:00Z"),
        )
        conn.commit()
        with pytest.raises(
            RuntimeError,
            match="canonical_contract_schema_v14_migration_required",
        ):
            apply_production_ssot_schema(conn)
        assert inspect_canonical_ssot_schema(conn)["state"] == "V13"
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 1


def test_fresh_v14_schema_is_technical_only_not_release_authority() -> None:
    with sqlite3.connect(":memory:") as conn:
        receipt = apply_production_ssot_schema(conn)
        status = production_ssot_schema_status(conn)

    for item in (receipt, status):
        assert item["schema"] == SCHEMA_VERSION
        assert item["canonical_release_state"] == CANONICAL_V14_RELEASE_STATE
        assert item["current_pointer_switched"] is False
        assert item["service_start_authorized"] is False
        assert item["basket_consumption_authorized"] is False
        assert item["formal_live_authorized"] is False
    assert receipt["canonical_contract_schema_ok"] is True
    assert status["canonical_contract_schema_ok"] is True


def test_existing_v14_facts_are_not_rebuilt_by_production_schema_apply(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v14.db"
    with sqlite3.connect(database) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V14_TABLE_DDL)
        for ddl in V14_TRIGGER_DDL:
            conn.execute(ddl)
        digest = "b" * 64
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            ("model:preserved", "model", "station:RKPK", digest, "{}", "{}",
             "2026-08-05T00:00:00Z"),
        )
        before = list(conn.execute("SELECT * FROM canonical_ssot_fact"))
        assert apply_production_ssot_schema(conn)["canonical_empty_upgrade"] == {
            "state": "not_required"
        }
        assert list(conn.execute("SELECT * FROM canonical_ssot_fact")) == before
        assert inspect_canonical_ssot_schema(conn)["state"] == "V14"


@pytest.mark.parametrize("apply", (False, True))
def test_retired_v10_installer_fails_before_opening_any_database(
    tmp_path: Path,
    apply: bool,
) -> None:
    with pytest.raises(RuntimeError, match=RETIRED_MESSAGE):
        migrate(tmp_path / "does-not-exist.db", apply=apply)
