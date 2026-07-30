import sqlite3
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from bayes_basket_target import (
    ProbabilityEnvelope,
    build_confirmed_inventory_snapshot,
    build_official_ws_snapshot,
    build_order_state_snapshot,
    ContractError,
    ForecastRevision,
    TAF_ARTIFACT_SCHEMA,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TafRevisionArtifact,
    compile_taf_revision_proof,
    content_sha256,
    insert_market_evidence_bundle,
    insert_probability_envelope,
    materialize_p0m,
    materialize_p1,
)
from bayes_basket_target.ssot import apply_production_ssot_schema, production_ssot_schema_status
from bayes_basket_target.calibration import (
    build_taf_nwp_live_compatibility_fingerprint,
)
from bayes_basket_target.taf_revision import (
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
)
from bayes_basket_target.p1_compiler import (
    compile_station_p1_packets,
    materialization_packet,
)
from bayes_basket_target.production_io import target_atomic_facts_from_cycle

from tests.test_bayes_basket_target import (
    _activation,
    _artifact,
    _cash_phase_inputs,
    _ensemble,
    _envelope as _base_envelope,
    _rehash_ensemble,
    _runs,
)


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "materialize_bayes_basket_target_probability.py"
_SPEC = importlib.util.spec_from_file_location("target_probability_materializer", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_CLI = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CLI)


def _taf_live_fingerprint() -> dict:
    return build_taf_nwp_live_compatibility_fingerprint(
        ensemble_artifact=_ensemble(),
        calibration_artifacts={
            model: _artifact(model, 0.0)
            for model in ("ECMWF", "GFS", "ICON")
        },
        available_models=("ECMWF", "GFS", "ICON"),
    )


def _envelope() -> ProbabilityEnvelope:
    base = _base_envelope()
    live = _taf_live_fingerprint()
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": {
            **dict(base.lineage),
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


def _lineage(*, event_id: str = "obs-1") -> dict[str, str]:
    return {
        "canonical_observation_event_id": event_id,
        "canonical_observation_sha256": "a" * 64,
        "hard_bound_id": "hard-1",
    }


def _trajectory_packet(
    root: ProbabilityEnvelope,
    *,
    event_id: str = "obs-1",
    temperature_c: float = 38.2,
    decision_at_utc: str = "2026-07-23T05:30:00Z",
) -> dict:
    registry = [
        {
            "station_id": root.station_id,
            "event_date": root.event_date,
            "settlement_timezone": root.settlement_timezone,
            "settlement_unit": "C",
            "outcome": bucket,
            "registry_json": json.dumps({"upper": upper}),
        }
        for bucket, upper in (("37", 37), ("38", 38), ("39", None))
    ]
    return compile_station_p1_packets(
        predecessor=root,
        observations=[{
            "observation_id": event_id,
            "station_id": root.station_id,
            "event_date_local": root.event_date,
            "reported_temperature_c": temperature_c,
            "valid_at_utc": decision_at_utc,
            "canonical_committed_at_utc": decision_at_utc,
            "raw_sha256": (
                ("a" if event_id == "obs-1" else "c") * 64
            ),
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        }],
        registry_rows=registry,
        compilation_at_utc=decision_at_utc,
    )[0]


def _taf_artifact(**overrides) -> TafRevisionArtifact:
    identity = {
        "schema": TAF_ARTIFACT_SCHEMA,
        "artifact_id": "taf-artifact-1",
        "activation_id": "taf-activation-1",
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "00Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": _envelope().settlement_timezone,
        "method_version": TAF_METHOD_VERSION,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "sample_n": 100,
        "effective_sample_n": 80.0,
        "oos_metrics": {"brier_score": 0.12, "nll": 0.4, "crps": 0.6},
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
        "nwp_combination_artifact_sha256": "9" * 64,
        "nwp_combination_artifact_kind": (
            TAF_NWP_CROSSFIT_ARTIFACT_KIND
        ),
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": (
            _taf_live_fingerprint()["fingerprint_sha256"]
        ),
        "nwp_live_compatibility_evidence_sha256": "8" * 64,
        "nwp_live_compatibility_validated": True,
        "oos_taf_nwp_residual_correlation": 0.25,
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": 0.75,
        "joint_physical_oos_validated": True,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activated": True,
    }
    identity.update(overrides)
    return TafRevisionArtifact.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    )


