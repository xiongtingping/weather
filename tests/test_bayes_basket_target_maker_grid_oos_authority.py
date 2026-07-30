from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.basket_kernel.contracts import ContractError
from bayes_basket_target.maker_grid_oos_artifact import build_maker_grid_oos_artifact
from bayes_basket_target.maker_grid_oos_authority import (
    append_maker_grid_oos_activation_manifest,
    append_maker_grid_oos_artifact_body,
    append_maker_grid_oos_promotion_evidence,
    ensure_maker_grid_oos_authority_schema,
    resolve_maker_grid_oos_artifact,
    resolve_maker_grid_oos_for_buckets,
    resolve_maker_grid_oos_point,
)
from bayes_basket_target.ssot import MAKER_GRID_OOS_AUTHORITY_TABLES
from tests.test_bayes_basket_target_maker_grid_oos_artifact import (
    AS_OF,
    SCOPE,
    _valid,
)

ROOT = Path(__file__).resolve().parents[1]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    result = ensure_maker_grid_oos_authority_schema(conn)
    assert result["ok"] is True
    assert tuple(result["tables"]) == MAKER_GRID_OOS_AUTHORITY_TABLES
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maker_oos_artifact_body'"
    ).fetchone()
    return conn


def test_legacy_single_point_maker_oos_implementation_is_removed() -> None:
    retired_paths = (
        ROOT / "bayes_basket_target" / "maker_oos_artifact.py",
        ROOT / "bayes_basket_target" / "maker_oos_authority.py",
        ROOT / "bayes_basket_target" / "maker_oos_training.py",
        ROOT / "scripts" / "build_bayes_basket_target_maker_oos_candidates.py",
    )
    assert all(not path.exists() for path in retired_paths)
    for relative in (
        "bayes_basket_target/production_io.py",
        "bayes_basket_target/production_worker.py",
        "bayes_basket_target/delta_v2_runtime.py",
        "bayes_basket_target/execution_transition_runtime.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "maker_oos_artifact" not in source
        assert "maker_oos_authority" not in source


def _artifact(*, artifact_id: str = "grid-v1"):
    points, expected = _valid()
    return build_maker_grid_oos_artifact(points, expected, artifact_id=artifact_id, as_of_utc=AS_OF)


def _promote(conn: sqlite3.Connection, artifact_id: str = "grid-v1") -> None:
    append_maker_grid_oos_promotion_evidence(
        conn,
        artifact_id=artifact_id,
        evidence={
            "decision_id": f"promotion-{artifact_id}",
            "decision": "PROMOTED",
            "gate_version": "maker-grid-authority-gate-v1",
            "decided_at_utc": "2026-07-20T01:00:00Z",
            "receipt_sha256": "a" * 64,
            "oos_metrics_sha256": "b" * 64,
        },
        stored_at_utc="2026-07-20T01:01:00Z",
    )


def _activate(
    conn: sqlite3.Connection,
    *,
    activation_id: str,
    generation: int,
    valid_from: str,
    valid_until: str,
    artifact_id: str = "grid-v1",
) -> None:
    append_maker_grid_oos_activation_manifest(
        conn,
        activation_id=activation_id,
        artifact_id=artifact_id,
        promotion_decision_id=f"promotion-{artifact_id}",
        scope={key: SCOPE[key] for key in (
            "station_id", "season_bin", "lead_bucket", "issuance", "liquidity_bin"
        )},
        generation=generation,
        valid_from_utc=valid_from,
        valid_until_utc=valid_until,
        activated_at_utc="2026-07-20T02:00:00Z",
    )


def _persist(conn: sqlite3.Connection, *, artifact_id: str = "grid-v1"):
    artifact = _artifact(artifact_id=artifact_id)
    append_maker_grid_oos_artifact_body(
        conn, artifact, stored_at_utc="2026-07-20T00:01:00Z"
    )
    _promote(conn, artifact_id)
    return artifact


def test_schema_is_independent_bounded_and_idempotent():
    conn = _db()
    again = ensure_maker_grid_oos_authority_schema(conn)
    assert again["ok"] is True
    assert again["rows_written"] == 0
    assert set(row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'maker_grid_oos_%'"
    )) == {
        "maker_grid_oos_artifact_body_no_update",
        "maker_grid_oos_artifact_body_no_delete",
        "maker_grid_oos_promotion_no_update",
        "maker_grid_oos_promotion_no_delete",
        "maker_grid_oos_manifest_no_update",
        "maker_grid_oos_manifest_no_delete",
        "maker_grid_oos_pointer_no_update",
        "maker_grid_oos_pointer_no_delete",
    }


