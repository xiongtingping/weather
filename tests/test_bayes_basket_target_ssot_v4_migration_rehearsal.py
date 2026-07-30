from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

import scripts.bayes_basket_target_ssot_v4_migration_rehearsal as rehearsal
from bayes_basket_target import forward_observation_store as forward_store
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.probability_evidence_store import (
    metar_trajectory_scope_key,
)
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    production_ssot_schema_status,
)
from tests.test_bayes_basket_target_forward_observation_storage import _packet
from tests.test_bayes_basket_target_probability_evidence_store import (
    _trajectory_artifact,
)
from tests.test_bayes_basket_target_artifact_ingress import (
    _activation_only_packet,
    _calibration_activation,
)


OLD_TRAINING_DDL = """
CREATE TABLE basket_policy_training_fact (
  fact_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  stage TEXT NOT NULL,
  posterior_sha256 TEXT NOT NULL CHECK(length(posterior_sha256)=64),
  quote_sha256 TEXT NOT NULL CHECK(length(quote_sha256)=64),
  inventory_sha256 TEXT NOT NULL CHECK(length(inventory_sha256)=64),
  order_state_sha256 TEXT NOT NULL CHECK(length(order_state_sha256)=64),
  outcome_json TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
)
"""


def _v3_target(path: Path, *, populated_training: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        apply_production_ssot_schema(conn)
        # Model the pre-manifest v3 image: the new authority object must be
        # created by the controlled migration itself.
        conn.execute("DROP TABLE basket_cash_phase_authority_manifest")
        conn.execute("DROP TABLE basket_policy_training_label")
        conn.execute("DROP TABLE basket_policy_training_fact")
        conn.execute("DROP TABLE basket_strict_delta_policy_activation")
        conn.execute("DROP TABLE basket_strict_delta_policy_promotion")
        conn.execute("DROP TABLE basket_strict_delta_policy_artifact")
        conn.execute(OLD_TRAINING_DDL)
        if populated_training:
            conn.execute(
                "INSERT INTO basket_policy_training_fact VALUES("
                "?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-fact",
                    "RKPK",
                    "2026-07-27",
                    "2026-07-26T16:00:00Z",
                    "P1",
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "{}",
                    "2026/RKPK",
                    "e" * 64,
                ),
            )
        conn.commit()


