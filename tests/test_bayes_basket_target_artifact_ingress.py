from __future__ import annotations

import sqlite3
from dataclasses import asdict, replace

import pytest

from bayes_basket_target import (
    BasketCashPhasePolicyArtifact,
    ContractError,
    PolicyArtifact,
    content_sha256,
    ingest_artifact_bundle,
    model_set_hash,
)
from bayes_basket_target.production_io import load_production_artifacts
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.cash_phase_authority import (
    CANDIDATE_AUTHORITY_SCOPE,
    FORMAL_AUTHORITY_SCOPE,
    CashPhaseAuthorityManifest,
)
from tests.test_bayes_basket_target import _cash_phase_inputs, _policy_inputs
from tests.test_bayes_basket_target import _ensemble


def _promotion(
    *,
    promotion_id: str,
    kind: str,
    artifact_id: str,
    seq: int = 1,
    gate_version: str = "gate-v1",
) -> dict[str, object]:
    return {
        "promotion_id": promotion_id,
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "decision_seq": seq,
        "decision": "PROMOTED",
        "gate_version": gate_version,
        "decided_at_utc": "2026-07-22T00:00:00Z",
        "receipt_sha256": "f" * 64,
    }


def _calibration_activation(
    *,
    activation_id: str,
    artifact_id: str,
    generation: int,
) -> dict[str, object]:
    identity: dict[str, object] = {
        "activation_id": activation_id,
        "artifact_kind": "MODEL_CALIBRATION",
        "artifact_id": artifact_id,
        "station_id": "RKPK",
        "model_id_or_star": "ECMWF",
        "season_bin": "summer",
        "lead_bucket": "d2_fallback",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_model_set_hash_or_star": "*",
        "generation": generation,
        "valid_from_utc": "2026-07-26T16:00:00Z",
        "valid_until_utc": "2026-08-25T16:00:00Z",
        "activated_at_utc": "2026-07-26T16:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _activation_only_packet(
    *activations: dict[str, object],
) -> dict[str, object]:
    stations = sorted({str(item["station_id"]) for item in activations})
    bindings = []
    for station in stations:
        identity = {
            "station_id": station,
            "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
            "source_lane": "AWC",
            "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "settlement_timezone": "Asia/Seoul",
            "reference_only": False,
            "dataset_path": f"/bounded/{station}/truth.json",
            "dataset_content_sha256": "d" * 64,
        }
        bindings.append({
            **identity,
            "content_sha256": content_sha256(identity),
        })
    return {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": list(activations),
        "calibration_promotions": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [],
        "truth_authority_bindings": bindings,
        "authority_receipt": {
            "schema": "bayes_basket_target_no_live_calibration_authority_v1",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "formal_promotion_created": False,
            "live_order_authorized": False,
        },
    }


def test_calibration_activation_generation_is_monotonic_and_replay_safe() -> None:
    first = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-1",
        generation=1,
    )
    second = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-2",
        artifact_id="rkpk-ecmwf-artifact-2",
        generation=2,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        committed = ingest_artifact_bundle(
            conn, _activation_only_packet(first), commit=True
        )
        replay = ingest_artifact_bundle(
            conn, _activation_only_packet(first), commit=True
        )
        advanced = ingest_artifact_bundle(
            conn, _activation_only_packet(second), commit=True
        )

        assert committed["counts"]["calibration_activations"] == 1
        assert replay["counts"]["calibration_activations"] == 0
        assert advanced["counts"]["calibration_activations"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_activation"
        ).fetchone()[0] == 2


def test_no_live_activation_cannot_bypass_truth_authority_binding() -> None:
    activation = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-1",
        generation=1,
    )
    packet = _activation_only_packet(activation)
    packet["truth_authority_bindings"] = []
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError, match="artifact_ingress_truth_binding_scope_mismatch"
        ):
            ingest_artifact_bundle(conn, packet, commit=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_activation"
        ).fetchone()[0] == 0


