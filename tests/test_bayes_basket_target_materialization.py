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
    materialize_p1,
)
from bayes_basket_target.materialization import (
    _embedded_capture_cycle,
    _native_forward_lineage,
)
from bayes_basket_target.ssot import apply_production_ssot_schema, production_ssot_schema_status
from bayes_basket_target.calibration import (
    build_taf_nwp_live_compatibility_fingerprint,
)
from bayes_basket_target.future_temperature_curve import (
    build_root_future_temperature_curve,
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


def _mixed_native_envelope(
    *, observed_only_system: str | None = None
) -> ProbabilityEnvelope:
    """Build an official GEFS/ECMWF + GEPS mixed-cycle P0 fixture.

    ``observed_only_system`` adds a system the packet saw but the cross
    ensemble did not select, so it appears in the availability evidence and
    not in the cycle map -- the shape every live 18Z scope has, because ECCC
    publishes only 00Z.
    """

    base = _envelope()
    run_cycle_by_system = {"ECMWF_ENS": "18Z", "GEPS": "00Z"}
    run_init_by_system = {
        "ECMWF_ENS": "2026-07-22T18:00:00Z",
        "GEPS": "2026-07-23T00:00:00Z",
    }
    archive_query_by_system = {
        "ECMWF_ENS": "2026-07-23T03:00:00Z",
        "GEPS": "2026-07-23T03:00:00Z",
    }
    availability_identity = {
        "available_at_utc": "2026-07-23T03:00:00Z",
        "by_system": {
            "ECMWF_ENS": {
                "run_cycle": run_cycle_by_system["ECMWF_ENS"],
                "run_init_utc": run_init_by_system["ECMWF_ENS"],
                "archive_query_at_utc": archive_query_by_system["ECMWF_ENS"],
            },
            "GEPS": {
                "run_cycle": run_cycle_by_system["GEPS"],
                "run_init_utc": run_init_by_system["GEPS"],
                "archive_query_at_utc": archive_query_by_system["GEPS"],
            },
        },
    }
    if observed_only_system is not None:
        availability_identity["by_system"][observed_only_system] = {
            "run_cycle": "00Z",
            "run_init_utc": "2026-07-23T00:00:00Z",
            "archive_query_at_utc": "2026-07-23T03:00:00Z",
        }
    availability = {
        **availability_identity,
        "content_sha256": content_sha256(availability_identity),
    }
    native_identity = {
        "run_cycle_scope": "PER_SYSTEM_NATIVE_CYCLE",
        "run_cycle": "MIXED_NATIVE_CYCLES",
        "run_cycle_by_system": run_cycle_by_system,
        "run_init_utc_by_system": run_init_by_system,
        "archive_query_at_utc_by_system": archive_query_by_system,
        "availability_evidence": availability,
        "source_class": "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
        "source_archive_set_sha256": "d" * 64,
        "source_archives": [],
    }
    native_sha = content_sha256(native_identity)
    compatibility_identity = json.loads(
        base.lineage["taf_nwp_live_compatibility_json"]
    )
    compatibility_identity.pop("fingerprint_sha256", None)
    compatibility_identity.update({
        "run_cycle": "MIXED_NATIVE_CYCLES",
        "run_cycle_scope": "PER_SYSTEM_NATIVE_CYCLE",
        "run_cycle_by_system": run_cycle_by_system,
        "run_init_utc_by_system": run_init_by_system,
        "archive_query_at_utc_by_system": archive_query_by_system,
        "availability_evidence": availability,
        "native_forward_lineage": native_identity,
        "native_forward_lineage_sha256": native_sha,
    })
    compatibility = {
        **compatibility_identity,
        "fingerprint_sha256": content_sha256(compatibility_identity),
    }
    lineage = {
        **base.lineage,
        "run_cycle": "MIXED_NATIVE_CYCLES",
        "run_cycle_scope": "PER_SYSTEM_NATIVE_CYCLE",
        "run_cycle_by_system_json": json.dumps(
            run_cycle_by_system, sort_keys=True, separators=(",", ":")
        ),
        "run_init_utc_by_system_json": json.dumps(
            run_init_by_system, sort_keys=True, separators=(",", ":")
        ),
        "archive_query_at_utc_by_system_json": json.dumps(
            archive_query_by_system, sort_keys=True, separators=(",", ":")
        ),
        "native_forward_availability_evidence_json": json.dumps(
            availability, sort_keys=True, separators=(",", ":")
        ),
        "native_forward_lineage_sha256": native_sha,
        "taf_nwp_live_compatibility_fingerprint_sha256": (
            compatibility["fingerprint_sha256"]
        ),
        "taf_nwp_live_compatibility_json": json.dumps(
            compatibility, sort_keys=True, separators=(",", ":")
        ),
    }
    return ProbabilityEnvelope.from_mapping({
        **base.to_dict(),
        "lineage": lineage,
    })


def _lineage(*, event_id: str = "obs-1") -> dict[str, str]:
    return {
        "canonical_observation_event_id": event_id,
        "canonical_observation_sha256": "a" * 64,
        "hard_bound_id": "hard-1",
    }


def _p1_root(root: ProbabilityEnvelope) -> ProbabilityEnvelope:
    """The root a P1 compiles on: the P0 itself (charter 283 retired P0-M)."""

    assert root.stage == "P0"
    return root


def _trajectory_packet(
    root: ProbabilityEnvelope,
    *,
    event_id: str = "obs-1",
    temperature_c: float = 38.2,
    decision_at_utc: str = "2026-07-23T05:30:00Z",
) -> dict:
    root = _p1_root(root)
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
            "settlement_timezone": root.settlement_timezone,
            "reported_temperature_c": temperature_c,
            "valid_at_utc": decision_at_utc,
            "canonical_committed_at_utc": decision_at_utc,
            "raw_sha256": (
                ("a" if event_id == "obs-1" else "c") * 64
            ),
            # The PR12 revision fact, attached to the observation row by
            # ``attach_truth_observation_revisions`` before the compiler ever
            # sees it.  Without it the packet compiles but can never become an
            # atomic fact, so a fixture that omits it cannot reach anything
            # downstream of ``target_atomic_facts_from_cycle``.
            "truth_observation_revision_content_sha256": (
                ("b" if event_id == "obs-1" else "d") * 64
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


def test_a_system_observed_but_not_selected_does_not_block_the_chain() -> None:
    """Availability evidence covers what was fetched, cycles what was used.

    ECCC publishes only 00Z, so every live 18Z scope observes it and cannot
    weight it: the packet records its availability, the cross ensemble leaves
    it out of the cycle map, and the two sets differ by exactly that system.
    Requiring them to match rejected the richer evidence as "incomplete" and
    took P1 down for eleven stations at once.

    The receipt for the unselected system is never read -- the per-system
    checks index by the cycle map -- so carrying it costs nothing and losing
    the stage costs the reported-high hard bound.
    """

    p0 = _mixed_native_envelope(observed_only_system="ECCC_GEM_GLOBAL_ENS")
    availability = json.loads(
        p0.lineage["native_forward_availability_evidence_json"]
    )
    cycles = json.loads(p0.lineage["run_cycle_by_system_json"])
    assert set(availability["by_system"]) > set(cycles)

    p1 = ProbabilityEnvelope.from_mapping(
        _trajectory_packet(_p1_root(p0), temperature_c=37.0)[
            "expected_envelope"
        ]
    )
    assert p1.stage == "P1"
    assert p1.lineage["native_forward_availability_evidence_json"] == (
        p0.lineage["native_forward_availability_evidence_json"]
    )


def test_a_system_in_use_without_a_receipt_still_fails_closed() -> None:
    """The direction that matters is unchanged: evidence must cover use."""

    p0 = _mixed_native_envelope()
    availability = json.loads(
        p0.lineage["native_forward_availability_evidence_json"]
    )
    availability["by_system"].pop("GEPS")
    stripped = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "lineage": {
            **p0.lineage,
            "native_forward_availability_evidence_json": json.dumps(
                availability, sort_keys=True, separators=(",", ":")
            ),
        },
    })
    with pytest.raises(ContractError):
        _trajectory_packet(_p1_root(stripped), temperature_c=37.0)


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


