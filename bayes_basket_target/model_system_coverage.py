"""Canonical read-only coverage matrix for PR05--PR09 model systems.

This is an execution inventory, not a forecast fact and not an activation.
Every adapter must emit one explicit terminal row for each
``station x d0/d1/d2 x product`` scope.  Missing run-cycle or evidence stays
missing and blocks the row; this module never guesses aliases or fills values
from another lead, product, provider, or source lineage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .canonical_contract import TYPED_SCOPE_BLOCKERS
from .contracts import ContractError, content_sha256
from .station_registry import coordinate_registry_manifest


ROW_SCHEMA = "bayes_basket_target_model_system_coverage_row_v1"
MATRIX_SCHEMA = "bayes_basket_target_model_system_coverage_matrix_v1"
LEADS = ("d0", "d1", "d2")
MODEL_FAMILIES = ("global_deterministic", "global_ensemble", "regional")
SOURCE_CLASSES = ("native_official", "aggregator_republisher")
EVIDENCE_ROLES = ("historical_strict_oos", "observational_candidate_not_oos")
TERMINAL_STATES = ("DATA_READY", "OOS_READY", "CANDIDATE_ONLY", "BLOCKED")
MIN_TRAIN_COUNT = 20
MIN_HOLDOUT_COUNT = 8

ROW_FIELDS = frozenset(
    {
        "schema",
        "station_id",
        "product_id",
        "provider_id",
        "model_family",
        "independence_family_id",
        "source_class",
        "lead_bucket",
        "run_cycle",
        "as_of_utc",
        "state",
        "blockers",
        "evidence_role",
        "strict_asof",
        "closed_awc_truth",
        "train_count",
        "holdout_count",
        "source_evidence_sha256",
        "raw_receipt_sha256",
        "oos_evidence_sha256",
        "single_system_artifact_sha256",
        "native_arrival_indexed",
        "formal_live_authority",
        "cross_selection_count",
        "cross_activation_count",
        "p0_count",
        "content_sha256",
    }
)
MATRIX_FIELDS = frozenset(
    {
        "schema",
        "as_of_utc",
        "station_registry_sha256",
        "station_count",
        "lead_buckets",
        "product_ids",
        "expected_scope_count",
        "scope_count",
        "state_counts",
        "cross_selection_count",
        "cross_activation_count",
        "p0_count",
        "rows",
        "content_sha256",
    }
)


def _utc_text(value: Any, code: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: Any, code: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(code)
    return result


def _nullable_sha(value: Any, code: str) -> str | None:
    if value is None:
        return None
    result = str(value).strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(code)
    return result


def _count(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ContractError(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(code) from exc
    if result < 0 or result != value:
        raise ContractError(code)
    return result


def _boolean(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(code)
    return value


def canonical_station_ids() -> tuple[str, ...]:
    manifest = coordinate_registry_manifest()
    stations = tuple(str(value).upper() for value in manifest["station_ids"])
    if len(stations) != 45 or len(set(stations)) != 45:
        raise ContractError("model_coverage_station_registry_not_exact_45")
    return stations


def validate_coverage_row(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("model_coverage_row_mapping_required")
    unknown = sorted(set(value) - ROW_FIELDS)
    missing = sorted(ROW_FIELDS - set(value))
    if unknown:
        raise ContractError("model_coverage_row_unknown_field:" + ",".join(unknown))
    if missing:
        raise ContractError("model_coverage_row_missing_field:" + ",".join(missing))
    if value.get("schema") != ROW_SCHEMA:
        raise ContractError("model_coverage_row_schema_invalid")

    station_id = _text(value["station_id"], "model_coverage_station_missing").upper()
    if station_id not in set(canonical_station_ids()):
        raise ContractError("model_coverage_station_out_of_scope")
    product_id = _text(value["product_id"], "model_coverage_product_missing")
    provider_id = _text(value["provider_id"], "model_coverage_provider_missing")
    model_family = _text(value["model_family"], "model_coverage_family_missing")
    if model_family not in MODEL_FAMILIES:
        raise ContractError("model_coverage_family_invalid")
    independence_family_id = _text(
        value["independence_family_id"], "model_coverage_independence_family_missing"
    )
    source_class = _text(value["source_class"], "model_coverage_source_class_missing")
    if source_class not in SOURCE_CLASSES:
        raise ContractError("model_coverage_source_class_invalid")
    lead_bucket = _text(value["lead_bucket"], "model_coverage_lead_missing").lower()
    if lead_bucket not in LEADS:
        raise ContractError("model_coverage_lead_invalid")
    run_cycle = value["run_cycle"]
    if run_cycle is not None:
        run_cycle = _text(run_cycle, "model_coverage_run_cycle_invalid")
    as_of_utc = _utc_text(value["as_of_utc"], "model_coverage_as_of_invalid")
    state = _text(value["state"], "model_coverage_state_missing")
    if state not in TERMINAL_STATES:
        raise ContractError("model_coverage_state_invalid")
    if not isinstance(value["blockers"], list):
        raise ContractError("model_coverage_blockers_list_required")
    blockers = tuple(value["blockers"])
    if any(not isinstance(item, str) or item not in TYPED_SCOPE_BLOCKERS for item in blockers):
        raise ContractError("model_coverage_blocker_invalid")
    if len(blockers) != len(set(blockers)):
        raise ContractError("model_coverage_blocker_duplicate")
    if state == "OOS_READY" and blockers:
        raise ContractError("model_coverage_ready_has_blocker")
    if state != "OOS_READY" and len(blockers) != 1:
        raise ContractError("model_coverage_terminal_blocker_not_unique")
    if run_cycle is None and blockers not in (("RUN_CYCLE_MISMATCH",), ("DATA_MISSING",)):
        raise ContractError("model_coverage_missing_cycle_without_exact_blocker")

    evidence_role = _text(value["evidence_role"], "model_coverage_evidence_role_missing")
    if evidence_role not in EVIDENCE_ROLES:
        raise ContractError("model_coverage_evidence_role_invalid")
    strict_asof = _boolean(value["strict_asof"], "model_coverage_strict_asof_boolean_required")
    closed_awc_truth = _boolean(
        value["closed_awc_truth"], "model_coverage_closed_truth_boolean_required"
    )
    train_count = _count(value["train_count"], "model_coverage_train_count_invalid")
    holdout_count = _count(value["holdout_count"], "model_coverage_holdout_count_invalid")
    source_evidence_sha256 = _nullable_sha(
        value["source_evidence_sha256"], "model_coverage_source_evidence_sha_invalid"
    )
    if source_evidence_sha256 is None:
        raise ContractError("model_coverage_source_evidence_sha_missing")
    raw_receipt_sha256 = _nullable_sha(
        value["raw_receipt_sha256"], "model_coverage_raw_receipt_sha_invalid"
    )
    oos_evidence_sha256 = _nullable_sha(
        value["oos_evidence_sha256"], "model_coverage_oos_evidence_sha_invalid"
    )
    single_system_artifact_sha256 = _nullable_sha(
        value["single_system_artifact_sha256"], "model_coverage_artifact_sha_invalid"
    )
    native_arrival_indexed = _boolean(
        value["native_arrival_indexed"], "model_coverage_native_arrival_boolean_required"
    )
    formal_live_authority = _boolean(
        value["formal_live_authority"], "model_coverage_formal_authority_boolean_required"
    )
    cross_selection_count = _count(
        value["cross_selection_count"], "model_coverage_cross_selection_count_invalid"
    )
    cross_activation_count = _count(
        value["cross_activation_count"], "model_coverage_cross_activation_count_invalid"
    )
    p0_count = _count(value["p0_count"], "model_coverage_p0_count_invalid")
    if cross_selection_count or cross_activation_count or p0_count:
        raise ContractError("model_coverage_cross_or_p0_authority_forbidden")
    if formal_live_authority:
        raise ContractError("model_coverage_formal_live_authority_forbidden")
    if source_class == "aggregator_republisher" and native_arrival_indexed:
        raise ContractError("model_coverage_republisher_native_arrival_forbidden")
    if evidence_role == "observational_candidate_not_oos":
        if strict_asof or closed_awc_truth or train_count or holdout_count:
            raise ContractError("model_coverage_forward_counted_as_oos")
        if state == "OOS_READY":
            raise ContractError("model_coverage_forward_marked_oos_ready")
    if state == "OOS_READY":
        if evidence_role != "historical_strict_oos" or not strict_asof or not closed_awc_truth:
            raise ContractError("model_coverage_oos_ready_evidence_invalid")
        if train_count < MIN_TRAIN_COUNT or holdout_count < MIN_HOLDOUT_COUNT:
            raise ContractError("model_coverage_oos_ready_sample_short")
        if oos_evidence_sha256 is None or single_system_artifact_sha256 is None:
            raise ContractError("model_coverage_oos_ready_artifact_missing")
        if source_class == "native_official" and (
            not native_arrival_indexed or raw_receipt_sha256 is None
        ):
            raise ContractError("model_coverage_native_oos_ready_arrival_missing")

    identity = {
        "schema": ROW_SCHEMA,
        "station_id": station_id,
        "product_id": product_id,
        "provider_id": provider_id,
        "model_family": model_family,
        "independence_family_id": independence_family_id,
        "source_class": source_class,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "as_of_utc": as_of_utc,
        "state": state,
        "blockers": list(blockers),
        "evidence_role": evidence_role,
        "strict_asof": strict_asof,
        "closed_awc_truth": closed_awc_truth,
        "train_count": train_count,
        "holdout_count": holdout_count,
        "source_evidence_sha256": source_evidence_sha256,
        "raw_receipt_sha256": raw_receipt_sha256,
        "oos_evidence_sha256": oos_evidence_sha256,
        "single_system_artifact_sha256": single_system_artifact_sha256,
        "native_arrival_indexed": native_arrival_indexed,
        "formal_live_authority": formal_live_authority,
        "cross_selection_count": cross_selection_count,
        "cross_activation_count": cross_activation_count,
        "p0_count": p0_count,
    }
    if value["content_sha256"] != content_sha256(identity):
        raise ContractError("model_coverage_row_content_sha_mismatch")
    return {**identity, "content_sha256": str(value["content_sha256"])}


def make_coverage_row(**values: Any) -> dict[str, Any]:
    """Build a row only from explicitly supplied fields.

    No field has a semantic default.  Callers must state zero/false/null
    explicitly so missing evidence cannot become authority by omission.
    """

    if "content_sha256" in values:
        raise ContractError("model_coverage_content_sha_is_derived")
    candidate = {"schema": ROW_SCHEMA, **values}
    if set(candidate) != ROW_FIELDS - {"content_sha256"}:
        unknown = sorted(set(candidate) - (ROW_FIELDS - {"content_sha256"}))
        missing = sorted((ROW_FIELDS - {"content_sha256"}) - set(candidate))
        suffix = ",".join([*(f"unknown={item}" for item in unknown), *(f"missing={item}" for item in missing)])
        raise ContractError("model_coverage_explicit_fields_required:" + suffix)
    candidate["content_sha256"] = content_sha256(candidate)
    return validate_coverage_row(candidate)


def build_coverage_matrix(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_product_ids: Sequence[str],
    as_of_utc: str,
) -> dict[str, Any]:
    canonical_as_of = _utc_text(as_of_utc, "model_coverage_matrix_as_of_invalid")
    expected_products = tuple(sorted({_text(value, "model_coverage_expected_product_missing") for value in expected_product_ids}))
    if not expected_products:
        raise ContractError("model_coverage_expected_products_missing")
    validated = tuple(validate_coverage_row(row) for row in rows)
    keys: set[tuple[str, str, str]] = set()
    for row in validated:
        if row["as_of_utc"] != canonical_as_of:
            raise ContractError("model_coverage_matrix_as_of_mismatch")
        key = (row["station_id"], row["lead_bucket"], row["product_id"])
        if key in keys:
            raise ContractError("model_coverage_scope_duplicate:" + ":".join(key))
        keys.add(key)
    expected_keys = {
        (station, lead, product)
        for station in canonical_station_ids()
        for lead in LEADS
        for product in expected_products
    }
    actual_products = {row["product_id"] for row in validated}
    if actual_products != set(expected_products):
        raise ContractError("model_coverage_product_set_mismatch")
    if keys != expected_keys:
        raise ContractError("model_coverage_scope_not_exact_45x3xproduct")
    ordered = sorted(
        validated,
        key=lambda row: (row["product_id"], row["lead_bucket"], row["station_id"]),
    )
    state_counts = {state: 0 for state in TERMINAL_STATES}
    for row in ordered:
        state_counts[row["state"]] += 1
    identity = {
        "schema": MATRIX_SCHEMA,
        "as_of_utc": canonical_as_of,
        "station_registry_sha256": coordinate_registry_manifest()["registry_sha256"],
        "station_count": 45,
        "lead_buckets": list(LEADS),
        "product_ids": list(expected_products),
        "expected_scope_count": 45 * len(LEADS) * len(expected_products),
        "scope_count": len(ordered),
        "state_counts": state_counts,
        "cross_selection_count": 0,
        "cross_activation_count": 0,
        "p0_count": 0,
        "rows": ordered,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_coverage_matrix(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("model_coverage_matrix_mapping_required")
    unknown = sorted(set(value) - MATRIX_FIELDS)
    missing = sorted(MATRIX_FIELDS - set(value))
    if unknown:
        raise ContractError("model_coverage_matrix_unknown_field:" + ",".join(unknown))
    if missing:
        raise ContractError("model_coverage_matrix_missing_field:" + ",".join(missing))
    if value.get("schema") != MATRIX_SCHEMA:
        raise ContractError("model_coverage_matrix_schema_invalid")
    rows = value.get("rows")
    products = value.get("product_ids")
    if not isinstance(rows, list) or not isinstance(products, list):
        raise ContractError("model_coverage_matrix_rows_or_products_invalid")
    rebuilt = build_coverage_matrix(
        rows,
        expected_product_ids=products,
        as_of_utc=str(value.get("as_of_utc") or ""),
    )
    if dict(value) != rebuilt:
        raise ContractError("model_coverage_matrix_identity_mismatch")
    return rebuilt


__all__ = [
    "EVIDENCE_ROLES",
    "LEADS",
    "MATRIX_SCHEMA",
    "ROW_SCHEMA",
    "SOURCE_CLASSES",
    "TERMINAL_STATES",
    "build_coverage_matrix",
    "canonical_station_ids",
    "make_coverage_row",
    "validate_coverage_matrix",
    "validate_coverage_row",
]
