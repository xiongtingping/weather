from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.strict_delta_artifact import (
    ACTIVATION_SCHEMA,
    ARTIFACT_SCHEMA,
    PROMOTION_SCHEMA,
    StrictDeltaPolicyArtifact,
    commit_strict_delta_authority,
    resolve_strict_delta_policy,
)


def _artifact_raw(*, gate_passed: bool = True) -> dict[str, object]:
    identity = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": "strict-delta-rkpk-oos-v1",
        "station_id": "RKPK",
        "stage": "P1",
        "season_bin": "summer",
        "maturity_bin": "d2_fallback",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "policy": {
            "schema": "strict_delta_policy_v1",
            "policy_version": "strict-delta-rkpk-oos-v1",
            "epsilon_score": 0.0001,
            "lambda_profit_floor": 0.0,
            "lambda_downside": 0.1,
            "lambda_shortfall": 0.0,
            "lambda_breakthrough": 0.0,
            "lambda_concentration": 0.1,
            "lambda_execution": 1.0,
            "expected_pnl_policy_floor": -10.0,
            "subsidy_absolute_cap": 0.0,
            "subsidy_ratio_cap": 0.0,
            "coverage_policy_oos_activated": False,
        },
        "training_evidence_sha256": "a" * 64,
        "train_start_date": "2026-05-01",
        "train_end_date": "2026-06-30",
        "holdout_start_date": "2026-07-01",
        "holdout_end_date": "2026-07-20",
        "sample_n": 60 if gate_passed else 0,
        "holdout_n": 20 if gate_passed else 0,
        "oos_metrics": ({
            "mean_delta_score": 0.04,
            "realized_expected_pnl_usdc": 1.2,
        } if gate_passed else {}),
        "oos_gate_version": "strict-delta-oos-gate-v1",
        "oos_gate_passed": gate_passed,
        "trained_through_utc": "2026-07-20T23:59:59Z",
        "valid_from_utc": "2026-07-21T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _seed_authority(
    conn: sqlite3.Connection,
    *,
    gate_passed: bool = True,
    decision: str = "PROMOTED",
) -> StrictDeltaPolicyArtifact:
    apply_production_ssot_schema(conn)
    artifact = StrictDeltaPolicyArtifact.from_mapping(
        _artifact_raw(gate_passed=gate_passed)
    )
    promotion_identity = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": "strict-delta-promotion-rkpk-v1",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": decision,
        "gate_version": artifact.oos_gate_version,
        "decided_at_utc": "2026-07-20T23:59:59Z",
        "receipt_sha256": "b" * 64,
    }
    promotion = {
        **promotion_identity,
        "content_sha256": content_sha256(promotion_identity),
    }
    activation_identity = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": "strict-delta-activation-rkpk-v1",
        "artifact_id": artifact.artifact_id,
        "station_id": artifact.station_id,
        "stage": artifact.stage,
        "season_bin": artifact.season_bin,
        "maturity_bin": artifact.maturity_bin,
        "liquidity_bin": artifact.liquidity_bin,
        "strategy_version": artifact.strategy_version,
        "generation": 1,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "activated_at_utc": artifact.valid_from_utc,
        "authority_scope": artifact.authority_scope,
    }
    activation = {
        **activation_identity,
        "content_sha256": content_sha256(activation_identity),
    }
    first = commit_strict_delta_authority(
        conn,
        artifact=artifact,
        promotion=promotion,
        activation=activation,
    )
    second = commit_strict_delta_authority(
        conn,
        artifact=artifact,
        promotion=promotion,
        activation=activation,
    )
    assert first["rows_written"] == 3
    assert second["state"] == "UNCHANGED_ZERO_WRITE"
    return artifact


def _resolve(conn: sqlite3.Connection) -> StrictDeltaPolicyArtifact:
    return resolve_strict_delta_policy(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d2_fallback",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        evaluation_at_utc="2026-07-27T02:00:00Z",
        required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
    )