def test_calibration_activation_same_id_content_drift_fails_closed() -> None:
    first = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-1",
        generation=1,
    )
    changed = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-drift",
        generation=1,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        ingest_artifact_bundle(
            conn, _activation_only_packet(first), commit=True
        )
        with pytest.raises(
            ContractError,
            match="calibration_activation_content_conflict",
        ):
            ingest_artifact_bundle(
                conn, _activation_only_packet(changed), commit=True
            )
        assert conn.execute(
            "SELECT artifact_id FROM calibration_activation"
        ).fetchone()[0] == "rkpk-ecmwf-artifact-1"


def test_calibration_activation_business_scope_cannot_reuse_generation() -> None:
    first = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-1",
        generation=1,
    )
    conflicting = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1-drift",
        artifact_id="rkpk-ecmwf-artifact-drift",
        generation=1,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        ingest_artifact_bundle(
            conn, _activation_only_packet(first), commit=True
        )
        with pytest.raises(
            ContractError,
            match="calibration_activation_generation_not_unique_highest",
        ):
            ingest_artifact_bundle(
                conn, _activation_only_packet(conflicting), commit=True
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_activation"
        ).fetchone()[0] == 1


def test_calibration_activation_packet_conflict_rolls_back_all_rows() -> None:
    first = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-1",
        generation=1,
    )
    conflicting = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1-drift",
        artifact_id="rkpk-ecmwf-artifact-drift",
        generation=1,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="calibration_activation_generation_not_unique_highest",
        ):
            ingest_artifact_bundle(
                conn,
                _activation_only_packet(first, conflicting),
                commit=True,
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_activation"
        ).fetchone()[0] == 0


def test_artifact_ingress_can_stage_inside_caller_transaction(
    tmp_path,
) -> None:
    activation = _calibration_activation(
        activation_id="rkpk-ecmwf-generation-1",
        artifact_id="rkpk-ecmwf-artifact-1",
        generation=1,
    )
    database = tmp_path / "target.sqlite3"
    with sqlite3.connect(database, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        staged = ingest_artifact_bundle(
            conn,
            _activation_only_packet(activation),
            commit=True,
            finalize_transaction=False,
        )
        assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"
        assert conn.in_transaction is True
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_activation"
        ).fetchone()[0] == 1
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_activation"
        ).fetchone()[0] == 0


def test_artifact_ingress_is_append_only_promoted_and_idempotent() -> None:
    prior, policies, activations = _policy_inputs()
    packet = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "global_priors": [prior.to_dict()],
        "policy_artifacts": [item.to_dict() for item in policies],
        "policy_activations": [item.to_dict() for item in activations],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [
                _promotion(promotion_id="prom-prior", kind="GLOBAL_PRIOR", artifact_id=prior.global_prior_id),
                *[_promotion(promotion_id=f"prom-{item.artifact_id}", kind="STATION_POLICY", artifact_id=item.artifact_id) for item in policies],
                _promotion(promotion_id="prom-activation", kind="STATION_ACTIVATION", artifact_id=activations[0].activation_id),
        ],
        "calibration_promotions": [],
        "calibration_artifacts": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
    }
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        preview = ingest_artifact_bundle(conn, packet)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM basket_global_prior_artifact").fetchone()[0] == 0
        result = ingest_artifact_bundle(conn, packet, commit=True)
        assert result["state"] == "COMMITTED"
        assert result["counts"]["global_priors"] == 1
        bundle = load_production_artifacts(
            conn,
            station_id="ZUUU",
            evaluation_at_utc="2026-07-23T00:30:00Z",
            require_policy=False,
        )
        assert bundle.global_priors[prior.global_prior_id].promoted is True
        replay = ingest_artifact_bundle(conn, packet, commit=True)
        assert sum(replay["counts"].values()) == 0
        with pytest.raises(ContractError, match="content_conflict"):
            changed = PolicyArtifact.create(
                **{
                    **policies[0].to_dict(),
                    "theta_local": {"changed": 1.0},
                }
            )
            altered = {**packet, "policy_artifacts": [changed.to_dict(), *[item.to_dict() for item in policies[1:]]]}
            ingest_artifact_bundle(conn, altered, commit=True)


