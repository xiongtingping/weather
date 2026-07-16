import json
import sqlite3
from pathlib import Path

from scripts import phase0_schema_probe
from scripts import production_identity_merge_fill_plan as plan
from scripts import production_identity_coverage_audit


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _column_type(column: str) -> str:
    if column == "id":
        return "INTEGER"
    if column.endswith("_json"):
        return "TEXT"
    return "TEXT"


def _create_fixture(db: Path) -> None:
    conn = sqlite3.connect(db)
    table_columns = {
        **phase0_schema_probe.REQUIRED_TABLE_COLUMNS,
        **phase0_schema_probe.PLANNED_TABLE_COLUMNS,
    }
    for table, required_columns in table_columns.items():
        contract = phase0_schema_probe.TABLE_ID_CONSTRAINTS.get(table, {})
        audit_contract = production_identity_coverage_audit.IDENTITY_ROW_CONTRACT.get(table, {})
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
            where = " AND ".join(
                f"{_quote(column)} IS NOT NULL AND {_quote(column)} <> ''"
                for column in unique_key
            )
            conn.execute(
                f"CREATE UNIQUE INDEX {index} ON {table} "
                f"({', '.join(_quote(column) for column in unique_key)}) WHERE {where}"
            )
        if table not in production_identity_coverage_audit.IDENTITY_ROW_CONTRACT:
            continue
        row = {column: (1 if column == "id" else f"{column}_{table}") for column in columns}
        if table == "event_fact_history":
            row["fact_json"] = json.dumps(
                {
                    "fact_snapshot_id": row["fact_snapshot_id"],
                    "posterior_run_id": "post_existing",
                    "source_event_ids": ["srcevt_existing"],
                }
            )
        conn.execute(
            f"INSERT INTO {table} ({', '.join(_quote(column) for column in row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            list(row.values()),
        )
    conn.commit()
    conn.close()