def _forecast_revision(
    *,
    revision_id: str = "taf-1",
    kind: str = "TAF",
    sequence: int = 1,
    supersedes: str | None = None,
    source_sha: str = "c" * 64,
    received_at: str = "2026-07-23T04:30:00Z",
) -> ForecastRevision:
    return ForecastRevision.from_mapping(
        {
            "revision_id": revision_id,
            "station_id": "ZUU",
            "event_date": "2026-07-23",
            "revision_kind": kind,
            "sequence": sequence,
            "issued_at_utc": "2026-07-23T04:00:00Z",
            "received_at_utc": received_at,
            "valid_from_utc": "2026-07-23T04:00:00Z",
            "valid_until_utc": "2026-07-24T04:00:00Z",
            "source_object_sha256": source_sha,
            "supersedes_revision_id": supersedes,
        }
    )


def _taf_proof(
    predecessor: ProbabilityEnvelope,
    *,
    p0_root: ProbabilityEnvelope,
    revision: ForecastRevision,
    artifact: TafRevisionArtifact,
    tx_c: float = 38.0,
    decision_at: str = "2026-07-23T05:00:00Z",
):
    # Test envelopes use a synthetic station id while their bucket labels are
    # real Celsius centers.
    revision = ForecastRevision.from_mapping(
        {**revision.to_dict(), "station_id": p0_root.station_id}
    )
    return compile_taf_revision_proof(
        predecessor,
        p0_root=p0_root,
        forecast_revision=revision,
        artifact=artifact,
        bucket_centers_c={"37": 37.0, "38": 38.0, "39": 39.0},
        taf_fields={
            "tx_c": tx_c,
            "cloud_cover_fraction": 0.0,
            "precipitation_probability": 0.0,
            "wind_speed_mps": 2.0,
        },
        decision_at_utc=decision_at,
    )


def test_production_schema_accepts_p0m_revision_stage() -> None:
    with sqlite3.connect(":memory:") as conn:
        result = apply_production_ssot_schema(conn)
        assert result["ok"] is True
        assert production_ssot_schema_status(conn)["probability_stage_contract_ok"] is True
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='basket_probability_envelope'"
        ).fetchone()[0]
        assert "'P0-M'" in sql


def test_cli_p0m_packet_uses_strict_asof_model_materializer() -> None:
    artifacts = {model: _artifact(model, 0.0) for model in ("ECMWF", "GFS", "ICON")}
    ensemble = _rehash_ensemble(_ensemble(), promoted=False)
    # The ensemble activation must match the exact three-model mask.
    from bayes_basket_target import model_set_hash

    activations = [
        _activation(
            kind="MODEL_ENSEMBLE",
            artifact_id="ens-1",
            mask=model_set_hash(("ECMWF", "GFS", "ICON")),
        ),
        *[
            _activation(kind="MODEL_CALIBRATION", artifact_id=f"cal-{model}", model=model)
            for model in ("ECMWF", "GFS", "ICON")
        ],
    ]
    packet = {
        "stage": "P0",
        "current_model_runs": [asdict(row) for row in _runs()],
        "diagnostic_model_runs": [asdict(row) for row in _runs()],
        "calibration_artifacts": {
            model: {**asdict(row), "promoted": False}
            for model, row in artifacts.items()
        },
        "diagnostic_calibration_artifacts": {
            model: {**asdict(row), "promoted": False}
            for model, row in artifacts.items()
        },
        "ensemble_artifact": asdict(ensemble),
        "calibration_activations": [
            {**asdict(row), "authority_scope": "PRODUCTION_NO_LIVE_ONLY"}
            for row in activations
        ],
        "bucket_intervals": {"37": [None, 38.0], "38": [38.0, 39.0], "39": [39.0, None]},
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "decision_at_utc": "2026-07-23T04:00:00Z",
        "fresh_until_utc": "2026-07-23T05:00:00Z",
        "bridge_sha256": "b" * 64,
    }
    envelope = _CLI.build_envelope(packet)
    assert envelope.stage == "P0"
    assert abs(sum(envelope.probabilities.values()) - 1.0) < 1e-9