def test_governed_ensemble_selection_evidence_round_trip_and_tamper_gate() -> None:
    base = _ensemble()
    selected_weights = base.supported_masks[model_set_hash(base.candidate_models)]
    selection_identity = {
        "selection_evidence_id": "selection-round-trip",
        "station_id": base.station_id,
        "season_bin": base.season_bin,
        "lead_bucket": base.lead_bucket,
        "run_cycle": base.run_cycle,
        "settlement_definition": base.settlement_definition,
        "method_version": "rolling_origin_probability_stack_v4_five_model_pool_min2",
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-06-20",
        "recent_start_date": "2026-06-14",
        "recent_end_date": "2026-06-20",
        "holdout_start_date": "2026-06-21",
        "holdout_end_date": "2026-06-30",
        "candidate_scores": [{"models": list(base.candidate_models), "eligible": True}],
        "selected_models": list(base.candidate_models),
        "selected_weights": selected_weights,
        "oos_metrics": {"brier_score": 0.2, "nll": 0.5, "crps": 0.3},
        "residual_covariance": {},
        "residual_correlation": {},
        "effective_sample_n": 20.0,
        "previous_ensemble_artifact_sha256": None,
        "max_weight_delta": 0.2,
        "actual_max_weight_delta": 0.0,
    }
    selection = {
        **selection_identity,
        "content_sha256": content_sha256(selection_identity),
    }
    ensemble = replace(
        base, selection_evidence_sha256=selection["content_sha256"]
    )
    ensemble_identity = asdict(ensemble)
    ensemble_identity.pop("content_sha256")
    ensemble = replace(
        ensemble,
        content_sha256=content_sha256(ensemble_identity),
    )
    packet = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [selection],
        "ensemble_artifacts": [asdict(ensemble)],
        "calibration_activations": [],
        "calibration_promotions": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "policy_promotions": [],
    }
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        result = ingest_artifact_bundle(conn, packet, commit=True)
        assert result["counts"]["ensemble_selection_evidence"] == 1
        bundle = load_production_artifacts(
            conn, station_id="ZUUU",
            evaluation_at_utc="2026-07-23T00:30:00Z",
            require_policy=False,
        )
        assert ensemble.ensemble_artifact_id in bundle.ensemble_artifacts
        assert selection["content_sha256"] in bundle.ensemble_selection_evidence

        # A legacy row that predates the global 20+8 OOS floor can remain in
        # append-only history, but it has no activation and must not poison
        # loading a later compatible generation.  New ingress still rejects
        # the same row in the dedicated test below.
        columns = (
            "ensemble_artifact_id", "station_id", "season_bin",
            "lead_bucket", "run_cycle", "settlement_definition",
            "candidate_models_json", "supported_masks_json",
            "residual_covariance_json", "correlation_penalty",
            "valid_from_utc", "valid_until_utc", "content_sha256",
            "selection_evidence_sha256",
            "previous_ensemble_artifact_sha256", "weight_method",
            "effective_sample_n", "max_weight_delta", "train_start_date",
            "train_end_date", "recent_start_date", "recent_end_date",
            "holdout_start_date", "holdout_end_date", "oos_metrics_json",
        )
        source = conn.execute(
            "SELECT " + ",".join(columns)
            + " FROM calibration_ensemble_artifact "
            "WHERE ensemble_artifact_id=?",
            (ensemble.ensemble_artifact_id,),
        ).fetchone()
        legacy = list(source)
        legacy[0] = "legacy-short-window-unactivated"
        legacy[12] = "a" * 64
        legacy[16] = 19.0
        conn.execute(
            "INSERT INTO calibration_ensemble_artifact("
            + ",".join(columns)
            + ") VALUES(" + ",".join("?" for _ in columns) + ")",
            legacy,
        )
        replay_bundle = load_production_artifacts(
            conn,
            station_id="ZUUU",
            evaluation_at_utc="2026-07-23T00:30:00Z",
            require_policy=False,
        )
        assert "legacy-short-window-unactivated" in (
            replay_bundle.ensemble_artifacts
        )

        tampered = dict(selection)
        tampered["actual_max_weight_delta"] = 0.1
        bad_packet = {**packet, "ensemble_selection_evidence": [tampered]}
        with pytest.raises(ContractError, match="content_sha256_mismatch"):
            ingest_artifact_bundle(conn, bad_packet, commit=True)


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        (
            {"effective_sample_n": 19.0},
            "formal_ensemble_training_sample_insufficient",
        ),
        (
            {
                "holdout_start_date": "2026-06-24",
                "holdout_end_date": "2026-06-30",
            },
            "formal_ensemble_holdout_sample_insufficient",
        ),
    ),
)
def test_artifact_ingress_rejects_ensemble_below_global_oos_minimums(
    changes: dict[str, object],
    reason: str,
) -> None:
    invalid = replace(_ensemble(), **changes)
    packet = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [asdict(invalid)],
        "calibration_activations": [],
        "calibration_promotions": [],
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "policy_promotions": [],
    }
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match=reason):
            ingest_artifact_bundle(conn, packet, commit=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_ensemble_artifact"
        ).fetchone()[0] == 0


