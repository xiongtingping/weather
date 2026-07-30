from __future__ import annotations

import copy
import sqlite3

import pytest

from bayes_basket_target.basket_kernel.contracts import (
    ContractError,
    content_sha256,
)
from bayes_basket_target.delta_v2_artifact import (
    ACTIVATION_SCHEMA,
    ARTIFACT_SCHEMA,
    PROMOTION_SCHEMA,
    DeltaV2PolicyArtifact,
    commit_delta_v2_authority,
    resolve_delta_v2_policy,
)
from bayes_basket_target.ssot import (
    apply_delta_v2_authority_schema,
    apply_production_ssot_schema,
)
import scripts.apply_bayes_basket_target_delta_v2_schema as migration_module
from scripts.apply_bayes_basket_target_delta_v2_schema import migrate


def _artifact(*, oos_passed: bool) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": (
            "delta-v2-rkpk-oos" if oos_passed else "delta-v2-rkpk-preregistered"
        ),
        "station_id": "RKPK",
        "stage": "P1",
        "season_bin": "summer",
        "maturity_bin": "d0",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v2",
        "value_weights": {
            "lambda_profit_floor": 0.5,
            "lambda_cvar": 0.5,
            "lambda_shortfall": 0.1,
            "lambda_breakthrough": 0.1,
            "lambda_overfill": 0.2,
            "lambda_excess_hhi": 0.2,
            "cvar_alpha": 0.1,
            "hhi_max": 0.5,
            "expected_pnl_scale": 1.0,
            "profit_floor_scale": 1.0,
            "cvar_scale": 1.0,
            "coverage_scale": 1.0,
            "overfill_scale": 1.0,
            "excess_hhi_scale": 1.0,
        },
        "execution_weights": {
            "epsilon_score": 0.001,
            "lambda_wait": 0.1,
            "lambda_queue": 0.1,
            "lambda_adverse_selection": 0.1,
            "lambda_reprice": 0.1,
            "lambda_capital_lock": 0.1,
            "wait_scale": 1.0,
            "queue_scale": 1.0,
            "adverse_selection_scale": 1.0,
            "reprice_scale": 1.0,
            "capital_lock_scale": 1.0,
            "maximum_candidates": 16,
        },
        "cash_clip_grid": [0.5, 1.0, 2.0],
        "price_grid": [0.05, 0.1, 0.2],
        "timeout_grid_seconds": [30.0, 60.0, 120.0],
        "fill_distribution_artifact_sha256": "1" * 64,
        "training_evidence_sha256": "2" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-05-31",
        "holdout_start_date": "2026-06-01",
        "holdout_end_date": "2026-06-30",
        "sample_n": 200 if oos_passed else 0,
        "holdout_n": 80 if oos_passed else 0,
        "consecutive_forward_windows": 2 if oos_passed else 0,
        "compared_baselines": (
            sorted(["v1", "EV_ONLY", "ZERO_WEIGHT", "HOLD"])
            if oos_passed
            else []
        ),
        "oos_metrics": (
            {
                "delta_score_lift_vs_v1": 0.1,
                "profit_floor_lift": 0.02,
                "resource_failures": 0.0,
            }
            if oos_passed
            else {}
        ),
        "oos_gate_version": "delta-v2-forward-oos-v1",
        "oos_gate_passed": oos_passed,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "valid_from_utc": "2026-07-02T00:00:00Z",
        "valid_until_utc": "2027-01-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE",
    }
    raw["content_sha256"] = content_sha256(raw)
    return raw


def _promotion(
    artifact: dict[str, object],
    *,
    decision: str,
) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": f"promotion-{artifact['artifact_id']}",
        "artifact_id": artifact["artifact_id"],
        "decision_seq": 1,
        "decision": decision,
        "gate_version": artifact["oos_gate_version"],
        "decided_at_utc": "2026-07-01T12:00:00Z",
        "receipt_sha256": "3" * 64,
    }
    raw["content_sha256"] = content_sha256(raw)
    return raw


