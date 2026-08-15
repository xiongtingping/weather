"""The single ingress for the canonical model contract.

All model/truth/residual/activation facts enter the target SSOT through this
function.  It is append-only and content addressed.  The legacy calibration
adapter and its reverse projection are permanently retired: a legacy packet
cannot manufacture a canonical fact.  The only retained projection direction
is exact formal AWC canonical truth into the historical audit table.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activation import assert_target_database_owner
from .canonical_contract import (
    ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA,
    CanonicalActivationFact,
    CanonicalActivationSupersessionFact,
    CanonicalActivationSupersessionContract,
    CanonicalContract,
    CanonicalEnsembleMemberFact,
    CanonicalEnsembleMemberReceiptManifestFact,
    CanonicalEnsembleMemberArrivalBindingFact,
    CanonicalNativeArrivalFact,
    CanonicalRepublisherOOSArtifactContract,
    CanonicalRepublisherOOSArtifactFact,
    CanonicalRepublisherCrossSelectionContract,
    CanonicalRepublisherCrossSelectionFact,
    CanonicalRepublisherNoLiveActivationContract,
    CanonicalRepublisherNoLiveActivationFact,
    CanonicalTruthFact,
    REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
    REPUBLISHER_CROSS_SELECTION_CONTRACT_SCHEMA,
    REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA,
    SCHEMA,
)
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .awc_observation_contract import (
    CONTRACT_SCHEMA as AWC_OBSERVATION_CONTRACT_SCHEMA,
    CanonicalAWCObservationDayBoundsFact,
    CanonicalAWCObservationContract,
    CanonicalAWCObservationRevisionFact,
)
from .band_reliability_store import load_band_table
from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .materialization import insert_probability_envelope, materialize_p1
from .market_bucket_bridge import (
    CONTRACT_SCHEMA as MARKET_BUCKET_BRIDGE_CONTRACT_SCHEMA,
    CanonicalMarketBucketBridgeContract,
    CanonicalMarketBucketBridgeFact,
)
from .model_oos_contract import (
    CONTRACT_SCHEMA as MODEL_OOS_CONTRACT_SCHEMA,
    CanonicalModelOOSCohortFact,
    CanonicalModelOOSContract,
    CanonicalModelOOSEventFact,
    derive_cohort_statistics,
)
from .native_ensemble_oos_contract import (
    CONTRACT_SCHEMA as NATIVE_ENSEMBLE_OOS_CONTRACT_SCHEMA,
    CanonicalNativeEnsembleOOSContract,
    CanonicalNativeEnsembleOOSCohortFact,
    CanonicalNativeEnsembleOOSEventFact,
)
from .model_oos_selection import (
    CONTRACT_SCHEMA as MODEL_OOS_SELECTION_CONTRACT_SCHEMA,
    CanonicalModelOOSSelectionContract,
    build_model_oos_selection_contract,
)
from .p0_calibrated_distribution import (
    CONTRACT_SCHEMA as P0_CALIBRATED_DISTRIBUTION_CONTRACT_SCHEMA,
    CanonicalP0CalibratedDistributionContract,
    build_p0_calibrated_distribution_contract,
)
from .p0_activation_root import (
    CONTRACT_SCHEMA as P0_ACTIVATION_ROOT_CONTRACT_SCHEMA,
    CanonicalP0ActivationRootContract,
    build_p0_activation_root_contract,
)
from .p0_forecast_snapshot import (
    CONTRACT_SCHEMA as P0_FORECAST_SNAPSHOT_CONTRACT_SCHEMA,
    CanonicalP0ForecastSnapshotContract,
    build_p0_forecast_snapshot_contract,
)
from .surface_observation_contract import (
    OBSERVATION_CONTRACT_SCHEMA as SURFACE_OBSERVATION_CONTRACT_SCHEMA,
    WINNER_CONTRACT_SCHEMA as SURFACE_OBSERVATION_WINNER_CONTRACT_SCHEMA,
    CanonicalSurfaceObservationContract,
    CanonicalSurfaceObservationWinnerContract,
    assert_surface_observation_external_evidence,
    assert_surface_observation_winner_external_evidence,
    build_surface_observation_winner_contract,
)
from .p1_base_contract import (
    CONTRACT_SCHEMA as P1_BASE_CONTRACT_SCHEMA,
    CanonicalP1BaseContract,
    build_p1_base_contract,
)
from .pure_yes_basket_plan_contract import (
    CONTRACT_SCHEMA as PURE_YES_BASKET_PLAN_CONTRACT_SCHEMA,
    CanonicalPureYesBasketPlanContract,
    build_pure_yes_basket_plan_contract,
)
from .resource_bounds import assert_connection_target_capacity
from .republisher_cross_selection import (
    assert_republisher_cross_selection_references,
)
from .republisher_no_live_activation import (
    assert_republisher_no_live_activation_references,
)
from .republisher_no_live_p0 import assert_republisher_no_live_p0_references
from .republisher_no_live_p0_contract import (
    CONTRACT_SCHEMA as REPUBLISHER_NO_LIVE_P0_CONTRACT_SCHEMA,
    CanonicalForwardApplicabilityFact,
    CanonicalRepublisherNoLiveP0Contract,
)
from .truth_authority_supersession import (
    CONTRACT_SCHEMA as TRUTH_AUTHORITY_SUPERSESSION_CONTRACT_SCHEMA,
    CanonicalTruthAuthoritySupersessionContract,
    assert_truth_authority_supersession_references,
    resolve_effective_truth_day_root,
)


INGRESS_SCHEMA = "bayes_basket_target_canonical_model_ingress_v1"
INGRESS_AUTHORIZATION_FUNCTION = "canonical_ingress_authorized"
P0_PROJECTION_AUTHORIZATION_FUNCTION = "canonical_p0_projection_authorized"
# V14 is an unpublished builder boundary.  Registering a contract parser is
# deliberately not an authorization to append a new production fact: each
# V14 writer is enabled only by its own later, reviewed change.  Keeping this
# immutable default closed prevents a partially implemented child builder
# from becoming consumable merely because the V14 table exists.
V14_WRITER_ALLOWLIST: frozenset[str] = frozenset()
V15_WRITER_ALLOWLIST: frozenset[str] = frozenset(
    {"truth_authority_supersession"}
)
V16_WRITER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "native_ensemble_oos_event",
        "native_ensemble_oos_cohort",
        "p0_model_family_selection",
        "p0_calibrated_distribution",
        "p0_activation_root",
        "p0_forecast_snapshot",
    }
)
V17_WRITER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "surface_observation", "surface_observation_winner", "p1_base",
        "pure_yes_basket_plan",
    }
)
FACT_TYPES = (
    "model",
    "truth",
    "residual",
    "activation",
    "activation_supersession",
    "ensemble_member",
    "native_arrival",
    "ensemble_member_receipt_manifest",
    "ensemble_member_arrival_binding",
    "republisher_oos_artifact",
    "republisher_cross_selection",
    "republisher_no_live_activation",
    "forward_applicability",
    "probability_distribution_artifact",
    "p0_snapshot",
    "market_bucket_bridge",
    "truth_observation_revision",
    "truth_observation_day_bounds",
    "model_oos_event",
    "model_oos_cohort",
    "p0_model_family_selection",
    "p0_calibrated_distribution",
    "p0_activation_root",
    "p0_forecast_snapshot",
    "truth_authority_supersession",
    "native_ensemble_oos_event",
    "native_ensemble_oos_cohort",
    "surface_observation",
    "surface_observation_winner",
    "p1_base",
    "pure_yes_basket_plan",
)
AWC_DAILY_TRUTH_PROJECTION_SCHEMA = (
    "bayes_basket_target_canonical_awc_daily_truth_projection_v1"
)
_FORMAL_AWC_TRUTH_ROLE = "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
_AWC_DAILY_TRUTH_PROJECTION_FIELDS = frozenset(
    {
        "truth_id",
        "station_id",
        "event_date",
        "truth_revision_no",
        "truth_value",
        "truth_content_sha256",
        "canonical_committed_at_utc",
        "station_day_end_utc",
        "closed_day",
        "source_lane",
        "authority_scope",
        "settlement_timezone",
        "settlement_definition",
        "published_at_utc",
        "received_at_utc",
        "timestamps_complete",
        "qc_status",
        "source_role",
        "observation_ids_json",
        "content_sha256",
    }
)


def adapt_legacy_calibration_packet_to_canonical(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any],
    *,
    builder_id: str,
    authority_scope_override: str | None = None,
) -> dict[str, Any]:
    """Retired compatibility boundary; legacy evidence cannot form SSOT facts.

    The public signature remains temporarily so installed historical tools
    receive a typed, fail-closed error instead of silently selecting default
    authority, source, model-family, or run-cycle values.  No argument is
    inspected and no database row is read or written: there is no supported
    legacy-to-canonical field mapping.
    """

    del conn, packet, builder_id, authority_scope_override
    raise ContractError("canonical_legacy_adapter_retired")


def materialize_p1_from_canonical_observation(
    conn: sqlite3.Connection,
    *,
    predecessor: ProbabilityEnvelope | Mapping[str, Any],
    prediction_root: ProbabilityEnvelope | Mapping[str, Any],
    observation_id: str,
    trajectory_artifact: Mapping[str, Any],
    trajectory_proof: Mapping[str, Any],
    trajectory_observations: Sequence[Mapping[str, Any]],
    bucket_centers_c: Mapping[str, Any],
    bucket_upper_bounds_c: Mapping[str, Any],
    reported_high_advanced: bool,
    decision_at_utc: str,
    fresh_until_utc: str,
    created_at_utc: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Bridge one canonical METAR/SPECI event into P1.

    This is retained as the observation-stage entry point.  Model/truth/
    residual/activation facts use :func:`ingest_canonical_contract` below;
    neither path is allowed to reinterpret the other's fields.
    """

    event_id = str(observation_id or "").strip()
    if not event_id:
        raise ContractError("canonical_observation_id_missing")
    target_fact_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='basket_target_observation_facts'"
    ).fetchone()
    if target_fact_table is None:
        raise ContractError("target_canonical_fact_table_missing")
    target_fact_columns = {
        str(column[1])
        for column in conn.execute(
            "PRAGMA table_info(basket_target_observation_facts)"
        )
    }
    if "settlement_timezone" not in target_fact_columns:
        raise ContractError("canonical_observation_settlement_timezone_missing")
    row = conn.execute(
        "SELECT observation_id,source_lane,station_id,report_type,raw_sha256,"
        "canonical_committed_at,0,event_date,settlement_timezone "
        "FROM basket_target_observation_facts WHERE observation_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ContractError("canonical_observation_missing")
    if str(row[1] or "").upper() != "AWC":
        raise ContractError("canonical_observation_source_lane_invalid")
    if str(row[3] or "").upper() not in {"METAR", "SPECI"}:
        raise ContractError("canonical_observation_report_type_invalid")
    if int(row[6] or 0) != 0:
        raise ContractError("canonical_observation_correction_requires_review")
    canonical_sha = str(row[4] or "").strip().lower()
    if len(canonical_sha) != 64 or any(char not in "0123456789abcdef" for char in canonical_sha):
        raise ContractError("canonical_observation_sha_invalid")
    canonical_committed_at = str(row[5] or "").strip()
    try:
        committed_at = datetime.fromisoformat(canonical_committed_at.replace("Z", "+00:00"))
        decision_at = datetime.fromisoformat(str(decision_at_utc or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("canonical_observation_asof_timestamp_invalid") from exc
    if committed_at.tzinfo is None or decision_at.tzinfo is None:
        raise ContractError("canonical_observation_asof_timezone_required")
    if committed_at > decision_at:
        raise ContractError("canonical_observation_not_strict_asof")
    proof_ids = tuple(str(item) for item in trajectory_proof.get("canonical_observation_ids", ()))
    if event_id not in proof_ids or proof_ids[-1] != event_id:
        raise ContractError("canonical_observation_not_trajectory_proof_tail")
    envelope = predecessor if isinstance(predecessor, ProbabilityEnvelope) else ProbabilityEnvelope.from_mapping(predecessor)
    if str(row[2] or "") != envelope.station_id:
        raise ContractError("canonical_observation_station_mismatch")
    if str(row[7] or "") != envelope.event_date:
        raise ContractError("canonical_observation_event_date_mismatch")
    settlement_timezone = str(row[8] or "").strip()
    if not settlement_timezone:
        raise ContractError("canonical_observation_settlement_timezone_missing")
    try:
        ZoneInfo(settlement_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError("canonical_observation_settlement_timezone_invalid") from exc
    if settlement_timezone != envelope.settlement_timezone:
        raise ContractError("canonical_observation_settlement_timezone_mismatch")
    prediction_envelope = (
        envelope
        if prediction_root is None
        else (
            prediction_root
            if isinstance(prediction_root, ProbabilityEnvelope)
            else ProbabilityEnvelope.from_mapping(prediction_root)
        )
    )
    if settlement_timezone != prediction_envelope.settlement_timezone:
        raise ContractError(
            "canonical_observation_prediction_root_settlement_timezone_mismatch"
        )
    result = materialize_p1(
        envelope,
        # P1's own residual table -- P0's correction already rode in through
        # the root, and an identity table (which is what P1 serves until its
        # evidence clears the hurdles) leaves the posterior byte-identical.
        band_table=load_band_table(conn, stage="P1"),
        prediction_root=prediction_root,
        trajectory_artifact=trajectory_artifact,
        trajectory_proof=trajectory_proof,
        trajectory_observations=trajectory_observations,
        bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
        reported_high_advanced=reported_high_advanced,
        decision_at_utc=decision_at_utc,
        fresh_until_utc=fresh_until_utc,
        bridge_sha256=envelope.bridge_sha256,
    )
    if result.settlement_timezone != settlement_timezone:
        raise ContractError("canonical_observation_p1_settlement_timezone_mismatch")
    savepoint = "basket_target_p1_canonical_ingress"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        receipt = insert_probability_envelope(conn, result, created_at_utc=created_at_utc or decision_at_utc)
        if commit:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            conn.commit()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    return {
        "schema": "bayes_basket_target_canonical_p1_ingress_v1",
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "observation_id": event_id,
        "source_lane": str(row[1]).lower(),
        "p1_checkpoint_id": result.checkpoint_id,
        "receipt": receipt,
        "places_orders": False,
        "calls_order_gateway": False,
    }


def stage_republisher_no_live_p0(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any] | CanonicalRepublisherNoLiveP0Contract,
    *,
    external_evidence_root: Path | str,
    created_at_utc: str,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    """Atomically stage one canonical republisher P0 and its probability envelope.

    The caller owns the outer transaction and is the only layer allowed to
    commit it.  This seam either releases both append-only writes into that
    transaction or rolls both back to its private savepoint.
    """

    if not conn.in_transaction:
        raise ContractError("republisher_p0_outer_transaction_required")
    contract = (
        packet
        if isinstance(packet, CanonicalRepublisherNoLiveP0Contract)
        else CanonicalRepublisherNoLiveP0Contract.from_mapping(packet)
    )
    if (
        contract.forward_applicability_facts
        or contract.probability_distribution_artifact_facts
        or len(contract.p0_snapshot_facts) != 1
    ):
        raise ContractError("republisher_p0_atomic_contract_shape_invalid")
    if inject_fault_at not in {None, "after_canonical", "after_envelope"}:
        raise ContractError("republisher_p0_injected_fault_stage_invalid")

    snapshot = contract.p0_snapshot_facts[0]
    assert_republisher_no_live_p0_references(conn, contract)
    applicability_rows = conn.execute(
        "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='forward_applicability' AND content_sha256=?",
        (snapshot.raw["forward_applicability_content_sha256"],),
    ).fetchall()
    if len(applicability_rows) != 1:
        raise ContractError("republisher_p0_forward_applicability_missing")
    applicability_raw = json.loads(str(applicability_rows[0][1]))
    applicability = CanonicalForwardApplicabilityFact.from_mapping(applicability_raw)
    if str(applicability_rows[0][0]) != applicability.content_sha256:
        raise ContractError("republisher_p0_forward_applicability_hash_mismatch")
    from .republisher_forward_evidence_store import verify_forward_manifest

    verify_forward_manifest(
        external_evidence_root,
        manifest_content_address=applicability.raw[
            "evidence_manifest_content_address"
        ],
        manifest_content_sha256=applicability.raw[
            "evidence_manifest_content_sha256"
        ],
        station_id=applicability.raw["station_id"],
        event_date=applicability.raw["event_date"],
        settlement_timezone=applicability.raw["settlement_timezone"],
        component_receipts=applicability.raw["component_receipts"],
        native_terminal_receipts=applicability.raw[
            "native_unavailability_receipts"
        ],
    )
    bridge_rows = conn.execute(
        "SELECT content_sha256,fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='market_bucket_bridge' AND content_sha256=?",
        (snapshot.raw["market_bucket_bridge_sha256"],),
    ).fetchall()
    if len(bridge_rows) != 1:
        raise ContractError("republisher_p0_market_bucket_bridge_missing")
    bridge = CanonicalMarketBucketBridgeFact.from_mapping(
        json.loads(str(bridge_rows[0][1]))
    )
    if str(bridge_rows[0][0]) != bridge.content_sha256:
        raise ContractError("republisher_p0_market_bucket_bridge_hash_mismatch")

    lineage = {
        "settlement_timezone": snapshot.raw["settlement_timezone"],
        "p0_probability_pathway": "REPUBLISHER_NO_LIVE_P0_V1",
        "probability_pathway": "REPUBLISHER_NO_LIVE_P0_V1",
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "source_class": "aggregator_republisher",
        "production_no_live_only": "true",
        "p0_snapshot_content_sha256": snapshot.content_sha256,
        "activation_content_sha256": snapshot.raw["activation_content_sha256"],
        "selection_content_sha256": snapshot.raw["selection_content_sha256"],
        "forward_applicability_content_sha256": applicability.content_sha256,
        "probability_distribution_artifact_content_sha256": snapshot.raw[
            "probability_distribution_artifact_content_sha256"
        ],
        "market_bucket_bridge_content_sha256": bridge.content_sha256,
        "market_registry_snapshot_sha256": bridge.raw[
            "market_registry_snapshot_sha256"
        ],
        "evidence_manifest_content_sha256": applicability.raw[
            "evidence_manifest_content_sha256"
        ],
        "probability_root_sha256": snapshot.raw["probability_root_sha256"],
    }
    envelope = ProbabilityEnvelope.from_mapping(
        {
            "stage": "P0",
            "checkpoint_id": snapshot.raw["snapshot_id"],
            "station_id": snapshot.raw["station_id"],
            "event_date": snapshot.raw["event_date"],
            "settlement_timezone": snapshot.raw["settlement_timezone"],
            "decision_at_utc": snapshot.raw["decision_at_utc"],
            "fresh_until_utc": snapshot.raw["fresh_until_utc"],
            "bucket_ordinals": {
                item["bucket_id"]: item["ordinal"]
                for item in snapshot.raw["bucket_probabilities"]
            },
            "probabilities": {
                item["bucket_id"]: item["probability"]
                for item in snapshot.raw["bucket_probabilities"]
            },
            "posterior_version": snapshot.raw["posterior_version"],
            "posterior_sha256": snapshot.raw["posterior_sha256"],
            "bridge_sha256": bridge.content_sha256,
            "lineage": lineage,
        }
    )
    canonical_exists = conn.execute(
        "SELECT 1 FROM canonical_ssot_fact WHERE fact_type='p0_snapshot' "
        "AND content_sha256=?",
        (snapshot.content_sha256,),
    ).fetchone() is not None
    envelope_exists = conn.execute(
        "SELECT 1 FROM basket_probability_envelope WHERE envelope_id=?",
        (envelope.checkpoint_id,),
    ).fetchone() is not None
    if canonical_exists != envelope_exists:
        raise ContractError("republisher_p0_atomic_pair_incomplete")

    projection_gate = {"enabled": False}
    try:
        conn.create_function(
            P0_PROJECTION_AUTHORIZATION_FUNCTION,
            0,
            lambda: 1 if projection_gate["enabled"] else 0,
            deterministic=True,
        )
    except sqlite3.Error as exc:
        raise ContractError("republisher_p0_projection_authorization_unavailable") from exc
    savepoint = "republisher_no_live_p0_atomic_projection"
    conn.execute(f"SAVEPOINT {savepoint}")
    active = True
    try:
        projection_gate["enabled"] = True
        canonical_receipt = ingest_canonical_contract(
            conn,
            contract,
            commit=True,
            finalize_transaction=False,
            ingested_at_utc=created_at_utc,
            external_evidence_root=external_evidence_root,
        )
        if inject_fault_at == "after_canonical":
            raise RuntimeError("injected_after_canonical")
        envelope_receipt = insert_probability_envelope(
            conn, envelope, created_at_utc=created_at_utc
        )
        projection_gate["enabled"] = False
        if inject_fault_at == "after_envelope":
            raise RuntimeError("injected_after_envelope")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        active = False
    except Exception:
        projection_gate["enabled"] = False
        if active:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    result = {
        "schema": "bayes_basket_target_republisher_no_live_p0_atomic_projection_v1",
        "state": "STAGED_IN_CALLER_TRANSACTION",
        "p0_snapshot_content_sha256": snapshot.content_sha256,
        "probability_envelope_content_sha256": envelope.content_sha256,
        "canonical_ingress_receipt": canonical_receipt,
        "probability_envelope_receipt": envelope_receipt,
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**result, "receipt_sha256": content_sha256(result)}


def stage_awc_observation_contract(
    conn: sqlite3.Connection,
    packet: Mapping[str, Any] | CanonicalAWCObservationContract,
    *,
    ingested_at_utc: str,
) -> dict[str, Any]:
    """Stage PR12 facts inside a caller-owned outer transaction.

    This is the only public PR12 write seam.  It never commits the caller's
    transaction and never writes the legacy observation projection.
    """

    if not conn.in_transaction:
        raise ContractError("canonical_awc_observation_outer_transaction_required")
    contract = (
        packet
        if isinstance(packet, CanonicalAWCObservationContract)
        else CanonicalAWCObservationContract.from_mapping(packet)
    )
    if not contract.revision_facts:
        raise ContractError("canonical_awc_observation_revision_fact_required")
    receipt = ingest_canonical_contract(
        conn,
        contract,
        commit=True,
        finalize_transaction=False,
        ingested_at_utc=ingested_at_utc,
    )
    result = {
        "schema": "bayes_basket_target_pr12_awc_observation_atomic_stage_v1",
        "state": "STAGED_IN_CALLER_TRANSACTION",
        "contract_content_sha256": contract.content_sha256,
        "canonical_ingress_receipt": receipt,
        "writes_legacy_projection": False,
        "commits_outer_transaction": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**result, "receipt_sha256": content_sha256(result)}


def stage_republisher_forward_and_p0(
    conn: sqlite3.Connection,
    forward_packet: Mapping[str, Any] | CanonicalRepublisherNoLiveP0Contract,
    *,
    bucket_intervals: Sequence[Mapping[str, Any]],
    market_bucket_bridge_sha256: str,
    external_evidence_root: Path | str,
    created_at_utc: str,
    as_of_utc: str,
    inject_fault_at: str | None = None,
) -> dict[str, Any]:
    """Stage forward applicability and its P0 pair in one caller transaction.

    PR10 may materialize this pair before PR04 publication.  The facts remain
    `MATERIALIZED_UNPUBLISHED / PRODUCTION_NO_LIVE_ONLY`; this seam never
    commits, publishes, calls the gateway, or authorizes a consumer.
    """

    if not conn.in_transaction:
        raise ContractError("republisher_forward_p0_outer_transaction_required")
    contract = (
        forward_packet
        if isinstance(forward_packet, CanonicalRepublisherNoLiveP0Contract)
        else CanonicalRepublisherNoLiveP0Contract.from_mapping(forward_packet)
    )
    if (
        len(contract.forward_applicability_facts) != 1
        or contract.probability_distribution_artifact_facts
        or contract.p0_snapshot_facts
    ):
        raise ContractError("republisher_forward_p0_contract_shape_invalid")
    if inject_fault_at not in {None, "after_forward", "after_p0"}:
        raise ContractError("republisher_forward_p0_injected_fault_stage_invalid")

    from .republisher_no_live_p0 import build_p0_snapshot_contract

    savepoint = "republisher_forward_and_p0_atomic_stage"
    conn.execute(f"SAVEPOINT {savepoint}")
    active = True
    try:
        forward_receipt = ingest_canonical_contract(
            conn,
            contract,
            commit=True,
            finalize_transaction=False,
            ingested_at_utc=created_at_utc,
            external_evidence_root=external_evidence_root,
        )
        if inject_fault_at == "after_forward":
            raise RuntimeError("injected_after_forward")
        applicability = contract.forward_applicability_facts[0]
        p0_packet = build_p0_snapshot_contract(
            conn,
            forward_applicability_content_sha256=applicability.content_sha256,
            bucket_intervals=bucket_intervals,
            market_bucket_bridge_sha256=market_bucket_bridge_sha256,
            materialized_at_utc=created_at_utc,
            as_of_utc=as_of_utc,
        )
        p0_receipt = stage_republisher_no_live_p0(
            conn,
            p0_packet,
            external_evidence_root=external_evidence_root,
            created_at_utc=created_at_utc,
        )
        if inject_fault_at == "after_p0":
            raise RuntimeError("injected_after_p0")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        active = False
    except Exception:
        if active:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    snapshot = p0_packet["p0_snapshot_facts"][0]
    result = {
        "schema": "bayes_basket_target_republisher_forward_and_p0_atomic_stage_v1",
        "state": "STAGED_IN_CALLER_TRANSACTION",
        "forward_applicability_content_sha256": applicability.content_sha256,
        "p0_snapshot_content_sha256": snapshot["content_sha256"],
        "probability_root_sha256": snapshot["probability_root_sha256"],
        "forward_ingress_receipt": forward_receipt,
        "p0_atomic_receipt": p0_receipt,
        "p0_contract": p0_packet,
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**result, "receipt_sha256": content_sha256(result)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _fact_rows(contract: CanonicalContract) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact_type, facts in (
        ("model", contract.model_facts),
        ("ensemble_member", contract.ensemble_member_facts),
        ("native_arrival", contract.native_arrival_facts),
        (
            "ensemble_member_receipt_manifest",
            contract.ensemble_member_receipt_manifest_facts,
        ),
        (
            "ensemble_member_arrival_binding",
            contract.ensemble_member_arrival_binding_facts,
        ),
        ("truth", contract.truth_facts),
        ("residual", contract.residual_facts),
        ("activation", contract.activation_facts),
    ):
        for fact in facts:
            mapping = fact.to_mapping()
            sha = str(mapping["content_sha256"] if fact_type != "activation" else mapping["activation_content_sha256"])
            identity = {
                key: value
                for key, value in mapping.items()
                if key not in {"content_sha256", "activation_content_sha256"}
            }
            lineage_fields = {
                "model": (
                    "provider_id",
                    "source_class",
                    "raw_object_sha256",
                    "truth_content_sha256",
                    "source_role",
                    "authority_scope",
                    "oos_role",
                    "native_arrival_indexed",
                    "p1_eligible",
                    "formal_live_authority",
                ),
                "ensemble_member": (
                    "provider_id",
                    "source_class",
                    "raw_receipt_set_sha256",
                    "source_member_fact_sha",
                    "archive_query_at_known",
                    "archive_query_at_utc",
                    "oos_role",
                ),
                "native_arrival": (
                    "provider_id",
                    "source_class",
                    "raw_object_sha256",
                    "published_at_known",
                    "published_at_utc",
                    "received_at_known",
                    "received_at_utc",
                    "native_arrival_indexed",
                ),
                "ensemble_member_receipt_manifest": (
                    "ensemble_member_content_sha256",
                    "source_raw_receipt_set_sha256",
                    "required_receipt_ref_set_sha256",
                ),
                "ensemble_member_arrival_binding": (
                    "ensemble_member_content_sha256",
                    "native_arrival_content_sha256",
                    "receipt_manifest_content_sha256",
                    "manifest_entry_ordinals",
                ),
                "truth": (
                    "truth_content_sha256",
                    "source_role",
                    "authority_scope",
                ),
                "residual": ("oos_role",),
                "activation": ("authority_scope", "oos_role"),
            }[fact_type]
            lineage = {
                key: identity[key] for key in lineage_fields if key in identity
            }
            scope = str(fact.scope_key)
            fact_id = f"{fact_type}:{sha}"
            rows.append(
                (
                    fact_id,
                    fact_type,
                    scope,
                    json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                )
            )
    return rows


def _republisher_oos_artifact_rows(
    contract: CanonicalRepublisherOOSArtifactContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.artifact_facts:
        mapping = fact.to_mapping()
        sha = fact.content_sha256
        lineage = {
            "upstream_model_id": fact.upstream_model_id,
            "republisher_product_id": fact.republisher_product_id,
            "independence_family_id": fact.independence_family_id,
            "source_class": fact.source_class,
            "authority_scope": fact.authority_scope,
            "strict_oos_cohort_sha256": fact.strict_oos_cohort_sha256,
            "evaluation_content_sha256": fact.evaluation_content_sha256,
            "canonical_truth_fact_content_sha256s": [
                item.canonical_truth_fact_content_sha256
                for item in fact.cohort_rows
            ],
            "native_arrival_indexed": False,
            "p1_eligible": False,
            "formal_live_authority": False,
        }
        rows.append(
            (
                f"republisher_oos_artifact:{sha}",
                "republisher_oos_artifact",
                fact.scope_key,
                json.dumps(
                    mapping,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    lineage,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    return rows


def _republisher_cross_selection_rows(
    contract: CanonicalRepublisherCrossSelectionContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.selection_facts:
        mapping = fact.to_mapping()
        lineage = {
            "source_class": fact.source_class,
            "transport_provider_id": fact.transport_provider_id,
            "authority_scope": fact.authority_scope,
            "component_artifact_content_sha256s": [
                item.artifact_content_sha256 for item in fact.component_artifact_refs
            ],
            "holdout_truth_ref_set_sha256": fact.holdout_truth_ref_set_sha256,
            "native_arrival_indexed": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "production_no_live_only": True,
        }
        rows.append(
            (
                f"republisher_cross_selection:{fact.content_sha256}",
                "republisher_cross_selection",
                fact.scope_key,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
    return rows


def _republisher_no_live_activation_rows(
    contract: CanonicalRepublisherNoLiveActivationContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.activation_facts:
        mapping = fact.to_mapping()
        lineage = {
            "source_class": fact.source_class,
            "transport_provider_id": fact.transport_provider_id,
            "authority_scope": fact.authority_scope,
            "selection_id": fact.selection_id,
            "selection_content_sha256": fact.selection_content_sha256,
            "temperature_distribution_artifact_content_sha256": (
                fact.temperature_distribution_artifact_content_sha256
            ),
            "predecessor_activation_content_sha256": fact.predecessor_activation_content_sha256,
            "native_arrival_indexed": False,
            "p1_eligible": False,
            "formal_live_authority": False,
            "production_no_live_only": True,
            "contains_forward_snapshot": False,
            "materializes_p0": False,
        }
        rows.append(
            (
                f"republisher_no_live_activation:{fact.content_sha256}",
                "republisher_no_live_activation",
                fact.scope_key,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
    return rows


def _republisher_no_live_p0_rows(
    contract: CanonicalRepublisherNoLiveP0Contract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact_type, facts in (
        ("forward_applicability", contract.forward_applicability_facts),
        (
            "probability_distribution_artifact",
            contract.probability_distribution_artifact_facts,
        ),
        ("p0_snapshot", contract.p0_snapshot_facts),
    ):
        for fact in facts:
            mapping = fact.to_mapping()
            lineage = {
                "source_class": "aggregator_republisher",
                "transport_provider_id": "OPEN_METEO",
                "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                "selection_content_sha256": mapping["selection_content_sha256"],
                "native_arrival_indexed": False,
                "p1_eligible": False,
                "formal_live_authority": False,
                "production_no_live_only": True,
                "strict_oos_sample_eligible": (
                    False if fact_type != "probability_distribution_artifact" else None
                ),
                "materializes_p0": fact_type == "p0_snapshot",
            }
            rows.append(
                (
                    f"{fact_type}:{fact.content_sha256}",
                    fact_type,
                    fact.scope_key,
                    json.dumps(
                        mapping,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        lineage,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
    return rows


def _market_bucket_bridge_rows(
    contract: CanonicalMarketBucketBridgeContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.bridge_facts:
        mapping = fact.to_mapping()
        lineage = {
            "source_lane": "AWC",
            "authority_scope": "OFFICIAL_MARKET_REGISTRY",
            "market_registry_snapshot_sha256": mapping[
                "market_registry_snapshot_sha256"
            ],
            "registry_scope_rowset_sha256": mapping[
                "registry_scope_rowset_sha256"
            ],
            "formal_live_authority": False,
            "production_no_live_only": True,
            "materializes_p0": False,
        }
        rows.append(
            (
                f"market_bucket_bridge:{fact.content_sha256}",
                "market_bucket_bridge",
                fact.scope_key,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
    return rows


def _awc_observation_rows(
    contract: CanonicalAWCObservationContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact_type, facts in (
        ("truth_observation_revision", contract.revision_facts),
        ("truth_observation_day_bounds", contract.day_bounds_facts),
    ):
        for fact in facts:
            mapping = fact.raw
            if fact_type == "truth_observation_revision":
                lineage = {
                    "source_lane": "AWC",
                    "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                    "raw_object_kind": mapping["raw_object_kind"],
                    "raw_object_sha256": mapping["raw_object_sha256"],
                    "awc_source_receipt_sha256": mapping[
                        "awc_source_receipt_sha256"
                    ],
                    "origin_source_id": mapping["origin_source_id"],
                    "origin_url": mapping["origin_url"],
                    "origin_transport": mapping["origin_transport"],
                    "origin_registry_sha256": mapping[
                        "origin_registry_sha256"
                    ],
                    "origin_record_sha256": mapping["origin_record_sha256"],
                    "production_no_live_only": True,
                    "formal_live_authority": False,
                    "p1_eligible": mapping["qc_state"] == "ACCEPTED"
                    and mapping["halt_state"] == "CLEAR",
                }
            else:
                lineage = {
                    "source_lane": "AWC",
                    "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
                    "observation_revision_vector_sha256": mapping[
                        "observation_revision_vector_sha256"
                    ],
                    "coverage_receipt_sha256": mapping[
                        "coverage_receipt_sha256"
                    ],
                    "production_no_live_only": True,
                    "formal_live_authority": False,
                    "p1_eligible": mapping["closed_day"] is True
                    and mapping["halt_state"] == "CLEAR",
                }
            rows.append((
                f"{fact_type}:{fact.content_sha256}",
                fact_type,
                fact.scope_key,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
    return rows


def _truth_authority_supersession_rows(
    contract: CanonicalTruthAuthoritySupersessionContract,
) -> list[tuple[str, str, str, str, str]]:
    rows = _awc_observation_rows(contract.awc_observation_contract)
    for fact in contract.supersession_facts:
        mapping = fact.raw
        lineage = {
            "source_lane": "AWC",
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "predecessor_truth_day_bounds_content_sha256": mapping[
                "predecessor_truth_day_bounds_content_sha256"
            ],
            "successor_truth_day_bounds_content_sha256": mapping[
                "successor_truth_day_bounds_content_sha256"
            ],
            "authority_scope_projection_content_sha256": mapping[
                "authority_scope_projection_content_sha256"
            ],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "consumable": False,
        }
        rows.append(
            (
                f"truth_authority_supersession:{fact.content_sha256}",
                "truth_authority_supersession",
                fact.scope_key,
                json.dumps(
                    mapping,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    lineage,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    return rows


def _model_oos_rows(
    contract: CanonicalModelOOSContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact_type, facts in (
        ("model_oos_event", contract.event_facts),
        ("model_oos_cohort", contract.cohort_facts),
    ):
        for fact in facts:
            mapping = fact.raw
            lineage = (
                {
                    "source_class": mapping["source_class"],
                    "authority_scope": mapping["authority_scope"],
                    "model_input_content_sha256": mapping[
                        "model_input_content_sha256"
                    ],
                    "model_receipt_sha256": mapping["model_receipt_sha256"],
                    "canonical_truth_fact_content_sha256": mapping[
                        "canonical_truth_fact_content_sha256"
                    ],
                    "truth_id": mapping["truth_id"],
                    "truth_revision_no": mapping["truth_revision_no"],
                    "truth_content_sha256": mapping["truth_content_sha256"],
                    "production_no_live_only": True,
                    "formal_live_authority": False,
                }
                if fact_type == "model_oos_event"
                else {
                    "source_class": mapping["source_class"],
                    "authority_scope": mapping["authority_scope"],
                    "train_event_set_sha256": mapping["train_event_set_sha256"],
                    "holdout_event_set_sha256": mapping[
                        "holdout_event_set_sha256"
                    ],
                    "cohort_event_vector_sha256": mapping[
                        "cohort_event_vector_sha256"
                    ],
                    "production_no_live_only": True,
                    "formal_live_authority": False,
                }
            )
            rows.append((
                f"{fact_type}:{fact.content_sha256}",
                fact_type,
                fact.scope_key,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ))
    return rows


def _native_ensemble_oos_rows(
    contract: CanonicalNativeEnsembleOOSContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact_type, facts in (
        ("native_ensemble_oos_event", contract.event_facts),
        ("native_ensemble_oos_cohort", contract.cohort_facts),
    ):
        for fact in facts:
            mapping = fact.raw
            if fact_type == "native_ensemble_oos_event":
                lineage = {
                    "source_class": "native_official",
                    "authority_scope": mapping["authority_scope"],
                    "ensemble_system_id": mapping["ensemble_system_id"],
                    "member_evidence_vector_sha256": mapping[
                        "member_evidence_vector_sha256"
                    ],
                    "raw_message_vector_sha256": mapping[
                        "raw_message_vector_sha256"
                    ],
                    "raw_object_id_vector_sha256": mapping[
                        "raw_object_id_vector_sha256"
                    ],
                    "source_packet_sha256": mapping["source_packet_sha256"],
                    "availability_evidence_kind": mapping[
                        "availability_evidence_kind"
                    ],
                    "official_object_available_by_utc": mapping[
                        "official_object_available_by_utc"
                    ],
                    "native_arrival_indexed": False,
                    "formal_live_authority": False,
                    "p1_eligible": False,
                    "canonical_truth_fact_content_sha256": mapping[
                        "canonical_truth_fact_content_sha256"
                    ],
                    "truth_id": mapping["truth_id"],
                    "truth_revision_no": mapping["truth_revision_no"],
                    "truth_content_sha256": mapping["truth_content_sha256"],
                    "production_no_live_only": True,
                }
            else:
                lineage = {
                    "source_class": "native_official",
                    "authority_scope": mapping["authority_scope"],
                    "ensemble_system_id": mapping["ensemble_system_id"],
                    "train_event_set_sha256": mapping[
                        "train_event_set_sha256"
                    ],
                    "holdout_event_set_sha256": mapping[
                        "holdout_event_set_sha256"
                    ],
                    "cohort_event_vector_sha256": mapping[
                        "cohort_event_vector_sha256"
                    ],
                    "production_no_live_only": True,
                    "formal_live_authority": False,
                    "p1_eligible": False,
                }
            rows.append((
                f"{fact_type}:{fact.content_sha256}",
                fact_type,
                fact.scope_key,
                json.dumps(
                    mapping,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    lineage,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ))
    return rows


def _model_oos_selection_rows(
    contract: CanonicalModelOOSSelectionContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.selection_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "source_class": mapping["source_class"],
            "component_cohort_set_sha256": mapping[
                "component_cohort_set_sha256"
            ],
            "available_model_set_hash": mapping["available_model_set_hash"],
            "independence_family_set_hash": mapping[
                "independence_family_set_hash"
            ],
            "cohort_truth_vector_sha256": mapping["cohort_truth_vector_sha256"],
            "pre_registered_mask_receipt_sha256": mapping[
                "pre_registered_mask_receipt_sha256"
            ],
            "holdout_label_access_receipt_sha256": mapping[
                "holdout_label_access_receipt_sha256"
            ],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "p1_eligible": False,
        }
        rows.append((
            f"p0_model_family_selection:{fact.content_sha256}",
            "p0_model_family_selection",
            fact.scope_key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    return rows


def _p0_calibrated_distribution_rows(
    contract: CanonicalP0CalibratedDistributionContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.distribution_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "source_class": mapping["source_class"],
            "selection_content_sha256": mapping["selection_content_sha256"],
            "component_cohort_set_sha256": mapping["component_cohort_set_sha256"],
            "train_residual_scenario_set_sha256": mapping[
                "train_residual_scenario_set_sha256"
            ],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "p1_eligible": False,
        }
        rows.append((
            f"p0_calibrated_distribution:{fact.content_sha256}",
            "p0_calibrated_distribution",
            fact.scope_key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    return rows


def _p0_activation_root_rows(
    contract: CanonicalP0ActivationRootContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.activation_root_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "source_class": mapping["source_class"],
            "selection_content_sha256": mapping["selection_content_sha256"],
            "calibrated_distribution_content_sha256": mapping[
                "calibrated_distribution_content_sha256"
            ],
            "predecessor_activation_root_content_sha256": mapping[
                "predecessor_activation_root_content_sha256"
            ],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "consumable": False,
        }
        rows.append((
            f"p0_activation_root:{fact.content_sha256}", "p0_activation_root",
            fact.scope_key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    return rows


def _p0_forecast_snapshot_rows(
    contract: CanonicalP0ForecastSnapshotContract,
) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for fact in contract.snapshot_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "source_class": mapping["source_class"],
            "activation_root_content_sha256": mapping["activation_root_content_sha256"],
            "selection_content_sha256": mapping["selection_content_sha256"],
            "calibrated_distribution_content_sha256": mapping["calibrated_distribution_content_sha256"],
            "component_forward_receipt_set_sha256": mapping["component_forward_receipt_set_sha256"],
            "market_bucket_bridge_content_sha256": mapping[
                "market_bucket_bridge_content_sha256"
            ],
            "bucket_probability_vector_sha256": mapping[
                "bucket_probability_vector_sha256"
            ],
            "production_no_live_only": True, "formal_live_authority": False,
            "consumable": False,
        }
        rows.append((f"p0_forecast_snapshot:{fact.content_sha256}", "p0_forecast_snapshot", fact.scope_key, json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")), json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
    return rows


def _surface_observation_rows(
    contract: CanonicalSurfaceObservationContract,
) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for fact in contract.observation_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "source_lane": mapping["source_lane"],
            "source_class": mapping["source_class"],
            "provider_id": mapping["provider_id"],
            "raw_record_sha256": mapping["raw_record_sha256"],
            "source_evidence_receipt_sha256": mapping[
                "source_evidence_receipt_sha256"
            ],
            "measurement_semantics": mapping["measurement_semantics"],
            "production_no_live_only": True,
            "formal_live_authority": False,
        }
        rows.append((
            f"surface_observation:{fact.content_sha256}",
            "surface_observation", fact.scope_key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    return rows


def _surface_observation_winner_rows(
    contract: CanonicalSurfaceObservationWinnerContract,
) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for fact in contract.winner_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "priority_contract_version": mapping["priority_contract_version"],
            "candidate_terminal_set_sha256": mapping[
                "candidate_terminal_set_sha256"
            ],
            "selected_source_lane": mapping["selected_source_lane"],
            "selected_source_class": mapping["selected_source_class"],
            "selected_observation_content_sha256": mapping[
                "selected_observation_content_sha256"
            ],
            "production_no_live_only": True,
            "formal_live_authority": False,
        }
        rows.append((
            f"surface_observation_winner:{fact.content_sha256}",
            "surface_observation_winner", fact.scope_key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    return rows


def _p1_base_rows(
    contract: CanonicalP1BaseContract,
) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for fact in contract.p1_base_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "p0_forecast_snapshot_content_sha256": mapping[
                "p0_forecast_snapshot_content_sha256"
            ],
            "market_bucket_bridge_content_sha256": mapping[
                "market_bucket_bridge_content_sha256"
            ],
            "surface_observation_winner_content_sha256": mapping[
                "surface_observation_winner_content_sha256"
            ],
            "surface_observation_source_lane": mapping[
                "surface_observation_source_lane"
            ],
            "bucket_probability_vector_sha256": mapping[
                "bucket_probability_vector_sha256"
            ],
            "production_no_live_only": True,
            "formal_live_authority": False,
            "consumable": False,
        }
        rows.append((
            f"p1_base:{fact.content_sha256}", "p1_base", fact.scope_key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ))
    return rows


def _pure_yes_basket_plan_rows(
    contract: CanonicalPureYesBasketPlanContract,
) -> list[tuple[str, str, str, str, str]]:
    rows = []
    for fact in contract.basket_plan_facts:
        mapping = fact.raw
        lineage = {
            "authority_scope": mapping["authority_scope"],
            "p1_base_content_sha256": mapping["p1_base_content_sha256"],
            "p0_forecast_snapshot_content_sha256": mapping[
                "p0_forecast_snapshot_content_sha256"
            ],
            "market_bucket_bridge_content_sha256": mapping[
                "market_bucket_bridge_content_sha256"
            ],
            "surface_observation_winner_content_sha256": mapping[
                "surface_observation_winner_content_sha256"
            ],
            "bucket_probability_vector_sha256": mapping[
                "bucket_probability_vector_sha256"
            ],
            "policy_artifact_content_sha256": mapping[
                "policy_artifact_content_sha256"
            ],
            "candidate_ordinal_set_sha256": mapping[
                "candidate_ordinal_set_sha256"
            ],
            "buy_yes_only": True,
            "buy_no_intent_emitted": False,
            "production_no_live_only": True,
            "formal_live_authority": False,
            "consumable": False,
        }
        rows.append((
            f"pure_yes_basket_plan:{fact.content_sha256}",
            "pure_yes_basket_plan",
            fact.scope_key,
            json.dumps(
                mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            json.dumps(
                lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ))
    return rows


def _assert_model_oos_selection_references(
    conn: sqlite3.Connection,
    contract: CanonicalModelOOSSelectionContract,
) -> None:
    for fact in contract.selection_facts:
        rebuilt = build_model_oos_selection_contract(
            conn,
            component_cohort_refs=[
                {
                    "event_fact_type": item["event_fact_type"],
                    "cohort_fact_type": item["cohort_fact_type"],
                    "cohort_content_sha256": item[
                        "cohort_content_sha256"
                    ],
                }
                for item in fact.raw["component_cohort_refs"]
            ],
            pre_registered_mask_receipt=fact.raw[
                "pre_registered_mask_receipt"
            ],
            holdout_label_access_receipt=fact.raw[
                "holdout_label_access_receipt"
            ],
            as_of_utc=fact.raw["selection_as_of_utc"],
        )
        rebuilt_fact = CanonicalModelOOSSelectionContract.from_mapping(
            rebuilt
        ).selection_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError(
                "canonical_model_oos_selection_reference_rebuild_mismatch"
            )


def _assert_p0_calibrated_distribution_references(
    conn: sqlite3.Connection,
    contract: CanonicalP0CalibratedDistributionContract,
) -> None:
    for fact in contract.distribution_facts:
        rebuilt = build_p0_calibrated_distribution_contract(
            conn,
            selection_content_sha256=fact.raw["selection_content_sha256"],
            as_of_utc=fact.raw["distribution_as_of_utc"],
        )
        rebuilt_fact = CanonicalP0CalibratedDistributionContract.from_mapping(
            rebuilt
        ).distribution_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError(
                "canonical_p0_calibrated_distribution_reference_rebuild_mismatch"
            )


def _assert_p0_activation_root_references(
    conn: sqlite3.Connection, contract: CanonicalP0ActivationRootContract,
) -> None:
    for fact in contract.activation_root_facts:
        rebuilt = build_p0_activation_root_contract(
            conn,
            selection_content_sha256=fact.raw["selection_content_sha256"],
            calibrated_distribution_content_sha256=fact.raw[
                "calibrated_distribution_content_sha256"
            ],
            activation_generation=fact.raw["activation_generation"],
            predecessor_activation_root_content_sha256=fact.raw[
                "predecessor_activation_root_content_sha256"
            ],
            as_of_utc=fact.raw["activated_at_utc"],
        )
        rebuilt_fact = CanonicalP0ActivationRootContract.from_mapping(
            rebuilt
        ).activation_root_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError("canonical_p0_activation_root_reference_rebuild_mismatch")


def _assert_p0_forecast_snapshot_references(
    conn: sqlite3.Connection, contract: CanonicalP0ForecastSnapshotContract,
) -> None:
    for fact in contract.snapshot_facts:
        rebuilt = build_p0_forecast_snapshot_contract(
            conn,
            activation_root_content_sha256=fact.raw["activation_root_content_sha256"],
            component_forward_receipts=fact.raw["component_forward_receipts"],
            market_bucket_bridge_content_sha256=fact.raw[
                "market_bucket_bridge_content_sha256"
            ],
            as_of_utc=contract.as_of_utc,
        )
        rebuilt_fact = CanonicalP0ForecastSnapshotContract.from_mapping(rebuilt).snapshot_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError("canonical_p0_forecast_snapshot_reference_rebuild_mismatch")


def _assert_surface_observation_winner_references(
    conn: sqlite3.Connection,
    contract: CanonicalSurfaceObservationWinnerContract,
) -> None:
    for fact in contract.winner_facts:
        rebuilt = build_surface_observation_winner_contract(
            conn,
            station_id=fact.raw["station_id"],
            event_date=fact.raw["event_date"],
            settlement_timezone=fact.raw["settlement_timezone"],
            candidate_terminals=fact.raw["candidate_terminals"],
            as_of_utc=fact.raw["winner_as_of_utc"],
        )
        rebuilt_fact = CanonicalSurfaceObservationWinnerContract.from_mapping(
            rebuilt
        ).winner_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError(
                "canonical_surface_observation_winner_reference_rebuild_mismatch"
            )


def _assert_p1_base_references(
    conn: sqlite3.Connection,
    contract: CanonicalP1BaseContract,
) -> None:
    for fact in contract.p1_base_facts:
        decision = datetime.fromisoformat(
            fact.raw["decision_at_utc"].replace("Z", "+00:00")
        )
        expires = datetime.fromisoformat(
            fact.raw["freshness_expires_at_utc"].replace("Z", "+00:00")
        )
        freshness_seconds = int((expires - decision).total_seconds())
        rebuilt = build_p1_base_contract(
            conn,
            p0_forecast_snapshot_content_sha256=fact.raw[
                "p0_forecast_snapshot_content_sha256"
            ],
            surface_observation_winner_content_sha256=fact.raw[
                "surface_observation_winner_content_sha256"
            ],
            decision_at_utc=fact.raw["decision_at_utc"],
            freshness_seconds=freshness_seconds,
        )
        rebuilt_fact = CanonicalP1BaseContract.from_mapping(rebuilt).p1_base_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError("canonical_p1_base_reference_rebuild_mismatch")


def _assert_pure_yes_basket_plan_references(
    conn: sqlite3.Connection,
    contract: CanonicalPureYesBasketPlanContract,
) -> None:
    for fact in contract.basket_plan_facts:
        rebuilt = build_pure_yes_basket_plan_contract(
            conn,
            p1_base_content_sha256=fact.raw["p1_base_content_sha256"],
            plan_at_utc=fact.raw["plan_at_utc"],
        )
        rebuilt_fact = CanonicalPureYesBasketPlanContract.from_mapping(
            rebuilt
        ).basket_plan_facts[0]
        if rebuilt_fact.raw != fact.raw:
            raise ContractError(
                "canonical_pure_yes_basket_plan_reference_rebuild_mismatch"
            )


def _assert_v14_writer_ready(
    fact_type: str,
    *,
    commit: bool,
) -> None:
    """Reject a V14 write until that exact builder has been explicitly enabled.

    Preview is intentionally outside this gate: it must still parse and
    rebuild every parent reference without creating a row or touching the
    writer transaction.  Only the first V14 builder is registered today;
    the remaining V14 fact types have no ingress contract and therefore
    cannot bypass this explicit allowlist.
    """

    if commit and fact_type not in V14_WRITER_ALLOWLIST:
        raise ContractError(f"canonical_v14_writer_not_ready:{fact_type}")


def _assert_v15_writer_ready(fact_type: str, *, commit: bool) -> None:
    if commit and fact_type not in V15_WRITER_ALLOWLIST:
        raise ContractError(f"canonical_v15_writer_not_ready:{fact_type}")


def _assert_v16_writer_ready(fact_type: str, *, commit: bool) -> None:
    if commit and fact_type not in V16_WRITER_ALLOWLIST:
        raise ContractError(f"canonical_v16_writer_not_ready:{fact_type}")


def _assert_v17_writer_ready(fact_type: str, *, commit: bool) -> None:
    if commit and fact_type not in V17_WRITER_ALLOWLIST:
        raise ContractError(f"canonical_v17_writer_not_ready:{fact_type}")


def _assert_model_oos_event_truth_reference(
    conn: sqlite3.Connection,
    event: CanonicalModelOOSEventFact,
    *,
    contract_as_of_utc: str,
) -> None:
    raw = event.raw
    row = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth' AND content_sha256=?",
        (raw["canonical_truth_fact_content_sha256"],),
    ).fetchone()
    if row is None:
        raise ContractError("canonical_model_oos_truth_fact_missing")
    try:
        truth_raw = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("canonical_model_oos_truth_fact_json_invalid") from exc
    if not isinstance(truth_raw, Mapping):
        raise ContractError("canonical_model_oos_truth_fact_mapping_required")
    truth = CanonicalTruthFact.from_mapping(truth_raw)
    truth_committed_at = datetime.fromisoformat(
        truth.canonical_committed_at_utc.replace("Z", "+00:00")
    )
    event_truth_committed_at = datetime.fromisoformat(
        str(raw["truth_canonical_committed_at_utc"]).replace("Z", "+00:00")
    )
    truth_day_end = datetime.fromisoformat(
        truth.station_day_end_utc.replace("Z", "+00:00")
    )
    event_truth_day_end = datetime.fromisoformat(
        str(raw["station_day_end_utc"]).replace("Z", "+00:00")
    )
    contract_as_of = datetime.fromisoformat(
        contract_as_of_utc.replace("Z", "+00:00")
    )
    if (
        truth.content_sha256 != raw["canonical_truth_fact_content_sha256"]
        or truth.truth_id != raw["truth_id"]
        or truth.truth_revision_no != raw["truth_revision_no"]
        or truth.truth_content_sha256 != raw["truth_content_sha256"]
        or truth.station_id != raw["station_id"]
        or truth.event_date != raw["event_date"]
        or truth.settlement_timezone != raw["settlement_timezone"]
        or truth.truth_value != raw["truth_value_c"]
        or truth_day_end != event_truth_day_end
        or truth_committed_at != event_truth_committed_at
        or truth_committed_at > contract_as_of
        or not truth.closed_day
        or truth.source_lane != "AWC"
        or truth.authority_scope != raw["authority_scope"]
        or truth.settlement_timezone != "America/New_York"
        or truth.source_role not in {
            "AWC_HISTORICAL_REPLAY_INGRESS",
            "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1",
        }
    ):
        raise ContractError("canonical_model_oos_truth_fact_lineage_mismatch")

    latest_revision = -1
    for (candidate_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='truth' AND scope_key LIKE ?",
        (f"{truth.station_id}|{truth.event_date}|%",),
    ):
        try:
            candidate_raw = json.loads(str(candidate_json))
            candidate = CanonicalTruthFact.from_mapping(candidate_raw)
        except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
            raise ContractError(
                "canonical_model_oos_truth_revision_catalog_invalid"
            ) from exc
        candidate_committed_at = datetime.fromisoformat(
            candidate.canonical_committed_at_utc.replace("Z", "+00:00")
        )
        if (
            candidate.station_id == truth.station_id
            and candidate.event_date == truth.event_date
            and candidate.source_lane == "AWC"
            and candidate.source_role in {
                "AWC_HISTORICAL_REPLAY_INGRESS",
                "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1",
            }
            and candidate.authority_scope == truth.authority_scope
            and candidate.settlement_timezone == "America/New_York"
            and candidate.closed_day is True
            and candidate_committed_at <= contract_as_of
        ):
            latest_revision = max(latest_revision, candidate.truth_revision_no)
    if latest_revision != truth.truth_revision_no:
        raise ContractError("canonical_model_oos_truth_revision_stale")


def _load_model_oos_event_by_hash(
    conn: sqlite3.Connection,
    content_sha256_value: str,
) -> CanonicalModelOOSEventFact | None:
    row = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='model_oos_event' AND content_sha256=?",
        (content_sha256_value,),
    ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("canonical_model_oos_event_json_invalid") from exc
    if not isinstance(raw, Mapping):
        raise ContractError("canonical_model_oos_event_mapping_required")
    return CanonicalModelOOSEventFact.from_mapping(raw)


def _assert_model_oos_references(
    conn: sqlite3.Connection,
    contract: CanonicalModelOOSContract,
) -> None:
    in_packet = {item.content_sha256: item for item in contract.event_facts}
    for event in contract.event_facts:
        _assert_model_oos_event_truth_reference(
            conn, event, contract_as_of_utc=contract.as_of_utc
        )
    for cohort in contract.cohort_facts:
        hashes = [
            *cohort.raw["train_event_content_sha256s"],
            *cohort.raw["holdout_event_content_sha256s"],
        ]
        events = [
            in_packet.get(value) or _load_model_oos_event_by_hash(conn, value)
            for value in hashes
        ]
        if any(item is None for item in events):
            raise ContractError("canonical_model_oos_cohort_event_missing")
        resolved = [item for item in events if item is not None]
        scope_fields = (
            "station_id", "model_id", "ensemble_system_id", "model_family",
            "provider_id", "provider_product_id", "independence_family_id",
            "source_class", "authority_scope", "settlement_timezone", "lead_bucket",
            "lead_bucket_scope", "run_cycle", "run_cycle_scope",
            "event_oos_protocol_version",
        )
        if any(
            any(event.raw[field] != cohort.raw[field] for field in scope_fields)
            for event in resolved
        ):
            raise ContractError("canonical_model_oos_cohort_event_scope_mismatch")
        dates = [event.raw["event_date"] for event in resolved]
        if dates[:20] != sorted(dates[:20]) or dates[20:] != sorted(dates[20:]) or dates[19] >= dates[20]:
            raise ContractError("canonical_model_oos_cohort_event_date_order_invalid")
        if any(
            max(
                event.raw["model_available_at_utc"],
                event.raw["truth_canonical_committed_at_utc"],
            )
            > cohort.raw["cohort_as_of_utc"]
            for event in resolved
        ):
            raise ContractError("canonical_model_oos_cohort_asof_invalid")
        parameters, train_metrics, holdout_metrics, baseline_metrics, evaluation_state = (
            derive_cohort_statistics(resolved)
        )
        if (
            cohort.raw["calibration_parameters"] != parameters
            or cohort.raw["selection_train_metrics"] != train_metrics
            or cohort.raw["holdout_metrics"] != holdout_metrics
            or cohort.raw["baseline_holdout_metrics"] != baseline_metrics
            or cohort.raw["evaluation_state"] != evaluation_state
        ):
            raise ContractError("canonical_model_oos_cohort_statistics_mismatch")


def _assert_native_ensemble_oos_references(
    conn: sqlite3.Connection,
    contract: CanonicalNativeEnsembleOOSContract,
) -> None:
    """Reverse-check historical native member sets without making arrivals."""

    for event in contract.event_facts:
        _assert_model_oos_event_truth_reference(
            conn, event, contract_as_of_utc=contract.as_of_utc
        )
        packet_ref = Path(str(event.raw["source_packet_ref"]))
        if not packet_ref.is_absolute():
            raise ContractError(
                "canonical_native_ensemble_oos_source_packet_ref_not_absolute"
            )
        try:
            packet_raw = json.loads(packet_ref.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError(
                "canonical_native_ensemble_oos_source_packet_unreadable"
            ) from exc
        packet_identity = (
            {
                key: value
                for key, value in packet_raw.items()
                if key != "content_sha256"
            }
            if isinstance(packet_raw, Mapping)
            else {}
        )
        if (
            not packet_identity
            or packet_raw.get("content_sha256")
            != event.raw["source_packet_sha256"]
            or content_sha256(packet_identity)
            != event.raw["source_packet_sha256"]
        ):
            raise ContractError(
                "canonical_native_ensemble_oos_source_packet_hash_mismatch"
            )
        from .native_ensemble_oos_builder import (
            build_native_ensemble_contract_event_identity,
            build_native_ensemble_oos_event_inputs,
        )

        adapter_inputs = build_native_ensemble_oos_event_inputs(
            packet_raw,
            source_packet_ref=packet_ref,
            station_ids=[event.raw["station_id"]],
        )
        matches = [
            item
            for item in adapter_inputs
            if item["station_id"] == event.raw["station_id"]
            and item["event_date"] == event.raw["event_date"]
            and item["ensemble_system_id"] == event.raw["ensemble_system_id"]
        ]
        if len(matches) != 1:
            raise ContractError(
                "canonical_native_ensemble_oos_adapter_input_not_unique"
            )
        truth_binding = {
            "station_id": event.raw["station_id"],
            "event_date": event.raw["event_date"],
            "forecast_decision_at_utc": event.raw[
                "forecast_decision_at_utc"
            ],
            "canonical_truth_fact_content_sha256": event.raw[
                "canonical_truth_fact_content_sha256"
            ],
            "truth_id": event.raw["truth_id"],
            "truth_revision_no": event.raw["truth_revision_no"],
            "truth_content_sha256": event.raw["truth_content_sha256"],
            "truth_value_c": event.raw["truth_value_c"],
            "station_day_end_utc": event.raw["station_day_end_utc"],
            "truth_canonical_committed_at_utc": event.raw[
                "truth_canonical_committed_at_utc"
            ],
        }
        rebuilt = build_native_ensemble_contract_event_identity(
            matches[0], truth_binding
        )
        if rebuilt != event.raw:
            raise ContractError(
                "canonical_native_ensemble_oos_adapter_rebuild_mismatch"
            )


def _assert_republisher_oos_artifact_references(
    conn: sqlite3.Connection,
    contract: CanonicalRepublisherOOSArtifactContract,
) -> None:
    """Resolve every case to exact active canonical AWC truth facts."""

    for artifact in contract.artifact_facts:
        registry_rows = conn.execute(
            "SELECT DISTINCT settlement_definition FROM calibration_station_registry "
            "WHERE station_id=? AND settlement_definition<>''",
            (artifact.station_id,),
        ).fetchall()
        if (
            len(registry_rows) != 1
            or str(registry_rows[0][0]) != artifact.settlement_definition
        ):
            raise ContractError(
                "canonical_republisher_oos_settlement_definition_mismatch"
            )
        for case in artifact.cohort_rows:
            row = conn.execute(
                "SELECT fact_json FROM canonical_ssot_fact "
                "WHERE fact_type='truth' AND content_sha256=?",
                (case.canonical_truth_fact_content_sha256,),
            ).fetchone()
            if row is None:
                raise ContractError(
                    "canonical_republisher_oos_truth_fact_missing"
                )
            try:
                raw = json.loads(str(row[0]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContractError(
                    "canonical_republisher_oos_truth_fact_json_invalid"
                ) from exc
            if not isinstance(raw, Mapping):
                raise ContractError(
                    "canonical_republisher_oos_truth_fact_mapping_required"
                )
            truth = CanonicalTruthFact.from_mapping(raw)
            if (
                truth.station_id != artifact.station_id
                or truth.event_date != case.event_date
                or truth.truth_id != case.awc_truth_id
                or truth.truth_revision_no != case.awc_truth_revision_no
                or truth.truth_content_sha256 != case.awc_truth_content_sha256
                or truth.canonical_committed_at_utc != case.awc_truth_asof_utc
                or truth.station_day_end_utc != case.station_day_end_utc
                or truth.settlement_timezone != artifact.settlement_timezone
                or truth.source_lane != "AWC"
                or truth.closed_day is not True
                or truth.canonical_committed_at_utc > contract.as_of_utc
            ):
                raise ContractError(
                    "canonical_republisher_oos_truth_fact_lineage_mismatch"
                )
            latest_revision = -1
            for (candidate_json,) in conn.execute(
                "SELECT fact_json FROM canonical_ssot_fact "
                "WHERE fact_type='truth' AND scope_key LIKE ?",
                (f"{artifact.station_id}|{case.event_date}|%",),
            ):
                try:
                    candidate_raw = json.loads(str(candidate_json))
                    candidate_truth = CanonicalTruthFact.from_mapping(candidate_raw)
                except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
                    raise ContractError(
                        "canonical_republisher_oos_truth_revision_catalog_invalid"
                    ) from exc
                if (
                    candidate_truth.station_id == artifact.station_id
                    and candidate_truth.event_date == case.event_date
                    and candidate_truth.source_lane == "AWC"
                    and candidate_truth.closed_day is True
                    and candidate_truth.canonical_committed_at_utc <= contract.as_of_utc
                ):
                    latest_revision = max(
                        latest_revision, candidate_truth.truth_revision_no
                    )
            if latest_revision != case.awc_truth_revision_no:
                raise ContractError(
                    "canonical_republisher_oos_truth_revision_stale"
                )


def _persisted_fact_by_content_hash(
    conn: sqlite3.Connection,
    *,
    fact_type: str,
    content_sha256_value: str,
) -> (
    CanonicalEnsembleMemberFact
    | CanonicalNativeArrivalFact
    | CanonicalEnsembleMemberReceiptManifestFact
    | None
):
    row = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type=? AND content_sha256=?",
        (fact_type, content_sha256_value),
    ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(str(row[0]))
        if not isinstance(raw, Mapping):
            raise TypeError("persisted fact is not an object")
        if fact_type == "ensemble_member":
            return CanonicalEnsembleMemberFact.from_mapping(raw)
        if fact_type == "native_arrival":
            return CanonicalNativeArrivalFact.from_mapping(raw)
        if fact_type == "ensemble_member_receipt_manifest":
            return CanonicalEnsembleMemberReceiptManifestFact.from_mapping(raw)
    except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
        raise ContractError(
            f"canonical_binding_persisted_{fact_type}_invalid"
        ) from exc
    raise ContractError("canonical_binding_fact_type_invalid")


def _assert_member_arrival_bindings(
    conn: sqlite3.Connection, contract: CanonicalContract
) -> None:
    """Resolve manifests/bindings and reject overlapping receipt coverage."""

    packet_members = {
        item.content_sha256: item for item in contract.ensemble_member_facts
    }
    packet_arrivals = {
        item.content_sha256: item for item in contract.native_arrival_facts
    }
    packet_manifests = {
        item.content_sha256: item
        for item in contract.ensemble_member_receipt_manifest_facts
    }
    for manifest in contract.ensemble_member_receipt_manifest_facts:
        member = packet_members.get(manifest.ensemble_member_content_sha256)
        if member is None:
            member = _persisted_fact_by_content_hash(
                conn,
                fact_type="ensemble_member",
                content_sha256_value=manifest.ensemble_member_content_sha256,
            )
        if not isinstance(member, CanonicalEnsembleMemberFact):
            raise ContractError("canonical_receipt_manifest_member_fact_missing")
        if member.source_class != "native_official":
            raise ContractError("canonical_receipt_manifest_member_not_native")
        if (
            manifest.source_raw_receipt_set_sha256
            != member.raw_receipt_set_sha256
        ):
            raise ContractError("canonical_receipt_manifest_source_set_mismatch")
        if any(
            ref.receipt_evidence_at_utc > contract.as_of_utc
            for ref in manifest.required_receipt_refs
        ):
            raise ContractError("canonical_receipt_manifest_after_contract_asof")

    existing_coverage: dict[tuple[str, str], set[int]] = {}
    for (raw_json,) in conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='ensemble_member_arrival_binding'"
    ):
        try:
            raw = json.loads(str(raw_json))
            if not isinstance(raw, Mapping):
                raise TypeError("binding fact is not an object")
            persisted_binding = (
                CanonicalEnsembleMemberArrivalBindingFact.from_mapping(raw)
            )
        except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
            raise ContractError("canonical_binding_persisted_binding_invalid") from exc
        key = (
            persisted_binding.ensemble_member_content_sha256,
            persisted_binding.receipt_manifest_content_sha256,
        )
        ordinals = existing_coverage.setdefault(key, set())
        overlap = ordinals.intersection(
            persisted_binding.manifest_entry_ordinals
        )
        if overlap:
            raise ContractError("canonical_binding_persisted_ordinal_overlap")
        ordinals.update(persisted_binding.manifest_entry_ordinals)

    for binding in contract.ensemble_member_arrival_binding_facts:
        member = packet_members.get(binding.ensemble_member_content_sha256)
        if member is None:
            member = _persisted_fact_by_content_hash(
                conn,
                fact_type="ensemble_member",
                content_sha256_value=binding.ensemble_member_content_sha256,
            )
        arrival = packet_arrivals.get(binding.native_arrival_content_sha256)
        if arrival is None:
            arrival = _persisted_fact_by_content_hash(
                conn,
                fact_type="native_arrival",
                content_sha256_value=binding.native_arrival_content_sha256,
            )
        manifest = packet_manifests.get(
            binding.receipt_manifest_content_sha256
        )
        if manifest is None:
            manifest = _persisted_fact_by_content_hash(
                conn,
                fact_type="ensemble_member_receipt_manifest",
                content_sha256_value=binding.receipt_manifest_content_sha256,
            )
        if member is None:
            raise ContractError("canonical_binding_member_fact_missing")
        if arrival is None:
            raise ContractError("canonical_binding_native_arrival_fact_missing")
        if not isinstance(manifest, CanonicalEnsembleMemberReceiptManifestFact):
            raise ContractError("canonical_binding_receipt_manifest_fact_missing")
        assert isinstance(member, CanonicalEnsembleMemberFact)
        assert isinstance(arrival, CanonicalNativeArrivalFact)
        if member.source_class != "native_official":
            raise ContractError("canonical_binding_member_not_native_official")
        if arrival.source_class != "native_official":
            raise ContractError("canonical_binding_arrival_not_native_official")
        if not (
            arrival.native_arrival_indexed
            and arrival.published_at_known
            and arrival.received_at_known
            and arrival.raw_object_sha256
        ):
            raise ContractError("canonical_binding_arrival_not_indexed")
        if arrival.received_at_utc > contract.as_of_utc:
            raise ContractError("canonical_binding_arrival_after_contract_asof")
        if manifest.ensemble_member_content_sha256 != member.content_sha256:
            raise ContractError("canonical_binding_manifest_member_mismatch")
        if (
            manifest.source_raw_receipt_set_sha256
            != member.raw_receipt_set_sha256
        ):
            raise ContractError("canonical_binding_manifest_source_set_mismatch")
        expected_common = (
            member.ensemble_system_id,
            member.provider_id,
            member.run_init_utc,
            member.lead_bucket,
            member.run_cycle,
        )
        if expected_common != (
            arrival.ensemble_system_id,
            arrival.provider_id,
            arrival.run_init_utc,
            arrival.lead_bucket,
            arrival.run_cycle,
        ):
            raise ContractError("canonical_binding_arrival_scope_mismatch")
        refs = manifest.required_receipt_refs
        if any(ordinal >= len(refs) for ordinal in binding.manifest_entry_ordinals):
            raise ContractError("canonical_binding_ordinal_out_of_range")
        if any(
            refs[ordinal].native_object_id != arrival.native_object_id
            or refs[ordinal].raw_object_sha256 != arrival.raw_object_sha256
            for ordinal in binding.manifest_entry_ordinals
        ):
            raise ContractError("canonical_binding_manifest_object_mismatch")
        key = (member.content_sha256, manifest.content_sha256)
        ordinals = existing_coverage.setdefault(key, set())
        overlap = ordinals.intersection(binding.manifest_entry_ordinals)
        if overlap:
            # An exact replay is handled by fact-id replay later.  A distinct
            # arrival claiming an already-covered ordinal is ambiguous.
            persisted_same = conn.execute(
                "SELECT 1 FROM canonical_ssot_fact WHERE fact_id=?",
                (f"ensemble_member_arrival_binding:{binding.content_sha256}",),
            ).fetchone()
            if persisted_same is None:
                raise ContractError("canonical_binding_ordinal_overlap")
        ordinals.update(binding.manifest_entry_ordinals)


def _supersession_fact_rows(
    contract: CanonicalActivationSupersessionContract,
) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for fact in contract.supersession_facts:
        mapping = fact.to_mapping()
        fact_id = f"activation_supersession:{fact.content_sha256}"
        lineage = {
            "authority_scope": fact.authority_scope,
            "reason": fact.reason,
            "candidate_activation_set_sha256": fact.candidate_activation_set_sha256,
            "semantic_equivalence_sha256": fact.semantic_equivalence_sha256,
        }
        rows.append(
            (
                fact_id,
                "activation_supersession",
                fact.scope_key,
                json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        )
    return rows


def _assert_activation_supersession_authority(
    conn: sqlite3.Connection,
    contract: CanonicalActivationSupersessionContract,
) -> None:
    existing_rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact "
        "WHERE fact_type='activation_supersession'"
    ).fetchall()
    predecessor_heads: dict[str, str] = {}
    successor_predecessors: dict[str, str] = {}
    for row in existing_rows:
        try:
            existing_fact = CanonicalActivationSupersessionFact.from_mapping(
                json.loads(str(row[0]))
            )
        except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
            raise ContractError(
                "canonical_activation_supersession_persisted_fact_invalid"
            ) from exc
        predecessor_heads[existing_fact.predecessor_activation_content_sha256] = (
            existing_fact.successor_activation_content_sha256
        )
        successor_predecessors[existing_fact.successor_activation_content_sha256] = (
            existing_fact.predecessor_activation_content_sha256
        )

    semantic_fields = (
        "artifact_content_sha256",
        "selection_evidence_sha256",
        "available_model_set_hash",
        "candidate_models",
        "selected_models",
        "selected_weights",
        "oos_role",
        "authority_scope",
    )
    for fact in contract.supersession_facts:
        predecessor = fact.predecessor_activation_content_sha256
        successor = fact.successor_activation_content_sha256
        if predecessor in predecessor_heads and predecessor_heads[predecessor] != successor:
            raise ContractError("canonical_activation_supersession_predecessor_conflict")
        if successor in successor_predecessors and successor_predecessors[successor] != predecessor:
            raise ContractError("canonical_activation_supersession_successor_conflict")
        predecessor_heads[predecessor] = successor
        successor_predecessors[successor] = predecessor

        rows = conn.execute(
            "SELECT fact_json,content_sha256 FROM canonical_ssot_fact "
            "WHERE fact_type='activation' AND ingested_at_utc<=? "
            "AND json_extract(fact_json,'$.station_id')=? "
            "AND json_extract(fact_json,'$.model_family')=? "
            "AND json_extract(fact_json,'$.lead_bucket')=? "
            "AND json_extract(fact_json,'$.run_cycle')=? "
            "ORDER BY content_sha256",
            (
                fact.authority_as_of_utc,
                fact.station_id,
                fact.model_family,
                fact.lead_bucket,
                fact.run_cycle,
            ),
        ).fetchall()
        activations: dict[str, CanonicalActivationFact] = {}
        for raw_json, stored_hash in rows:
            try:
                activation = CanonicalActivationFact.from_mapping(json.loads(str(raw_json)))
            except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
                raise ContractError(
                    "canonical_activation_supersession_activation_fact_invalid"
                ) from exc
            if activation.activation_content_sha256 != str(stored_hash):
                raise ContractError("canonical_activation_supersession_activation_hash_mismatch")
            activations[activation.activation_content_sha256] = activation
        if tuple(sorted(activations)) != fact.candidate_activation_content_sha256s:
            raise ContractError("canonical_activation_supersession_candidate_set_incomplete")
        before = activations.get(predecessor)
        after = activations.get(successor)
        if before is None or after is None:
            raise ContractError("canonical_activation_supersession_activation_missing")
        expected_scope = (fact.station_id, fact.model_family, fact.lead_bucket, fact.run_cycle)
        for activation, generation in (
            (before, fact.predecessor_generation),
            (after, fact.successor_generation),
        ):
            actual_scope = (
                activation.station_id,
                activation.model_family,
                activation.lead_bucket,
                activation.run_cycle,
            )
            if actual_scope != expected_scope or activation.activation_generation != generation:
                raise ContractError("canonical_activation_supersession_scope_mismatch")
        if fact.reason == "IDENTITY_DEDUPLICATION":
            before_semantic = {field: before.to_mapping()[field] for field in semantic_fields}
            after_semantic = {field: after.to_mapping()[field] for field in semantic_fields}
            if before_semantic != after_semantic:
                raise ContractError("canonical_activation_supersession_semantic_mismatch")
            if content_sha256(before_semantic) != fact.semantic_equivalence_sha256:
                raise ContractError("canonical_activation_supersession_semantic_hash_mismatch")


def _assert_activation_generation_not_rewound(
    conn: sqlite3.Connection,
    contract: CanonicalContract,
) -> None:
    """Reject an activation generation lower than an already persisted head.

    Generation remains part of the append-only activation fact scope, but it
    is not permission to append history backwards.  This preflight runs before
    the ingress savepoint writes *any* fact, so a packet containing otherwise
    insertable model/truth rows cannot leave a partial write when one of its
    activation generations is a rewind.  It deliberately does not select a
    current activation; multiple forward generations still require PR04's
    explicit current-root authority and otherwise remain blocked.
    """

    for activation in contract.activation_facts:
        try:
            rows = conn.execute(
                "SELECT fact_json FROM canonical_ssot_fact "
                "WHERE fact_type='activation' "
                "AND json_extract(fact_json,'$.station_id')=? "
                "AND json_extract(fact_json,'$.model_family')=? "
                "AND json_extract(fact_json,'$.lead_bucket')=? "
                "AND json_extract(fact_json,'$.run_cycle')=?",
                (
                    activation.station_id,
                    activation.model_family,
                    activation.lead_bucket,
                    activation.run_cycle,
                ),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ContractError(
                "canonical_activation_generation_preflight_unavailable"
            ) from exc
        persisted_generations: list[int] = []
        for row in rows:
            try:
                raw = json.loads(str(row[0]))
                persisted = CanonicalActivationFact.from_mapping(raw)
            except (TypeError, ValueError, json.JSONDecodeError, ContractError) as exc:
                raise ContractError(
                    "canonical_activation_generation_persisted_fact_invalid"
                ) from exc
            persisted_generations.append(persisted.activation_generation)
        if (
            persisted_generations
            and activation.activation_generation < max(persisted_generations)
        ):
            raise ContractError("canonical_activation_generation_rewind")


def _projection_timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"canonical_awc_truth_projection_{field}_missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(
            f"canonical_awc_truth_projection_{field}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            f"canonical_awc_truth_projection_{field}_timezone_required"
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _project_awc_daily_truths(
    conn: sqlite3.Connection,
    contract: CanonicalContract,
    projection: Mapping[str, Any],
    *,
    commit: bool,
) -> dict[str, Any]:
    """Project exact formal AWC truth rows from the just-validated contract.

    This deliberately accepts no legacy truth packet.  Each projection row
    repeats the canonical truth identity plus the legacy-only raw-evidence
    fields, then proves an exact one-to-one mapping before touching the
    projection table.  It is called only inside :func:`ingest_canonical_contract`'s
    savepoint, so an insertion failure rolls both canonical and legacy rows
    back together.
    """

    if not isinstance(projection, Mapping):
        raise ContractError("canonical_awc_truth_projection_mapping_required")
    if projection.get("schema") != AWC_DAILY_TRUTH_PROJECTION_SCHEMA:
        raise ContractError("canonical_awc_truth_projection_schema_invalid")
    if set(projection) != {"schema", "truth_rows"}:
        raise ContractError("canonical_awc_truth_projection_unknown_field")
    raw_rows = projection.get("truth_rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ContractError("canonical_awc_truth_projection_rows_required")
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ContractError("canonical_awc_truth_projection_row_invalid")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_station_daily_high_truth'"
    ).fetchone()
    if table is None:
        raise ContractError("canonical_awc_truth_projection_table_missing")

    truths = {fact.truth_id: fact.to_mapping() for fact in contract.truth_facts}
    if not truths:
        raise ContractError("canonical_awc_truth_projection_contract_truth_missing")
    if len(raw_rows) != len(truths):
        raise ContractError("canonical_awc_truth_projection_truth_count_mismatch")

    inserted = 0
    replayed = 0
    seen_truth_ids: set[str] = set()
    columns = (
        "truth_id",
        "station_id",
        "local_date",
        "settlement_timezone",
        "official_tmax_c",
        "source_lane",
        "settlement_definition",
        "revision_no",
        "published_at_utc",
        "received_at_utc",
        "canonical_committed_at_utc",
        "timestamps_complete",
        "qc_status",
        "source_role",
        "source_payload_sha256",
        "observation_ids_json",
        "authority_scope",
        "content_sha256",
    )
    for raw in raw_rows:
        if set(raw) != _AWC_DAILY_TRUTH_PROJECTION_FIELDS:
            missing = sorted(_AWC_DAILY_TRUTH_PROJECTION_FIELDS - set(raw))
            unknown = sorted(set(raw) - _AWC_DAILY_TRUTH_PROJECTION_FIELDS)
            suffix = ":" + ",".join(missing or unknown) if (missing or unknown) else ""
            raise ContractError("canonical_awc_truth_projection_fields_invalid" + suffix)
        truth_id = raw.get("truth_id")
        if not isinstance(truth_id, str) or not truth_id.strip():
            raise ContractError("canonical_awc_truth_projection_truth_id_missing")
        if truth_id in seen_truth_ids:
            raise ContractError("canonical_awc_truth_projection_truth_duplicate")
        seen_truth_ids.add(truth_id)
        canonical = truths.get(truth_id)
        if canonical is None:
            raise ContractError("canonical_awc_truth_projection_truth_not_in_contract")
        for field in (
            "station_id",
            "event_date",
            "truth_revision_no",
            "truth_value",
            "truth_content_sha256",
            "canonical_committed_at_utc",
            "station_day_end_utc",
            "closed_day",
            "source_lane",
            "source_role",
            "authority_scope",
            "settlement_timezone",
        ):
            if raw.get(field) != canonical[field]:
                raise ContractError(
                    "canonical_awc_truth_projection_canonical_mismatch:" + field
                )
        if (
            raw["source_lane"] != "AWC"
            or raw["authority_scope"] != "FORMAL_NATIVE_ELIGIBLE"
            or raw["closed_day"] is not True
            or raw["source_role"] != _FORMAL_AWC_TRUTH_ROLE
            or raw["settlement_definition"] != "official_daily_tmax_c"
            or raw["qc_status"] != "ACCEPTED"
            or raw["timestamps_complete"] != 1
        ):
            raise ContractError("canonical_awc_truth_projection_authority_invalid")
        published_at = _projection_timestamp(
            raw["published_at_utc"], field="published_at_utc"
        )
        received_at = _projection_timestamp(
            raw["received_at_utc"], field="received_at_utc"
        )
        if published_at > received_at:
            raise ContractError("canonical_awc_truth_projection_timestamp_order_invalid")
        if not isinstance(raw["observation_ids_json"], str) or not raw[
            "observation_ids_json"
        ]:
            raise ContractError("canonical_awc_truth_projection_observation_evidence_missing")
        legacy_identity = {
            "truth_id": truth_id,
            "station_id": raw["station_id"],
            "local_date": raw["event_date"],
            "settlement_timezone": raw["settlement_timezone"],
            "official_tmax_c": raw["truth_value"],
            "source_lane": raw["source_lane"],
            "settlement_definition": raw["settlement_definition"],
            "revision_no": raw["truth_revision_no"],
            "published_at_utc": published_at,
            "received_at_utc": received_at,
            "canonical_committed_at_utc": raw["canonical_committed_at_utc"],
            "timestamps_complete": raw["timestamps_complete"],
            "qc_status": raw["qc_status"],
            "source_role": raw["source_role"],
            "source_payload_sha256": raw["truth_content_sha256"],
            "observation_ids_json": raw["observation_ids_json"],
            "authority_scope": raw["authority_scope"],
        }
        supplied_sha = raw["content_sha256"]
        if not isinstance(supplied_sha, str) or supplied_sha.lower() != content_sha256(
            legacy_identity
        ):
            raise ContractError("canonical_awc_truth_projection_content_sha256_mismatch")
        values = tuple(legacy_identity[column] for column in columns[:-1]) + (
            supplied_sha.lower(),
        )
        existing = conn.execute(
            "SELECT " + ",".join(columns) + " "
            "FROM calibration_station_daily_high_truth WHERE truth_id=?",
            (truth_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != values:
                raise ContractError("canonical_awc_truth_projection_content_conflict")
            replayed += 1
            continue
        scope = conn.execute(
            "SELECT truth_id,content_sha256 FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND local_date=? "
            "AND settlement_definition=? AND revision_no=?",
            (
                legacy_identity["station_id"],
                legacy_identity["local_date"],
                legacy_identity["settlement_definition"],
                legacy_identity["revision_no"],
            ),
        ).fetchone()
        if scope is not None:
            raise ContractError("canonical_awc_truth_projection_scope_content_conflict")
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            values,
        )
        inserted += 1
    if seen_truth_ids != set(truths):
        raise ContractError("canonical_awc_truth_projection_truth_set_mismatch")
    result = {
        "schema": AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "validated_truth_count": len(raw_rows),
        "inserted_count": inserted if commit else 0,
        "replayed_count": replayed,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**result, "receipt_sha256": content_sha256(result)}


def ingest_canonical_contract(
    conn: sqlite3.Connection,
    packet: (
        Mapping[str, Any]
        | CanonicalContract
        | CanonicalActivationSupersessionContract
        | CanonicalRepublisherOOSArtifactContract
        | CanonicalRepublisherCrossSelectionContract
        | CanonicalRepublisherNoLiveActivationContract
        | CanonicalRepublisherNoLiveP0Contract
        | CanonicalMarketBucketBridgeContract
        | CanonicalAWCObservationContract
        | CanonicalModelOOSContract
        | CanonicalNativeEnsembleOOSContract
        | CanonicalModelOOSSelectionContract
        | CanonicalP0CalibratedDistributionContract
        | CanonicalP0ActivationRootContract
        | CanonicalP0ForecastSnapshotContract
        | CanonicalSurfaceObservationContract
        | CanonicalSurfaceObservationWinnerContract
        | CanonicalP1BaseContract
        | CanonicalPureYesBasketPlanContract
        | CanonicalTruthAuthoritySupersessionContract
    ),
    *,
    commit: bool = False,
    finalize_transaction: bool = True,
    ingested_at_utc: str | None = None,
    # Kept only for an explicit, typed retirement failure below.  Callers
    # must submit canonical facts directly and may use only the AWC
    # canonical-to-audit projection.
    legacy_projection: Mapping[str, Any] | None = None,
    awc_daily_truth_projection: Mapping[str, Any] | None = None,
    external_evidence_root: Path | str | None = None,
) -> dict[str, Any]:
    """Validate and append one canonical contract atomically.

    ``commit=False`` performs the complete validation and transaction replay
    check but leaves no rows behind.  No DDL is installed here; a missing
    ``canonical_ssot_fact`` table is an explicit migration blocker.  A
    ``commit=True, finalize_transaction=False`` call releases only this
    ingress's savepoint into an already-open caller transaction.
    """

    if not commit and not finalize_transaction:
        raise ContractError("canonical_ingress_uncommitted_finalize_mode_invalid")
    if commit and not finalize_transaction and not conn.in_transaction:
        # In SQLite, releasing a top-level savepoint commits it.  Staged
        # mode therefore requires an actual caller-owned outer transaction.
        raise ContractError("canonical_ingress_outer_transaction_required")
    if commit and finalize_transaction and conn.in_transaction:
        # Finalizing a caller-owned transaction would commit unrelated caller
        # work.  Callers that own the outer transaction must explicitly use
        # staged mode and decide whether to commit or roll back themselves.
        raise ContractError(
            "canonical_ingress_caller_transaction_requires_finalize_false"
        )
    if legacy_projection is not None:
        raise ContractError("canonical_ingress_legacy_projection_retired")
    if isinstance(
        packet,
        (
            CanonicalContract,
            CanonicalActivationSupersessionContract,
            CanonicalRepublisherOOSArtifactContract,
            CanonicalRepublisherCrossSelectionContract,
            CanonicalRepublisherNoLiveActivationContract,
            CanonicalRepublisherNoLiveP0Contract,
            CanonicalMarketBucketBridgeContract,
            CanonicalAWCObservationContract,
            CanonicalModelOOSContract,
            CanonicalNativeEnsembleOOSContract,
            CanonicalModelOOSSelectionContract,
            CanonicalP0CalibratedDistributionContract,
            CanonicalP0ActivationRootContract,
            CanonicalP0ForecastSnapshotContract,
            CanonicalSurfaceObservationContract,
            CanonicalSurfaceObservationWinnerContract,
            CanonicalP1BaseContract,
            CanonicalPureYesBasketPlanContract,
            CanonicalTruthAuthoritySupersessionContract,
        ),
    ):
        contract = packet
    elif packet.get("schema") == ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA:
        contract = CanonicalActivationSupersessionContract.from_mapping(packet)
    elif packet.get("schema") == REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA:
        contract = CanonicalRepublisherOOSArtifactContract.from_mapping(packet)
    elif packet.get("schema") == REPUBLISHER_CROSS_SELECTION_CONTRACT_SCHEMA:
        contract = CanonicalRepublisherCrossSelectionContract.from_mapping(packet)
    elif packet.get("schema") == REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA:
        contract = CanonicalRepublisherNoLiveActivationContract.from_mapping(packet)
    elif packet.get("schema") == REPUBLISHER_NO_LIVE_P0_CONTRACT_SCHEMA:
        contract = CanonicalRepublisherNoLiveP0Contract.from_mapping(packet)
    elif packet.get("schema") == MARKET_BUCKET_BRIDGE_CONTRACT_SCHEMA:
        contract = CanonicalMarketBucketBridgeContract.from_mapping(packet)
    elif packet.get("schema") == AWC_OBSERVATION_CONTRACT_SCHEMA:
        contract = CanonicalAWCObservationContract.from_mapping(packet)
    elif packet.get("schema") == MODEL_OOS_CONTRACT_SCHEMA:
        contract = CanonicalModelOOSContract.from_mapping(packet)
    elif packet.get("schema") == NATIVE_ENSEMBLE_OOS_CONTRACT_SCHEMA:
        contract = CanonicalNativeEnsembleOOSContract.from_mapping(packet)
    elif packet.get("schema") == MODEL_OOS_SELECTION_CONTRACT_SCHEMA:
        contract = CanonicalModelOOSSelectionContract.from_mapping(packet)
    elif packet.get("schema") == P0_CALIBRATED_DISTRIBUTION_CONTRACT_SCHEMA:
        contract = CanonicalP0CalibratedDistributionContract.from_mapping(packet)
    elif packet.get("schema") == P0_ACTIVATION_ROOT_CONTRACT_SCHEMA:
        contract = CanonicalP0ActivationRootContract.from_mapping(packet)
    elif packet.get("schema") == P0_FORECAST_SNAPSHOT_CONTRACT_SCHEMA:
        contract = CanonicalP0ForecastSnapshotContract.from_mapping(packet)
    elif packet.get("schema") == SURFACE_OBSERVATION_CONTRACT_SCHEMA:
        contract = CanonicalSurfaceObservationContract.from_mapping(packet)
    elif packet.get("schema") == SURFACE_OBSERVATION_WINNER_CONTRACT_SCHEMA:
        contract = CanonicalSurfaceObservationWinnerContract.from_mapping(packet)
    elif packet.get("schema") == P1_BASE_CONTRACT_SCHEMA:
        contract = CanonicalP1BaseContract.from_mapping(packet)
    elif packet.get("schema") == PURE_YES_BASKET_PLAN_CONTRACT_SCHEMA:
        contract = CanonicalPureYesBasketPlanContract.from_mapping(packet)
    elif packet.get("schema") == TRUTH_AUTHORITY_SUPERSESSION_CONTRACT_SCHEMA:
        contract = CanonicalTruthAuthoritySupersessionContract.from_mapping(packet)
    else:
        contract = CanonicalContract.from_mapping(packet)
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
    ).fetchone()
    if table is None:
        raise ContractError("canonical_ssot_fact_table_missing")
    inspection = inspect_canonical_ssot_schema(conn)
    if (
        inspection.get("state") in {"V13", "V14", "V15", "V16", "V17"}
        and isinstance(
            contract,
            (
                CanonicalRepublisherOOSArtifactContract,
                CanonicalRepublisherCrossSelectionContract,
                CanonicalRepublisherNoLiveActivationContract,
                CanonicalRepublisherNoLiveP0Contract,
            ),
        )
    ):
        raise ContractError("canonical_legacy_republisher_contract_retired")
    if isinstance(contract, CanonicalRepublisherCrossSelectionContract):
        if inspection.get("state") not in {"V8", "V9", "V10", "V11", "V12", "V13", "V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_republisher_cross_selection_v8_required")
    if isinstance(contract, CanonicalRepublisherNoLiveActivationContract):
        if inspection.get("state") not in {"V9", "V10", "V11", "V12", "V13", "V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_republisher_no_live_activation_v9_required")
    if isinstance(contract, CanonicalRepublisherNoLiveP0Contract):
        if inspection.get("state") not in {"V10", "V11", "V12", "V13", "V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_republisher_no_live_p0_v10_required")
    if isinstance(contract, CanonicalMarketBucketBridgeContract):
        if inspection.get("state") not in {"V11", "V12", "V13", "V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_market_bucket_bridge_v11_required")
    if isinstance(contract, CanonicalAWCObservationContract):
        if inspection.get("state") not in {"V12", "V13", "V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_awc_observation_v12_required")
    if isinstance(contract, CanonicalModelOOSContract):
        if inspection.get("state") not in {"V13", "V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_model_oos_v13_required")
    if isinstance(contract, CanonicalNativeEnsembleOOSContract):
        if inspection.get("state") not in {"V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_native_ensemble_oos_v16_required")
        _assert_v16_writer_ready("native_ensemble_oos_event", commit=commit)
        if contract.cohort_facts:
            _assert_v16_writer_ready("native_ensemble_oos_cohort", commit=commit)
    if isinstance(contract, CanonicalModelOOSSelectionContract):
        if inspection.get("state") not in {"V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_model_oos_selection_v14_required")
        if inspection.get("state") in {"V16", "V17"}:
            _assert_v16_writer_ready("p0_model_family_selection", commit=commit)
        else:
            _assert_v14_writer_ready("p0_model_family_selection", commit=commit)
    if isinstance(contract, CanonicalP0CalibratedDistributionContract):
        if inspection.get("state") not in {"V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_p0_calibrated_distribution_v14_required")
        if inspection.get("state") in {"V16", "V17"}:
            _assert_v16_writer_ready("p0_calibrated_distribution", commit=commit)
        else:
            _assert_v14_writer_ready("p0_calibrated_distribution", commit=commit)
    if isinstance(contract, CanonicalP0ActivationRootContract):
        if inspection.get("state") not in {"V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_p0_activation_root_v14_required")
        if inspection.get("state") in {"V16", "V17"}:
            _assert_v16_writer_ready("p0_activation_root", commit=commit)
        else:
            _assert_v14_writer_ready("p0_activation_root", commit=commit)
    if isinstance(contract, CanonicalP0ForecastSnapshotContract):
        if inspection.get("state") not in {"V14", "V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError("canonical_p0_forecast_snapshot_v14_required")
        _assert_v16_writer_ready("p0_forecast_snapshot", commit=commit)
    if isinstance(contract, CanonicalTruthAuthoritySupersessionContract):
        if inspection.get("state") not in {"V15", "V16", "V17"} or inspection.get("errors"):
            raise ContractError(
                "canonical_truth_authority_supersession_v15_required"
            )
        _assert_v15_writer_ready("truth_authority_supersession", commit=commit)
    if isinstance(contract, CanonicalSurfaceObservationContract):
        if inspection.get("state") != "V17" or inspection.get("errors"):
            raise ContractError("canonical_surface_observation_v17_required")
        _assert_v17_writer_ready("surface_observation", commit=commit)
    if isinstance(contract, CanonicalSurfaceObservationWinnerContract):
        if inspection.get("state") != "V17" or inspection.get("errors"):
            raise ContractError("canonical_surface_observation_winner_v17_required")
        _assert_v17_writer_ready("surface_observation_winner", commit=commit)
    if isinstance(contract, CanonicalP1BaseContract):
        if inspection.get("state") != "V17" or inspection.get("errors"):
            raise ContractError("canonical_p1_base_v17_required")
        _assert_v17_writer_ready("p1_base", commit=commit)
    if isinstance(contract, CanonicalPureYesBasketPlanContract):
        if inspection.get("state") != "V17" or inspection.get("errors"):
            raise ContractError("canonical_pure_yes_basket_plan_v17_required")
        _assert_v17_writer_ready("pure_yes_basket_plan", commit=commit)
    if isinstance(contract, CanonicalActivationSupersessionContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_activation_supersession_truth_projection_forbidden")
        _assert_activation_supersession_authority(conn, contract)
        rows = _supersession_fact_rows(contract)
    elif isinstance(contract, CanonicalRepublisherOOSArtifactContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_republisher_oos_truth_projection_forbidden"
            )
        _assert_republisher_oos_artifact_references(conn, contract)
        rows = _republisher_oos_artifact_rows(contract)
    elif isinstance(contract, CanonicalRepublisherCrossSelectionContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_republisher_cross_selection_truth_projection_forbidden"
            )
        assert_republisher_cross_selection_references(conn, contract)
        rows = _republisher_cross_selection_rows(contract)
    elif isinstance(contract, CanonicalRepublisherNoLiveActivationContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_republisher_no_live_activation_truth_projection_forbidden"
            )
        assert_republisher_no_live_activation_references(conn, contract)
        rows = _republisher_no_live_activation_rows(contract)
    elif isinstance(contract, CanonicalRepublisherNoLiveP0Contract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_republisher_no_live_p0_truth_projection_forbidden"
            )
        if contract.forward_applicability_facts:
            if external_evidence_root is None:
                raise ContractError(
                    "canonical_republisher_forward_evidence_root_required"
                )
            from .republisher_forward_evidence_store import verify_forward_manifest

            for applicability in contract.forward_applicability_facts:
                verify_forward_manifest(
                    external_evidence_root,
                    manifest_content_address=applicability.raw[
                        "evidence_manifest_content_address"
                    ],
                    manifest_content_sha256=applicability.raw[
                        "evidence_manifest_content_sha256"
                    ],
                    station_id=applicability.raw["station_id"],
                    event_date=applicability.raw["event_date"],
                    settlement_timezone=applicability.raw["settlement_timezone"],
                    component_receipts=applicability.raw["component_receipts"],
                    native_terminal_receipts=applicability.raw[
                        "native_unavailability_receipts"
                    ],
                )
        if contract.p0_snapshot_facts:
            try:
                p0_projection_authorized = int(
                    conn.execute(
                        f"SELECT {P0_PROJECTION_AUTHORIZATION_FUNCTION}()"
                    ).fetchone()[0]
                )
            except sqlite3.Error as exc:
                raise ContractError(
                    "canonical_republisher_p0_atomic_projection_required"
                ) from exc
            if p0_projection_authorized != 1:
                raise ContractError(
                    "canonical_republisher_p0_atomic_projection_required"
                )
        assert_republisher_no_live_p0_references(conn, contract)
        rows = _republisher_no_live_p0_rows(contract)
    elif isinstance(contract, CanonicalMarketBucketBridgeContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_market_bucket_bridge_truth_projection_forbidden"
            )
        rows = _market_bucket_bridge_rows(contract)
    elif isinstance(contract, CanonicalAWCObservationContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_awc_observation_truth_projection_forbidden")
        rows = _awc_observation_rows(contract)
    elif isinstance(contract, CanonicalTruthAuthoritySupersessionContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_truth_authority_supersession_projection_forbidden"
            )
        assert_truth_authority_supersession_references(conn, contract)
        rows = _truth_authority_supersession_rows(contract)
    elif isinstance(contract, CanonicalModelOOSContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_model_oos_truth_projection_forbidden")
        _assert_model_oos_references(conn, contract)
        rows = _model_oos_rows(contract)
    elif isinstance(contract, CanonicalNativeEnsembleOOSContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_native_ensemble_oos_truth_projection_forbidden"
            )
        _assert_native_ensemble_oos_references(conn, contract)
        rows = _native_ensemble_oos_rows(contract)
    elif isinstance(contract, CanonicalModelOOSSelectionContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_model_oos_selection_truth_projection_forbidden"
            )
        _assert_model_oos_selection_references(conn, contract)
        rows = _model_oos_selection_rows(contract)
    elif isinstance(contract, CanonicalP0CalibratedDistributionContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_p0_calibrated_distribution_truth_projection_forbidden"
            )
        _assert_p0_calibrated_distribution_references(conn, contract)
        rows = _p0_calibrated_distribution_rows(contract)
    elif isinstance(contract, CanonicalP0ActivationRootContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_p0_activation_root_truth_projection_forbidden")
        _assert_p0_activation_root_references(conn, contract)
        rows = _p0_activation_root_rows(contract)
    elif isinstance(contract, CanonicalP0ForecastSnapshotContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_p0_forecast_snapshot_truth_projection_forbidden")
        _assert_p0_forecast_snapshot_references(conn, contract)
        rows = _p0_forecast_snapshot_rows(contract)
    elif isinstance(contract, CanonicalSurfaceObservationContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_surface_observation_truth_projection_forbidden")
        if external_evidence_root is None:
            raise ContractError("canonical_surface_observation_evidence_root_required")
        assert_surface_observation_external_evidence(external_evidence_root, contract)
        rows = _surface_observation_rows(contract)
    elif isinstance(contract, CanonicalSurfaceObservationWinnerContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_surface_observation_winner_truth_projection_forbidden"
            )
        if external_evidence_root is None:
            raise ContractError(
                "canonical_surface_observation_winner_evidence_root_required"
            )
        assert_surface_observation_winner_external_evidence(
            external_evidence_root, contract
        )
        _assert_surface_observation_winner_references(conn, contract)
        rows = _surface_observation_winner_rows(contract)
    elif isinstance(contract, CanonicalP1BaseContract):
        if awc_daily_truth_projection is not None:
            raise ContractError("canonical_p1_base_truth_projection_forbidden")
        _assert_p1_base_references(conn, contract)
        rows = _p1_base_rows(contract)
    elif isinstance(contract, CanonicalPureYesBasketPlanContract):
        if awc_daily_truth_projection is not None:
            raise ContractError(
                "canonical_pure_yes_basket_plan_truth_projection_forbidden"
            )
        _assert_pure_yes_basket_plan_references(conn, contract)
        rows = _pure_yes_basket_plan_rows(contract)
    else:
        _assert_member_arrival_bindings(conn, contract)
        _assert_activation_generation_not_rewound(conn, contract)
        rows = _fact_rows(contract)
    if commit:
        assert_connection_target_capacity(conn)
        assert_target_database_owner(conn)
    ingress_gate = {"enabled": False}
    try:
        conn.create_function(
            INGRESS_AUTHORIZATION_FUNCTION,
            0,
            lambda: 1 if ingress_gate["enabled"] else 0,
            deterministic=True,
        )
    except sqlite3.Error as exc:
        raise ContractError("canonical_ingress_authorization_unavailable") from exc
    savepoint = "canonical_fact_ingress"
    owns_outer_transaction = bool(commit and finalize_transaction)
    if owns_outer_transaction:
        # A top-level SQLite savepoint commits when released.  Begin a real
        # outer transaction first so the savepoint can be released before the
        # one and only final commit, and a commit failure can still roll the
        # complete canonical-plus-projection unit back.
        conn.execute("BEGIN")
    savepoint_active = False
    inserted = 0
    replayed = 0
    projection_result: dict[str, Any] | None = None
    try:
        ingress_gate["enabled"] = True
        conn.execute(f"SAVEPOINT {savepoint}")
        savepoint_active = True
        for fact_id, fact_type, scope, fact_json, lineage_json in rows:
            existing = conn.execute(
                "SELECT fact_type,scope_key,content_sha256,fact_json,lineage_json "
                "FROM canonical_ssot_fact WHERE fact_id=?",
                (fact_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing)[1:] != (
                    scope,
                    fact_id.split(":", 1)[1],
                    fact_json,
                    lineage_json,
                ) or str(existing[0]) != fact_type:
                    raise ContractError("canonical_fact_content_conflict")
                replayed += 1
                continue
            scope_existing = conn.execute(
                "SELECT fact_id,content_sha256,fact_json FROM canonical_ssot_fact "
                "WHERE fact_type=? AND scope_key=?",
                (fact_type, scope),
            ).fetchone()
            if scope_existing is not None:
                raise ContractError("canonical_scope_content_conflict")
            # A preview must be a genuinely read-only validation, not a
            # temporary INSERT followed by rollback.  This keeps the same
            # contract/reference/idempotency/scope checks usable through a
            # mode=ro/query_only connection and avoids touching WAL state.
            if not commit:
                continue
            conn.execute(
                "INSERT INTO canonical_ssot_fact("
                "fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    fact_id,
                    fact_type,
                    scope,
                    fact_id.split(":", 1)[1],
                    fact_json,
                    lineage_json,
                    ingested_at_utc or _utc_now(),
                ),
            )
            inserted += 1
        if awc_daily_truth_projection is not None:
            assert isinstance(contract, CanonicalContract)
            projection_result = _project_awc_daily_truths(
                conn,
                contract,
                awc_daily_truth_projection,
                commit=commit,
            )
        if commit:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
            if finalize_transaction:
                conn.commit()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            savepoint_active = False
    except Exception:
        # Preserve the original write/commit fault.  In particular, do not
        # attempt to roll back a savepoint that was already released just
        # before a failed outer commit.
        if savepoint_active:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                pass
        if owns_outer_transaction and conn.in_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        ingress_gate["enabled"] = False
    result = {
        "schema": INGRESS_SCHEMA,
        "canonical_schema": (
            ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalActivationSupersessionContract)
            else REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalRepublisherOOSArtifactContract)
            else REPUBLISHER_CROSS_SELECTION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalRepublisherCrossSelectionContract)
            else REPUBLISHER_NO_LIVE_ACTIVATION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalRepublisherNoLiveActivationContract)
            else REPUBLISHER_NO_LIVE_P0_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalRepublisherNoLiveP0Contract)
            else MARKET_BUCKET_BRIDGE_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalMarketBucketBridgeContract)
            else TRUTH_AUTHORITY_SUPERSESSION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalTruthAuthoritySupersessionContract)
            else AWC_OBSERVATION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalAWCObservationContract)
            else MODEL_OOS_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalModelOOSContract)
            else NATIVE_ENSEMBLE_OOS_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalNativeEnsembleOOSContract)
            else MODEL_OOS_SELECTION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalModelOOSSelectionContract)
            else P0_CALIBRATED_DISTRIBUTION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalP0CalibratedDistributionContract)
            else P0_ACTIVATION_ROOT_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalP0ActivationRootContract)
            else P0_FORECAST_SNAPSHOT_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalP0ForecastSnapshotContract)
            else SURFACE_OBSERVATION_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalSurfaceObservationContract)
            else SURFACE_OBSERVATION_WINNER_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalSurfaceObservationWinnerContract)
            else P1_BASE_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalP1BaseContract)
            else PURE_YES_BASKET_PLAN_CONTRACT_SCHEMA
            if isinstance(contract, CanonicalPureYesBasketPlanContract)
            else SCHEMA
        ),
        "state": (
            "COMMITTED"
            if commit and finalize_transaction
            else "STAGED_IN_CALLER_TRANSACTION"
            if commit
            else "VALIDATED_NO_WRITE"
        ),
        "builder_id": contract.builder_id,
        "contract_content_sha256": contract.content_sha256,
        "validated_counts": (
            {"activation_supersession": len(contract.supersession_facts)}
            if isinstance(contract, CanonicalActivationSupersessionContract)
            else {
                "republisher_oos_artifact": len(contract.artifact_facts)
            }
            if isinstance(contract, CanonicalRepublisherOOSArtifactContract)
            else {
                "republisher_cross_selection": len(contract.selection_facts)
            }
            if isinstance(contract, CanonicalRepublisherCrossSelectionContract)
            else {
                "republisher_no_live_activation": len(contract.activation_facts)
            }
            if isinstance(contract, CanonicalRepublisherNoLiveActivationContract)
            else {
                "forward_applicability": len(
                    contract.forward_applicability_facts
                ),
                "probability_distribution_artifact": len(
                    contract.probability_distribution_artifact_facts
                ),
                "p0_snapshot": len(contract.p0_snapshot_facts),
            }
            if isinstance(contract, CanonicalRepublisherNoLiveP0Contract)
            else {
                "market_bucket_bridge": len(contract.bridge_facts),
            }
            if isinstance(contract, CanonicalMarketBucketBridgeContract)
            else {
                "truth_observation_revision": len(contract.revision_facts),
                "truth_observation_day_bounds": len(contract.day_bounds_facts),
            }
            if isinstance(contract, CanonicalAWCObservationContract)
            else {
                "truth_observation_revision": len(
                    contract.awc_observation_contract.revision_facts
                ),
                "truth_observation_day_bounds": len(
                    contract.awc_observation_contract.day_bounds_facts
                ),
                "truth_authority_supersession": len(contract.supersession_facts),
            }
            if isinstance(contract, CanonicalTruthAuthoritySupersessionContract)
            else {
                "model_oos_event": len(contract.event_facts),
                "model_oos_cohort": len(contract.cohort_facts),
            }
            if isinstance(contract, CanonicalModelOOSContract)
            else {
                "native_ensemble_oos_event": len(contract.event_facts),
                "native_ensemble_oos_cohort": len(contract.cohort_facts),
            }
            if isinstance(contract, CanonicalNativeEnsembleOOSContract)
            else {
                "p0_model_family_selection": len(contract.selection_facts),
            }
            if isinstance(contract, CanonicalModelOOSSelectionContract)
            else {
                "p0_calibrated_distribution": len(contract.distribution_facts),
            }
            if isinstance(contract, CanonicalP0CalibratedDistributionContract)
            else {
                "p0_activation_root": len(contract.activation_root_facts),
            }
            if isinstance(contract, CanonicalP0ActivationRootContract)
            else {
                "p0_forecast_snapshot": len(contract.snapshot_facts),
            }
            if isinstance(contract, CanonicalP0ForecastSnapshotContract)
            else {
                "surface_observation": len(contract.observation_facts),
            }
            if isinstance(contract, CanonicalSurfaceObservationContract)
            else {
                "surface_observation_winner": len(contract.winner_facts),
            }
            if isinstance(contract, CanonicalSurfaceObservationWinnerContract)
            else {
                "p1_base": len(contract.p1_base_facts),
            }
            if isinstance(contract, CanonicalP1BaseContract)
            else {
                "pure_yes_basket_plan": len(contract.basket_plan_facts),
            }
            if isinstance(contract, CanonicalPureYesBasketPlanContract)
            else {
                "model": len(contract.model_facts),
                "ensemble_member": len(contract.ensemble_member_facts),
                "native_arrival": len(contract.native_arrival_facts),
                "ensemble_member_receipt_manifest": len(
                    contract.ensemble_member_receipt_manifest_facts
                ),
                "ensemble_member_arrival_binding": len(
                    contract.ensemble_member_arrival_binding_facts
                ),
                "truth": len(contract.truth_facts),
                "residual": len(contract.residual_facts),
                "activation": len(contract.activation_facts),
            }
        ),
        "inserted_count": inserted if commit else 0,
        "replayed_count": replayed,
        "writes_target_database": bool(commit and inserted),
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if projection_result is not None:
        result["awc_daily_truth_projection"] = projection_result
    return {**result, "receipt_sha256": content_sha256(result)}


__all__ = [
    "adapt_legacy_calibration_packet_to_canonical",
    "AWC_DAILY_TRUTH_PROJECTION_SCHEMA",
    "FACT_TYPES",
    "INGRESS_SCHEMA",
    "INGRESS_AUTHORIZATION_FUNCTION",
    "P0_PROJECTION_AUTHORIZATION_FUNCTION",
    "V14_WRITER_ALLOWLIST",
    "V15_WRITER_ALLOWLIST",
    "V16_WRITER_ALLOWLIST",
    "V17_WRITER_ALLOWLIST",
    "ingest_canonical_contract",
    "materialize_p1_from_canonical_observation",
    "stage_republisher_forward_and_p0",
    "stage_republisher_no_live_p0",
    "stage_awc_observation_contract",
]
