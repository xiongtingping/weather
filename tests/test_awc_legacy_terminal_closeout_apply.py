import sqlite3
from pathlib import Path

from scripts import awc_legacy_terminal_closeout_apply as apply


def _create_ledger_schema(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE awc_hard_source_signals(
                signal_id TEXT PRIMARY KEY,
                source_event_id TEXT,
                slug TEXT,
                event_date TEXT,
                awc_valid_time TEXT,
                awc_hard_source_signal_at TEXT,
                acceptance_state TEXT,
                terminal_state TEXT,
                state_reason TEXT,
                created_at TEXT
            );
            CREATE TABLE signal_to_order_timing(
                signal_id TEXT PRIMARY KEY,
                decision_id TEXT,
                order_intent_id TEXT,
                awc_hard_source_signal_at TEXT,
                fact_snapshot_written_at TEXT,
                posterior_written_at TEXT,
                auto_trade_decision_written_at TEXT,
                execution_consumer_seen_decision_at TEXT,
                order_job_enqueued_at TEXT,
                order_submitted_at TEXT,
                signal_to_order_slo_violation_reason TEXT,
                timing_json TEXT,
                created_at TEXT
            );
            """
        )


def _contract(db: Path, *, ok=True):
    return {
        "schema": "awc_legacy_terminal_closeout_apply_contract_v1",
        "ok": ok,
        "read_only": True,
        "dry_run": True,
        "db": str(db),
        "operation_count": 1 if ok else 0,
        "operations": [
            {
                "signal_id": "sig_reviewed",
                "terminal_state": "no_action",
                "terminal_reason": "reviewed legacy signal had no executable decision",
                "payload": {
                    "signal_id": "sig_reviewed",
                    "source_event_id": "source_event_1",
                    "slug": "highest-temperature-in-chengdu-on-july-4",
                    "event_date": "2026-07-04",
                    "awc_valid_time": "2026-07-04T09:55:00Z",
                    "awc_hard_source_signal_at": "2026-07-04T10:06:00Z",
                    "acceptance_state": "accepted",
                    "signal_to_order_timing": {
                        "signal_id": "sig_reviewed",
                        "source_event_id": "source_event_1",
                        "slug": "highest-temperature-in-chengdu-on-july-4",
                        "event_date": "2026-07-04",
                        "awc_valid_time": "2026-07-04T09:55:00Z",
                        "awc_hard_source_signal_at": "2026-07-04T10:06:00Z",
                        "terminal_state": "no_action",
                        "terminal_reason": "reviewed legacy signal had no executable decision",
                        "terminal_at": "2026-07-04T10:06:30Z",
                    },
                },
                "write_targets": ["awc_hard_source_signals", "signal_to_order_timing"],
                "uses_writers": ["upsert_awc_hard_source_signal", "upsert_signal_to_order_timing"],
            }
        ]
        if ok
        else [],
        "operator_contract": {
            "confirmation_phrase": "APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
            "requires_backup": True,
            "requires_writer_quiescence": True,
        },
        "blockers": [] if ok else ["decision_manifest_required"],
        "guards": {
            "writes_archive_db": False,
        },
    }


def test_awc_legacy_terminal_closeout_apply_blocks_without_required_preflight(monkeypatch, tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_ledger_schema(db)
    monkeypatch.setattr(apply.apply_contract, "build_contract", lambda *args, **kwargs: _contract(db))

    report = apply.build_apply_report(
        db,
        decision_manifest={"decisions": []},
        backup=tmp_path / "missing.bak",
        writer_quiescent=False,
        confirm="",
    )

    assert report["ok"] is False
    assert report["applied"] is False
    assert report["guards"]["writes_archive_db"] is False
    assert "confirmation_phrase_missing_or_invalid" in report["blockers"]
    assert "backup_file_missing" in report["blockers"]
    assert "writer_quiescence_not_satisfied" in report["blockers"]


def test_awc_legacy_terminal_closeout_apply_writes_both_ledgers_after_contract_preflight(
    monkeypatch, tmp_path: Path
):
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.bak"
    backup.write_bytes(b"backup")
    _create_ledger_schema(db)
    monkeypatch.setattr(apply.apply_contract, "build_contract", lambda *args, **kwargs: _contract(db))

    report = apply.build_apply_report(
        db,
        decision_manifest={"decisions": []},
        backup=backup,
        writer_quiescent=True,
        confirm="APPLY_AWC_LEGACY_TERMINAL_CLOSEOUT",
        maintenance_report={"ok": True, "writer_quiescent": True, "counts": {"writer_quiescence_blocker_count": 0}},
    )

    assert report["ok"] is True
    assert report["applied"] is True
    assert report["guards"]["writes_archive_db"] is True
    assert report["guards"]["writes_awc_hard_source_signals"] is True
    assert report["guards"]["writes_signal_to_order_timing"] is True
    assert report["applied_count"] == 1
    with sqlite3.connect(db) as conn:
        awc = conn.execute(
            "SELECT terminal_state, state_reason FROM awc_hard_source_signals WHERE signal_id = ?",
            ("sig_reviewed",),
        ).fetchone()
        timing = conn.execute(
            "SELECT timing_json FROM signal_to_order_timing WHERE signal_id = ?",
            ("sig_reviewed",),
        ).fetchone()
    assert awc == ("no_action", "reviewed legacy signal had no executable decision")
    assert '"terminal_state":"no_action"' in timing[0]