def _activation(artifact: dict[str, object]) -> dict[str, object]:
    raw: dict[str, object] = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": f"activation-{artifact['artifact_id']}",
        "artifact_id": artifact["artifact_id"],
        "station_id": artifact["station_id"],
        "stage": artifact["stage"],
        "season_bin": artifact["season_bin"],
        "maturity_bin": artifact["maturity_bin"],
        "liquidity_bin": artifact["liquidity_bin"],
        "strategy_version": artifact["strategy_version"],
        "generation": 1,
        "valid_from_utc": artifact["valid_from_utc"],
        "valid_until_utc": artifact["valid_until_utc"],
        "activated_at_utc": "2026-07-01T12:00:00Z",
        "authority_scope": artifact["authority_scope"],
    }
    raw["content_sha256"] = content_sha256(raw)
    return raw


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    return conn


def test_schema_installs_v2_authority_append_only() -> None:
    conn = _db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "basket_delta_v2_policy_artifact",
        "basket_delta_v2_policy_promotion",
        "basket_delta_v2_policy_activation",
    } <= tables


def test_narrow_schema_migration_is_idempotent_and_has_no_backfill(
    tmp_path,
) -> None:
    path = tmp_path / "target-copy.sqlite3"
    sqlite3.connect(path).close()
    first = migrate(db_path=path, confirmed_writer_stopped=False)
    second = migrate(db_path=path, confirmed_writer_stopped=False)

    assert first["quick_check"] == "ok"
    assert second["quick_check"] == "ok"
    assert first["performs_legacy_backfill"] is False
    assert first["orders_submitted"] == 0
    assert first["production_target"] is False
    assert first["target_ledger_schema"]["ok"] is True
    assert first["execution_transition_schema"]["state"] == "READY"
    with sqlite3.connect(path) as conn:
        post_fill_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info("
                "basket_target_post_fill_recompute_requests)"
            )
        }
    assert {
        "economic_policy_schema",
        "economic_policy_artifact_sha256",
    } <= post_fill_columns


