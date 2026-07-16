import json
from pathlib import Path
import sqlite3

from scripts.amsc_probability_pending_queue import (
    COMPACT_CONFIRM,
    EXPIRE_CONFIRM,
    ARCHIVE_CONSUMED_CONFIRM,
    archive_consumed,
    compact,
    enqueue,
    expire_noncurrent,
)
from production_probability_runtime_manifest import RUNTIME_FILES
from production_probability_successor_activation_contract import activation_sha256


def _plan(fact_as_of: str) -> dict:
    valid_time = "2026-07-14T14:30:00Z"
    return {
        "event_date": "2026-07-14",
        "fact_as_of": fact_as_of,
        "effective_payload": {
            "weather_likelihood_features": {
                "zuuu_metar_raw_amsc_latest_at": valid_time,
                "zuuu_metar_raw_amsc_latest_raw": "METAR ZUUU 141430Z 09003MPS 9999 31/24 Q1001=",
            },
            "source_observations": [
                {
                    "source_id": "zuuu_metar_raw_amsc",
                    "source_observation_id": "srcobs_same",
                    "event_date": "2026-07-14",
                    "valid_time": valid_time,
                    "raw_sha256": "abc123",
                }
            ],
        },
    }


def test_pending_queue_is_bound_into_production_runtime_manifest() -> None:
    assert "scripts/amsc_probability_pending_queue.py" in RUNTIME_FILES


def test_enqueue_preserves_earliest_plan_and_counts_duplicate(tmp_path: Path) -> None:
    pending = tmp_path / "pending"
    result = tmp_path / "result.json"
    result.write_text('{"state":"blocked"}', encoding="utf-8")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(_plan("2026-07-14T14:30:01Z")), encoding="utf-8")
    second.write_text(json.dumps(_plan("2026-07-14T14:30:09Z")), encoding="utf-8")

    first_pending = enqueue(first, result, pending)
    second_pending = enqueue(second, result, pending)

    assert first_pending == second_pending
    assert json.loads(first_pending.read_text())["fact_as_of"] == "2026-07-14T14:30:01Z"
    receipt = json.loads(first_pending.with_suffix(".deferred.json").read_text())
    assert receipt["duplicate_deferral_count"] == 1
    assert receipt["earliest_frozen_plan_preserved"] is True
    assert receipt["plan_created_this_attempt"] is False


def test_compact_moves_legacy_duplicates_and_keeps_earliest_causal_plan(tmp_path: Path) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    late = pending / "late.plan.json"
    early = pending / "early.plan.json"
    late.write_text(json.dumps(_plan("2026-07-14T14:30:09Z")), encoding="utf-8")
    early.write_text(json.dumps(_plan("2026-07-14T14:30:01Z")), encoding="utf-8")
    late.with_suffix(".deferred.json").write_text(
        '{"queued_at":"2026-07-14T14:30:10Z"}', encoding="utf-8"
    )
    early.with_suffix(".deferred.json").write_text(
        '{"queued_at":"2026-07-14T14:30:02Z"}', encoding="utf-8"
    )

    preview = compact(pending, apply=False, confirm="")
    assert preview["duplicate_plan_count"] == 1
    assert late.exists() and early.exists()

    applied = compact(pending, apply=True, confirm=COMPACT_CONFIRM)
    assert applied["state"] == "compacted"
    assert early.exists()
    assert not late.exists()
    assert list((pending / "superseded").glob("*/late.plan.json"))
    receipt = json.loads(early.with_suffix(".deferred.json").read_text())
    assert receipt["compacted_duplicate_plan_count"] == 1


