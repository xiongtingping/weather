from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.artifact_ingress import (
    SCHEMA,
    ingest_artifact_bundle,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.policy import (
    GlobalBasketPriorArtifact,
    StationBasketPolicyActivation,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.build_bayes_basket_target_p1_no_live_activation import build


def _packet(*, priors: list[dict], activations: list[dict]) -> dict:
    return {
        "schema": SCHEMA,
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "execution_mode": "production_no_live",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "global_priors": priors,
        "policy_artifacts": [],
        "policy_activations": activations,
        "cash_phase_policies": [],
        "calibration_promotions": [],
        "policy_promotions": [],
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
    }


def test_p1_no_live_activation_is_distinct_global_fallback_and_idempotent(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id="test-global-prior",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {}, "cadence": {}},
        default_policy={"total_budget_usdc": 0.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        promoted=False,
    )
    p0 = StationBasketPolicyActivation.create(
        activation_id="rkpk-p0-source",
        station_id="RKPK",
        stage="P0",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        global_prior_id=prior.global_prior_id,
        target_artifact_id=None,
        reserve_artifact_id=None,
        cadence_artifact_id=None,
        fallback_families=["target", "reserve", "cadence"],
        generation=3,
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        activated_at_utc="2026-07-01T00:00:00Z",
        promoted=False,
    )
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        ingest_artifact_bundle(
            conn,
            _packet(priors=[prior.to_dict()], activations=[p0.to_dict()]),
            commit=True,
        )
    packet = build(db_path=db, station_id="RKPK")
    p1 = packet["policy_activations"][0]
    assert p1["stage"] == "P1"
    assert p1["activation_id"] != p0.activation_id
    assert p1["target_artifact_id"] is None
    assert p1["reserve_artifact_id"] is None
    assert p1["cadence_artifact_id"] is None
    assert p1["fallback_families"] == ["target", "reserve", "cadence"]
    assert packet["boundary"]["p0_policy_reused"] is False
    assert packet["boundary"]["p1_station_oos_claimed"] is False
    with sqlite3.connect(db) as conn:
        first = ingest_artifact_bundle(conn, packet, commit=True)
        replay = ingest_artifact_bundle(conn, packet, commit=True)
        assert first["counts"]["policy_activations"] == 1
        assert replay["counts"]["policy_activations"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_policy_activation WHERE stage='P1'"
        ).fetchone()[0] == 1


def test_p1_activation_same_identity_with_changed_content_is_rejected(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id="test-global-prior",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {}, "cadence": {}},
        default_policy={"total_budget_usdc": 0.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        promoted=False,
    )
    p0 = StationBasketPolicyActivation.create(
        activation_id="rkpk-p0-source",
        station_id="RKPK",
        stage="P0",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        global_prior_id=prior.global_prior_id,
        target_artifact_id=None,
        reserve_artifact_id=None,
        cadence_artifact_id=None,
        fallback_families=["target", "reserve", "cadence"],
        generation=3,
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        activated_at_utc="2026-07-01T00:00:00Z",
        promoted=False,
    )
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        ingest_artifact_bundle(
            conn,
            _packet(priors=[prior.to_dict()], activations=[p0.to_dict()]),
            commit=True,
        )
    packet = build(db_path=db, station_id="RKPK")
    with sqlite3.connect(db) as conn:
        ingest_artifact_bundle(conn, packet, commit=True)
        original = packet["policy_activations"][0]
        conflicting = StationBasketPolicyActivation.create(
            activation_id=original["activation_id"],
            station_id=original["station_id"],
            stage=original["stage"],
            season_bin=original["season_bin"],
            maturity_bin=original["maturity_bin"],
            liquidity_bin=original["liquidity_bin"],
            strategy_version=original["strategy_version"],
            global_prior_id=original["global_prior_id"],
            target_artifact_id=original["target_artifact_id"],
            reserve_artifact_id=original["reserve_artifact_id"],
            cadence_artifact_id=original["cadence_artifact_id"],
            fallback_families=original["fallback_families"],
            generation=original["generation"],
            valid_from_utc=original["valid_from_utc"],
            valid_until_utc="2026-07-31T00:00:00Z",
            activated_at_utc=original["activated_at_utc"],
            promoted=original["promoted"],
        )
        changed = {
            **packet,
            "policy_activations": [conflicting.to_dict()],
        }
        with pytest.raises(ContractError, match="content_conflict"):
            ingest_artifact_bundle(conn, changed, commit=True)


def test_p1_activation_refuses_ambiguous_p0_source_scope(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id="test-global-prior",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {}, "cadence": {}},
        default_policy={"total_budget_usdc": 0.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        promoted=False,
    )
    activations = [
        StationBasketPolicyActivation.create(
            activation_id=f"rkpk-p0-source-{generation}",
            station_id="RKPK",
            stage="P0",
            season_bin="summer",
            maturity_bin="d1",
            liquidity_bin="liquid",
            strategy_version="target-v1",
            global_prior_id=prior.global_prior_id,
            target_artifact_id=None,
            reserve_artifact_id=None,
            cadence_artifact_id=None,
            fallback_families=["target", "reserve", "cadence"],
            generation=generation,
            valid_from_utc="2026-07-01T00:00:00Z",
            valid_until_utc="2026-08-01T00:00:00Z",
            activated_at_utc="2026-07-01T00:00:00Z",
            promoted=False,
        )
        for generation in (1, 2)
    ]
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        ingest_artifact_bundle(
            conn,
            _packet(
                priors=[prior.to_dict()],
                activations=[item.to_dict() for item in activations],
            ),
            commit=True,
        )
    with pytest.raises(
        ValueError,
        match="p1_activation_source_p0_scope_not_unique",
    ):
        build(db_path=db, station_id="RKPK")


def test_p1_activation_selects_exact_probability_maturity(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id="test-global-prior",
        strategy_version="target-v1",
        family_parameters={"target": {}, "reserve": {}, "cadence": {}},
        default_policy={"total_budget_usdc": 0.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        promoted=False,
    )
    activations = [
        StationBasketPolicyActivation.create(
            activation_id=f"rkpk-p0-{maturity}",
            station_id="RKPK",
            stage="P0",
            season_bin="summer",
            maturity_bin=maturity,
            liquidity_bin="liquid",
            strategy_version="target-v1",
            global_prior_id=prior.global_prior_id,
            target_artifact_id=None,
            reserve_artifact_id=None,
            cadence_artifact_id=None,
            fallback_families=["target", "reserve", "cadence"],
            generation=1,
            valid_from_utc="2026-07-01T00:00:00Z",
            valid_until_utc="2026-08-01T00:00:00Z",
            activated_at_utc="2026-07-01T00:00:00Z",
            promoted=False,
        )
        for maturity in ("d1", "d2_fallback")
    ]
    with sqlite3.connect(db) as conn:
        apply_production_ssot_schema(conn)
        ingest_artifact_bundle(
            conn,
            _packet(
                priors=[prior.to_dict()],
                activations=[item.to_dict() for item in activations],
            ),
            commit=True,
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope("
            "envelope_id,stage,checkpoint_id,station_id,event_date,"
            "settlement_timezone,decision_at_utc,fresh_until_utc,"
            "bucket_ordinals_json,probabilities_json,posterior_version,"
            "posterior_sha256,bridge_sha256,lineage_json,content_sha256,"
            "state,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p1",
                "P1",
                "p1",
                "RKPK",
                "2026-07-27",
                "Asia/Seoul",
                "2026-07-26T20:00:00Z",
                "2026-07-26T22:00:00Z",
                '{"35°C":0}',
                '{"35°C":1.0}',
                "p1-test",
                "a" * 64,
                "b" * 64,
                '{"lead_bucket":"d2_fallback"}',
                "c" * 64,
                "COMMITTED",
                "2026-07-26T20:00:00Z",
            ),
        )
        conn.commit()

    packet = build(db_path=db, station_id="RKPK")

    assert packet["policy_activations"][0]["maturity_bin"] == "d2_fallback"