def test_public_resolver_signature_has_no_price_or_queue_scope():
    parameters = set(inspect.signature(resolve_maker_grid_oos_artifact).parameters)
    assert parameters == {
        "conn", "station_id", "season_bin", "lead_bucket", "issuance",
        "liquidity_bin", "as_of_utc",
    }


def test_bucket_resolver_shares_one_formal_grid_but_keeps_exact_queue_scope() -> None:
    conn = _db()
    artifact = _persist(conn)
    _activate(
        conn,
        activation_id="bucket-grid",
        generation=1,
        valid_from="2026-07-20T03:00:00Z",
        valid_until="2026-08-01T00:00:00Z",
    )
    common = {
        key: SCOPE[key]
        for key in (
            "station_id",
            "season_bin",
            "lead_bucket",
            "issuance",
            "liquidity_bin",
        )
    }
    result = resolve_maker_grid_oos_for_buckets(
        conn,
        bucket_scopes={
            "35°C": {**common, "queue_bin": "q1_6"},
            "36°C": {**common, "queue_bin": "q2_7"},
        },
        as_of_utc="2026-07-21T00:00:00Z",
    )
    assert result["artifact"] == artifact.to_dict()
    assert set(result["maker_grid_oos_models"]) == {"35°C", "36°C"}
    assert result["maker_grid_oos_context"]["scope_by_bucket"]["35°C"][
        "queue_bin"
    ] == "q1_6"
    with pytest.raises(ContractError, match="bucket_scope_fields_invalid"):
        resolve_maker_grid_oos_for_buckets(
            conn,
            bucket_scopes={
                "35°C": {
                    **common,
                    "queue_bin": "q1_6",
                    "price_distance_bin": "near",
                }
            },
            as_of_utc="2026-07-21T00:00:00Z",
        )


def test_body_promotion_manifest_pointer_roundtrip_and_exact_grid_point():
    conn = _db()
    artifact = _persist(conn)
    _activate(
        conn,
        activation_id="gen-1",
        generation=1,
        valid_from="2026-07-20T03:00:00Z",
        valid_until="2026-08-01T00:00:00Z",
    )
    result = resolve_maker_grid_oos_artifact(
        conn,
        station_id="RKPK", season_bin="summer", lead_bucket="d1",
        issuance="00Z", liquidity_bin="liquid",
        as_of_utc="2026-07-21T00:00:00Z",
    )
    assert result["artifact"] == artifact.to_dict()
    assert result["maker_grid_oos_context"]["grid_size"] == 3
    point = resolve_maker_grid_oos_point(
        conn,
        station_id="RKPK", season_bin="summer", lead_bucket="d1",
        issuance="00Z", liquidity_bin="liquid",
        candidate_price=0.10, price_tick=0.01, timeout_seconds=10,
        price_distance_bin="near", queue_bin="q1_6",
        as_of_utc="2026-07-21T00:00:00Z",
    )
    assert point["grid_point"]["timeout_seconds"] == 10
    with pytest.raises(ContractError, match="exact_grid_point_missing"):
        resolve_maker_grid_oos_point(
            conn,
            station_id="RKPK", season_bin="summer", lead_bucket="d1",
            issuance="00Z", liquidity_bin="liquid",
            candidate_price=0.10, price_tick=0.01, timeout_seconds=10,
            price_distance_bin="near", queue_bin="q99",
            as_of_utc="2026-07-21T00:00:00Z",
        )


