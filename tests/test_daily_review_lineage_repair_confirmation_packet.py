import json
import sqlite3
from pathlib import Path

from daily_review_lineage_repair_writer import CONFIRMATION_PHRASE
from scripts import daily_review_lineage_repair_confirmation_packet as packet_mod
from scripts import daily_review_lineage_repair_operator_runbook as runbook_mod
from scripts import daily_review_lineage_repair_risk_audit as risk_audit_mod


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"


def _create_db(
    tmp_path: Path,
    *,
    fact_snapshot_id: str = "",
) -> Path:
    db_path = tmp_path / "archive.db"
    fact_payload = {
        "slug": SLUG,
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "source_observations": [{"source_event_id": "srcevt_packet_1"}],
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.8,
        },
    }
    if fact_snapshot_id:
        fact_payload["fact_snapshot_id"] = fact_snapshot_id
        fact_payload["lineage_state"] = {"fact_snapshot_id": fact_snapshot_id}
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                city TEXT,
                station_id TEXT,
                settlement_actual REAL,
                rounding_rule TEXT,
                market_closed INTEGER,
                event_end_utc TEXT,
                created_at TEXT
            );
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                fact_as_of TEXT,
                fact_json TEXT,
                created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                run_id INTEGER,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                reasons_json TEXT,
                details_json TEXT,
                created_at TEXT
            );
            CREATE TABLE auto_orders (
                id INTEGER PRIMARY KEY,
                order_id TEXT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                price REAL,
                size REAL,
                strategy TEXT,
                created_at TEXT,
                order_status TEXT,
                order_checked_at TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', 'chengdu', 'ZUUU', 30.0,
                   'whole_degrees', 1, '2026-07-04T16:00:00Z', '2026-07-05T00:10:00Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO event_fact_history
                (id, run_date, slug, event_date, fact_as_of, fact_json, created_at)
            VALUES(1, '2026-07-04', ?, '2026-07-04', '2026-07-04T09:05:38Z', ?, '2026-07-04T09:05:39Z')
            """,
            (SLUG, json.dumps(fact_payload)),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
                (id, run_id, run_date, slug, event_date, outcome, side, action,
                 decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at)
            VALUES(1, 7, '2026-07-04', ?, '2026-07-04', '30', 'YES', 'entry',
                   'entry', 1, '[]', '[]', '{}', '2026-07-04T09:05:39Z')
            """,
            (SLUG,),
        )
        conn.execute(
            """
            INSERT INTO auto_orders
                (id, order_id, slug, event_date, outcome, side, action, price,
                 size, strategy, created_at, order_status)
            VALUES(1, 'ord_packet_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (SLUG,),
        )
    return db_path


def _add_settled_event_without_fact(db_path: Path) -> None:
    slug = "highest-temperature-in-chengdu-on-july-5-2026"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO event_runs
                (id, run_date, slug, event_date, city, station_id, settlement_actual,
                 rounding_rule, market_closed, event_end_utc, created_at)
            VALUES(2, '2026-07-05', ?, '2026-07-05', 'chengdu', 'ZUUU', 31.0,
                   'whole_degrees', 1, '2026-07-05T16:00:00Z', '2026-07-06T00:10:00Z')
            """,
            (slug,),
        )


