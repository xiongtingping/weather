"""One canonical model-product identity registry and receipt contract.

Coverage rows and retained native decode evidence are two ingress sources for
the same receipt schema.  The receipt resolves product semantics only; it
never manufactures an arrival, decision, raw object, forecast or OOS event.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .model_system_coverage import validate_coverage_row


SCHEMA = "bayes_basket_target_canonical_model_product_identity_receipt_v2"
REGISTRY_VERSION = "CANONICAL_MODEL_PRODUCT_IDENTITY_REGISTRY_V2"
SETTLEMENT_TIMEZONE = "America/New_York"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
EVENT_PROTOCOL_BY_SOURCE = {
    "aggregator_republisher": "STRICT_HISTORICAL_AWC_REPUBLISHER_V13_V1",
    "native_official": "STRICT_HISTORICAL_AWC_NATIVE_V13_V1",
}
LEAD_SCOPE = {
    "d0": "SAME_SETTLEMENT_DAY",
    "d1": "PREVIOUS_DAY1",
    "d2": "PREVIOUS_DAY2",
}


def _product(
    product_id: str, *, model_id: str, ensemble_system_id: str | None,
    model_family: str, provider_id: str, provider_product_id: str,
    independence_family_id: str, source_class: str, ingress_kind: str,
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "model_id": model_id,
        "ensemble_system_id": ensemble_system_id,
        "model_family": model_family,
        "provider_id": provider_id,
        "provider_product_id": provider_product_id,
        "independence_family_id": independence_family_id,
        "source_class": source_class,
        "ingress_kind": ingress_kind,
    }


_COVERAGE = "COVERAGE_MATRIX_ROW"
_NATIVE = "NATIVE_DECODE_EVIDENCE"
PRODUCT_ROWS = (
    _product("ECMWF_IFS_ENS_OPEN_PF50", model_id="ECMWF", ensemble_system_id="ECMWF_IFS_ENS_OPEN_PF50", model_family="global_ensemble", provider_id="ECMWF", provider_product_id="ecmwf_ifs_ens_open_pf50", independence_family_id="ECMWF", source_class="native_official", ingress_kind=_COVERAGE),
    _product("ECMWF_AIFS_ENS_GLOBAL", model_id="AIFS", ensemble_system_id="ECMWF_AIFS_ENS_GLOBAL", model_family="global_ensemble", provider_id="ecmwf_aifs_ens_official", provider_product_id="ecmwf_aifs025_ensemble", independence_family_id="ECMWF", source_class="native_official", ingress_kind=_COVERAGE),
    _product("DWD_ICON_GLOBAL_EPS", model_id="ICON", ensemble_system_id="DWD_ICON_GLOBAL_EPS", model_family="global_ensemble", provider_id="DWD", provider_product_id="dwd_icon_global_eps", independence_family_id="DWD", source_class="native_official", ingress_kind=_COVERAGE),
    _product("NOAA_GEFS_05", model_id="GFS", ensemble_system_id="NOAA_GEFS_05", model_family="global_ensemble", provider_id="NOAA", provider_product_id="noaa_gefs_0p5", independence_family_id="NOAA", source_class="native_official", ingress_kind=_COVERAGE),
    _product("ECCC_GEM_GLOBAL_ENS", model_id="GEM", ensemble_system_id="ECCC_GEM_GLOBAL_ENS", model_family="global_ensemble", provider_id="ECCC", provider_product_id="eccc_gem_global_ensemble", independence_family_id="ECCC", source_class="native_official", ingress_kind=_COVERAGE),
)
PRODUCT_BY_ID = {row["product_id"]: row for row in PRODUCT_ROWS}
if len(PRODUCT_BY_ID) != len(PRODUCT_ROWS):
    raise RuntimeError("canonical_model_product_identity_duplicate")
if any(
    row["source_class"] != "native_official"
    or row["model_family"] != "global_ensemble"
    or row["ensemble_system_id"] is None
    for row in PRODUCT_ROWS
):
    raise RuntimeError("canonical_active_product_registry_native_ensemble_only")
COVERAGE_PRODUCT_IDS = frozenset(
    row["product_id"] for row in PRODUCT_ROWS if row["ingress_kind"] == _COVERAGE
)
NATIVE_PRODUCT_IDS = frozenset(
    row["product_id"] for row in PRODUCT_ROWS if row["ingress_kind"] == _NATIVE
)
CANONICAL_INDEPENDENCE_BY_PRODUCT = {
    key: row["independence_family_id"] for key, row in PRODUCT_BY_ID.items()
}
PROVIDER_BY_PRODUCT = {key: row["provider_id"] for key, row in PRODUCT_BY_ID.items()}
MODEL_FAMILY_BY_PRODUCT = {
    key: row["model_family"] for key, row in PRODUCT_BY_ID.items()
}
SOURCE_CLASS_BY_PRODUCT = {
    key: row["source_class"] for key, row in PRODUCT_BY_ID.items()
}
FORMAL_P0_NATIVE_ENSEMBLE_PRODUCT_IDS = frozenset(
    row["product_id"]
    for row in PRODUCT_ROWS
    if row["source_class"] == "native_official"
    and row["model_family"] == "global_ensemble"
    and row["ensemble_system_id"] is not None
)
REGISTRY_CONTENT_SHA256 = content_sha256({
    "version": REGISTRY_VERSION,
    "rows": list(PRODUCT_ROWS),
})
IDENTITY_FIELDS = frozenset({
    "schema", "registry_version", "registry_content_sha256",
    "identity_source_kind", "identity_source_receipt_sha256", "product_id",
    "station_id", "model_id", "ensemble_system_id", "model_family",
    "provider_id", "provider_product_id", "independence_family_id",
    "source_class", "authority_scope", "settlement_timezone", "lead_bucket",
    "lead_bucket_scope", "run_cycle", "run_cycle_scope",
    "event_oos_protocol_version", "state",
    "historical_availability_receipt_present", "consumable", "content_sha256",
})

FORMAL_P0_COMPONENT_IDENTITY_FIELDS = (
    "model_id", "ensemble_system_id", "model_family", "provider_id",
    "provider_product_id", "independence_family_id", "source_class",
)


def assert_formal_p0_native_ensemble_systems(
    components: Any,
) -> tuple[str, ...]:
    """Validate the constitutional product gate for a formal P0 mask.

    Product semantics come only from ``PRODUCT_ROWS``.  A component with
    renamed, inferred or partially matching fields is deliberately rejected.
    """

    if not isinstance(components, (list, tuple)) or len(components) < 2:
        raise ContractError("formal_p0_two_native_ensemble_systems_required")
    matched_product_ids: list[str] = []
    ensemble_system_ids: set[str] = set()
    independence_family_ids: set[str] = set()
    for component in components:
        if not isinstance(component, Mapping):
            raise ContractError("formal_p0_component_identity_invalid")
        matches = [
            row for row in PRODUCT_ROWS
            if all(component.get(field) == row[field]
                   for field in FORMAL_P0_COMPONENT_IDENTITY_FIELDS)
        ]
        if len(matches) != 1:
            raise ContractError("formal_p0_component_registry_match_required")
        product = matches[0]
        if product["product_id"] not in FORMAL_P0_NATIVE_ENSEMBLE_PRODUCT_IDS:
            raise ContractError("formal_p0_native_global_ensemble_required")
        ensemble_system_id = str(product["ensemble_system_id"] or "").strip()
        independence_family_id = str(
            product["independence_family_id"] or ""
        ).strip()
        if not ensemble_system_id or not independence_family_id:
            raise ContractError("formal_p0_ensemble_identity_missing")
        matched_product_ids.append(str(product["product_id"]))
        ensemble_system_ids.add(ensemble_system_id)
        independence_family_ids.add(independence_family_id)
    if len(ensemble_system_ids) < 2:
        raise ContractError("formal_p0_two_native_ensemble_systems_required")
    if len(independence_family_ids) < 2:
        raise ContractError("formal_p0_two_independence_families_required")
    return tuple(matched_product_ids)


def _run_scope(source_class: str, run_cycle: Any) -> str:
    if source_class == "native_official" and run_cycle is None:
        raise ContractError("canonical_model_product_identity_native_cycle_missing")
    if run_cycle is None:
        return "REPUBLISHER_NO_NATIVE_CYCLE"
    if str(run_cycle).startswith("PREVIOUS_DAY"):
        if source_class != "aggregator_republisher":
            raise ContractError("canonical_model_product_identity_native_relative_cycle_forbidden")
        return "REPUBLISHER_ARCHIVE_RELATIVE_DAY"
    return "OFFICIAL_RUN_CYCLE"


def _build_receipt(
    *, product: Mapping[str, Any], station_id: Any, lead_bucket: Any,
    run_cycle: Any, identity_source_receipt_sha256: Any,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    lead = str(lead_bucket or "").strip()
    source_sha = str(identity_source_receipt_sha256 or "").strip().lower()
    if (
        not station or lead not in LEAD_SCOPE or len(source_sha) != 64
        or any(char not in "0123456789abcdef" for char in source_sha)
    ):
        raise ContractError("canonical_model_product_identity_scope_invalid")
    run_scope = _run_scope(str(product["source_class"]), run_cycle)
    identity = {
        "schema": SCHEMA,
        "registry_version": REGISTRY_VERSION,
        "registry_content_sha256": REGISTRY_CONTENT_SHA256,
        "identity_source_kind": product["ingress_kind"],
        "identity_source_receipt_sha256": source_sha,
        "product_id": product["product_id"],
        "station_id": station,
        "model_id": product["model_id"],
        "ensemble_system_id": product["ensemble_system_id"],
        "model_family": product["model_family"],
        "provider_id": product["provider_id"],
        "provider_product_id": product["provider_product_id"],
        "independence_family_id": product["independence_family_id"],
        "source_class": product["source_class"],
        "authority_scope": AUTHORITY_SCOPE,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "lead_bucket": lead,
        "lead_bucket_scope": LEAD_SCOPE[lead],
        "run_cycle": run_cycle,
        "run_cycle_scope": run_scope,
        "event_oos_protocol_version": EVENT_PROTOCOL_BY_SOURCE[
            str(product["source_class"])
        ],
        "state": "IDENTITY_READY_EVENT_DATA_MISSING",
        "historical_availability_receipt_present": False,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_model_product_identity_receipt(
    coverage_row: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    row = validate_coverage_row(coverage_row)
    product_id = str(row["product_id"])
    product = PRODUCT_BY_ID.get(product_id)
    if product is None or product_id not in COVERAGE_PRODUCT_IDS:
        raise ContractError("canonical_model_product_identity_product_unknown")
    if any(row[field] != product[field] for field in (
        "provider_id", "model_family", "source_class",
    )):
        raise ContractError("canonical_model_product_identity_coverage_mapping_mismatch")
    if product["source_class"] == "native_official" and row["run_cycle"] is None:
        return None, ("run_cycle", "run_cycle_scope")
    receipt = _build_receipt(
        product=product, station_id=row["station_id"],
        lead_bucket=row["lead_bucket"], run_cycle=row["run_cycle"],
        identity_source_receipt_sha256=row["content_sha256"],
    )
    return validate_model_product_identity_receipt(receipt), ()


def build_registered_model_product_identity_receipt(
    *, product_id: str, station_id: str, lead_bucket: str, run_cycle: str,
    identity_source_receipt_sha256: str,
) -> dict[str, Any]:
    product = PRODUCT_BY_ID.get(str(product_id))
    if product is None or product_id not in NATIVE_PRODUCT_IDS:
        raise ContractError("canonical_model_product_identity_registered_product_invalid")
    return validate_model_product_identity_receipt(_build_receipt(
        product=product, station_id=station_id, lead_bucket=lead_bucket,
        run_cycle=run_cycle,
        identity_source_receipt_sha256=identity_source_receipt_sha256,
    ))


def validate_model_product_identity_receipt(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != IDENTITY_FIELDS:
        raise ContractError("canonical_model_product_identity_receipt_fields_mismatch")
    value = dict(raw)
    supplied = str(value.get("content_sha256") or "").lower()
    if supplied != content_sha256({
        key: item for key, item in value.items() if key != "content_sha256"
    }):
        raise ContractError("canonical_model_product_identity_receipt_hash_mismatch")
    product = PRODUCT_BY_ID.get(str(value.get("product_id") or ""))
    if product is None:
        raise ContractError("canonical_model_product_identity_product_unknown")
    try:
        expected_run_scope = _run_scope(str(product["source_class"]), value["run_cycle"])
    except ContractError as exc:
        raise ContractError("canonical_model_product_identity_receipt_semantics_mismatch") from exc
    source_sha = str(value.get("identity_source_receipt_sha256") or "").lower()
    if (
        value.get("schema") != SCHEMA
        or value.get("registry_version") != REGISTRY_VERSION
        or value.get("registry_content_sha256") != REGISTRY_CONTENT_SHA256
        or any(value.get(field) != product[field] for field in (
            "product_id", "model_id", "ensemble_system_id", "model_family",
            "provider_id", "provider_product_id", "independence_family_id",
            "source_class",
        ))
        or value.get("identity_source_kind") != product["ingress_kind"]
        or value.get("authority_scope") != AUTHORITY_SCOPE
        or value.get("settlement_timezone") != SETTLEMENT_TIMEZONE
        or value.get("lead_bucket_scope") != LEAD_SCOPE.get(value.get("lead_bucket"))
        or value.get("run_cycle_scope") != expected_run_scope
        or value.get("event_oos_protocol_version")
        != EVENT_PROTOCOL_BY_SOURCE.get(value.get("source_class"))
        or value.get("state") != "IDENTITY_READY_EVENT_DATA_MISSING"
        or value.get("historical_availability_receipt_present") is not False
        or value.get("consumable") is not False
        or not str(value.get("station_id") or "").isupper()
        or len(source_sha) != 64
        or any(char not in "0123456789abcdef" for char in source_sha)
    ):
        raise ContractError("canonical_model_product_identity_receipt_semantics_mismatch")
    return value


__all__ = [
    "AUTHORITY_SCOPE", "CANONICAL_INDEPENDENCE_BY_PRODUCT",
    "COVERAGE_PRODUCT_IDS", "EVENT_PROTOCOL_BY_SOURCE", "IDENTITY_FIELDS",
    "LEAD_SCOPE", "MODEL_FAMILY_BY_PRODUCT", "NATIVE_PRODUCT_IDS",
    "FORMAL_P0_COMPONENT_IDENTITY_FIELDS",
    "FORMAL_P0_NATIVE_ENSEMBLE_PRODUCT_IDS", "PRODUCT_BY_ID", "PRODUCT_ROWS",
    "PROVIDER_BY_PRODUCT",
    "REGISTRY_CONTENT_SHA256", "REGISTRY_VERSION", "SCHEMA",
    "SETTLEMENT_TIMEZONE", "SOURCE_CLASS_BY_PRODUCT",
    "build_model_product_identity_receipt",
    "build_registered_model_product_identity_receipt",
    "assert_formal_p0_native_ensemble_systems",
    "validate_model_product_identity_receipt",
]