def test_duplicate_is_idempotent_but_content_and_evidence_tampering_conflicts():
    conn = _db()
    artifact = _persist(conn)
    assert append_maker_grid_oos_artifact_body(
        conn, artifact, stored_at_utc="2026-07-20T00:02:00Z"
    ) == artifact.to_dict()
    tampered = deepcopy(artifact.to_dict())
    tampered["points"][0]["curve"]["fill_probability"] = 0.99
    with pytest.raises(ContractError, match="artifact_invalid"):
        append_maker_grid_oos_artifact_body(
            conn, tampered, stored_at_utc="2026-07-20T00:02:00Z"
        )
    bad = {
        "decision_id": "promotion-grid-v1",
        "decision": "PROMOTED",
        "gate_version": "maker-grid-authority-gate-v1",
        "decided_at_utc": "2026-07-20T01:00:00Z",
        "receipt_sha256": "c" * 64,
        "oos_metrics_sha256": "b" * 64,
    }
    with pytest.raises(ContractError, match="promotion_conflict"):
        append_maker_grid_oos_promotion_evidence(
            conn, artifact_id="grid-v1", evidence=bad,
            stored_at_utc="2026-07-20T01:02:00Z",
        )


def test_rejected_promotion_cannot_activate_and_activation_is_atomic():
    conn = _db()
    artifact = _artifact(artifact_id="rejected")
    append_maker_grid_oos_artifact_body(conn, artifact, stored_at_utc="2026-07-20T00:01:00Z")
    append_maker_grid_oos_promotion_evidence(
        conn,
        artifact_id="rejected",
        evidence={
            "decision_id": "promotion-rejected",
            "decision": "REJECTED",
            "gate_version": "maker-grid-authority-gate-v1",
            "decided_at_utc": "2026-07-20T01:00:00Z",
            "receipt_sha256": "a" * 64,
            "oos_metrics_sha256": "b" * 64,
        },
        stored_at_utc="2026-07-20T01:01:00Z",
    )
    with pytest.raises(ContractError, match="promotion_not_promoted"):
        _activate(
            conn,
            activation_id="must-not-write",
            generation=1,
            valid_from="2026-07-20T03:00:00Z",
            valid_until="2026-08-01T00:00:00Z",
            artifact_id="rejected",
        )
    assert conn.execute("SELECT COUNT(*) FROM maker_grid_oos_activation_manifest").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM maker_grid_oos_activation_pointer").fetchone()[0] == 0


def test_cross_as_of_grid_body_is_rejected_before_persistence():
    conn = _db()
    points, expected = _valid()
    with pytest.raises(ContractError, match="cross_as_of_scope"):
        build_maker_grid_oos_artifact(
            points, expected, artifact_id="cross-as-of", as_of_utc="2026-07-21T00:00:00Z"
        )
    assert conn.execute("SELECT COUNT(*) FROM maker_grid_oos_artifact_body").fetchone()[0] == 0