def _candidate_cash_packet() -> tuple[dict[str, object], BasketCashPhasePolicyArtifact]:
    cash_policy, _cash_input = _cash_phase_inputs()
    candidate = BasketCashPhasePolicyArtifact.create(
        **{
            **{
                key: value
                for key, value in cash_policy.to_dict().items()
                if key not in {"content_sha256", "promoted", "evidence_status"}
            },
            "evidence_status": "DESIGN_CANDIDATE",
            "promoted": False,
        }
    )
    manifest = CashPhaseAuthorityManifest.create(
        station_id=candidate.station_id,
        policy_version=candidate.policy_version,
        artifact_sha256=candidate.content_sha256,
        station_timezone=candidate.station_timezone,
        valid_from_utc=candidate.valid_from_utc,
        valid_until_utc=candidate.valid_until_utc,
        authority_scope=CANDIDATE_AUTHORITY_SCOPE,
    )
    packet: dict[str, object] = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "policy_authority_scope": CANDIDATE_AUTHORITY_SCOPE,
        "execution_mode": "production_no_live",
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [candidate.to_dict()],
        "cash_phase_authority_manifests": [manifest.to_dict()],
        "policy_promotions": [],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "calibration_promotions": [],
    }
    return packet, candidate


def test_candidate_cash_bundle_is_append_only_and_idempotent() -> None:
    packet, candidate = _candidate_cash_packet()
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        preview = ingest_artifact_bundle(conn, packet)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        committed = ingest_artifact_bundle(conn, packet, commit=True)
        assert committed["counts"]["cash_phase_policies"] == 1
        assert committed["counts"]["cash_phase_authority_manifests"] == 1
        replay = ingest_artifact_bundle(conn, packet, commit=True)
        assert sum(replay["counts"].values()) == 0
        stored = conn.execute(
            "SELECT content_sha256 FROM basket_cash_phase_policy_artifact "
            "WHERE policy_version=?",
            (candidate.policy_version,),
        ).fetchone()
        assert stored[0] == candidate.content_sha256


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "cash_phase_authority_manifests_required"),
        ("count", "cash_phase_authority_manifest_count_mismatch"),
        ("sha", "cash_phase_authority_manifest_artifact_mismatch"),
        ("timezone", "cash_phase_authority_manifest_timezone_mismatch"),
        ("window", "cash_phase_authority_manifest_validity_mismatch"),
        ("scope", "cash_phase_authority_manifest_scope_mismatch"),
    ],
)
def test_candidate_cash_authority_binding_fails_closed(
    mutation: str,
    reason: str,
) -> None:
    packet, policy = _candidate_cash_packet()
    if mutation == "missing":
        packet.pop("cash_phase_authority_manifests")
    elif mutation == "count":
        packet["cash_phase_authority_manifests"] = []
    else:
        values = dict(packet["cash_phase_authority_manifests"][0])  # type: ignore[index]
        if mutation == "sha":
            values["artifact_sha256"] = "f" * 64
        elif mutation == "timezone":
            values["station_timezone"] = "UTC"
        elif mutation == "window":
            values["valid_from_utc"] = "2026-01-01T00:00:00Z"
        else:
            values["authority_scope"] = FORMAL_AUTHORITY_SCOPE
        values.pop("manifest_sha256", None)
        packet["cash_phase_authority_manifests"] = [values]

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match=reason):
            ingest_artifact_bundle(conn, packet)
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_cash_phase_policy_artifact"
        ).fetchone()[0] == 0


