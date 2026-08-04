"""No-live binding from ensemble scenario evidence to basket candidates.

This module is deliberately downstream of both the scenario-role selector and
the probability-stage materializer.  It does not select ensemble roles, fit a
probability model, read a market, or submit an order.  Its only job is to
attach the descriptive ``M/L/H`` evidence to the highest available
probability stage while preserving that stage's probability vector byte for
byte at the JSON-value level.

Official-native evidence remains the preferred authority.  When the native
packet for a station×event-day is unavailable, an Open-Meteo
``aggregator_republisher`` artifact may bind to a no-live P0/P0-M candidate
only with an explicit fallback authority receipt.  The receipt keeps the
republisher out of native-arrival, P1 and formal/live authority; it does not
turn the packet into native evidence.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    timestamp_lte,
    timestamp_lt,
)


SCHEMA = "native_ensemble_basket_binding_v1"
METHOD_VERSION = "native_or_republisher_fallback_roles_stage_precedence_basket_candidates_v2"
SCENARIO_SCHEMA = "native_ensemble_scenario_roles_v1"
SCENARIO_ARTIFACT_SCHEMAS = frozenset({
    SCENARIO_SCHEMA,
    "native_ensemble_scenario_roles_inbox_v1",
})
SCENARIO_CANDIDATE_SCHEMA = "native_ensemble_scenario_roles_station_v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"
STAGES = ("P1", "P0-M", "P0")
MAX_STATIONS = 45
MAX_SNAPSHOTS = 3 * MAX_STATIONS
MAX_ROLE_LINEAGE_PRODUCTS = 256
ROLE_NAMES = ("M", "L", "H")
OFFICIAL_SOURCE_KIND = "official_native"
REPUBLISHER_SOURCE_KIND = "open_meteo_aggregator"
REPUBLISHER_SOURCE_CLASS = "aggregator_republisher"
REPUBLISHER_FALLBACK_AUTHORITY = "PRODUCTION_NO_LIVE_REPUBLISHER_FALLBACK"
OBSERVATIONAL_CANDIDATE_NOT_OOS = "observational_candidate_not_oos"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"basket_binding_{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"basket_binding_{name}_invalid")
    return result


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"basket_binding_{name}_invalid") from exc
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"basket_binding_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"basket_binding_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"basket_binding_{name}_invalid")
    return result


def _list(name: str, value: Any, *, allow_empty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ContractError(f"basket_binding_{name}_invalid")
    return value


def _verify_hashed_mapping(
    raw: Mapping[str, Any],
    *,
    hash_key: str,
    error: str,
) -> str:
    supplied = _sha(hash_key, raw.get(hash_key))
    identity = {key: value for key, value in raw.items() if key != hash_key}
    if supplied != content_sha256(identity):
        raise ContractError(error)
    return supplied


def _verify_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise ContractError("basket_binding_scenario_candidate_invalid")
    supplied = _sha("scenario_candidate_content_sha256", candidate.get("content_sha256"))
    identity = {key: value for key, value in candidate.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("basket_binding_scenario_candidate_hash_mismatch")
    if candidate.get("schema") != SCENARIO_CANDIDATE_SCHEMA:
        raise ContractError("basket_binding_scenario_candidate_schema_invalid")
    station_id = _text("station_id", candidate.get("station_id")).upper()
    event_date = _date("event_date", candidate.get("event_date"))
    _text("settlement_timezone", candidate.get("settlement_timezone"))
    _sha(
        "source_packet_content_sha256",
        candidate.get("source_packet_content_sha256"),
    )
    authority_blockers: list[str] = []
    source_kind = str(candidate.get("source_kind") or "").strip()
    fallback_authority = candidate.get("fallback_authority")
    if source_kind not in {OFFICIAL_SOURCE_KIND, REPUBLISHER_SOURCE_KIND}:
        authority_blockers.append("scenario_source_kind_invalid")
    if source_kind == REPUBLISHER_SOURCE_KIND:
        if not isinstance(fallback_authority, Mapping):
            authority_blockers.append("republisher_fallback_authority_missing")
        else:
            required_false = (
                "native_arrival_index_eligible",
                "native_formal_p0_eligible",
                "formal_p0_eligible",
            )
            required_true = (
                "native_source_unavailable",
                "production_no_live_fallback_eligible",
                "historical_oos_activation_required",
            )
            if str(fallback_authority.get("authority_mode") or "") != REPUBLISHER_FALLBACK_AUTHORITY:
                authority_blockers.append("republisher_fallback_authority_mode_invalid")
            if any(fallback_authority.get(key) is not False for key in required_false):
                authority_blockers.append("republisher_native_authority_not_disabled")
            if any(fallback_authority.get(key) is not True for key in required_true):
                authority_blockers.append("republisher_fallback_activation_contract_missing")
            if str(fallback_authority.get("oos_event_temporal_scope") or "") != OBSERVATIONAL_CANDIDATE_NOT_OOS:
                authority_blockers.append("republisher_forward_oos_forbidden")

    lineage = candidate.get("role_selection_lineage")
    if not isinstance(lineage, Mapping):
        raise ContractError("basket_binding_role_selection_lineage_missing")
    if lineage.get("schema") != SCENARIO_SCHEMA:
        raise ContractError("basket_binding_role_selection_lineage_schema_invalid")
    _sha("role_selection_lineage_content_sha256", lineage.get("content_sha256"))
    _sha("role_selection_lineage_bucket_contract_sha256", lineage.get("bucket_contract_sha256"))
    family_ids = _list("source_family_ids", lineage.get("source_family_ids"))
    normalized_families = [str(value).strip() for value in family_ids]
    if any(not value for value in normalized_families):
        raise ContractError("basket_binding_source_family_id_missing")
    if len(set(normalized_families)) != len(normalized_families):
        raise ContractError("basket_binding_source_family_vote_duplicate")
    product_ids = _list("source_product_ids", lineage.get("source_product_ids"))
    normalized_products = [str(value).strip() for value in product_ids]
    if any(not value for value in normalized_products) or len(set(normalized_products)) != len(normalized_products):
        raise ContractError("basket_binding_source_product_lineage_invalid")
    products = lineage.get("source_product_lineage")
    if not isinstance(products, list) or not products or len(products) > MAX_ROLE_LINEAGE_PRODUCTS:
        raise ContractError("basket_binding_source_product_lineage_missing")
    seen_product_ids: set[str] = set()
    for product in products:
        if not isinstance(product, Mapping):
            raise ContractError("basket_binding_source_product_lineage_invalid")
        product_id = _text("source_product_id", product.get("ensemble_system_id"))
        family_id = _text("source_family_id", product.get("independence_family_id"))
        source_class = str(product.get("source_class") or "").strip().lower()
        if product_id in seen_product_ids:
            raise ContractError("basket_binding_source_product_duplicate")
        seen_product_ids.add(product_id)
        if source_kind == OFFICIAL_SOURCE_KIND:
            if source_class not in {
                "native_official",
                "official_native",
                "official_native_archive",
                "native",
            }:
                authority_blockers.append("official_native_product_lineage_required")
            if bool(product.get("aggregator_or_republisher", False)):
                authority_blockers.append("aggregator_not_formal_authority")
        else:
            if source_class != REPUBLISHER_SOURCE_CLASS:
                authority_blockers.append("republisher_product_lineage_required")
            if product.get("aggregator_or_republisher") is not True:
                authority_blockers.append("republisher_product_role_required")
        if family_id not in normalized_families:
            raise ContractError("basket_binding_product_family_scope_mismatch")
    if set(seen_product_ids) != set(normalized_products):
        raise ContractError("basket_binding_source_product_lineage_scope_mismatch")

    registry = candidate.get("market_registry")
    if not isinstance(registry, Mapping):
        raise ContractError("basket_binding_market_registry_missing")
    ordinals = registry.get("bucket_ordinals")
    intervals = registry.get("bucket_intervals")
    if not isinstance(ordinals, Mapping) or not ordinals:
        raise ContractError("basket_binding_bucket_ordinals_missing")
    if not isinstance(intervals, Mapping) or set(map(str, intervals)) != set(map(str, ordinals)):
        raise ContractError("basket_binding_bucket_contract_invalid")
    role_map = candidate.get("roles")
    buckets = candidate.get("buckets")
    if not isinstance(role_map, Mapping) or not isinstance(buckets, Mapping):
        raise ContractError("basket_binding_roles_missing")
    normalized_buckets: dict[str, str | None] = {}
    for role in ROLE_NAMES:
        raw_bucket = buckets.get(role)
        normalized_buckets[role] = None if raw_bucket in (None, "") else _text(f"{role}_bucket", raw_bucket)
        role_value = role_map.get(role)
        if normalized_buckets[role] is None:
            if role_value not in (None, {}):
                raise ContractError(f"basket_binding_{role}_role_bucket_mismatch")
            continue
        if not isinstance(role_value, Mapping):
            raise ContractError(f"basket_binding_{role}_role_evidence_missing")
        if str(role_value.get("role") or role) != role:
            raise ContractError(f"basket_binding_{role}_role_name_mismatch")
        if str(role_value.get("bucket") or "") != normalized_buckets[role]:
            raise ContractError(f"basket_binding_{role}_role_bucket_mismatch")
        supporting_families = role_value.get("supporting_families")
        if not isinstance(supporting_families, list) or len(set(map(str, supporting_families))) != len(supporting_families):
            raise ContractError(f"basket_binding_{role}_family_vote_duplicate")
        if not supporting_families:
            raise ContractError(f"basket_binding_{role}_supporting_families_missing")
        if not set(map(str, supporting_families)).issubset(set(normalized_families)):
            raise ContractError(f"basket_binding_{role}_family_scope_mismatch")
        supporting_products = role_value.get("supporting_products")
        if not isinstance(supporting_products, list) or not supporting_products:
            raise ContractError(f"basket_binding_{role}_supporting_products_missing")
        role_families: set[str] = set()
        for product in supporting_products:
            if not isinstance(product, Mapping):
                raise ContractError(f"basket_binding_{role}_supporting_product_invalid")
            family = _text("role_supporting_family", product.get("independence_family_id"))
            if family in role_families:
                raise ContractError(f"basket_binding_{role}_family_vote_duplicate")
            role_families.add(family)
        if role_families != set(map(str, supporting_families)):
            raise ContractError(f"basket_binding_{role}_family_lineage_mismatch")
    return {
        "raw": dict(candidate),
        "content_sha256": supplied,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": str(candidate["settlement_timezone"]),
        "source_kind": source_kind,
        "fallback_authority": (
            dict(fallback_authority)
            if isinstance(fallback_authority, Mapping)
            else None
        ),
        "buckets": normalized_buckets,
        "role_selection_lineage": dict(lineage),
        "registry": dict(registry),
        "authority_blockers": sorted(set(authority_blockers)),
    }


def _verify_scenario_roles(raw: Any) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    if not isinstance(raw, Mapping):
        raise ContractError("basket_binding_scenario_roles_invalid")
    if raw.get("schema") not in SCENARIO_ARTIFACT_SCHEMAS:
        raise ContractError("basket_binding_scenario_roles_schema_invalid")
    artifact_hash = _verify_hashed_mapping(
        raw,
        hash_key="content_sha256",
        error="basket_binding_scenario_roles_hash_mismatch",
    )
    candidates = _list("scenario_candidates", raw.get("candidates"))
    if len(candidates) > MAX_STATIONS:
        raise ContractError("basket_binding_scenario_station_bound_exceeded")
    verified = [_verify_candidate(candidate) for candidate in candidates]
    keys = [(row["station_id"], row["event_date"]) for row in verified]
    if len(set(keys)) != len(keys):
        raise ContractError("basket_binding_scenario_scope_duplicate")
    if not verified:
        raise ContractError("basket_binding_scenario_candidates_empty")
    source_kinds = raw.get("source_kinds")
    if not isinstance(source_kinds, list) or not source_kinds:
        raise ContractError("basket_binding_scenario_source_kinds_missing")
    if any(
        str(value).strip() not in {OFFICIAL_SOURCE_KIND, REPUBLISHER_SOURCE_KIND}
        for value in source_kinds
    ):
        artifact_blocker = "scenario_source_kind_invalid"
        for candidate in verified:
            candidate["authority_blockers"] = sorted(
                set(candidate["authority_blockers"]) | {artifact_blocker}
            )
    # The artifact may contain both official and fallback scopes, but each
    # republisher scope must carry its own authority receipt.  This prevents
    # an official packet's lineage from being reused to bless a different
    # Open-Meteo station-day.
    declared = {str(value).strip() for value in source_kinds}
    actual = {str(row["source_kind"]) for row in verified}
    if declared != actual:
        for candidate in verified:
            candidate["authority_blockers"] = sorted(
                set(candidate["authority_blockers"]) | {"scenario_source_kind_scope_mismatch"}
            )
    return dict(raw), artifact_hash, verified


def _unwrap_stage_snapshot(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, list):
        if any(not isinstance(item, Mapping) for item in raw):
            raise ContractError("basket_binding_probability_snapshot_invalid")
        return list(raw)
    if not isinstance(raw, Mapping):
        raise ContractError("basket_binding_probability_snapshot_invalid")
    for key in ("snapshots", "envelopes", "probability_stages", "stages"):
        value = raw.get(key)
        if isinstance(value, list):
            if any(not isinstance(item, Mapping) for item in value):
                raise ContractError("basket_binding_probability_snapshot_invalid")
            return list(value)
    # A small stage map is convenient for batch callers.
    if "stage" not in raw:
        stage_values = [value for key, value in raw.items() if str(key) in STAGES]
        if stage_values:
            if any(not isinstance(item, Mapping) for item in stage_values):
                raise ContractError("basket_binding_probability_snapshot_invalid")
            return stage_values
    return [raw]


def _verify_stage_snapshots(raw_inputs: Any) -> tuple[list[dict[str, Any]], str]:
    inputs = raw_inputs if isinstance(raw_inputs, list) else [raw_inputs]
    if not inputs or len(inputs) > MAX_SNAPSHOTS:
        raise ContractError("basket_binding_probability_snapshot_count_invalid")
    rows: list[dict[str, Any]] = []
    for item in inputs:
        for snapshot in _unwrap_stage_snapshot(item):
            if not isinstance(snapshot, Mapping):
                raise ContractError("basket_binding_probability_snapshot_invalid")
            wrapper_hash: str | None = None
            envelope_raw: Mapping[str, Any]
            if isinstance(snapshot.get("envelope"), Mapping):
                envelope_raw = snapshot["envelope"]
                if snapshot.get("content_sha256") is not None:
                    wrapper_hash = _verify_hashed_mapping(
                        snapshot,
                        hash_key="content_sha256",
                        error="basket_binding_probability_snapshot_hash_mismatch",
                    )
            elif isinstance(snapshot.get("probability_envelope"), Mapping):
                envelope_raw = snapshot["probability_envelope"]
                if snapshot.get("content_sha256") is not None:
                    wrapper_hash = _verify_hashed_mapping(
                        snapshot,
                        hash_key="content_sha256",
                        error="basket_binding_probability_snapshot_hash_mismatch",
                    )
            else:
                # ProbabilityEnvelope intentionally has no hash field.  A
                # raw envelope is therefore hashed from its canonical dict.
                envelope_raw = {
                    key: value for key, value in snapshot.items() if key != "content_sha256"
                }
                if snapshot.get("content_sha256") is not None:
                    wrapper_hash = _verify_hashed_mapping(
                        snapshot,
                        hash_key="content_sha256",
                        error="basket_binding_probability_snapshot_hash_mismatch",
                    )
            try:
                envelope = ProbabilityEnvelope.from_mapping(envelope_raw)
            except ContractError:
                raise
            computed_hash = envelope.content_sha256
            rows.append({
                "envelope": envelope,
                "content_sha256": wrapper_hash or computed_hash,
                "computed_content_sha256": computed_hash,
            })
    if not rows:
        raise ContractError("basket_binding_probability_snapshots_empty")
    # The input hash records the normalized, verified evidence set; no stage
    # probability is recomputed or reweighted here.
    identity = [
        {
            "content_sha256": row["content_sha256"],
            "computed_content_sha256": row["computed_content_sha256"],
            "envelope": row["envelope"].to_dict(),
        }
        for row in rows
    ]
    return rows, content_sha256(identity)


def _choose_stage(
    rows: Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    blockers: list[str] = []
    scoped = [
        dict(row)
        for row in rows
        if row["envelope"].station_id == station_id
        and row["envelope"].event_date == event_date
    ]
    if not scoped:
        return None, ["probability_stage_scope_missing"]
    if as_of_utc is not None:
        _text("as_of_utc", as_of_utc)
        visible = [
            row
            for row in scoped
            if timestamp_lte(row["envelope"].decision_at_utc, as_of_utc)
        ]
        if not visible:
            return None, ["probability_stage_no_asof_checkpoint"]
        scoped = visible
    for stage in STAGES:
        candidates = [row for row in scoped if row["envelope"].stage == stage]
        if not candidates:
            continue
        latest_at = max(row["envelope"].decision_at_utc for row in candidates)
        latest = [row for row in candidates if row["envelope"].decision_at_utc == latest_at]
        if len(latest) != 1:
            return None, ["probability_stage_ambiguous_checkpoint"]
        selected = latest[0]
        # P1 is authoritative only after the TAF/physical confirmation has
        # materialized P0-M when available; otherwise a P0-root P1 is the
        # legal no-TAF path.  Any other root remains fail-closed and must not
        # silently downgrade this scope to an older stage.
        if (
            stage == "P1"
            and str(
                selected["envelope"].lineage.get("prediction_root_stage")
                or ""
            ).strip().upper()
            not in {"P0", "P0-M"}
        ):
            return None, ["probability_p1_root_stage_mismatch"]
        if as_of_utc is not None and not selected["envelope"].is_fresh(as_of_utc):
            return None, ["probability_stage_stale_fail_closed"]
        return selected, blockers
    return None, ["probability_stage_missing"]


def _stage_record(row: Mapping[str, Any]) -> dict[str, Any]:
    envelope: ProbabilityEnvelope = row["envelope"]
    return {
        "stage": envelope.stage,
        "checkpoint_id": envelope.checkpoint_id,
        "station_id": envelope.station_id,
        "event_date": envelope.event_date,
        "settlement_timezone": envelope.settlement_timezone,
        "decision_at_utc": envelope.decision_at_utc,
        "fresh_until_utc": envelope.fresh_until_utc,
        "bucket_ordinals": dict(envelope.bucket_ordinals),
        "probabilities": dict(envelope.probabilities),
        "posterior_version": envelope.posterior_version,
        "posterior_sha256": envelope.posterior_sha256,
        "bridge_sha256": envelope.bridge_sha256,
        "lineage": dict(envelope.lineage),
        "content_sha256": row["content_sha256"],
        "computed_content_sha256": row["computed_content_sha256"],
    }


def _bind_station(
    candidate: Mapping[str, Any],
    stage_row: Mapping[str, Any] | None,
    stage_blockers: Sequence[str],
) -> dict[str, Any]:
    blockers = list(stage_blockers) + list(candidate.get("authority_blockers") or [])
    stage_record = None if stage_row is None else _stage_record(stage_row)
    basket_candidates: list[dict[str, Any]] = []
    selected_stage = None
    if stage_row is not None:
        envelope: ProbabilityEnvelope = stage_row["envelope"]
        selected_stage = envelope.stage
        expected_buckets = set(candidate["registry"]["bucket_ordinals"])
        if set(envelope.probabilities) != expected_buckets or set(envelope.bucket_ordinals) != expected_buckets:
            blockers.append("probability_stage_bucket_contract_mismatch")
        if envelope.settlement_timezone != candidate["settlement_timezone"]:
            blockers.append("probability_stage_timezone_mismatch")
        for role in ROLE_NAMES:
            bucket = candidate["buckets"][role]
            if bucket is None:
                if role == "M":
                    blockers.append("scenario_M_missing")
                continue
            if bucket not in envelope.probabilities:
                blockers.append(f"scenario_{role}_bucket_missing_in_stage")
        if not blockers:
            # M/L/H are evidence roles only.  The probability value is copied
            # exactly from the selected stage and is never member-derived.
            unique: dict[str, list[str]] = {}
            for role in ROLE_NAMES:
                bucket = candidate["buckets"][role]
                if bucket is not None:
                    unique.setdefault(bucket, []).append(role)
            for bucket, roles in unique.items():
                basket_candidates.append({
                    "bucket": bucket,
                    "roles": roles,
                    "stage_probability": envelope.probabilities[bucket],
                    "stage_probability_source": "materialized_probability_stage",
                    "raw_member_probability_used": False,
                    "scenario_role_evidence_only": True,
                    "bucket_ordinal": envelope.bucket_ordinals[bucket],
                })
            basket_candidates.sort(
                key=lambda row: (
                    min(ROLE_NAMES.index(role) for role in row["roles"]),
                    row["bucket_ordinal"],
                    row["bucket"],
                )
            )
    status = "BUILT_NO_LIVE" if not blockers else "BLOCKED_FAIL_CLOSED"
    identity = {
        "schema": "native_ensemble_basket_binding_station_v1",
        "method_version": METHOD_VERSION,
        "station_id": candidate["station_id"],
        "event_date": candidate["event_date"],
        "settlement_timezone": candidate["settlement_timezone"],
        "status": status,
        "scenario_roles_content_sha256": candidate["content_sha256"],
        "role_selection_lineage": candidate["role_selection_lineage"],
        "source_kind": candidate.get("source_kind"),
        "fallback_authority": candidate.get("fallback_authority"),
        "scenario_buckets": dict(candidate["buckets"]),
        "selected_probability_stage": selected_stage,
        "probability_stage": stage_record,
        "basket_candidates": basket_candidates,
        "blockers": sorted(set(blockers)),
        "raw_member_probability_is_not_final_probability": True,
        "probability_stage_is_unchanged": not blockers,
        "reads_clob": False,
        "reads_shared_live_database": False,
        "places_orders": False,
        "calls_clob": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_native_ensemble_basket_binding(
    *,
    scenario_roles_artifact: Mapping[str, Any],
    probability_stage_snapshots: Any,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Build one content-addressed, no-live basket-candidate artifact.

    ``probability_stage_snapshots`` accepts a raw ``ProbabilityEnvelope``
    mapping, a list of them, or a wrapper containing ``snapshots``/``envelopes``.
    For each station the highest available stage is selected in the strict
    order ``P1`` → ``P0-M`` → ``P0``.
    """

    scenario_raw, scenario_hash, candidates = _verify_scenario_roles(scenario_roles_artifact)
    stage_rows, stage_input_hash = _verify_stage_snapshots(probability_stage_snapshots)
    if as_of_utc is not None:
        _text("as_of_utc", as_of_utc)
        try:
            datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("basket_binding_as_of_utc_invalid") from exc
    event_dates = {row["event_date"] for row in candidates}
    if len(event_dates) != 1:
        raise ContractError("basket_binding_event_date_scope_mismatch")
    event_date = next(iter(event_dates))
    station_candidates: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda row: row["station_id"]):
        selected, blockers = _choose_stage(
            stage_rows,
            station_id=candidate["station_id"],
            event_date=event_date,
            as_of_utc=as_of_utc,
        )
        station_candidates.append(_bind_station(candidate, selected, blockers))
    blocked_count = sum(row["status"] != "BUILT_NO_LIVE" for row in station_candidates)
    identity = {
        "schema": SCHEMA,
        "method_version": METHOD_VERSION,
        "authority_scope": AUTHORITY_SCOPE,
        "state": "BUILT_NO_LIVE" if blocked_count == 0 else "BLOCKED_FAIL_CLOSED",
        "event_date": event_date,
        "station_scope": [row["station_id"] for row in station_candidates],
        "station_count": len(station_candidates),
        "blocked_station_count": blocked_count,
        "scenario_roles_schema": str(scenario_raw["schema"]),
        "scenario_roles_content_sha256": scenario_hash,
        "probability_stage_input_sha256": stage_input_hash,
        "stage_precedence": list(STAGES),
        "candidates": station_candidates,
        "raw_member_probability_is_not_final_probability": True,
        "stage_probabilities_are_not_recomputed": True,
        "open_meteo_aggregator_is_not_formal_authority": True,
        "republisher_fallback_allowed_only_with_authority": True,
        "reads_clob": False,
        "reads_shared_live_database": False,
        "places_orders": False,
        "writes_p0": False,
        "writes_p0m": False,
        "writes_p1": False,
        "writes_shared_live_database": False,
        "calls_clob": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def select_probability_stage(
    probability_stage_snapshots: Any,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Return the selected verified stage record for small callers/tests."""

    rows, _ = _verify_stage_snapshots(probability_stage_snapshots)
    selected, blockers = _choose_stage(
        rows,
        station_id=_text("station_id", station_id),
        event_date=_date("event_date", event_date),
        as_of_utc=as_of_utc,
    )
    if selected is None:
        raise ContractError(";".join(blockers))
    return _stage_record(selected)


# Explicit aliases make the module easy to call from small offline tools
# without introducing a second implementation or touching production code.
build_basket_binding_artifact = build_native_ensemble_basket_binding
bind_native_scenario_roles_to_probability_stage = build_native_ensemble_basket_binding


__all__ = [
    "AUTHORITY_SCOPE",
    "METHOD_VERSION",
    "SCHEMA",
    "STAGES",
    "bind_native_scenario_roles_to_probability_stage",
    "build_basket_binding_artifact",
    "build_native_ensemble_basket_binding",
    "select_probability_stage",
]
