import sqlite3
from pathlib import Path

from scripts import phase0_schema_probe
from scripts import production_identity_coverage_audit as audit


def _column_type(column: str) -> str:
    if column == "id":
        return "INTEGER"
    if column.endswith("_json"):
        return "TEXT"
    if column.endswith("_c") or column.endswith("_ms"):
        return "REAL"
    return "TEXT"


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _create_identity_fixture(db: Path) -> None:
    conn = sqlite3.connect(db)
    table_columns = {
        **phase0_schema_probe.REQUIRED_TABLE_COLUMNS,
        **phase0_schema_probe.PLANNED_TABLE_COLUMNS,
    }
    for table, required_columns in table_columns.items():
        contract = phase0_schema_probe.TABLE_ID_CONSTRAINTS.get(table, {})
        audit_contract = audit.IDENTITY_ROW_CONTRACT.get(table, {})
        columns = list(
            dict.fromkeys(
                [
                    *required_columns,
                    *audit_contract.get("primary_key_fields", ()),
                    *audit_contract.get("layer_identity_fields", ()),
                    *audit_contract.get("correlation_fields", ()),
                ]
            )
        )
        primary_key = list(contract.get("primary_key") or ())
        defs = []
        for column in columns:
            suffix = " PRIMARY KEY" if primary_key == [column] else ""
            defs.append(f"{_quote(column)} {_column_type(column)}{suffix}")
        if len(primary_key) > 1:
            defs.append("PRIMARY KEY (" + ", ".join(_quote(column) for column in primary_key) + ")")
        conn.execute(f"CREATE TABLE {table} ({', '.join(defs)})")
        for unique_key in contract.get("unique_keys") or ():
            if tuple(unique_key) == tuple(primary_key):
                continue
            index = "idx_" + table + "_" + "_".join(unique_key)
            conn.execute(
                f"CREATE UNIQUE INDEX {index} ON {table} "
                f"({', '.join(_quote(column) for column in unique_key)})"
            )

        if table in audit.IDENTITY_ROW_CONTRACT:
            row = {}
            for column in columns:
                row[column] = 1 if column == "id" else f"{column}_{table}"
            if table == "event_fact_history":
                row["fact_json"] = "{}"
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(_quote(column) for column in row)}) VALUES ({placeholders})",
                list(row.values()),
            )
    conn.commit()
    conn.close()


def test_production_identity_coverage_passes_when_rows_have_required_identity(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_identity_fixture(db)

    report = audit.audit(db, sample_limit=3)

    assert report["ok"] is True
    assert report["schema_identity_ready"] is True
    assert report["production_row_identity_ready"] is True
    assert report["gap_table_count"] == 0


def test_production_identity_coverage_flags_empty_required_field(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_identity_fixture(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE event_fact_history SET correlation_id = ''")
    conn.commit()
    conn.close()

    report = audit.audit(db, sample_limit=3)

    assert report["ok"] is False
    table = report["tables"]["event_fact_history"]
    assert table["production_row_identity_ready"] is False
    assert table["empty_required_field_counts"]["correlation_id"] == 1
    blockers = {
        (item["blocker"], item.get("table"), item.get("field"))
        for item in report["blockers"]
    }
    assert (
        "production_identity_required_field_empty",
        "event_fact_history",
        "correlation_id",
    ) in blockers


def test_row_count_evidence_uses_partial_nonempty_index_and_blocks_large_unindexed_scan(
    tmp_path: Path,
):
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
          id INTEGER PRIMARY KEY,
          decision_id TEXT,
          correlation_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_phase0_auto_trade_decisions_decision_id
        ON auto_trade_decisions(decision_id)
        WHERE decision_id IS NOT NULL AND decision_id <> ''
        """
    )
    for idx in range(5):
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(id, decision_id, correlation_id)
            VALUES (?, ?, ?)
            """,
            (idx + 1, f"decision_{idx}", f"corr_{idx}"),
        )
    conn.commit()
    conn.row_factory = sqlite3.Row

    result, evidence = audit._row_and_empty_counts_with_evidence(
        conn,
        table="auto_trade_decisions",
        fields=["id", "decision_id", "correlation_id"],
        slow_scan_row_threshold=1,
    )

    assert result["row_count"] == 5
    assert result["empty_counts"]["id"] == 0
    assert result["empty_counts"]["decision_id"] == 0
    assert evidence["decision_id"]["method"] == "partial_nonempty_index_count"
    assert evidence["correlation_id"]["method"] == "unverified_large_table_requires_supporting_index"
    assert "correlation_id" not in result["empty_counts"]


def test_large_table_missing_row_samples_are_skipped_without_slow_scan(tmp_path: Path):
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
          id INTEGER PRIMARY KEY,
          decision_id TEXT,
          correlation_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX idx_phase0_auto_trade_decisions_decision_id
        ON auto_trade_decisions(decision_id)
        WHERE decision_id IS NOT NULL AND decision_id <> ''
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_phase0_auto_trade_decisions_correlation_id_nonempty_scan
        ON auto_trade_decisions(correlation_id)
        WHERE correlation_id IS NOT NULL AND correlation_id <> ''
        """
    )
    for idx in range(3):
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(id, decision_id, correlation_id)
            VALUES (?, ?, ?)
            """,
            (idx + 1, "" if idx == 0 else f"decision_{idx}", "" if idx == 0 else f"corr_{idx}"),
        )
    conn.commit()
    conn.close()

    report = audit.audit(db, sample_limit=3, slow_scan_row_threshold=1)

    table = report["tables"]["auto_trade_decisions"]
    assert table["empty_required_field_counts"]["decision_id"] == 1
    assert table["empty_required_field_counts"]["correlation_id"] == 1
    assert table["sample_missing_rows"] == []
    assert table["sample_missing_rows_skipped_reason"] == "large_table_missing_row_samples_require_allow_slow_scan"