def test_candidate_non_cash_bundle_does_not_require_empty_cash_manifest_field() -> None:
    prior, policies, activations = _policy_inputs()
    packet = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "policy_authority_scope": CANDIDATE_AUTHORITY_SCOPE,
        "execution_mode": "production_no_live",
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "global_priors": [prior.to_dict()],
        "policy_artifacts": [item.to_dict() for item in policies],
        "policy_activations": [item.to_dict() for item in activations],
        "cash_phase_policies": [],
        "policy_promotions": [],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "calibration_promotions": [],
    }

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        result = ingest_artifact_bundle(conn, packet)

    assert result["counts"]["cash_phase_policies"] == 0
    assert result["counts"]["cash_phase_authority_manifests"] == 0


def test_formal_cash_bundle_without_exact_manifest_fails_closed() -> None:
    prior, policies, activations = _policy_inputs()
    cash_policy, _cash_input = _cash_phase_inputs()
    cash_policy = BasketCashPhasePolicyArtifact.create(
        **{
            **{
                key: value
                for key, value in cash_policy.to_dict().items()
                if key not in {"content_sha256", "promoted", "evidence_status"}
            },
            "evidence_status": "PROMOTED",
            "promoted": True,
        }
    )
    packet = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "global_priors": [prior.to_dict()],
        "policy_artifacts": [item.to_dict() for item in policies],
        "policy_activations": [item.to_dict() for item in activations],
        "cash_phase_policies": [cash_policy.to_dict()],
        "policy_promotions": [
            _promotion(promotion_id="formal-prior", kind="GLOBAL_PRIOR", artifact_id=prior.global_prior_id),
            *[_promotion(promotion_id=f"formal-{item.artifact_id}", kind="STATION_POLICY", artifact_id=item.artifact_id) for item in policies],
            _promotion(promotion_id="formal-activation", kind="STATION_ACTIVATION", artifact_id=activations[0].activation_id),
        ],
        "calibration_promotions": [],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
    }
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="cash_phase_authority_manifests_required",
        ):
            ingest_artifact_bundle(conn, packet)


def test_formal_cash_bundle_with_manifest_still_requires_dedicated_oos_schema() -> None:
    cash_policy, _cash_input = _cash_phase_inputs()
    formal = BasketCashPhasePolicyArtifact.create(
        **{
            **{
                key: value
                for key, value in cash_policy.to_dict().items()
                if key not in {"content_sha256", "promoted", "evidence_status"}
            },
            "evidence_status": "PROMOTED",
            "promoted": True,
        }
    )
    manifest = CashPhaseAuthorityManifest.create(
        station_id=formal.station_id,
        policy_version=formal.policy_version,
        artifact_sha256=formal.content_sha256,
        station_timezone=formal.station_timezone,
        valid_from_utc=formal.valid_from_utc,
        valid_until_utc=formal.valid_until_utc,
        authority_scope=FORMAL_AUTHORITY_SCOPE,
    )
    packet = {
        "schema": "bayes_basket_target_artifact_ingress_v1",
        "global_priors": [],
        "policy_artifacts": [],
        "policy_activations": [],
        "cash_phase_policies": [formal.to_dict()],
        "cash_phase_authority_manifests": [manifest.to_dict()],
        "policy_promotions": [],
        "calibration_promotions": [],
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
    }
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="cash_phase_formal_oos_authority_schema_missing",
        ):
            ingest_artifact_bundle(conn, packet)