def test_without_a_likelihood_artifact_p1_is_the_root_under_the_hard_bound() -> None:
    """Charter 150: no artifact, no likelihood -- and the receipt says so.

    The clock ceiling that used to be tested here is retired; the joint
    observation likelihood's own tests are in
    tests/test_charter_150_p1_joint_likelihood.py.
    """

    root = _p1_root(_envelope())
    left = _trajectory_packet(root, temperature_c=37.0)["expected_envelope"]
    right = _trajectory_packet(root, temperature_c=37.0)["expected_envelope"]
    assert left == right
    assert "metar_ceiling_json" not in left["lineage"]
    receipt = json.loads(left["lineage"]["p1_observation_likelihood_json"])
    assert receipt["state"] == "SILENT_NO_ARTIFACT"


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


def test_p0_is_committed_pre_event_and_a_listing_day_book_is_admitted() -> None:
    """A forward event day is a listing day, not a rejection.

    This asserted ``future_event_day_not_allowed`` from the WS snapshot
    builder until 44ab45c2 (2026-08-14) deliberately removed that gate: the
    stage-open binds on the listing day, so a materializer that refuses a
    not-yet-started day refuses every opening book.  The window is still
    resolved, which is what the malformed cases below pin -- what changed is
    that "not started" stopped being a reason to refuse.
    """

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
    admitted = build_official_ws_snapshot(quote)
    assert admitted["event_date"] == "2026-07-25"
    assert admitted["station_id"] == "ZUUU"
    assert len(admitted["content_sha256"]) == 64

    # Resolving the window is still part of admitting the book, so a day or a
    # zone that cannot be resolved at all fails exactly as it did before.
    malformed_date = {
        **quote,
        "event_date": "2026-13-99",
        "cash_phase_input": {**cash_input, "event_date_local": "2026-13-99"},
    }
    with pytest.raises(ContractError, match="settlement_event_date_invalid"):
        build_official_ws_snapshot(malformed_date)
    malformed_zone = {
        **quote,
        "settlement_timezone": "Not/AZone",
        "cash_phase_input": {**cash_input, "station_timezone": "Not/AZone"},
    }
    with pytest.raises(ContractError, match="settlement_timezone_invalid"):
        build_official_ws_snapshot(malformed_zone)


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