def test_expire_noncurrent_archives_evidence_without_probability_or_orders(tmp_path: Path) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    plan = pending / "old.plan.json"
    deferred = plan.with_suffix(".deferred.json")
    plan.write_text(json.dumps(_plan("2026-07-14T14:30:01Z")), encoding="utf-8")
    deferred.write_text('{"state":"pending_replay"}', encoding="utf-8")

    preview = expire_noncurrent(
        plan,
        current_event_date="2026-07-15",
        apply=False,
        confirm="",
    )
    assert preview["state"] == "expired_noncurrent_physical_station_day"
    assert plan.exists()

    result = expire_noncurrent(
        plan,
        current_event_date="2026-07-15",
        apply=True,
        confirm=EXPIRE_CONFIRM,
    )
    assert result["state"] == "expired_noncurrent_physical_station_day"
    assert result["writes_posterior"] is False
    assert result["places_orders"] is False
    assert not plan.exists()
    receipt_paths = list((pending / "expired" / "2026-07-14").glob("*/expiry_receipt.json"))
    assert len(receipt_paths) == 1
    receipt = json.loads(receipt_paths[0].read_text())
    assert receipt["raw_archive_remains_authoritative"] is True
    assert receipt["reason"] == "noncurrent_physical_station_day_never_replayed_as_live"


def test_expire_noncurrent_retains_current_plan(tmp_path: Path) -> None:
    plan = tmp_path / "current.plan.json"
    value = _plan("2026-07-14T14:30:01Z")
    value["event_date"] = "2026-07-15"
    plan.write_text(json.dumps(value), encoding="utf-8")

    result = expire_noncurrent(
        plan,
        current_event_date="2026-07-15",
        apply=True,
        confirm=EXPIRE_CONFIRM,
    )
    assert result["state"] == "current_or_future_plan_retained"
    assert plan.exists()


def test_archive_consumed_moves_only_plan_bound_to_active_checkpoint(tmp_path: Path) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    plan = pending / "consumed.plan.json"
    deferred = plan.with_suffix(".deferred.json")
    plan.write_text(json.dumps(_plan("2026-07-14T14:30:01Z")), encoding="utf-8")
    deferred.write_text('{"state":"pending_replay"}', encoding="utf-8")
    activation = tmp_path / "activation.json"
    activation_payload = {"schema": "unit_activation", "state": "production_enabled"}
    activation.write_text(json.dumps(activation_payload), encoding="utf-8")
    activation_contract_sha = activation_sha256(activation_payload)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE production_initial_posterior_lineage (
                target_identity TEXT, activation_contract_sha256 TEXT,
                posterior_checkpoint_id TEXT
            );
            CREATE TABLE posterior_checkpoints (
                posterior_checkpoint_id TEXT PRIMARY KEY,target_identity TEXT,
                replay_generation INTEGER,state TEXT,committed_at TEXT,
                consumed_event_ids_json TEXT
            );
            CREATE TABLE canonical_observation_events (
                canonical_observation_event_id TEXT PRIMARY KEY,target_identity TEXT,
                valid_time TEXT,station_id TEXT,selected_source_id TEXT,state TEXT,
                selected_source_observation_id TEXT
            );
            """
        )
        target = "physical_weather_day_v1:ZUUU:2026-07-14"
        conn.execute(
            "INSERT INTO posterior_checkpoints VALUES (?,?,?,?,?,?)",
            ("pchk-1", target, 4, "committed", "2026-07-14T14:30:02Z", '["canobs-1"]'),
        )
        conn.execute(
            "INSERT INTO production_initial_posterior_lineage VALUES (?,?,?)",
            (target, activation_contract_sha, "pchk-1"),
        )
        conn.execute(
            "INSERT INTO canonical_observation_events VALUES (?,?,?,?,?,?,?)",
            (
                "canobs-1",
                target,
                "2026-07-14T14:30:00Z",
                "ZUUU",
                "zuuu_metar_raw_amsc",
                "accepted_primary",
                "srcobs_same",
            ),
        )

    preview = archive_consumed(
        pending,
        db=db,
        activation_path=activation,
        event_date="2026-07-14",
        apply=False,
        confirm="",
    )
    assert preview["archived_plan_count"] == 1
    assert plan.exists()

    result = archive_consumed(
        pending,
        db=db,
        activation_path=activation,
        event_date="2026-07-14",
        apply=True,
        confirm=ARCHIVE_CONSUMED_CONFIRM,
    )
    assert result["state"] == "archived"
    assert result["archived_plan_count"] == 1
    assert not plan.exists()
    receipts = list((pending / "consumed" / "2026-07-14").glob("*/consumed_checkpoint_receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["posterior_checkpoint_id"] == "pchk-1"
    assert receipt["canonical_observation_event_ids"] == ["canobs-1"]
    assert receipt["writes_archive_db"] is False
    assert receipt["places_orders"] is False


def _archive_db(
    path: Path,
    *,
    activation_payload: dict,
    consumed_event_ids_json: str,
    selected_source_observation_id: str = "srcobs_same",
) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE production_initial_posterior_lineage (
                target_identity TEXT, activation_contract_sha256 TEXT,
                posterior_checkpoint_id TEXT
            );
            CREATE TABLE posterior_checkpoints (
                posterior_checkpoint_id TEXT PRIMARY KEY,target_identity TEXT,
                replay_generation INTEGER,state TEXT,committed_at TEXT,
                consumed_event_ids_json TEXT
            );
            CREATE TABLE canonical_observation_events (
                canonical_observation_event_id TEXT PRIMARY KEY,target_identity TEXT,
                valid_time TEXT,station_id TEXT,selected_source_id TEXT,state TEXT,
                selected_source_observation_id TEXT
            );
            """
        )
        target = "physical_weather_day_v1:ZUUU:2026-07-14"
        conn.execute(
            "INSERT INTO posterior_checkpoints VALUES (?,?,?,?,?,?)",
            ("pchk-1", target, 4, "committed", "2026-07-14T14:30:02Z", consumed_event_ids_json),
        )
        conn.execute(
            "INSERT INTO production_initial_posterior_lineage VALUES (?,?,?)",
            (target, activation_sha256(activation_payload), "pchk-1"),
        )
        conn.execute(
            "INSERT INTO canonical_observation_events VALUES (?,?,?,?,?,?,?)",
            (
                "canobs-1",
                target,
                "2026-07-14T14:30:00Z",
                "ZUUU",
                "zuuu_metar_raw_amsc",
                "accepted_primary",
                selected_source_observation_id,
            ),
        )