def test_merge_fill_plan_blocks_duplicate_fact_snapshot_id(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    duplicate_payload = json.dumps(
        {
            "fact_snapshot_id": "fact_duplicate",
            "posterior_run_id": "post_duplicate",
            "source_event_ids": ["srcevt_duplicate"],
        }
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', posterior_run_id='', source_event_id='', fact_json=? WHERE id=1",
        (duplicate_payload,),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history (
            id, run_date, slug, event_date, fact_as_of, fact_json, created_at,
            correlation_id, source_event_id, fact_snapshot_id, posterior_run_id, signal_id, review_id
        )
        VALUES (2, '2026-07-06', 'slug2', '2026-07-06', '2026-07-06T01:00:00Z', ?, 'now',
            '', '', '', '', '', '')
        """,
        (duplicate_payload,),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, limit=100, max_items_output=10)

    assert report["ok"] is False
    assert "unique_key_duplicate_proposal:fact_snapshot_id" in report["blockers"]
    assert "duplicate_fact_snapshot:duplicate_fact_snapshot_decision_manifest_missing" in report["blockers"]
    assert report["duplicate_fact_snapshot_precondition"]["duplicate_group_count"] == 1
    assert report["duplicate_fact_snapshot_precondition"]["decision_manifest_audit"]["blocking_for_current_apply"] is True
    assert report["table_counts"]["event_fact_history"]["blocked_count"] == 2


def test_merge_fill_plan_marks_candidate_scope_truncated(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.execute(
        """
        INSERT INTO auto_trade_decisions (
            id, run_date, slug, event_date, outcome, side, action, will_order,
            details_json, created_at, correlation_id, fact_snapshot_id, posterior_run_id,
            decision_id, signal_id, order_intent_id
        )
        VALUES (2, '2026-07-06', 'slug', '2026-07-06', '37°C', 'YES', 'entry', 0,
            '{}', 'later', '', '', '', '', '', '')
        """
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, limit=1, max_items_output=10)

    assert report["ok"] is False
    assert report["candidate_scope_truncated"] is True
    assert "candidate_scope_truncated" in report["blockers"]
    assert report["table_counts"]["auto_trade_decisions"]["required_candidate_count"] == 2
    assert report["table_counts"]["auto_trade_decisions"]["candidate_count"] == 1


def test_merge_fill_plan_fills_decision_signal_identity_from_details_json(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    details = json.dumps(
        {
            "signal_id": "sig_awc_payload",
            "order_intent_id": "intent_awc_payload",
            "fact_snapshot_id": "fact_awc_payload",
            "posterior_run_id": "post_awc_payload",
        }
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions (
            id, run_date, slug, event_date, outcome, side, action, will_order,
            details_json, created_at, correlation_id, fact_snapshot_id, posterior_run_id,
            decision_id, signal_id, order_intent_id
        )
        VALUES (2, '2026-07-06', 'slug', '2026-07-06', '37°C', 'YES', 'entry', 1,
            ?, 'later', 'corr_existing', '', '', 'dec_existing', '', '')
        """,
        (details,),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, limit=100, max_items_output=20)

    decision = next(
        item
        for item in report["candidate_operations"]
        if item["table"] == "auto_trade_decisions" and item["primary_key"] == {"id": 2}
    )
    assert decision["set_fields"]["signal_id"] == "sig_awc_payload"
    assert decision["set_fields"]["order_intent_id"] == "intent_awc_payload"
    assert decision["set_fields"]["fact_snapshot_id"] == "fact_awc_payload"
    assert decision["set_fields"]["posterior_run_id"] == "post_awc_payload"


def test_merge_fill_plan_fills_signal_timing_decision_id_from_decision_payload(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    details = json.dumps(
        {
            "signal_id": "sig_awc_payload",
            "order_intent_id": "intent_awc_payload",
        }
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions (
            id, run_date, slug, event_date, outcome, side, action, will_order,
            details_json, created_at, correlation_id, fact_snapshot_id, posterior_run_id,
            decision_id, signal_id, order_intent_id
        )
        VALUES (2, '2026-07-06', 'slug', '2026-07-06', '37°C', 'YES', 'entry', 1,
            ?, 'later', 'corr_existing', '', '', 'dec_payload', '', '')
        """,
        (details,),
    )
    conn.execute(
        """
        INSERT INTO signal_to_order_timing (
            signal_id, decision_id, order_intent_id, awc_hard_source_signal_at,
            fact_snapshot_written_at, posterior_written_at, auto_trade_decision_written_at,
            execution_consumer_seen_decision_at, order_job_enqueued_at, order_submitted_at,
            signal_to_order_slo_violation_reason, timing_json, created_at
        )
        VALUES ('sig_awc_payload', '', '', '2026-07-06T10:06:00Z',
            '', '', '', '', '', '', '', '{}', '2026-07-06T10:06:10Z')
        """
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, limit=100, max_items_output=20)

    timing = next(
        item
        for item in report["candidate_operations"]
        if item["table"] == "signal_to_order_timing"
        and item["primary_key"] == {"signal_id": "sig_awc_payload"}
    )
    assert timing["set_fields"]["decision_id"] == "dec_payload"
    assert timing["set_fields"]["order_intent_id"] == "intent_awc_payload"


def test_merge_fill_plan_synthesizes_legacy_fact_snapshot_id_from_history_key(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    legacy_payload = json.dumps(
        {
            "slug": "legacy-slug",
            "event_date": "2026-07-06",
            "source_event_ids": ["srcevt_legacy"],
        }
    )
    conn.execute(
        """
        UPDATE event_fact_history
        SET slug='legacy-slug', event_date='2026-07-06',
            fact_as_of='2026-07-06T08:00:00Z', fact_json=?,
            fact_snapshot_id='', correlation_id=''
        WHERE id=1
        """,
        (legacy_payload,),
    )
    conn.execute(
        """
        UPDATE event_fact_latest
        SET slug='legacy-slug', event_date='2026-07-06',
            fact_id=1, fact_snapshot_id='', correlation_id=''
        """
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, limit=100, max_items_output=10)

    assert report["blocked_candidate_count"] == 0
    history_candidate = next(
        item for item in report["candidate_operations"] if item["table"] == "event_fact_history"
    )
    latest_candidate = next(
        item for item in report["candidate_operations"] if item["table"] == "event_fact_latest"
    )
    generated = history_candidate["set_fields"]["fact_snapshot_id"]
    assert generated.startswith("fact_")
    assert latest_candidate["set_fields"]["fact_snapshot_id"] == generated


def test_merge_fill_plan_uses_row_id_tiebreaker_for_legacy_history_without_sources(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    legacy_payload = json.dumps({"slug": "legacy-slug", "event_date": "2026-07-06"})
    conn = sqlite3.connect(db)
    conn.execute(
        """
        UPDATE event_fact_history
        SET slug='legacy-slug', event_date='2026-07-06',
            fact_as_of='2026-07-06T08:00:00Z', fact_json=?,
            fact_snapshot_id='', correlation_id=''
        WHERE id=1
        """,
        (legacy_payload,),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history (
            id, run_date, slug, event_date, fact_as_of, fact_json, created_at,
            correlation_id, source_event_id, fact_snapshot_id, posterior_run_id, signal_id, review_id
        )
        VALUES (2, '2026-07-06', 'legacy-slug', '2026-07-06',
            '2026-07-06T08:00:00Z', ?, 'now', '', '', '', '', '', '')
        """,
        (legacy_payload,),
    )
    conn.commit()
    conn.close()

    report = plan.build_plan(db, limit=100, max_items_output=10)

    history_ids = [
        item["set_fields"]["fact_snapshot_id"]
        for item in report["candidate_operations"]
        if item["table"] == "event_fact_history"
    ]
    assert len(history_ids) == 2
    assert len(set(history_ids)) == 2
    assert report["blocked_candidate_count"] == 0


def test_merge_fill_plan_scopes_candidate_builders_from_coverage_report(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    payload = json.dumps({"fact_snapshot_id": "fact_scoped", "posterior_run_id": "post_scoped"})
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', fact_json=? WHERE id=1",
        (payload,),
    )
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.commit()
    conn.close()
    coverage_report = {
        "schema": "production_identity_coverage_audit_v1",
        "ok": False,
        "gap_tables": ["event_fact_history"],
        "tables": {
            "event_fact_history": {
                "required_nonempty_fields": ["id", "fact_snapshot_id", "correlation_id"],
                "empty_required_field_counts": {
                    "id": 0,
                    "fact_snapshot_id": 1,
                    "correlation_id": 1,
                },
            },
            "auto_trade_decisions": {
                "required_nonempty_fields": ["id", "decision_id", "correlation_id"],
                "empty_required_field_counts": {
                    "id": 0,
                    "decision_id": 0,
                    "correlation_id": 0,
                },
            },
        },
    }

    report = plan.build_plan(
        db,
        limit=100,
        max_items_output=10,
        coverage_report=coverage_report,
    )

    assert report["candidate_count"] == 1
    assert report["candidate_operations"][0]["table"] == "event_fact_history"


def test_merge_fill_plan_scopes_candidate_builders_from_computed_coverage(tmp_path: Path, monkeypatch):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    payload = json.dumps({"fact_snapshot_id": "fact_scoped", "posterior_run_id": "post_scoped"})
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', fact_json=? WHERE id=1",
        (payload,),
    )
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.commit()
    conn.close()

    coverage_report = {
        "schema": "production_identity_coverage_audit_v1",
        "ok": False,
        "gap_tables": ["event_fact_history"],
        "tables": {
            "event_fact_history": {
                "required_nonempty_fields": ["id", "fact_snapshot_id", "correlation_id"],
                "empty_required_field_counts": {
                    "id": 0,
                    "fact_snapshot_id": 1,
                    "correlation_id": 1,
                },
            },
            "auto_trade_decisions": {
                "required_nonempty_fields": ["id", "decision_id", "correlation_id"],
                "empty_required_field_counts": {
                    "id": 0,
                    "decision_id": 0,
                    "correlation_id": 0,
                },
            },
        },
    }
    monkeypatch.setattr(plan.production_identity_coverage_audit, "audit", lambda *_args, **_kwargs: coverage_report)

    report = plan.build_plan(db, limit=100, max_items_output=10)

    assert report["candidate_count"] == 1
    assert report["candidate_operations"][0]["table"] == "event_fact_history"


def test_merge_fill_plan_skips_duplicate_precondition_for_decision_only_gap(tmp_path: Path, monkeypatch):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.commit()
    conn.close()

    coverage_report = {
        "schema": "production_identity_coverage_audit_v1",
        "ok": False,
        "gap_tables": ["auto_trade_decisions"],
        "tables": {
            "auto_trade_decisions": {
                "required_nonempty_fields": ["id", "decision_id", "correlation_id"],
                "empty_required_field_counts": {
                    "id": 0,
                    "decision_id": 1,
                    "correlation_id": 1,
                },
            },
        },
    }
    monkeypatch.setattr(plan.production_identity_coverage_audit, "audit", lambda *_args, **_kwargs: coverage_report)

    def fail_duplicate_packet(*_args, **_kwargs):
        raise AssertionError("duplicate packet should not run for decision-only identity gaps")

    monkeypatch.setattr(plan.production_identity_duplicate_fact_snapshot_packet, "build_packet", fail_duplicate_packet)
    monkeypatch.setattr(
        plan,
        "_json_loads_map",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("decision-only required fields should not parse details_json")
        ),
    )

    report = plan.build_plan(db, limit=100, max_items_output=10)

    assert report["candidate_count"] == 1
    assert report["candidate_operations"][0]["table"] == "auto_trade_decisions"
    assert report["duplicate_fact_snapshot_precondition"]["not_applicable"] is True
    assert report["operator_contract"]["requires_duplicate_fact_snapshot_manifest_audit"] is False
    assert report["operator_contract"]["duplicate_operator_runbook_command"] == ""
    assert "reviewed_duplicate_manifest.json" not in " ".join(
        report["operator_contract"]["post_apply_verification_commands"]
    )


def test_missing_partial_nonempty_ids_uses_tail_gap_without_full_index_diff(tmp_path: Path):
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
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
    conn.executemany(
        """
        INSERT INTO auto_trade_decisions(id, decision_id, correlation_id)
        VALUES (?, ?, ?)
        """,
        [
            (1, "decision_1", "corr_1"),
            (2, "decision_2", "corr_2"),
            (3, "", ""),
            (4, "", ""),
        ],
    )

    ids = plan._ids_missing_partial_nonempty_fields(
        conn,
        table="auto_trade_decisions",
        id_field="id",
        fields=("decision_id",),
        limit=10,
    )

    conn.close()
    assert ids == [4, 3]