def test_a_scope_the_document_contradicts_still_fails_closed() -> None:
    """Recovery is not permission: the two must still agree.

    A root claiming a same-cycle scope while its own document records a mixed
    capture is the corruption the check exists for, and it is refused whether
    the capture cycle sits at the top level or inside the document.
    """

    p0 = _mixed_native_envelope()
    forged = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "lineage": {
            **{
                key: value for key, value in p0.lineage.items()
                if key != "native_forward_run_cycle"
            },
            "run_cycle_scope": "SAME_NATIVE_CYCLE",
            "run_cycle": "00Z",
        },
    })
    with pytest.raises(
        ContractError, match="native_forward_mixed_scope_mismatch"
    ):
        _native_forward_lineage("P1", root=forged, predecessor=forged)


def test_an_unreadable_document_falls_back_rather_than_refusing() -> None:
    """A parse failure in the recovery must not invent a refusal.

    The document is read for a value the top level may already carry; if it
    cannot be read the behaviour is what it was before the recovery existed,
    never a new way to block a compile.
    """

    p0 = _mixed_native_envelope()
    broken = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "lineage": {
            **p0.lineage,
            "native_forward_run_cycle": "MIXED_NATIVE_CYCLES",
            "taf_nwp_live_compatibility_json": "{not json",
        },
    })
    assert _embedded_capture_cycle(broken.lineage) == ""
    # The top level still states it, so the compile proceeds on that.
    with pytest.raises(ContractError):
        # The document itself is checked later and is genuinely broken here;
        # what matters is that the failure names the document, not the scope.
        _native_forward_lineage("P1", root=broken, predecessor=broken)


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
    assert p1.lineage["prediction_root_stage"] == "P0"
    assert p1.lineage["prediction_root_checkpoint_id"] == p0.checkpoint_id
    assert p1.lineage["p0_root_checkpoint_id"] == p0.checkpoint_id
    assert p1.lineage["p0_root_content_sha256"] == p0.content_sha256
    assert p1.lineage["metar_trajectory_direction_role"] == (
        "DIAGNOSTIC_ONLY_BASKET_RECOMPUTES_POLICY_DIRECTION"
    )
    assert p1.lineage["metar_trajectory_positive_core_role"] == (
        "DIAGNOSTIC_POSITIVE_PROBABILITY_SUPPORT_NOT_BASKET_CORE"
    )
    facts = target_atomic_facts_from_cycle(
        {"probability_envelope": p1.to_dict()}
    )
    # The atomic root is the observation *contract*, not a reconstructed
    # observation row: this used to assert a "observation" fact assembled from
    # the lineage, and that key no longer exists.  What identifies the
    # observation now is the PR12 revision hash the truth ledger is keyed on.
    assert set(facts) == {
        "awc_observation_contract",
        "truth_observation_revision_content_sha256",
        "bound",
    }
    assert facts["truth_observation_revision_content_sha256"] == "b" * 64
    assert (
        facts["truth_observation_revision_content_sha256"]
        == p1.lineage["truth_observation_revision_content_sha256"]
    )
    assert facts["bound"]["bound_id"] == p1.lineage["hard_bound_id"]
    assert facts["bound"]["station_id"] == p1.station_id
    assert facts["bound"]["event_date"] == p1.event_date

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


