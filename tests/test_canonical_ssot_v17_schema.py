from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from bayes_basket_target.canonical_ssot_schema import (
    FACT_TYPES_V17,
    inspect_canonical_ssot_schema,
)
from bayes_basket_target.canonical_ssot_v17_migration import (
    CONFIRMATION,
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
    migrate_canonical_ssot_fact_v17,
)
from bayes_basket_target.canonical_ssot_v16_migration import (
    TARGET_TABLE_DDL as V16_TABLE_DDL,
    TARGET_TRIGGER_DDL as V16_TRIGGER_DDL,
)
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)


def test_v17_schema_is_exact_and_append_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.create_function("canonical_ingress_authorized", 0, lambda: 0)
    conn.execute(TARGET_TABLE_DDL)
    for ddl in TARGET_TRIGGER_DDL:
        conn.execute(ddl)

    inspection = inspect_canonical_ssot_schema(conn)

    assert inspection["ok"] is True
    assert inspection["state"] == "V17"
    assert tuple(inspection["fact_types"]) == FACT_TYPES_V17
    with __import__("pytest").raises(sqlite3.IntegrityError, match="canonical_ingress_only"):
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            ("x", "p1_base", "scope", "0" * 64, "{}", "{}", "2026-08-05T00:00:00Z"),
        )


def test_v17_rejects_reordered_fact_type_check() -> None:
    conn = sqlite3.connect(":memory:")
    reordered = TARGET_TABLE_DDL.replace(
        "'surface_observation','surface_observation_winner'",
        "'surface_observation_winner','surface_observation'",
    )
    conn.execute(reordered)
    inspection = inspect_canonical_ssot_schema(conn, require_triggers=False)
    assert inspection["ok"] is False
    assert inspection["state"] == "INVALID"
    assert "fact_type_check_mismatch" in inspection["errors"]


def test_v17_preview_apply_replay_preserves_v16_rows(tmp_path: Path) -> None:
    path = tmp_path / "canonical-v16.db"
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(V16_TABLE_DDL)
        for ddl in V16_TRIGGER_DDL:
            conn.execute(ddl)
        digest = "a" * 64
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                f"p0_forecast_snapshot:{digest}",
                "p0_forecast_snapshot",
                "KMIA|2026-08-03|America/New_York|d2|18Z",
                digest,
                json.dumps({"content_sha256": digest}, separators=(",", ":")),
                json.dumps({"fact_type": "p0_forecast_snapshot"}, separators=(",", ":")),
                "2026-08-05T00:00:00Z",
            ),
        )
    before = path.read_bytes()

    preview = migrate_canonical_ssot_fact_v17(path)
    assert preview["state"] == "PREVIEW_REQUIRES_APPLY"
    assert preview["schema_before"] == "V16"
    assert preview["source_table_row_count"] == 1
    assert preview["source_table_logical_bytes"] > 0
    assert path.read_bytes() == before

    applied = migrate_canonical_ssot_fact_v17(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    assert applied["state"] == "APPLIED_V17"
    with sqlite3.connect(path) as conn:
        assert inspect_canonical_ssot_schema(conn)["state"] == "V17"
        assert conn.execute(
            "SELECT fact_type,content_sha256 FROM canonical_ssot_fact"
        ).fetchone() == ("p0_forecast_snapshot", digest)

    replay = migrate_canonical_ssot_fact_v17(
        path,
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(path),
    )
    assert replay["state"] == "ALREADY_V17_NO_WRITE"