def test_resolver_returns_only_promoted_oos_exact_scope_artifact() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        expected = _seed_authority(conn)
        actual = _resolve(conn)

    assert actual.artifact_id == expected.artifact_id
    assert actual.policy.policy_version == expected.artifact_id
    assert actual.oos_gate_passed is True


@pytest.mark.parametrize(
    ("gate_passed", "decision", "reason"),
    (
        (False, "PROMOTED", "strict_delta_oos_gate_not_passed"),
        (True, "REJECTED", "strict_delta_artifact_not_candidate_active"),
    ),
)
def test_resolver_fails_closed_without_both_oos_gate_and_promotion(
    gate_passed: bool,
    decision: str,
    reason: str,
) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        _seed_authority(
            conn,
            gate_passed=gate_passed,
            decision=decision,
        )
        with pytest.raises(ContractError, match=reason):
            _resolve(conn)


def test_preregistered_no_live_candidate_breaks_forward_oos_bootstrap_cycle() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        expected = _seed_authority(
            conn,
            gate_passed=False,
            decision="CANDIDATE",
        )
        actual = _resolve(conn)

    assert actual.artifact_id == expected.artifact_id
    assert actual.authority_scope == "PRODUCTION_NO_LIVE_CANDIDATE"
    assert actual.oos_gate_passed is False
    assert actual.sample_n == actual.holdout_n == 0
    assert actual.oos_metrics == {}


def test_preregistered_candidate_cannot_claim_fake_oos_counts() -> None:
    raw = _artifact_raw(gate_passed=False)
    raw["sample_n"] = 1
    raw["content_sha256"] = content_sha256({
        key: value for key, value in raw.items() if key != "content_sha256"
    })
    with pytest.raises(
        ContractError,
        match="candidate_must_not_claim_oos_evidence",
    ):
        StrictDeltaPolicyArtifact.from_mapping(raw)


def test_schema_upgrade_adds_candidate_state_without_relabelling_receipts() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE basket_strict_delta_policy_promotion("
            "promotion_id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL,"
            "decision_seq INTEGER NOT NULL CHECK(decision_seq>=1),"
            "decision TEXT NOT NULL CHECK(decision IN "
            "('PROMOTED','REJECTED','SUPERSEDED')),"
            "gate_version TEXT NOT NULL,decided_at_utc TEXT NOT NULL,"
            "receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),"
            "content_sha256 TEXT NOT NULL UNIQUE "
            "CHECK(length(content_sha256)=64),"
            "UNIQUE(artifact_id,decision_seq))"
        )
        conn.execute(
            "INSERT INTO basket_strict_delta_policy_promotion VALUES"
            "(?,?,?,?,?,?,?,?)",
            (
                "promotion-existing",
                "artifact-existing",
                1,
                "PROMOTED",
                "gate-v1",
                "2026-07-20T00:00:00Z",
                "a" * 64,
                "b" * 64,
            ),
        )

        result = apply_production_ssot_schema(conn)
        sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='basket_strict_delta_policy_promotion'"
            ).fetchone()[0]
        )
        preserved = conn.execute(
            "SELECT promotion_id,decision FROM "
            "basket_strict_delta_policy_promotion"
        ).fetchall()

    assert result["strict_delta_candidate_upgrade"]["state"] == "migrated"
    assert "'CANDIDATE'" in sql
    assert preserved == [("promotion-existing", "PROMOTED")]


def test_artifact_rejects_policy_version_or_content_drift() -> None:
    raw = _artifact_raw()
    raw["policy"] = {
        **raw["policy"],
        "policy_version": "different-inline-version",
    }
    raw["content_sha256"] = content_sha256({
        key: value for key, value in raw.items() if key != "content_sha256"
    })
    with pytest.raises(
        ContractError,
        match="strict_delta_policy_artifact_version_mismatch",
    ):
        StrictDeltaPolicyArtifact.from_mapping(raw)

    tampered = _artifact_raw()
    tampered["holdout_n"] = 999
    with pytest.raises(
        ContractError,
        match="strict_delta_artifact_content_sha_mismatch",
    ):
        StrictDeltaPolicyArtifact.from_mapping(tampered)
