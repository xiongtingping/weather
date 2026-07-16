import json
import sqlite3
from pathlib import Path

from daily_review_lineage_repair_writer import (
    CONFIRMATION_PHRASE,
    apply_lineage_repair_contract,
    preview_lineage_repair_contract,
)
from scripts import daily_review_lineage_repair_apply
from scripts import daily_review_persistence_batch_plan as batch_plan


SLUG = "highest-temperature-in-chengdu-on-july-4-2026"


def _create_db(tmp_path: Path, *, payload: dict | None = None) -> Path:
    db_path = tmp_path / "archive.db"
    fact_payload = payload or {
        "slug": SLUG,
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "source_observations": [{"source_event_id": "srcevt_repair_1"}],
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.8},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.2},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.8,
        },
    }
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
            VALUES(1, 'ord_repair_1', ?, '2026-07-04', '30', 'YES', 'entry',
                   0.5, 1.0, 'hard_source_direct', '2026-07-04T09:05:40Z', 'submitted')
            """,
            (SLUG,),
        )
    return db_path


def _repair_contract(db_path: Path) -> tuple[dict, dict]:
    plan = batch_plan.build_batch_plan(db_path, since_date="2026-07-01", max_items_output=5)
    repair_id = plan["lineage_repair_candidates"][0]["repair_correlation_id"]
    manifest = batch_plan.build_lineage_repair_decision_manifest(
        plan["lineage_repair_candidates"],
        decisions={repair_id: {"decision": "confirm_repair", "reason": "unit test"}},
    )
    rechecked = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )
    return batch_plan.build_lineage_repair_apply_contract(rechecked, lineage_decision_manifest=manifest), manifest


def _fact_payload(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = 1").fetchone()[0]
    return json.loads(raw)


def test_lineage_repair_apply_defaults_to_no_write(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    contract, _manifest = _repair_contract(db_path)
    before = _fact_payload(db_path)

    with sqlite3.connect(db_path) as conn:
        result = apply_lineage_repair_contract(conn, contract)

    after = _fact_payload(db_path)
    assert result["ok"] is False
    assert result["applied"] is False
    assert "apply_flag" in result["blockers"]
    assert result["guards"]["writes_archive_db"] is False
    assert after == before


def test_lineage_repair_risk_audit_previews_merge_fill_without_write(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    contract, _manifest = _repair_contract(db_path)
    before = _fact_payload(db_path)

    with sqlite3.connect(db_path) as conn:
        audit = preview_lineage_repair_contract(conn, contract)

    after = _fact_payload(db_path)
    candidate_fact_snapshot_id = contract["repair_items"][0]["candidate_fact_snapshot_id"]
    item = audit["items"][0]

    assert audit["schema"] == "daily_review_lineage_repair_risk_audit_v1"
    assert audit["ok"] is True
    assert audit["read_only"] is True
    assert audit["guards"]["writes_archive_db"] is False
    assert audit["repair_item_count"] == 1
    assert audit["would_change_count"] == 1
    assert audit["lineage_blocker_expected_removed_count"] == 1
    assert item["current_values"]["fact_snapshot_id_missing"] is True
    assert item["current_values"]["lineage_state_fact_snapshot_id_missing"] is True
    assert item["expected_diff"][0]["json_path"] == "fact_snapshot_id"
    assert item["expected_diff"][0]["current_value"] is None
    assert item["expected_diff"][0]["new_value"] == candidate_fact_snapshot_id
    assert item["expected_diff"][0]["overwrite_allowed"] is False
    assert item["expected_diff"][0]["would_write"] is True
    assert item["post_apply_prediction"]["lineage_blocker_expected_removed"] is True
    assert after == before


def test_lineage_repair_apply_merge_fills_only_target_legacy_fact(tmp_path: Path) -> None:
    db_path = _create_db(tmp_path)
    contract, manifest = _repair_contract(db_path)
    backup = tmp_path / "archive.db.bak"
    backup.write_text("backup", encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        result = apply_lineage_repair_contract(
            conn,
            contract,
            allow_apply=True,
            confirmation_phrase=CONFIRMATION_PHRASE,
            backup_path=backup,
            writer_quiescent=True,
        )

    repaired = _fact_payload(db_path)
    candidate_fact_snapshot_id = contract["repair_items"][0]["candidate_fact_snapshot_id"]
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["applied_count"] == 1
    assert result["guards"]["writes_event_fact_history"] is True
    assert repaired["fact_snapshot_id"] == candidate_fact_snapshot_id
    assert repaired["lineage_state"]["fact_snapshot_id"] == candidate_fact_snapshot_id
    assert repaired["posterior_target_bucket_selection"]["target_bucket"] == "30"

    rechecked = batch_plan.build_batch_plan(
        db_path,
        since_date="2026-07-01",
        max_items_output=5,
        lineage_decision_manifest=manifest,
    )
    assert rechecked["lineage_repair_candidate_count"] == 0
    assert rechecked["lineage_apply_blocker_counts"] == {}
    assert "lineage_decision_manifest_incomplete_or_invalid" not in rechecked["apply_blockers"]


def test_lineage_repair_apply_blocks_conflicting_existing_fact_snapshot_id(tmp_path: Path) -> None:
    db_path = _create_db(
        tmp_path,
        payload={
            "slug": SLUG,
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_existing",
            "lineage_state": {"fact_snapshot_id": "fact_existing"},
        },
    )
    contract = {
        "schema": "daily_review_lineage_repair_apply_contract_v1",
        "db": str(db_path),
        "repair_items": [
            {
                "repair_correlation_id": "corr_conflict",
                "target_table": "event_fact_history",
                "target_row_id": "1",
                "slug": SLUG,
                "event_date": "2026-07-04",
                "candidate_fact_snapshot_id": "fact_candidate",
                "expected_merge_fill": {
                    "paths": [
                        {"json_path": "fact_snapshot_id", "value": "fact_candidate"},
                        {"json_path": "lineage_state.fact_snapshot_id", "value": "fact_candidate"},
                    ]
                },
            }
        ],
    }
    backup = tmp_path / "archive.db.bak"
    backup.write_text("backup", encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        result = apply_lineage_repair_contract(
            conn,
            contract,
            allow_apply=True,
            confirmation_phrase=CONFIRMATION_PHRASE,
            backup_path=backup,
            writer_quiescent=True,
        )

    payload = _fact_payload(db_path)
    assert result["ok"] is False
    assert result["applied"] is False
    assert "fact_snapshot_id_conflict" in result["blockers"]
    assert payload["fact_snapshot_id"] == "fact_existing"


def test_lineage_repair_risk_audit_blocks_conflicting_existing_fact_snapshot_id(tmp_path: Path) -> None:
    db_path = _create_db(
        tmp_path,
        payload={
            "slug": SLUG,
            "event_date": "2026-07-04",
            "fact_snapshot_id": "fact_existing",
            "lineage_state": {"fact_snapshot_id": "fact_existing"},
        },
    )
    contract = {
        "schema": "daily_review_lineage_repair_apply_contract_v1",
        "db": str(db_path),
        "repair_items": [
            {
                "repair_correlation_id": "corr_conflict",
                "target_table": "event_fact_history",
                "target_row_id": "1",
                "slug": SLUG,
                "event_date": "2026-07-04",
                "candidate_fact_snapshot_id": "fact_candidate",
                "expected_merge_fill": {
                    "paths": [
                        {"json_path": "fact_snapshot_id", "value": "fact_candidate"},
                        {"json_path": "lineage_state.fact_snapshot_id", "value": "fact_candidate"},
                    ]
                },
            }
        ],
    }
    before = _fact_payload(db_path)

    with sqlite3.connect(db_path) as conn:
        audit = preview_lineage_repair_contract(conn, contract)

    after = _fact_payload(db_path)
    assert audit["ok"] is False
    assert audit["risk_level"] == "high"
    assert "fact_snapshot_id_conflict" in audit["blockers"]
    assert "lineage_state_fact_snapshot_id_conflict" in audit["blockers"]
    assert audit["lineage_blocker_expected_removed_count"] == 0
    assert audit["items"][0]["post_apply_prediction"]["lineage_blocker_expected_removed"] is False
    assert after == before


def test_lineage_repair_apply_cli_applies_only_with_explicit_preconditions(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = _create_db(tmp_path)
    contract, manifest = _repair_contract(db_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    backup = tmp_path / "archive.db.bak"
    backup.write_text("backup", encoding="utf-8")

    code = daily_review_lineage_repair_apply.main(
        [
            "--db",
            str(db_path),
            "--since-date",
            "2026-07-01",
            "--lineage-decision-manifest",
            str(manifest_path),
            "--apply",
            "--confirm",
            CONFIRMATION_PHRASE,
            "--backup",
            str(backup),
            "--writer-quiescent",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    repaired = _fact_payload(db_path)
    assert code == 0
    assert output["apply_result"]["applied"] is True
    assert repaired["fact_snapshot_id"] == contract["repair_items"][0]["candidate_fact_snapshot_id"]