def test_direct_cli_p0_materialization_is_forbidden(
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "bare-p0.json"
    packet_path.write_text(
        json.dumps({"stage": "P0"}),
        encoding="utf-8",
    )

    with pytest.raises(
        ContractError,
        match="direct_p0_materialization_forbidden_use_resident_ingress",
    ):
        _CLI.run(
            db=tmp_path / "target.db",
            packet_path=packet_path,
            apply=False,
            confirm="",
        )


def test_p0m_first_and_replacement_are_weak_only_and_asof_ordered() -> None:
    p0 = _envelope()
    artifact = _taf_artifact(station_id=p0.station_id)
    first_revision = _forecast_revision()
    first_proof = _taf_proof(
        p0,
        p0_root=p0,
        revision=first_revision,
        artifact=artifact,
    )
    first = materialize_p0m(
        p0,
        p0_root=p0,
        taf_revision_artifact=artifact,
        taf_revision_proof=first_proof,
        decision_at_utc="2026-07-23T05:00:00Z",
        fresh_until_utc="2026-07-23T06:00:00Z",
        bridge_sha256="b" * 64,
    )
    assert first.stage == "P0-M"
    assert first.lineage["taf_hard_exclusion"] == "FORBIDDEN"
    assert first.lineage["taf_tx_calibrated_center_c"] == (
        f"{first_proof.tx_calibrated_center_c:.17g}"
    )
    assert first.lineage["taf_tx_c"] == f"{first_proof.taf_tx_c:.17g}"
    assert first.lineage["taf_physical_features_sha256"] == (
        first_proof.physical_features_sha256
    )
    assert first.lineage["taf_cloud_cover_fraction"] == (
        f"{first_proof.physical_features['cloud_cover_fraction']:.17g}"
    )
    assert first.lineage["taf_precipitation_probability"] == (
        f"{first_proof.physical_features['precipitation_probability']:.17g}"
    )
    assert first.lineage["taf_wind_speed_normalized"] == (
        f"{first_proof.physical_features['wind_speed_normalized']:.17g}"
    )
    assert first.lineage["taf_physically_confirmed_center_c"] == (
        f"{first_proof.physically_confirmed_center_c:.17g}"
    )
    assert first.lineage["taf_physical_adjustment_c"] == (
        f"{first_proof.physical_adjustment_c:.17g}"
    )
    assert first.lineage["taf_physical_confirmation_state"] == (
        first_proof.physical_confirmation_state
    )
    assert first.lineage["taf_physical_consistency_factor"] == (
        f"{first_proof.physical_consistency_factor:.17g}"
    )
    replacement_revision = _forecast_revision(
        revision_id="taf-2",
        kind="AMD",
        sequence=2,
        supersedes="taf-1",
        source_sha="d" * 64,
        received_at="2026-07-23T05:15:00Z",
    )
    replacement_proof = _taf_proof(
        first,
        p0_root=p0,
        revision=replacement_revision,
        artifact=artifact,
        tx_c=39.0,
        decision_at="2026-07-23T05:30:00Z",
    )
    replacement = materialize_p0m(
        first,
        revision=True,
        p0_root=p0,
        taf_revision_artifact=artifact,
        taf_revision_proof=replacement_proof,
        decision_at_utc="2026-07-23T05:30:00Z",
        fresh_until_utc="2026-07-23T07:00:00Z",
        bridge_sha256="b" * 64,
    )
    assert replacement.stage == "P0-M"
    assert replacement.probabilities["39"] > first.probabilities["39"]
    assert replacement.probabilities == replacement_proof.posterior_probabilities
    assert replacement.lineage["revision_mode"] == "root_replacement"
    with pytest.raises(ContractError, match="decision_time_rewind"):
        materialize_p0m(
            p0,
            p0_root=p0,
            taf_revision_artifact=artifact,
            taf_revision_proof=first_proof,
            decision_at_utc="2026-07-23T03:00:00Z",
            fresh_until_utc="2026-07-23T05:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_taf_low_high_low_revisions_replace_from_one_p0_root_without_accumulation() -> None:
    p0 = _envelope()
    artifact = _taf_artifact(station_id=p0.station_id)
    revisions = (
        _forecast_revision(revision_id="taf-low-1"),
        _forecast_revision(
            revision_id="taf-high-2",
            kind="AMD",
            sequence=2,
            supersedes="taf-low-1",
            source_sha="d" * 64,
            received_at="2026-07-23T05:10:00Z",
        ),
        _forecast_revision(
            revision_id="taf-low-3",
            kind="COR",
            sequence=3,
            supersedes="taf-high-2",
            source_sha="e" * 64,
            received_at="2026-07-23T05:40:00Z",
        ),
    )
    decisions = (
        "2026-07-23T05:00:00Z",
        "2026-07-23T05:30:00Z",
        "2026-07-23T06:00:00Z",
    )
    tx_values = (37.0, 39.0, 37.0)
    predecessor = p0
    envelopes = []
    for index, (revision, decision, tx_c) in enumerate(
        zip(revisions, decisions, tx_values)
    ):
        proof = _taf_proof(
            predecessor,
            p0_root=p0,
            revision=revision,
            artifact=artifact,
            tx_c=tx_c,
            decision_at=decision,
        )
        predecessor = materialize_p0m(
            predecessor,
            p0_root=p0,
            taf_revision_artifact=artifact,
            taf_revision_proof=proof,
            decision_at_utc=decision,
            fresh_until_utc="2026-07-23T07:00:00Z",
            bridge_sha256="b" * 64,
            revision=index > 0,
        )
        envelopes.append(predecessor)

    assert envelopes[0].probabilities == envelopes[2].probabilities
    assert envelopes[1].probabilities["39"] > envelopes[0].probabilities["39"]
    assert all(
        envelope.lineage["p0_root_content_sha256"] == p0.content_sha256
        for envelope in envelopes
    )


def test_taf_physical_conflict_discounts_correction_and_caps_mass_without_zeroing() -> None:
    p0 = _envelope()
    artifact = _taf_artifact(
        station_id=p0.station_id,
        physical_feature_effect_c={
            "cloud_cover_fraction": -0.1,
            "precipitation_probability": -0.1,
            "wind_speed_normalized": 0.0,
        },
        max_probability_mass_transfer=0.01,
    )
    revision = ForecastRevision.from_mapping(
        {
            **_forecast_revision(revision_id="taf-physical").to_dict(),
            "station_id": p0.station_id,
        }
    )
    clear = compile_taf_revision_proof(
        p0,
        p0_root=p0,
        forecast_revision=revision,
        artifact=artifact,
        bucket_centers_c={"37": 37.0, "38": 38.0, "39": 39.0},
        taf_fields={
            "tx_c": 50.0,
            "cloud_cover_fraction": 0.0,
            "precipitation_probability": 0.0,
            "wind_speed_mps": 2.0,
        },
        decision_at_utc="2026-07-23T05:00:00Z",
    )
    conflicting = compile_taf_revision_proof(
        p0,
        p0_root=p0,
        forecast_revision=revision,
        artifact=artifact,
        bucket_centers_c={"37": 37.0, "38": 38.0, "39": 39.0},
        taf_fields={
            "tx_c": 50.0,
            "cloud_cover_fraction": 1.0,
            "precipitation_probability": 1.0,
            "wind_speed_mps": 2.0,
        },
        decision_at_utc="2026-07-23T05:00:00Z",
    )
    assert clear.mode == conflicting.mode == "CORRECTION"
    assert clear.physical_confirmation_state == "NEUTRAL"
    assert conflicting.physical_confirmation_state == "CONFLICTS"
    assert conflicting.physical_consistency_factor == 0.5
    assert conflicting.applied_strength <= clear.applied_strength
    for proof in (clear, conflicting):
        assert proof.probability_mass_transfer <= 0.01 + 1e-12
        assert all(probability > 0.0 for probability in proof.posterior_probabilities.values())
        assert all(
            artifact.likelihood_ratio_floor <= ratio <= artifact.likelihood_ratio_ceiling
            for ratio in proof.likelihood_ratio_by_bucket.values()
        )


def test_taf_matching_p0m_is_bounded_confirmation_not_a_second_center() -> None:
    p0 = _envelope()
    base_mean = sum(
        p0.probabilities[bucket] * center
        for bucket, center in {"37": 37.0, "38": 38.0, "39": 39.0}.items()
    )
    artifact = _taf_artifact(
        station_id=p0.station_id,
        tx_bias_c=0.0,
        physical_feature_effect_c={
            "cloud_cover_fraction": 0.0,
            "precipitation_probability": 0.0,
            "wind_speed_normalized": 0.0,
        },
    )
    proof = _taf_proof(
        p0,
        p0_root=p0,
        revision=_forecast_revision(revision_id="taf-confirmation"),
        artifact=artifact,
        tx_c=base_mean,
    )
    assert proof.mode == "CONFIRMATION"
    assert proof.tx_calibrated_center_c == pytest.approx(base_mean)
    assert proof.physically_confirmed_center_c == pytest.approx(base_mean)
    assert proof.physical_confirmation_state == "NEUTRAL"
    assert proof.center_displacement_buckets == 0
    assert proof.probability_mass_transfer <= artifact.max_probability_mass_transfer
    assert all(value > 0.0 for value in proof.posterior_probabilities.values())


def test_taf_fails_closed_on_timezone_or_hard_exclusion_tampering() -> None:
    p0 = _envelope()
    wrong_timezone = _taf_artifact(
        station_id=p0.station_id,
        settlement_timezone="Asia/Seoul",
    )
    with pytest.raises(ContractError, match="settlement_timezone_mismatch"):
        _taf_proof(
            p0,
            p0_root=p0,
            revision=_forecast_revision(revision_id="taf-wrong-zone"),
            artifact=wrong_timezone,
        )

    artifact = _taf_artifact(station_id=p0.station_id)
    proof = _taf_proof(
        p0,
        p0_root=p0,
        revision=_forecast_revision(revision_id="taf-no-hard-bound"),
        artifact=artifact,
    )
    tampered = {
        **proof.to_dict(include_content_sha256=False),
        "hard_excluded_buckets": ["37"],
    }
    tampered["content_sha256"] = content_sha256(tampered)
    from bayes_basket_target import TafRevisionProof

    with pytest.raises(ContractError, match="hard_exclusion_forbidden"):
        TafRevisionProof.from_mapping(tampered)

    dishonest_mass = {
        **proof.to_dict(include_content_sha256=False),
        "probability_mass_transfer": 0.0,
    }
    dishonest_mass["content_sha256"] = content_sha256(dishonest_mass)
    with pytest.raises(ContractError, match="mass_transfer_proof_mismatch"):
        materialize_p0m(
            p0,
            p0_root=p0,
            taf_revision_artifact=artifact,
            taf_revision_proof=dishonest_mass,
            decision_at_utc="2026-07-23T05:00:00Z",
            fresh_until_utc="2026-07-23T06:00:00Z",
            bridge_sha256="b" * 64,
        )

    dishonest_center = {
        **proof.to_dict(include_content_sha256=False),
        "center_after": "37" if proof.center_after != "37" else "39",
    }
    dishonest_center["content_sha256"] = content_sha256(dishonest_center)
    with pytest.raises(ContractError, match="center_proof_mismatch"):
        materialize_p0m(
            p0,
            p0_root=p0,
            taf_revision_artifact=artifact,
            taf_revision_proof=dishonest_center,
            decision_at_utc="2026-07-23T05:00:00Z",
            fresh_until_utc="2026-07-23T06:00:00Z",
            bridge_sha256="b" * 64,
        )

    dishonest_physical_confirmation = {
        **proof.to_dict(include_content_sha256=False),
        "physically_confirmed_center_c":
            proof.physically_confirmed_center_c + 0.25,
    }
    dishonest_physical_confirmation["content_sha256"] = content_sha256(
        dishonest_physical_confirmation
    )
    with pytest.raises(
        ContractError,
        match="physical_second_confirmation_arithmetic_invalid",
    ):
        materialize_p0m(
            p0,
            p0_root=p0,
            taf_revision_artifact=artifact,
            taf_revision_proof=dishonest_physical_confirmation,
            decision_at_utc="2026-07-23T05:00:00Z",
            fresh_until_utc="2026-07-23T06:00:00Z",
            bridge_sha256="b" * 64,
        )


def test_peripheral_model_reliability_lineage_survives_p0m_and_p1() -> None:
    reliability = {"37": 0.0, "38": 0.8, "39": 0.2}
    p0 = ProbabilityEnvelope.from_mapping(
        {
            **_envelope().to_dict(),
            "checkpoint_id": "p0m-reliability-lineage",
            "lineage": {
                **_envelope().lineage,
                "peripheral_model_reliability_by_bucket_json": json.dumps(
                    reliability,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        }
    )
    artifact = _taf_artifact(station_id=p0.station_id)
    proof = _taf_proof(
        p0,
        p0_root=p0,
        revision=_forecast_revision(revision_id="taf-reliability"),
        artifact=artifact,
    )
    p0m = materialize_p0m(
        p0,
        p0_root=p0,
        taf_revision_artifact=artifact,
        taf_revision_proof=proof,
        decision_at_utc="2026-07-23T05:00:00Z",
        fresh_until_utc="2026-07-23T06:00:00Z",
        bridge_sha256="b" * 64,
    )
    p1 = ProbabilityEnvelope.from_mapping(
        _trajectory_packet(
            p0m, temperature_c=37.0
        )["expected_envelope"]
    )

    key = "peripheral_model_reliability_by_bucket_json"
    assert json.loads(p0m.lineage[key]) == reliability
    assert json.loads(p1.lineage[key]) == reliability


def test_p1_consumes_canonical_event_once_and_applies_hard_support() -> None:
    p0 = _envelope()
    p1 = ProbabilityEnvelope.from_mapping(
        _trajectory_packet(p0)["expected_envelope"]
    )
    assert p1.stage == "P1"
    assert p1.probabilities["37"] == 0.0
    assert abs(sum(p1.probabilities.values()) - 1.0) < 1e-9
    assert p1.lineage["canonical_observation_event_id"] == "obs-1"
    assert p1.lineage["canonical_observation_sha256"] == "a" * 64
    assert (
        p1.lineage["canonical_observation_committed_at"]
        == "2026-07-23T05:30:00.000000Z"
    )
    assert p1.lineage["source_lane"] == "AWC"
    assert p1.lineage["report_type"] == "METAR"
    assert p1.lineage["metar_trajectory_direction_role"] == (
        "DIAGNOSTIC_ONLY_BASKET_RECOMPUTES_POLICY_DIRECTION"
    )
    assert p1.lineage["metar_trajectory_positive_core_role"] == (
        "DIAGNOSTIC_POSITIVE_PROBABILITY_SUPPORT_NOT_BASKET_CORE"
    )
    facts = target_atomic_facts_from_cycle(
        {"probability_envelope": p1.to_dict()}
    )
    assert facts["observation"] == {
        "observation_id": "obs-1",
        "station_id": p1.station_id,
        "event_date": p1.event_date,
        "source_lane": "AWC",
        "report_type": "METAR",
        "raw_sha256": "a" * 64,
        "canonical_committed_at": "2026-07-23T05:30:00.000000Z",
    }
    assert facts["bound"]["bound_id"] == p1.lineage["hard_bound_id"]

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        p0_row = insert_probability_envelope(
            conn, p0, created_at_utc="2026-07-23T04:01:00Z"
        )
        p1_row = insert_probability_envelope(
            conn, p1, created_at_utc="2026-07-23T05:01:00Z"
        )
        assert p0_row["wrote"] is True
        assert p1_row["wrote"] is True
        assert insert_probability_envelope(
            conn, p1, created_at_utc="2026-07-23T05:02:00Z"
        )["wrote"] is False

        altered = ProbabilityEnvelope.from_mapping(
            _trajectory_packet(
                p0,
                temperature_c=39.0,
                decision_at_utc="2026-07-23T05:35:00Z",
            )["expected_envelope"]
        )
        with pytest.raises(ContractError, match="already_consumed"):
            insert_probability_envelope(
                conn, altered, created_at_utc="2026-07-23T05:06:00Z"
            )


def test_p1_rejects_legacy_raw_likelihood_boundary() -> None:
    packet = {
        "stage": "P1",
        "predecessor": _envelope().to_dict(),
        "likelihood_by_bucket": {"37": 1.0, "38": 1.0, "39": 1.0},
        "hard_excluded_buckets": [],
    }
    with pytest.raises(ContractError, match="unknown_fields"):
        _CLI.build_envelope(packet)


def test_p1_with_no_hard_exclusions_records_explicit_none_lineage() -> None:
    p0 = _envelope()
    p1 = ProbabilityEnvelope.from_mapping(
        _trajectory_packet(
            p0, temperature_c=37.0
        )["expected_envelope"]
    )
    assert p1.lineage["hard_excluded_buckets"] == "NONE"


def test_cli_packet_boundary_rejects_unknown_fields_and_builds_no_live_p1() -> None:
    p0 = _envelope().to_dict()
    rich = _trajectory_packet(_envelope())
    packet = materialization_packet(rich)
    assert set(packet) <= _CLI.ALLOWED_KEYS
    envelope = _CLI.build_envelope(packet)
    assert envelope.stage == "P1"
    assert envelope.probabilities["37"] == 0.0
    with pytest.raises(ContractError, match="unknown_fields"):
        _CLI.build_envelope({**packet, "raw_metar": {"temperature": 38.0}})
    with pytest.raises(ContractError, match="unknown_fields"):
        _CLI.build_envelope(
            {
                "stage": "P0-M",
                "predecessor": p0,
                "likelihood_ratio_by_bucket": {
                    bucket: 1.0 for bucket in p0["probabilities"]
                },
            }
        )


def test_official_ws_inventory_order_state_bundle_is_atomic_and_monotone() -> None:
    _policy, phase_input = _cash_phase_inputs()
    buckets = [
        {"bucket": bucket, "ordinal": ordinal, "ask_levels": [{"price": 0.30, "shares": 10.0}]}
        for bucket, ordinal in (("37", 37), ("38", 38), ("39", 39))
    ]
    quote = {
        "snapshot_id": "quote-1",
        "market_id": "market-1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "sequence": 1,
        "observed_at": "2026-07-23T04:00:00Z",
        "quote_version": "q-1",
        "quote_observed_at": "2026-07-23T04:00:00Z",
        "quote_expires_at": "2026-07-23T04:00:05Z",
        "quote_freshness_policy_sha256": "a" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inv-1",
        "order_state_version": "ord-1",
        "remaining_budget_usdc": 12.0,
        "buckets": buckets,
        "cash_phase_input": phase_input.to_dict(),
    }
    inventory = {
        "inventory_snapshot_id": "inv-snapshot-1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "inventory_version": "inv-1",
        "observed_at": "2026-07-23T04:00:00Z",
        "complete": 1,
        "inventory_json": {"filled_by_bucket": {bucket["bucket"]: 0.0 for bucket in buckets}},
    }
    order_state = {
        "order_state_snapshot_id": "ord-snapshot-1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
            "settlement_timezone": "Asia/Shanghai",
        "order_state_version": "ord-1",
        "observed_at": "2026-07-23T04:00:00Z",
        "known": 1,
        "order_state_json": {"pending_by_bucket": {bucket["bucket"]: 0.0 for bucket in buckets}},
    }
    assert build_official_ws_snapshot(quote)["quote_source"] == "OFFICIAL_CLOB_WS"
    assert build_confirmed_inventory_snapshot(inventory)["complete"] == 1
    assert build_order_state_snapshot(order_state)["known"] == 1
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        receipt = insert_market_evidence_bundle(conn, quote, inventory, order_state)
        assert receipt["wrote_quote"] is True
        assert receipt["wrote_inventory"] is True
        assert receipt["wrote_order_state"] is True
        replay = insert_market_evidence_bundle(conn, quote, inventory, order_state)
        assert replay["wrote_quote"] is False
        with pytest.raises(ContractError, match="sequence_rewind"):
            insert_market_evidence_bundle(
                conn,
                {**quote, "snapshot_id": "quote-0", "sequence": 0},
                inventory,
                order_state,
            )
        with pytest.raises(ContractError, match="version_mismatch"):
            insert_market_evidence_bundle(
                conn,
                quote,
                inventory,
                {**order_state, "order_state_version": "ord-2"},
            )
    with pytest.raises(ContractError, match="source_required"):
        build_official_ws_snapshot({**quote, "quote_source": "REST"})
    with pytest.raises(ContractError, match="strictly_increasing"):
        build_official_ws_snapshot({
            **quote,
            "buckets": [{**buckets[0], "ask_levels": [{"price": 0.30, "shares": 1.0}, {"price": 0.20, "shares": 1.0}]}] + buckets[1:],
        })


def test_official_ws_rejects_quote_time_and_generation_rewinds() -> None:
    _policy, phase_input = _cash_phase_inputs()
    buckets = [
        {
            "bucket": bucket,
            "ordinal": ordinal,
            "token_id": f"token-{bucket}",
            "shard_generation": 2,
            "ask_levels": [{"price": 0.30, "shares": 10.0}],
        }
        for bucket, ordinal in (("37", 37), ("38", 38), ("39", 39))
    ]
    quote = {
        "snapshot_id": "quote-new",
        "market_id": "market-1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
            "settlement_timezone": "Asia/Shanghai",
        "sequence": 1,
        "observed_at": "2026-07-23T04:01:00Z",
        "quote_version": "q-new",
        "quote_observed_at": "2026-07-23T04:01:00Z",
        "quote_expires_at": "2026-07-23T04:01:05Z",
        "quote_freshness_policy_sha256": "a" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inv-1",
        "order_state_version": "ord-1",
        "remaining_budget_usdc": 12.0,
        "buckets": buckets,
        "cash_phase_input": phase_input.to_dict(),
    }
    inventory = {
        "inventory_snapshot_id": "inv-snapshot-1", "station_id": "ZUUU", "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "inventory_version": "inv-1", "observed_at": "2026-07-23T04:00:00Z", "complete": 1,
        "inventory_json": {"filled_by_bucket": {item["bucket"]: 0.0 for item in buckets}},
    }
    order_state = {
        "order_state_snapshot_id": "ord-snapshot-1", "station_id": "ZUUU", "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
        "order_state_version": "ord-1", "observed_at": "2026-07-23T04:00:00Z", "known": 1,
        "order_state_json": {"pending_by_bucket": {item["bucket"]: 0.0 for item in buckets}},
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        insert_market_evidence_bundle(conn, quote, inventory, order_state)
        with pytest.raises(ContractError, match="quote_observed_at_rewind"):
            insert_market_evidence_bundle(
                conn,
                {**quote, "snapshot_id": "quote-old", "sequence": 2, "observed_at": "2026-07-23T04:00:30Z", "quote_observed_at": "2026-07-23T04:00:30Z"},
                inventory,
                order_state,
            )
        with pytest.raises(ContractError, match="generation_rewind"):
            insert_market_evidence_bundle(
                conn,
                {
                    **quote,
                    "snapshot_id": "quote-rollback",
                    "sequence": 2,
                    "buckets": [
                        {**item, "shard_generation": 1}
                        for item in buckets
                    ],
                },
                inventory,
                order_state,
            )


def test_mixed_timezone_market_bundle_fails_before_any_write() -> None:
    _policy, phase_input = _cash_phase_inputs()
    cash_input = phase_input.to_dict()
    cash_input["station_timezone"] = "UTC"
    quote = {
        "snapshot_id": "mixed-zone-quote",
        "market_id": "mixed-zone-market",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Seoul",
        "sequence": 1,
        "observed_at": "2026-07-23T04:00:00Z",
        "quote_version": "mixed-zone-q1",
        "quote_observed_at": "2026-07-23T04:00:00Z",
        "quote_expires_at": "2026-07-23T04:00:05Z",
        "quote_freshness_policy_sha256": "a" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "mixed-zone-inv1",
        "order_state_version": "mixed-zone-ord1",
        "remaining_budget_usdc": 10.0,
        "buckets": [
            {
                "bucket": "38",
                "ordinal": 38,
                "ask_levels": [{"price": 0.3, "shares": 1.0}],
            }
        ],
        "cash_phase_input": cash_input,
    }
    inventory = {
        "inventory_snapshot_id": "mixed-zone-inventory",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Seoul",
        "inventory_version": "mixed-zone-inv1",
        "observed_at": "2026-07-23T04:00:00Z",
        "complete": 1,
        "inventory_json": {"filled_by_bucket": {"38": 0.0}},
    }
    order_state = {
        "order_state_snapshot_id": "mixed-zone-order-state",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Seoul",
        "order_state_version": "mixed-zone-ord1",
        "observed_at": "2026-07-23T04:00:00Z",
        "known": 1,
        "order_state_json": {"pending_by_bucket": {"38": 0.0}},
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="cash_phase_input_settlement_timezone_mismatch",
        ):
            insert_market_evidence_bundle(
                conn, quote, inventory, order_state
            )
        for table in (
            "basket_official_ws_snapshot",
            "basket_confirmed_inventory_snapshot",
            "basket_order_state_snapshot",
        ):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0


def test_p0_is_committed_pre_event_but_market_evidence_remains_gated() -> None:
    future = ProbabilityEnvelope.from_mapping({
        **_envelope().to_dict(),
        "event_date": "2026-07-25",
        "decision_at_utc": "2026-07-24T23:00:00Z",
        "fresh_until_utc": "2026-07-25T01:00:00Z",
    })
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        receipt = insert_probability_envelope(
            conn, future, created_at_utc="2026-07-24T23:01:00Z"
        )
        assert receipt["state"] == "COMMITTED"
        assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 1

    _policy, phase_input = _cash_phase_inputs()
    cash_input = phase_input.to_dict()
    cash_input["event_date_local"] = "2026-07-25"
    cash_input["station_timezone"] = "Asia/Shanghai"
    cash_input["observed_at_utc"] = "2026-07-24T15:00:00Z"
    quote = {
        "snapshot_id": "future-quote",
        "market_id": "future-market",
        "station_id": "ZUUU",
        "event_date": "2026-07-25",
        "settlement_timezone": "Asia/Shanghai",
        "sequence": 1,
        "observed_at": "2026-07-24T15:00:00Z",
        "quote_version": "future-q",
        "quote_observed_at": "2026-07-24T15:00:00Z",
        "quote_expires_at": "2026-07-24T15:00:05Z",
        "quote_freshness_policy_sha256": "a" * 64,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "future-inv",
        "order_state_version": "future-ord",
        "remaining_budget_usdc": 10.0,
        "buckets": [{"bucket": "38", "ordinal": 38, "ask_levels": [{"price": 0.3, "shares": 1.0}]}],
        "cash_phase_input": cash_input,
    }
    with pytest.raises(ContractError, match="future_event_day_not_allowed"):
        build_official_ws_snapshot(quote)


def test_p1_still_rejects_pre_event_materialization() -> None:
    future_p1 = ProbabilityEnvelope.from_mapping({
        **_envelope().to_dict(),
        "stage": "P1",
        "checkpoint_id": "p1-future",
        "event_date": "2026-07-25",
        "decision_at_utc": "2026-07-24T23:00:00Z",
        "fresh_until_utc": "2026-07-25T01:00:00Z",
        "lineage": {
            "canonical_observation_event_id": "future-observation",
            "canonical_observation_sha256": "a" * 64,
            "hard_bound_id": "future-hard-bound",
        },
    })
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match="future_event_day_not_allowed"):
            insert_probability_envelope(
                conn,
                future_p1,
                created_at_utc="2026-07-24T23:01:00Z",
            )
        assert conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0] == 0


def test_probability_insert_replay_is_idempotent_but_same_stage_instant_conflicts() -> None:
    first = _base_envelope()
    competing = ProbabilityEnvelope.from_mapping(
        {
            **first.to_dict(),
            "checkpoint_id": "p0-competing-same-instant",
        }
    )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        committed = insert_probability_envelope(
            conn,
            first,
            created_at_utc="2026-07-23T05:00:01Z",
        )
        replayed = insert_probability_envelope(
            conn,
            first,
            created_at_utc="2026-07-23T05:00:02Z",
        )
        assert committed["state"] == "COMMITTED"
        assert replayed["state"] == "ALREADY_COMMITTED"
        assert replayed["wrote"] is False
        with pytest.raises(
            ContractError,
            match="probability_stage_decision_time_conflict",
        ):
            insert_probability_envelope(
                conn,
                competing,
                created_at_utc="2026-07-23T05:00:03Z",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope"
        ).fetchone()[0] == 1