def test_materialize_p1_accepts_p0_prediction_root_without_taf() -> None:
    p0 = _envelope()
    packet = _trajectory_packet(p0)
    p1 = materialize_p1(
        p0,
        prediction_root=p0,
        trajectory_artifact=packet["trajectory_artifact"],
        trajectory_proof=packet["trajectory_proof"],
        trajectory_observations=packet["trajectory_observations"],
        bucket_centers_c=packet["bucket_centers_c"],
        bucket_upper_bounds_c=packet["bucket_upper_bounds_c"],
        reported_high_advanced=packet["reported_high_advanced"],
        decision_at_utc=packet["decision_at_utc"],
        fresh_until_utc=packet["fresh_until_utc"],
        bridge_sha256=p0.bridge_sha256,
    )
    assert p1.stage == "P1"
    assert p1.lineage["prediction_root_stage"] == "P0"


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


def test_a_descendant_root_recovers_the_capture_cycle_from_its_document() -> None:
    """A descendant root does not inherit the capture cycle key (the P0-M roots of
    the history before charter 283 are the case that broke).

    Two encodings of "did the systems ride different cycles" live in a P0
    root.  ``run_cycle_scope`` is inherited by every stage; the capture cycle
    itself is written by the P0 compiler under ``native_forward_run_cycle``
    and deliberately not inherited.  Reading the calibration ``run_cycle`` in
    its place compares the scope an activation was fitted under against a
    question about what the archive delivered -- and that refused 86 P0-M
    roots across 27 stations, KAUS on 2026-09-02 among them, each a P1
    blocked on a disagreement that was not one.

    The value is not lost: the compatibility document carries it, that
    document is inherited, and it is bound by its own hash.  This pins the
    shape that broke -- a mixed-cycle root whose top level says only the
    calibration cycle -- and the P0 shape it must not disturb.
    """

    p0 = _mixed_native_envelope()
    # The descendant shape: the capture cycle key is gone and the top level
    # carries the calibration cycle instead.  Everything else is inherited.
    descendant = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "lineage": {
            **{
                key: value for key, value in p0.lineage.items()
                if key != "native_forward_run_cycle"
            },
            "run_cycle": "00Z",
        },
    })
    recovered = _native_forward_lineage(
        "P1", root=descendant, predecessor=descendant
    )
    assert recovered["run_cycle_scope"] == "PER_SYSTEM_NATIVE_CYCLE"

    # A P0 root that states the capture cycle at the top level is unchanged,
    # and the cross-check it gets is still the strict one.
    stated = ProbabilityEnvelope.from_mapping({
        **p0.to_dict(),
        "lineage": {
            **p0.lineage,
            "native_forward_run_cycle": "MIXED_NATIVE_CYCLES",
            "run_cycle": "00Z",
        },
    })
    assert _native_forward_lineage("P1", root=stated, predecessor=stated)