def test_authority_never_backdates_storage_promotion_or_activation() -> None:
    conn = _db()
    artifact = _artifact(artifact_id="time-ordered")
    with pytest.raises(ContractError, match="artifact_stored_before_as_of"):
        append_maker_grid_oos_artifact_body(
            conn,
            artifact,
            stored_at_utc="2026-07-19T23:59:59Z",
        )
    append_maker_grid_oos_artifact_body(
        conn,
        artifact,
        stored_at_utc="2026-07-20T00:01:00Z",
    )
    with pytest.raises(ContractError, match="promotion_as_of_order_invalid"):
        append_maker_grid_oos_promotion_evidence(
            conn,
            artifact_id="time-ordered",
            evidence={
                "decision_id": "promotion-time-ordered",
                "decision": "PROMOTED",
                "gate_version": "maker-grid-authority-gate-v1",
                "decided_at_utc": "2026-07-19T23:59:59Z",
                "receipt_sha256": "a" * 64,
                "oos_metrics_sha256": "b" * 64,
            },
            stored_at_utc="2026-07-20T01:01:00Z",
        )
    _promote(conn, "time-ordered")
    with pytest.raises(ContractError, match="activation_as_of_order_invalid"):
        _activate(
            conn,
            activation_id="backdated",
            generation=1,
            valid_from="2026-07-20T01:30:00Z",
            valid_until="2026-08-01T00:00:00Z",
            artifact_id="time-ordered",
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM maker_grid_oos_activation_manifest"
    ).fetchone()[0] == 0


def test_overlapping_active_generations_fail_closed_and_nonoverlap_replays_low_high_low():
    conn = _db()
    _persist(conn)
    _activate(conn, activation_id="low", generation=1, valid_from="2026-07-20T03:00:00Z", valid_until="2026-07-23T00:00:00Z")
    _activate(conn, activation_id="high", generation=2, valid_from="2026-07-23T00:00:00Z", valid_until="2026-07-26T00:00:00Z")
    _activate(conn, activation_id="low-again", generation=3, valid_from="2026-07-26T00:00:00Z", valid_until="2026-08-01T00:00:00Z")
    for when, activation in (
        ("2026-07-21T00:00:00Z", "low"),
        ("2026-07-24T00:00:00Z", "high"),
        ("2026-07-27T00:00:00Z", "low-again"),
    ):
        result = resolve_maker_grid_oos_artifact(
            conn, station_id="RKPK", season_bin="summer", lead_bucket="d1",
            issuance="00Z", liquidity_bin="liquid", as_of_utc=when,
        )
        assert result["maker_grid_oos_context"]["activation_id"] == activation
    _activate(conn, activation_id="overlap", generation=4, valid_from="2026-07-22T00:00:00Z", valid_until="2026-07-25T00:00:00Z")
    with pytest.raises(ContractError, match="multiple_active"):
        resolve_maker_grid_oos_artifact(
            conn, station_id="RKPK", season_bin="summer", lead_bucket="d1",
            issuance="00Z", liquidity_bin="liquid", as_of_utc="2026-07-22T12:00:00Z",
        )


def test_generation_must_be_unique_and_strictly_highest():
    conn = _db()
    _persist(conn)
    _activate(conn, activation_id="first", generation=5, valid_from="2026-07-20T03:00:00Z", valid_until="2026-07-24T00:00:00Z")
    with pytest.raises(ContractError, match="generation_not_unique_highest"):
        _activate(conn, activation_id="rewind", generation=4, valid_from="2026-07-24T00:00:00Z", valid_until="2026-07-28T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM maker_grid_oos_activation_manifest").fetchone()[0] == 1


def test_append_only_triggers_cover_all_four_tables():
    conn = _db()
    artifact = _persist(conn)
    _activate(conn, activation_id="one", generation=1, valid_from="2026-07-20T03:00:00Z", valid_until="2026-08-01T00:00:00Z")
    statements = (
        "UPDATE maker_grid_oos_artifact_body SET artifact_json=artifact_json",
        "DELETE FROM maker_grid_oos_artifact_body",
        "UPDATE maker_grid_oos_promotion_evidence SET evidence_json=evidence_json",
        "DELETE FROM maker_grid_oos_promotion_evidence",
        "UPDATE maker_grid_oos_activation_manifest SET manifest_json=manifest_json",
        "DELETE FROM maker_grid_oos_activation_manifest",
        "UPDATE maker_grid_oos_activation_pointer SET pointer_json=pointer_json",
        "DELETE FROM maker_grid_oos_activation_pointer",
    )
    for statement in statements:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(statement)
    assert artifact["artifact_id"] == "grid-v1"
