"""The single ingress for the canonical model contract.

All model/truth/residual/activation facts enter the target SSOT through this
function.  It is append-only and content addressed.  The legacy calibration
adapter and its reverse projection are permanently retired: a legacy packet
cannot manufacture a canonical fact.  The only retained projection direction
is exact formal AWC canonical truth into the historical audit table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
    CanonicalTruthFact,
    REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA,
    SCHEMA,
)
from .contracts import ContractError, ProbabilityEnvelope, content_sha256
from .materialization import insert_probability_envelope, materialize_p1
from .resource_bounds import assert_connection_target_capacity


INGRESS_SCHEMA = "bayes_basket_target_canonical_model_ingress_v1"
INGRESS_AUTHORIZATION_FUNCTION = "canonical_ingress_authorized"
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
        ),
    ):
        contract = packet
    elif packet.get("schema") == ACTIVATION_SUPERSESSION_CONTRACT_SCHEMA:
        contract = CanonicalActivationSupersessionContract.from_mapping(packet)
    elif packet.get("schema") == REPUBLISHER_OOS_ARTIFACT_CONTRACT_SCHEMA:
        contract = CanonicalRepublisherOOSArtifactContract.from_mapping(packet)
    else:
        contract = CanonicalContract.from_mapping(packet)
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact'"
    ).fetchone()
    if table is None:
        raise ContractError("canonical_ssot_fact_table_missing")
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
    "ingest_canonical_contract",
    "materialize_p1_from_canonical_observation",
]