def test_exact_v4_rehearsal_upgrades_clone_and_leaves_source_unchanged(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target-v3.db"
    _v3_target(source)
    before = source.read_bytes()

    result = rehearsal.run_rehearsal(source_db=source)

    assert result["state"] == "passed"
    assert result["after_schema"]["ok"] is True
    assert result["probability_schema_rehearsal"]["state"] == "ready"
    assert result["probability_schema_rehearsal"]["missing_tables"] == []
    assert result["probability_schema_rehearsal"]["missing_triggers"] == []
    assert result["idempotent_second_state"] == "already_ready"
    assert result["snapshot_counts"] == result["after_counts"]
    assert result["temporary_trial_database_removed"] is True
    assert result["temporary_backup_removed"] is True
    assert result["temporary_working_directory_removed"] is True
    assert result["source_manifest_schema_before"]["ok"] is False
    assert result["manifest_schema_before"]["ok"] is False
    assert result["manifest_schema_after"] == {
        "ok": True,
        "table_present": True,
        "columns_ok": True,
        "table_constraints_ok": True,
        "index_ok": True,
        "triggers_ok": True,
        "columns": [
            ["manifest_sha256", "TEXT", 0, 1],
            ["station_id", "TEXT", 1, 0],
            ["policy_version", "TEXT", 1, 0],
            ["artifact_sha256", "TEXT", 1, 0],
            ["station_timezone", "TEXT", 1, 0],
            ["valid_from_utc", "TEXT", 1, 0],
            ["valid_until_utc", "TEXT", 1, 0],
            ["authority_scope", "TEXT", 1, 0],
        ],
        "index_columns": [
            "station_id",
            "authority_scope",
            "valid_from_utc",
            "valid_until_utc",
        ],
        "trigger_names": [
            "basket_cash_phase_authority_no_delete",
            "basket_cash_phase_authority_no_update",
        ],
        "missing": [],
    }
    assert result["backup_manifest_schema"]["ok"] is False
    assert result["resource_budget"]["trial_database_bytes"] <= result[
        "resource_budget"
    ]["max_database_bytes"]
    assert result["resource_budget"]["trial_wal_bytes"] <= result[
        "resource_budget"
    ]["max_wal_bytes"]
    assert result["resource_budget"]["backup_database_bytes"] <= result[
        "resource_budget"
    ]["max_database_bytes"]
    assert result["boundary"]["writes_source_database"] is False
    assert source.read_bytes() == before
    with sqlite3.connect(source) as conn:
        assert production_ssot_schema_status(conn)["ok"] is False


def test_exact_v4_rehearsal_ingests_and_replays_probability_evidence_packet(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target-v3.db"
    _v3_target(source)
    before = source.read_bytes()
    artifact = _trajectory_artifact(
        activation_id="rehearsal-activation",
        formal=True,
    )
    scope_key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin="JJA",
        local_time_bucket="AFTERNOON",
        maturity_bin="D0",
        settlement_definition="station_local_calendar_day_tmax",
    )
    artifact_row = {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "scope_key": scope_key,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": True,
        "authority_scope": "FORMAL_PROMOTED",
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    }
    promotion_identity = {
        "promotion_id": "rehearsal-promotion",
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": "PROMOTED",
        "gate_version": "strict-forward-oos-v1",
        "metrics_sha256": "1" * 64,
        "decided_at_utc": "2026-07-19T00:00:00Z",
        "receipt_sha256": "2" * 64,
    }
    activation_identity = {
        "activation_id": artifact.activation_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "station_id": "RKPK",
        "scope_key": scope_key,
        "generation": 1,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "activated_at_utc": artifact.valid_from_utc,
        "authority_scope": "FORMAL_PROMOTED",
    }
    packet_identity = {
        "schema": "bayes_basket_probability_evidence_ingress_v1",
        "artifacts": [artifact_row],
        "promotions": [{
            **promotion_identity,
            "content_sha256": content_sha256(promotion_identity),
        }],
        "activations": [{
            **activation_identity,
            "content_sha256": content_sha256(activation_identity),
        }],
    }
    packet = {
        **packet_identity,
        "packet_sha256": content_sha256(packet_identity),
    }
    packet_path = tmp_path / "probability-evidence-packet.json"
    packet_path.write_text(
        json.dumps(packet, sort_keys=True),
        encoding="utf-8",
    )

    result = rehearsal.run_rehearsal(
        source_db=source,
        probability_evidence_packet=packet_path,
    )

    validation = result["probability_evidence_packet_validation"]
    assert validation["state"] == (
        "COMMITTED_IDEMPOTENT_READER_REPLAYED_ON_DISPOSABLE_CLONE"
    )
    assert validation["row_count"] == 3
    assert validation["activation_count"] == 1
    assert validation["resolved_activations"] == [{
        "activation_id": "rehearsal-activation",
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
    }]
    assert result["boundary"]["writes_source_database"] is False
    assert source.read_bytes() == before


def test_exact_v4_rehearsal_accepts_model_only_artifact_packet(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target-v3.db"
    _v3_target(source)
    before = source.read_bytes()
    packet_identity = _activation_only_packet(
        _calibration_activation(
            activation_id="clone-model-only-activation",
            artifact_id="clone-model-only-artifact",
            generation=1,
        )
    )
    packet = {
        **packet_identity,
        "packet_sha256": content_sha256(packet_identity),
    }
    packet_path = tmp_path / "model-only-packet.json"
    packet_path.write_text(
        json.dumps(packet, sort_keys=True),
        encoding="utf-8",
    )

    result = rehearsal.run_rehearsal(
        source_db=source,
        artifact_packet=packet_path,
    )

    validation = result["artifact_packet_validation"]
    assert validation["state"] == (
        "COMMITTED_IDEMPOTENT_ON_DISPOSABLE_CLONE"
    )
    assert validation["declared_counts"] == {
        "calibration_artifacts": 0,
        "ensemble_selection_evidence": 0,
        "ensemble_artifacts": 0,
        "calibration_activations": 1,
        "cash_phase_policies": 0,
        "cash_phase_authority_manifests": 0,
    }
    assert result["boundary"]["writes_source_database"] is False
    assert source.read_bytes() == before


def test_exact_v4_rehearsal_fails_closed_on_populated_legacy_training_fact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target-v3-populated.db"
    _v3_target(source, populated_training=True)
    before = source.read_bytes()

    with pytest.raises(
        RuntimeError,
        match="policy_training_exact_version_upgrade_requires_empty_tables",
    ):
        rehearsal.run_rehearsal(source_db=source)

    assert source.read_bytes() == before


def test_exact_v4_rehearsal_rejects_oversized_source_before_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "target-v3.db"
    _v3_target(source)
    monkeypatch.setattr(rehearsal, "MAX_TARGET_DB_BYTES", 0)

    with pytest.raises(RuntimeError, match="source_database_budget_exceeded"):
        rehearsal.run_rehearsal(source_db=source)


def test_v2_store_two_round_replay_is_idempotent_and_monotonic(
    tmp_path: Path,
) -> None:
    """Rehearse the exact post-cutover checks, including a zero-new-row replay."""

    root = tmp_path / "target-runtime"
    store_path = root / "forward_observations.sqlite3"
    row = _packet()["observations"][0]
    record = {
        "observation": row,
        "decision_id": row["decision_id"],
        "decision_sha256": "f" * 64,
        "source_rowid": 17,
    }

    first = forward_store.commit_forward_observation_batch(
        store_path,
        records=[record],
        expected_generation=0,
        last_source_rowid=17,
        materialized_at_utc="2026-07-28T00:00:00Z",
        allowed_root=root,
    )
    second = forward_store.commit_forward_observation_batch(
        store_path,
        records=[record],
        expected_generation=first["generation"],
        last_source_rowid=17,
        materialized_at_utc="2026-07-28T00:00:01Z",
        allowed_root=root,
    )
    state = forward_store.read_forward_store_state(
        store_path,
        allowed_root=root,
    )

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["replayed"] == 1
    assert second["last_source_rowid"] >= first["last_source_rowid"]
    assert second["generation"] >= first["generation"]
    assert state["observation_count"] == 1
    assert state["last_source_rowid"] >= 17
    assert store_path.stat().st_size <= forward_store.MAX_STORE_BYTES
    wal_path = store_path.with_name(store_path.name + "-wal")
    assert not wal_path.exists() or wal_path.stat().st_size <= forward_store.MAX_STORE_WAL_BYTES


def test_v2_store_rejects_root_and_database_symlinks_before_access(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real-target"
    real_root.mkdir()
    linked_root = tmp_path / "linked-target"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(
        forward_store.ForwardObservationStoreError,
        match="root_symlink|outside|symlink",
    ):
        forward_store.resolve_forward_store_path(
            linked_root / "forward_observations.sqlite3",
            allowed_root=linked_root,
        )

    real_store = real_root / "real.sqlite3"
    real_store.touch()
    linked_store = real_root / "forward_observations.sqlite3"
    linked_store.symlink_to(real_store)
    with pytest.raises(
        forward_store.ForwardObservationStoreError,
        match="symlink",
    ):
        forward_store.resolve_forward_store_path(
            linked_store,
            allowed_root=real_root,
        )


def test_v2_store_meta_tamper_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target-runtime"
    store_path = root / "forward_observations.sqlite3"
    row = _packet()["observations"][0]
    forward_store.commit_forward_observation_batch(
        store_path,
        records=[{
            "observation": row,
            "decision_id": row["decision_id"],
            "decision_sha256": "e" * 64,
            "source_rowid": 1,
        }],
        expected_generation=0,
        last_source_rowid=1,
        materialized_at_utc="2026-07-28T00:00:00Z",
        allowed_root=root,
    )
    before = store_path.read_bytes()
    with sqlite3.connect(store_path) as conn:
        conn.execute(
            "UPDATE forward_store_meta SET schema_version=999 WHERE singleton=1"
        )
        conn.commit()
    with pytest.raises(
        forward_store.ForwardObservationStoreError,
        match="meta_drift",
    ):
        forward_store.read_forward_store_state(store_path, allowed_root=root)
    # The tamper is deliberately left in SQLite's WAL; the fail-closed reader
    # must not repair, vacuum, or rewrite the resident database file.
    assert store_path.read_bytes() == before
    assert store_path.with_name(store_path.name + "-wal").exists()
