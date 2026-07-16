import sqlite3

from weak_forecast_revision_schema_contract import (
    audit_weak_forecast_revision_schema,
    ensure_weak_forecast_revision_schema,
)


def test_additive_weak_revision_schema_is_exact_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_weak_forecast_revision_schema(conn)
    ensure_weak_forecast_revision_schema(conn)
    audit = audit_weak_forecast_revision_schema(conn)
    assert audit["ok"] is True
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "weak_forecast_evidence_snapshots" in tables
    assert "weak_forecast_revision_updates" in tables