def test_lineage_repair_confirmation_packet_is_no_apply_and_complete(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    packet = packet_mod.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    assert packet["schema"] == "daily_review_lineage_repair_confirmation_packet_v1"
    assert packet["read_only"] is True
    assert packet["dry_run"] is True
    assert packet["guards"]["executes_sql"] is False
    assert packet["guards"]["writes_archive_db"] is False
    assert packet["guards"]["writes_manifest_file"] is False
    assert packet["guards"]["creates_backup"] is False
    assert packet["candidate_count"] == 1
    assert packet["backup_exists"] is False
    assert packet["lineage_decision_manifest_template"]["decisions"][0]["decision"] == "undecided"
    assert packet["proposed_confirm_repair_manifest"]["decisions"][0]["decision"] == "confirm_repair"
    recheck_readiness = packet["proposed_recheck_summary"]["lineage_decision_apply_readiness"]
    assert recheck_readiness["schema"] == "daily_review_lineage_decision_apply_readiness_v1"
    assert recheck_readiness["operator_decision_counts"]["confirm_repair"] == 1
    assert recheck_readiness["lineage_repair"]["repair_item_count"] == 1
    assert recheck_readiness["daily_review_persistence"]["ready_after_explicit_confirmation"] is False
    contract = packet["lineage_repair_apply_contract"]
    assert contract["repair_item_count"] == 1
    assert contract["apply_supported"] is True
    assert "apply_flag" in contract["apply_blockers"]
    assert "confirmation_phrase" in contract["apply_blockers"]
    assert "fresh_archive_db_backup" in contract["apply_blockers"]
    assert "writer_quiescence" in contract["apply_blockers"]
    risk_audit = packet["lineage_repair_risk_audit"]
    assert risk_audit["schema"] == "daily_review_lineage_repair_risk_audit_v1"
    assert risk_audit["ok"] is True
    assert risk_audit["repair_item_count"] == 1
    assert risk_audit["would_change_count"] == 1
    assert risk_audit["lineage_blocker_expected_removed_count"] == 1
    preflight_commands = [item["command"] for item in packet["operator_packet"]["preflight"]]
    assert any(".backup" in command for command in preflight_commands)
    assert any("daily_review_lineage_repair_operator_runbook.py" in command for command in preflight_commands)
    assert any("daily_review_lineage_repair_risk_audit.py" in command for command in preflight_commands)
    apply_steps = [item for item in packet["operator_packet"]["preflight"] if item["step"] == "apply_lineage_repair"]
    assert apply_steps
    assert CONFIRMATION_PHRASE in apply_steps[0]["command"]
    assert "daily_review_lineage_repair_apply.py" in apply_steps[0]["command"]
    assert "automatic_lineage_repair_apply" in packet["blocked_operations"]
    assert Path(packet["backup_path"]).exists() is False


def test_lineage_repair_confirmation_packet_does_not_confirm_missing_fact_snapshot(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    _add_settled_event_without_fact(db_path)

    packet = packet_mod.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    decisions = {
        row["repair_type"]: row["decision"]
        for row in packet["proposed_confirm_repair_manifest"]["decisions"]
    }
    assert decisions["legacy_fact_snapshot_lineage_repair"] == "confirm_repair"
    assert decisions["missing_fact_snapshot_for_settled_event"] == "manual_rebuild_fact_snapshot"
    readiness = packet["proposed_recheck_summary"]["lineage_decision_apply_readiness"]
    assert readiness["operator_decision_counts"]["confirm_repair"] == 1
    assert readiness["operator_decision_counts"]["manual_rebuild_fact_snapshot"] == 1
    assert readiness["operator_decision_counts"]["not_allowed"] == 0
    assert packet["lineage_repair_apply_contract"]["repair_item_count"] == 1


def test_lineage_repair_confirmation_packet_can_exclude_missing_fact_snapshot(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    _add_settled_event_without_fact(db_path)

    packet = packet_mod.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
        missing_fact_decision="exclude_sample",
    )

    decisions = {
        row["repair_type"]: row["decision"]
        for row in packet["proposed_confirm_repair_manifest"]["decisions"]
    }
    assert packet["missing_fact_decision_policy"] == "exclude_sample"
    assert decisions["legacy_fact_snapshot_lineage_repair"] == "confirm_repair"
    assert decisions["missing_fact_snapshot_for_settled_event"] == "exclude_sample"
    recheck = packet["proposed_recheck_summary"]
    readiness = recheck["lineage_decision_apply_readiness"]
    audit = recheck["lineage_decision_manifest_audit"]
    assert recheck["settled_event_count"] == 2
    assert recheck["effective_settled_event_count"] == 1
    assert recheck["excluded_settled_event_count"] == 1
    assert readiness["operator_decision_counts"]["confirm_repair"] == 1
    assert readiness["operator_decision_counts"]["exclude_sample"] == 1
    assert readiness["operator_decision_counts"]["manual_rebuild_fact_snapshot"] == 0
    assert audit["excluded_candidate_count"] == 1
    assert audit["pending_resolution_count"] == 1
    assert packet["lineage_repair_apply_contract"]["repair_item_count"] == 1
    assert packet["lineage_repair_risk_audit"]["repair_item_count"] == 1
    assert packet["guards"]["writes_archive_db"] is False


def test_lineage_repair_confirmation_packet_can_wait_for_missing_fact_snapshot(
    tmp_path: Path,
) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    _add_settled_event_without_fact(db_path)

    packet = packet_mod.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
        missing_fact_decision="wait_for_new_fact",
    )

    decisions = {
        row["repair_type"]: row["decision"]
        for row in packet["proposed_confirm_repair_manifest"]["decisions"]
    }
    readiness = packet["proposed_recheck_summary"]["lineage_decision_apply_readiness"]
    assert packet["missing_fact_decision_policy"] == "wait_for_new_fact"
    assert decisions["legacy_fact_snapshot_lineage_repair"] == "confirm_repair"
    assert decisions["missing_fact_snapshot_for_settled_event"] == "wait_for_new_fact"
    assert readiness["operator_decision_counts"]["confirm_repair"] == 1
    assert readiness["operator_decision_counts"]["wait_for_new_fact"] == 1
    assert readiness["operator_decision_counts"]["manual_rebuild_fact_snapshot"] == 0
    assert "lineage_decision_manifest_pending_resolution" in packet["proposed_recheck_summary"]["apply_blockers"]
    assert packet["lineage_repair_apply_contract"]["repair_item_count"] == 1


def test_lineage_repair_confirmation_packet_noops_without_candidates(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="fact_ready")
    packet = packet_mod.build_confirmation_packet(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    assert packet["candidate_count"] == 0
    assert packet["proposed_confirm_repair_manifest"]["decision_count"] == 0
    assert packet["lineage_repair_apply_contract"]["repair_item_count"] == 0
    assert packet["lineage_repair_risk_audit"]["repair_item_count"] == 0
    assert packet["next_actions"] == ["no_lineage_repair_candidates"]


def test_lineage_repair_confirmation_packet_main_prints_json(tmp_path: Path, capsys) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")

    code = packet_mod.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--backup-dir",
            str(tmp_path / "backups"),
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["schema"] == "daily_review_lineage_repair_confirmation_packet_v1"
    assert out["candidate_count"] == 1
    assert out["guards"]["writes_archive_db"] is False


def test_lineage_repair_confirmation_packet_summary_is_compact_no_apply(tmp_path: Path, capsys) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")

    code = packet_mod.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--summary",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["schema"] == "daily_review_lineage_repair_confirmation_packet_v1_summary"
    assert out["source_schema"] == "daily_review_lineage_repair_confirmation_packet_v1"
    assert out["read_only"] is True
    assert out["dry_run"] is True
    assert out["guards"]["writes_archive_db"] is False
    assert out["guards"]["writes_manifest_file"] is False
    assert out["candidate_count"] == 1
    assert out["missing_fact_decision_policy"] == "manual_rebuild_fact_snapshot"
    assert out["proposed_decision_counts"] == {"confirm_repair": 1}
    assert out["proposed_recheck_summary"]["operator_decision_counts"]["confirm_repair"] == 1
    assert out["proposed_recheck_summary"]["lineage_repair"]["repair_item_count"] == 1
    assert out["lineage_repair_apply_contract_summary"]["repair_item_count"] == 1
    assert "apply_flag" in out["lineage_repair_apply_contract_summary"]["apply_blockers"]
    assert out["lineage_repair_risk_summary"]["ok"] is True
    assert out["lineage_repair_risk_summary"]["would_change_count"] == 1
    assert any(item["step"] == "apply_lineage_repair" for item in out["preflight_steps"])
    assert out["controlled_apply_step"]["step"] == "apply_lineage_repair"
    assert CONFIRMATION_PHRASE in out["controlled_apply_step"]["command"]
    assert "proposed_confirm_repair_manifest" not in out
    assert "lineage_repair_apply_contract" not in out
    assert "lineage_repair_risk_audit" not in out
    assert (tmp_path / "backups").exists() is False


def test_lineage_repair_confirmation_packet_summary_can_show_excluded_missing_fact(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    _add_settled_event_without_fact(db_path)

    code = packet_mod.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--missing-fact-decision",
            "exclude_sample",
            "--summary",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["schema"] == "daily_review_lineage_repair_confirmation_packet_v1_summary"
    assert out["missing_fact_decision_policy"] == "exclude_sample"
    assert out["candidate_count"] == 2
    assert out["proposed_decision_counts"] == {"confirm_repair": 1, "exclude_sample": 1}
    assert out["proposed_recheck_summary"]["excluded_settled_event_count"] == 1
    assert out["proposed_recheck_summary"]["operator_decision_counts"]["exclude_sample"] == 1
    assert out["lineage_repair_apply_contract_summary"]["repair_item_count"] == 1
    assert out["lineage_repair_risk_summary"]["ok"] is True
    assert out["guards"]["writes_archive_db"] is False


def test_lineage_repair_operator_runbook_is_compact_no_apply_packet(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    runbook = runbook_mod.build_operator_runbook(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    assert runbook["schema"] == "daily_review_lineage_repair_operator_runbook_v1"
    assert runbook["read_only"] is True
    assert runbook["dry_run"] is True
    assert runbook["guards"]["writes_archive_db"] is False
    assert runbook["guards"]["writes_manifest_file"] is False
    assert runbook["guards"]["creates_backup"] is False
    assert runbook["candidate_count"] == 1
    assert runbook["missing_fact_decision_policy"] == "manual_rebuild_fact_snapshot"
    assert runbook["ready_for_controlled_apply_after_runbook"] is True
    assert runbook["risk_summary"]["risk_level"] == "low"
    assert runbook["risk_summary"]["blocked_candidate_count"] == 0
    assert runbook["risk_summary"]["lineage_blocker_expected_removed_count"] == 1
    assert runbook["contract_summary"]["targeted_legacy_fact_lineage_repair_only"] is True
    assert runbook["apply_readiness_summary"]["schema"] == "daily_review_lineage_decision_apply_readiness_v1"
    assert runbook["apply_readiness_summary"]["operator_decision_counts"]["confirm_repair"] == 1
    assert runbook["apply_readiness_summary"]["lineage_repair"]["repair_item_count"] == 1
    assert runbook["apply_readiness_summary"]["next_action"] == "run_controlled_lineage_repair_apply_then_rerun_batch_plan"
    assert runbook["candidate_review_table"][0]["would_change"] is True
    assert runbook["proposed_manifest_json"]["decisions"][0]["decision"] == "confirm_repair"
    steps = {item["step"]: item for item in runbook["checklist"]}
    assert steps["materialize_reviewed_manifest"]["writes_manifest_file"] is True
    assert steps["materialize_reviewed_manifest"]["blocked_by_runbook"] is False
    assert str(db_path) in steps["materialize_reviewed_manifest"]["command"]
    assert "--since-date 2026-07-01" in steps["materialize_reviewed_manifest"]["command"]
    assert "daily_review_lineage_repair_apply.py" in steps["apply_lineage_repair"]["command"]
    assert CONFIRMATION_PHRASE in steps["apply_lineage_repair"]["command"]
    assert steps["apply_lineage_repair"]["required"] is True
    assert Path(runbook["manifest_output_path"]).exists() is False
    assert Path(runbook["backup_path"]).exists() is False


def test_lineage_repair_operator_runbook_emit_manifest_honors_missing_fact_policy(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")
    _add_settled_event_without_fact(db_path)

    code = runbook_mod.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--missing-fact-decision",
            "exclude_sample",
            "--emit-proposed-manifest-json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    decisions = {
        row["repair_type"]: row["decision"]
        for row in out["decisions"]
    }

    assert code == 0
    assert out["schema"] == "daily_review_lineage_repair_decision_manifest_v1"
    assert decisions["legacy_fact_snapshot_lineage_repair"] == "confirm_repair"
    assert decisions["missing_fact_snapshot_for_settled_event"] == "exclude_sample"
    assert (tmp_path / "backups").exists() is False

    runbook = runbook_mod.build_operator_runbook(
        db_path,
        since_date="2026-07-01",
        backup_dir=tmp_path / "backups",
        missing_fact_decision="exclude_sample",
        generated_at="2026-07-06T00:00:00Z",
    )
    steps = {item["step"]: item for item in runbook["checklist"]}
    runbook_decisions = {
        row["repair_type"]: row["decision"]
        for row in runbook["proposed_manifest_json"]["decisions"]
    }
    assert runbook["missing_fact_decision_policy"] == "exclude_sample"
    assert "--missing-fact-decision exclude_sample" in steps["materialize_reviewed_manifest"]["command"]
    assert runbook_decisions["missing_fact_snapshot_for_settled_event"] == "exclude_sample"


def test_lineage_repair_operator_runbook_blocks_apply_when_risk_audit_blocks() -> None:
    packet = {
        "ok": True,
        "generated_at": "2026-07-06T00:00:00Z",
        "db": "/tmp/archive.db",
        "candidate_count": 1,
        "manifest_output_path": "/tmp/manifest.json",
        "backup_path": "/tmp/archive.db.bak",
        "backup_exists": False,
        "proposed_confirm_repair_manifest": {
            "decisions": [
                {
                    "repair_correlation_id": "corr_blocked",
                    "decision": "confirm_repair",
                    "slug": "highest-temperature-in-chengdu-on-may-31-2026",
                    "event_date": "2026-05-30",
                    "event_run_id": 319,
                    "target_table": "event_fact_history",
                    "target_row_id": "",
                    "candidate_fact_snapshot_id": "",
                }
            ]
        },
        "lineage_repair_risk_audit": {
            "ok": False,
            "risk_level": "high",
            "blockers": ["target_row_id_invalid"],
            "repair_item_count": 1,
            "would_change_count": 0,
            "lineage_blocker_expected_removed_count": 0,
            "post_apply_prediction_summary": {
                "lineage_blocker_expected_remaining_count": 1,
            },
            "items": [
                {
                    "repair_correlation_id": "corr_blocked",
                    "blockers": ["target_row_id_invalid"],
                    "would_change": False,
                    "expected_diff": [],
                    "post_apply_prediction": {
                        "lineage_blocker_expected_removed": False,
                    },
                }
            ],
        },
        "lineage_repair_apply_contract": {
            "repair_item_count": 1,
            "apply_blockers": ["repair_item_validation"],
            "confirmation_phrase_required": CONFIRMATION_PHRASE,
            "boundary": {"targeted_legacy_fact_lineage_repair_only": True},
        },
        "operator_packet": {
            "preflight": [
                {"step": "review_current_batch_plan", "command": "batch"},
                {"step": "create_fresh_backup", "command": "backup"},
                {"step": "dry_run_lineage_repair_apply_entrypoint", "command": "dry-run"},
                {"step": "review_lineage_repair_risk_audit", "command": "risk"},
                {
                    "step": "apply_lineage_repair",
                    "command": "apply",
                    "confirmation_phrase": CONFIRMATION_PHRASE,
                },
            ],
            "verification": [
                {"step": "recheck_lineage_scope", "command": "recheck"},
                {"step": "daily_review_coverage_audit", "command": "coverage"},
                {"step": "ssot_alignment", "command": "ssot"},
            ],
            "rollback": [
                {"step": "restore_backup_if_verification_fails", "commands": ["rollback"]},
            ],
        },
    }

    runbook = runbook_mod.build_operator_runbook_from_packet(packet)
    steps = {item["step"]: item for item in runbook["checklist"]}

    assert runbook["ok"] is False
    assert runbook["ready_for_controlled_apply_after_runbook"] is False
    assert runbook["next_boundary"] == "resolve_runbook_blockers_before_manifest_materialization_or_apply"
    assert runbook["risk_summary"]["blocked_candidate_count"] == 1
    assert runbook["risk_summary"]["blocker_counts"] == {"target_row_id_invalid": 1}
    assert runbook["blocked_candidate_table"][0]["repair_correlation_id"] == "corr_blocked"
    assert steps["materialize_reviewed_manifest"]["blocked_by_runbook"] is True
    assert steps["apply_lineage_repair"]["required"] is False
    assert steps["apply_lineage_repair"]["blocked_by_runbook"] is True


def test_lineage_repair_operator_runbook_emit_manifest_prints_only_manifest_json(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")

    code = runbook_mod.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--backup-dir",
            str(tmp_path / "backups"),
            "--emit-proposed-manifest-json",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["schema"] == "daily_review_lineage_repair_decision_manifest_v1"
    assert out["decisions"][0]["decision"] == "confirm_repair"
    assert (tmp_path / "backups").exists() is False


def test_lineage_repair_risk_audit_main_prints_no_apply_json(tmp_path: Path, capsys) -> None:
    db_path = _create_db(tmp_path, fact_snapshot_id="")

    code = risk_audit_mod.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
        ]
    )
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["schema"] == "daily_review_lineage_repair_risk_audit_report_v1"
    assert out["manifest_source"] == "proposed_confirm_repair_for_current_candidates"
    assert out["risk_audit"]["repair_item_count"] == 1
    assert out["risk_audit"]["lineage_blocker_expected_removed_count"] == 1
    assert out["guards"]["writes_archive_db"] is False