def test_archive_consumed_retains_plan_not_consumed_by_active_checkpoint(tmp_path: Path) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    plan = pending / "unconsumed.plan.json"
    plan.write_text(json.dumps(_plan("2026-07-14T14:30:01Z")), encoding="utf-8")
    activation = tmp_path / "activation.json"
    activation_payload = {"schema": "unit_activation", "state": "production_enabled"}
    activation.write_text(json.dumps(activation_payload), encoding="utf-8")
    db = tmp_path / "archive.db"
    _archive_db(db, activation_payload=activation_payload, consumed_event_ids_json="[]")

    result = archive_consumed(
        pending,
        db=db,
        activation_path=activation,
        event_date="2026-07-14",
        apply=True,
        confirm=ARCHIVE_CONSUMED_CONFIRM,
    )

    assert result["ok"] is True
    assert result["archived_plan_count"] == 0
    assert result["retained_plan_count"] == 1
    assert result["retained"][0]["reason"] == "canonical_event_not_consumed_by_active_checkpoint"
    assert result["retained"][0]["canonical_event_ids"] == ["canobs-1"]
    assert plan.exists()
    assert not (pending / "consumed").exists()


def test_archive_consumed_refuses_time_only_match_without_source_observation_identity(
    tmp_path: Path,
) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    plan = pending / "legacy.plan.json"
    value = _plan("2026-07-14T14:30:01Z")
    value["effective_payload"]["source_observations"] = []
    plan.write_text(json.dumps(value), encoding="utf-8")
    activation = tmp_path / "activation.json"
    activation_payload = {"schema": "unit_activation", "state": "production_enabled"}
    activation.write_text(json.dumps(activation_payload), encoding="utf-8")
    db = tmp_path / "archive.db"
    _archive_db(
        db,
        activation_payload=activation_payload,
        consumed_event_ids_json='["canobs-1"]',
        selected_source_observation_id="different-source-observation",
    )

    result = archive_consumed(
        pending,
        db=db,
        activation_path=activation,
        event_date="2026-07-14",
        apply=True,
        confirm=ARCHIVE_CONSUMED_CONFIRM,
    )

    assert result["archived_plan_count"] == 0
    assert result["retained_plan_count"] == 1
    assert result["retained"][0]["reason"] == "primary_source_observation_identity_missing"
    assert plan.exists()
