from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from bayes_basket_target.calibration import (
    TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA,
    bind_taf_artifact_to_live_nwp_compatibility,
    build_taf_nwp_live_compatibility_fingerprint,
)
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.event_contracts import ForecastRevision
from bayes_basket_target.taf_revision import (
    TAF_ARTIFACT_SCHEMA,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
    TafRevisionArtifact,
    TafRevisionProof,
    compile_taf_revision_proof,
    validate_taf_revision_proof,
)
from tests.test_bayes_basket_target import _artifact, _ensemble, _envelope


CROSSFIT_SHA = "9" * 64
TRANSPORT_EVIDENCE_SHA = "8" * 64


def _live_fingerprint() -> dict:
    return build_taf_nwp_live_compatibility_fingerprint(
        ensemble_artifact=_ensemble(),
        calibration_artifacts={
            model: _artifact(model, 0.0)
            for model in ("ECMWF", "GFS", "ICON")
        },
        available_models=("ECMWF", "GFS", "ICON"),
    )


def _transport_evidence(
    live: dict,
    *,
    crossfit_sha256: str = CROSSFIT_SHA,
    validated: bool = True,
) -> dict:
    identity = {
        "schema": TAF_NWP_TRANSPORT_EVIDENCE_SCHEMA,
        "evidence_id": "taf-transport-oos-rkpk-v1",
        "crossfit_prediction_artifact_sha256": crossfit_sha256,
        "live_fingerprint_sha256": live["fingerprint_sha256"],
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "sample_n": 40,
        "effective_sample_n": 30.0,
        "baseline_oos_metrics": {
            "brier_score": 0.25,
            "nll": 0.60,
            "crps": 0.40,
        },
        "transport_oos_metrics": {
            "brier_score": 0.20,
            "nll": 0.55,
            "crps": 0.35,
        },
        "nll_not_worse": validated,
        "crps_not_worse": validated,
        "oos_transport_validated": validated,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _root(*, live: dict | None = None) -> ProbabilityEnvelope:
    live = live or _live_fingerprint()
    base = _envelope()
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {
            **dict(base.lineage),
            "bucket_centers_c_json": json.dumps(
                {"37": 37.0, "38": 38.0, "39": 39.0},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "ensemble_artifact_content_sha256": str(
                live["ensemble_artifact_content_sha256"]
            ),
            "taf_nwp_live_compatibility_fingerprint_sha256": str(
                live["fingerprint_sha256"]
            ),
            "taf_nwp_live_compatibility_json": json.dumps(
                live,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    })


def _taf_artifact(
    *,
    live_fingerprint_sha256: str | None = None,
    crossfit_sha256: str = CROSSFIT_SHA,
    bridge: bool = True,
) -> TafRevisionArtifact:
    live_fingerprint_sha256 = (
        live_fingerprint_sha256 or _live_fingerprint()["fingerprint_sha256"]
    )
    identity = {
        "schema": TAF_ARTIFACT_SCHEMA,
        "artifact_id": "taf-zuuu-live-bridge-v1",
        "activation_id": "taf-zuuu-live-bridge-activation-v1",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "America/New_York",
        "method_version": TAF_METHOD_VERSION,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "sample_n": 40,
        "effective_sample_n": 30.0,
        "oos_metrics": {
            "brier_score": 0.12,
            "nll": 0.4,
            "crps": 0.6,
        },
        "tx_bias_c": 0.0,
        "taf_nwp_delta_coefficient": 0.6,
        "tx_residual_scale_c": 0.8,
        "physical_feature_effect_c": {
            "cloud_cover_fraction": -0.5,
            "precipitation_probability": -0.5,
            "wind_speed_normalized": 0.0,
        },
        "wind_speed_cap_mps": 20.0,
        "confirmation_tolerance_c": 0.75,
        "max_confirmation_strength": 0.25,
        "max_correction_strength": 0.20,
        "max_probability_mass_transfer": 0.10,
        "max_center_displacement_buckets": 1,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "nwp_combination_artifact_sha256": crossfit_sha256,
        "oos_taf_nwp_residual_correlation": 0.25,
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": 0.75,
        "joint_physical_oos_validated": True,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activated": True,
    }
    if bridge:
        identity.update({
            "nwp_combination_artifact_kind": (
                TAF_NWP_CROSSFIT_ARTIFACT_KIND
            ),
            "nwp_live_compatibility_contract_version": (
                TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
            ),
            "nwp_live_compatibility_fingerprint_sha256": (
                live_fingerprint_sha256
            ),
            "nwp_live_compatibility_evidence_sha256": (
                TRANSPORT_EVIDENCE_SHA
            ),
            "nwp_live_compatibility_validated": True,
        })
    return TafRevisionArtifact.from_mapping({
        **identity,
        "content_sha256": content_sha256(identity),
    })


def _revision() -> ForecastRevision:
    return ForecastRevision.from_mapping({
        "revision_id": "taf-zuuu-1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-07-23T04:00:00Z",
        "received_at_utc": "2026-07-23T04:30:00Z",
        "valid_from_utc": "2026-07-23T04:00:00Z",
        "valid_until_utc": "2026-07-24T04:00:00Z",
        "source_object_sha256": "c" * 64,
        "supersedes_revision_id": None,
    })


def _compile(
    root: ProbabilityEnvelope,
    artifact: TafRevisionArtifact,
):
    return compile_taf_revision_proof(
        root,
        p0_root=root,
        forecast_revision=_revision(),
        artifact=artifact,
        bucket_centers_c={"37": 37.0, "38": 38.0, "39": 39.0},
        taf_fields={
            "tx_c": 38.0,
            "cloud_cover_fraction": 0.0,
            "precipitation_probability": 0.0,
            "wind_speed_mps": 2.0,
        },
        decision_at_utc="2026-07-23T05:00:00Z",
    )


def test_explicit_crossfit_to_exact_live_combination_bridge_passes() -> None:
    live = _live_fingerprint()
    root = _root(live=live)
    proof = _compile(
        root,
        _taf_artifact(
            live_fingerprint_sha256=live["fingerprint_sha256"]
        ),
    )

    assert proof.nwp_crossfit_prediction_artifact_sha256 == CROSSFIT_SHA
    assert proof.nwp_live_compatibility_fingerprint_sha256 == (
        live["fingerprint_sha256"]
    )
    assert proof.nwp_live_compatibility_evidence_sha256 == (
        TRANSPORT_EVIDENCE_SHA
    )
    assert CROSSFIT_SHA != live["ensemble_artifact_content_sha256"]


def test_content_addressed_transport_oos_evidence_builds_bridge() -> None:
    live = _live_fingerprint()
    bridged = bind_taf_artifact_to_live_nwp_compatibility(
        taf_artifact=_taf_artifact(bridge=False),
        live_fingerprint=live,
        transport_oos_evidence=_transport_evidence(live),
    )

    assert bridged.nwp_combination_artifact_sha256 == CROSSFIT_SHA
    assert bridged.nwp_live_compatibility_fingerprint_sha256 == (
        live["fingerprint_sha256"]
    )
    assert bridged.nwp_live_compatibility_evidence_sha256 == (
        _transport_evidence(live)["content_sha256"]
    )
    assert bridged.nwp_live_compatibility_validated is True


def test_transport_bridge_rejects_unproved_or_wrong_crossfit_mapping() -> None:
    live = _live_fingerprint()
    with pytest.raises(ContractError, match="oos_gate_failed"):
        bind_taf_artifact_to_live_nwp_compatibility(
            taf_artifact=_taf_artifact(bridge=False),
            live_fingerprint=live,
            transport_oos_evidence=_transport_evidence(
                live,
                validated=False,
            ),
        )
    with pytest.raises(ContractError, match="binding_mismatch"):
        bind_taf_artifact_to_live_nwp_compatibility(
            taf_artifact=_taf_artifact(bridge=False),
            live_fingerprint=live,
            transport_oos_evidence=_transport_evidence(
                live,
                crossfit_sha256="6" * 64,
            ),
        )


def test_legacy_crossfit_hash_without_transport_bridge_fails_closed() -> None:
    with pytest.raises(
        ContractError,
        match="nwp_live_compatibility_bridge_missing",
    ):
        _compile(_root(), _taf_artifact(bridge=False))


def test_bridge_for_different_live_combination_fails_closed() -> None:
    with pytest.raises(
        ContractError,
        match="nwp_live_compatibility_bridge_mismatch",
    ):
        _compile(
            _root(),
            _taf_artifact(live_fingerprint_sha256="6" * 64),
        )


def test_crossfit_hash_cannot_alias_live_ensemble_content_hash() -> None:
    live = _live_fingerprint()
    with pytest.raises(
        ContractError,
        match="crossfit_live_artifact_hash_alias_forbidden",
    ):
        _compile(
            _root(live=live),
            _taf_artifact(
                live_fingerprint_sha256=live["fingerprint_sha256"],
                crossfit_sha256=live[
                    "ensemble_artifact_content_sha256"
                ],
            ),
        )


def test_tampered_live_fingerprint_payload_fails_closed() -> None:
    live = _live_fingerprint()
    tampered = dict(live)
    tampered["resolved_weights"] = {
        "ECMWF": 0.8,
        "GFS": 0.1,
        "ICON": 0.1,
    }
    root = _root(live=tampered)
    with pytest.raises(
        ContractError,
        match="content_sha_mismatch",
    ):
        _compile(
            root,
            _taf_artifact(
                live_fingerprint_sha256=live["fingerprint_sha256"]
            ),
        )


def test_live_fingerprint_changes_with_exact_ensemble_or_calibration() -> None:
    ensemble = _ensemble()
    calibrations = {
        model: _artifact(model, 0.0)
        for model in ("ECMWF", "GFS", "ICON")
    }
    baseline = build_taf_nwp_live_compatibility_fingerprint(
        ensemble_artifact=ensemble,
        calibration_artifacts=calibrations,
        available_models=("ECMWF", "GFS", "ICON"),
    )
    changed_raw = asdict(calibrations["ECMWF"])
    changed_raw.update({
        "calibration_artifact_id": "cal-ECMWF-v2",
        "content_sha256": "d" * 64,
    })
    changed = build_taf_nwp_live_compatibility_fingerprint(
        ensemble_artifact=ensemble,
        calibration_artifacts={
            **calibrations,
            "ECMWF": type(calibrations["ECMWF"]).from_mapping(changed_raw),
        },
        available_models=("ECMWF", "GFS", "ICON"),
    )

    assert baseline["fingerprint_sha256"] != changed["fingerprint_sha256"]
    assert baseline["fingerprint_sha256"] != CROSSFIT_SHA


def test_rehashed_proof_cannot_rebind_to_another_live_combination() -> None:
    root = _root()
    artifact = _taf_artifact()
    proof = _compile(root, artifact)
    tampered = proof.to_dict()
    tampered["nwp_live_compatibility_fingerprint_sha256"] = "6" * 64
    identity = {
        key: value
        for key, value in tampered.items()
        if key != "content_sha256"
    }
    rewritten = TafRevisionProof.from_mapping({
        **identity,
        "content_sha256": content_sha256(identity),
    })

    with pytest.raises(
        ContractError,
        match="nwp_live_compatibility_binding_mismatch",
    ):
        validate_taf_revision_proof(
            rewritten,
            artifact=artifact,
            predecessor=root,
            p0_root=root,
        )