def test_narrow_schema_migration_rolls_back_all_ddl_on_transition_failure(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "target-fault.sqlite3"
    sqlite3.connect(path).close()

    def fail_transition(*_args, **_kwargs):
        raise RuntimeError("injected_transition_schema_failure")

    monkeypatch.setattr(
        migration_module,
        "apply_execution_transition_schema",
        fail_transition,
    )
    with pytest.raises(
        RuntimeError,
        match="injected_transition_schema_failure",
    ):
        migrate(db_path=path, confirmed_writer_stopped=False)

    with sqlite3.connect(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "basket_delta_v2_policy_artifact" not in tables
        assert "basket_target_execution_version_vectors" not in tables
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_old_promotion_constraint_is_upgraded_without_losing_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE basket_delta_v2_policy_promotion (
          promotion_id TEXT PRIMARY KEY,
          artifact_id TEXT NOT NULL,
          decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),
          decision TEXT NOT NULL CHECK(decision IN (
            'PREREGISTERED','PROMOTED','REJECTED','SUPERSEDED'
          )),
          gate_version TEXT NOT NULL,
          decided_at_utc TEXT NOT NULL,
          receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
          content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
          UNIQUE(artifact_id,decision_seq)
        )
        """
    )
    conn.execute(
        "INSERT INTO basket_delta_v2_policy_promotion VALUES"
        "(?,?,?,?,?,?,?,?)",
        (
            "old-promotion",
            "old-artifact",
            1,
            "PREREGISTERED",
            "old-gate",
            "2026-07-01T00:00:00Z",
            "a" * 64,
            "b" * 64,
        ),
    )

    first = apply_delta_v2_authority_schema(conn)
    second = apply_delta_v2_authority_schema(conn)
    table_sql = str(
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='basket_delta_v2_policy_promotion'"
        ).fetchone()[0]
    )

    assert first["policy_promotion_upgrade"]["state"] == "migrated"
    assert first["policy_promotion_upgrade"]["rows_preserved"] == 1
    assert second["policy_promotion_upgrade"]["state"] == "unchanged"
    assert "NO_LIVE_ACTIVATED" in table_sql
    assert conn.execute(
        "SELECT promotion_id,decision "
        "FROM basket_delta_v2_policy_promotion"
    ).fetchall() == [("old-promotion", "PREREGISTERED")]


def test_invalid_direct_promotion_write_is_not_silently_ignored() -> None:
    conn = _db()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        conn.execute(
            "INSERT INTO basket_delta_v2_policy_promotion VALUES"
            "(?,?,?,?,?,?,?,?)",
            (
                "bad-promotion",
                "artifact",
                1,
                "UNKNOWN",
                "gate",
                "2026-07-01T00:00:00Z",
                "a" * 64,
                "b" * 64,
            ),
        )


def test_preregistered_version_can_be_stored_but_not_activated() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=False)
    receipt = commit_delta_v2_authority(
        conn,
        artifact=artifact,
        promotion=_promotion(artifact, decision="PREREGISTERED"),
    )
    assert receipt["rows_written"] == 2
    assert receipt["activation_id"] is None
    assert receipt["orders_submitted"] == 0

    with pytest.raises(ContractError, match="requires_promotion"):
        commit_delta_v2_authority(
            conn,
            artifact=artifact,
            promotion=_promotion(artifact, decision="PREREGISTERED"),
            activation=_activation(artifact),
        )


def test_no_live_activation_runs_before_forward_oos() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=False)
    promotion = _promotion(
        artifact,
        decision="NO_LIVE_ACTIVATED",
    )
    activation = _activation(artifact)

    receipt = commit_delta_v2_authority(
        conn,
        artifact=artifact,
        promotion=promotion,
        activation=activation,
    )
    resolved = resolve_delta_v2_policy(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        strategy_version="target-v2",
        evaluation_at_utc="2026-07-28T00:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE",
    )

    assert receipt["rows_written"] == 3
    assert resolved.artifact_id == artifact["artifact_id"]
    assert resolved.oos_gate_passed is False
    assert receipt["live_submit_authorized"] is False


def test_promoted_oos_version_commits_resolves_and_replays_zero_write() -> None:
    conn = _db()
    artifact = _artifact(oos_passed=True)
    promotion = _promotion(artifact, decision="PROMOTED")
    activation = _activation(artifact)
    first = commit_delta_v2_authority(
        conn,
        artifact=artifact,
        promotion=promotion,
        activation=activation,
    )
    second = commit_delta_v2_authority(
        conn,
        artifact=copy.deepcopy(artifact),
        promotion=copy.deepcopy(promotion),
        activation=copy.deepcopy(activation),
    )
    resolved = resolve_delta_v2_policy(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d0",
        liquidity_bin="liquid",
        strategy_version="target-v2",
        evaluation_at_utc="2026-07-28T00:00:00Z",
    )

    assert first["rows_written"] == 3
    assert second["rows_written"] == 0
    assert second["state"] == "UNCHANGED_ZERO_WRITE"
    assert resolved.artifact_id == artifact["artifact_id"]
    assert resolved.value_weights.expected_pnl_scale == 1.0
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE basket_delta_v2_policy_artifact "
            "SET station_id='XXXX' WHERE artifact_id=?",
            (artifact["artifact_id"],),
        )


def test_promoted_receipt_cannot_lie_about_missing_oos() -> None:
    artifact = _artifact(oos_passed=False)
    with pytest.raises(ContractError, match="promotion_without_oos"):
        commit_delta_v2_authority(
            _db(),
            artifact=artifact,
            promotion=_promotion(artifact, decision="PROMOTED"),
        )


def test_oos_gate_requires_two_windows_and_all_four_baselines() -> None:
    raw = _artifact(oos_passed=True)
    raw["consecutive_forward_windows"] = 1
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="comparison_gate_incomplete"):
        DeltaV2PolicyArtifact.from_mapping(raw)

    raw = _artifact(oos_passed=True)
    raw["compared_baselines"] = ["v1", "EV_ONLY", "HOLD"]
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="comparison_gate_incomplete"):
        DeltaV2PolicyArtifact.from_mapping(raw)


def test_artifact_hash_scope_and_activation_drift_fail_closed() -> None:
    raw = _artifact(oos_passed=True)
    raw["price_grid"] = [0.05, 0.11, 0.2]
    with pytest.raises(ContractError, match="content_sha_mismatch"):
        DeltaV2PolicyArtifact.from_mapping(raw)

    artifact = _artifact(oos_passed=True)
    activation = _activation(artifact)
    activation["station_id"] = "XXXX"
    activation["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in activation.items()
            if key != "content_sha256"
        }
    )
    with pytest.raises(ContractError, match="scope_mismatch"):
        commit_delta_v2_authority(
            _db(),
            artifact=artifact,
            promotion=_promotion(artifact, decision="PROMOTED"),
            activation=activation,
        )
