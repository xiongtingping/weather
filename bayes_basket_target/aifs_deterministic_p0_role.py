"""Deterministic-aware, non-materializing P0 role selection for AIFS Single.

The legacy deterministic P0 path collapses AIFS and IFS into one ``ECMWF``
model id.  This isolated adapter deliberately does neither that nor an AIFS
member fabrication.  It joins a promoted AIFS Single provider authority to an
independent *non-ECMWF* formal native P0-family authority and emits only the
exact provider/role selection needed by a future P0 compiler.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .aifs_deterministic_authority import (
    validate_aifs_deterministic_provider_authority,
)
from .aifs_deterministic_oos import (
    INDEPENDENCE_FAMILY_ID,
    MODEL_ID,
    PRODUCT_ID,
    PROVIDER_ID,
)
from .contracts import ContractError, content_sha256
from .native_ensemble_contract import native_ensemble_family_set_hash


INDEPENDENT_FAMILY_P0_AUTHORITY_SCHEMA = (
    "official_native_independent_family_p0_role_authority_v1"
)
SELECTION_SCHEMA = "aifs_deterministic_p0_role_selection_v1"


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"aifs_deterministic_p0_role_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            f"aifs_deterministic_p0_role_{name}_timezone_required"
        )
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"aifs_deterministic_p0_role_{name}_invalid_sha256")
    return result


def _hash_identity(raw: Mapping[str, Any], *, label: str) -> None:
    supplied = _sha(label, raw.get("content_sha256"))
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(f"aifs_deterministic_p0_role_{label}_sha_mismatch")


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"aifs_deterministic_p0_role_{name}_missing")
    return result


def validate_independent_family_p0_authority(
    authority: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate a non-ECMWF formal P0-family authority receipt.

    This adapter does not issue authority for another model family.  It only
    accepts an external, immutable official-native authority that is already
    formally OOS-qualified for its exact P0 scope.
    """

    if (
        not isinstance(authority, Mapping)
        or authority.get("schema") != INDEPENDENT_FAMILY_P0_AUTHORITY_SCHEMA
    ):
        raise ContractError("aifs_deterministic_p0_role_independent_schema_invalid")
    _hash_identity(authority, label="independent_authority")
    family = _text("independent_family", authority.get("independence_family_id"))
    provider = _text("independent_provider", authority.get("provider_id")).lower()
    model = _text("independent_model", authority.get("model_id")).upper()
    if (
        family == INDEPENDENCE_FAMILY_ID
        or provider == PROVIDER_ID
        or model == MODEL_ID
        or authority.get("publication_authority") != "official_native"
        or authority.get("forecast_role") != "NATIVE_ENSEMBLE"
        or authority.get("formal_oos_state") != "FORMAL_P0_READY"
        or authority.get("formal_provider_eligible") is not True
        or authority.get("formal_p0_role_eligible") is not True
        or int(authority.get("native_member_count") or 0) < 2
    ):
        raise ContractError("aifs_deterministic_p0_role_independent_identity_invalid")
    for name in (
        "formal_oos_artifact_sha256",
        "formal_p0_authority_sha256",
        "native_member_evidence_sha256",
    ):
        _sha(name, authority.get(name))
    for name in (
        "station_id",
        "target_timezone",
        "season_bin",
        "lead_bucket",
        "run_cycle",
        "settlement_definition",
    ):
        _text("independent_" + name, authority.get(name))
    valid_from = _utc("independent_valid_from", authority.get("valid_from_utc"))
    valid_until = _utc("independent_valid_until", authority.get("valid_until_utc"))
    if valid_until <= valid_from:
        raise ContractError("aifs_deterministic_p0_role_independent_validity_invalid")
    boundary = authority.get("boundary")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("writes_production_database") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
    ):
        raise ContractError("aifs_deterministic_p0_role_independent_boundary_invalid")
    return authority


def build_aifs_deterministic_p0_role_selection(
    *,
    aifs_training_batch: Mapping[str, Any],
    aifs_strict_oos_report: Mapping[str, Any],
    aifs_promotion_receipt: Mapping[str, Any],
    aifs_provider_authority: Mapping[str, Any],
    independent_family_p0_authority: Mapping[str, Any],
    station_id: str,
    target_timezone: str,
    season_bin: str,
    lead_bucket: str,
    run_cycle: str,
    settlement_definition: str,
    selected_at_utc: str,
) -> dict[str, Any]:
    """Build an exact two-family role selection without producing P0.

    The AIFS authority proves only the ECMWF deterministic provider.  The
    companion authority must prove an independent, formal native P0 family.
    Any same-family input, stale authority, scope mismatch, or missing proof
    rejects the selection before a probability compiler can see it.
    """

    aifs_candidate = validate_aifs_deterministic_provider_authority(
        aifs_provider_authority,
        training_batch=aifs_training_batch,
        strict_oos_report=aifs_strict_oos_report,
        promotion_receipt=aifs_promotion_receipt,
    )
    other = validate_independent_family_p0_authority(
        independent_family_p0_authority
    )
    station = _text("station_id", station_id).upper()
    timezone_name = _text("target_timezone", target_timezone)
    season = _text("season_bin", season_bin)
    lead = _text("lead_bucket", lead_bucket)
    cycle = _text("run_cycle", run_cycle)
    settlement = _text("settlement_definition", settlement_definition)
    selected_at = _utc("selected_at", selected_at_utc)
    scope = {
        "station_id": station,
        "target_timezone": timezone_name,
        "season_bin": season,
        "lead_bucket": lead,
        "run_cycle": cycle,
        "settlement_definition": settlement,
    }
    if (
        aifs_provider_authority.get("station_id") != station
        or aifs_provider_authority.get("target_timezone") != timezone_name
        or aifs_provider_authority.get("season_bin") != season
        or aifs_provider_authority.get("lead_bucket") != lead
        or aifs_provider_authority.get("run_cycle") != cycle
    ):
        raise ContractError("aifs_deterministic_p0_role_aifs_scope_mismatch")
    if any(other.get(key) != value for key, value in scope.items()):
        raise ContractError("aifs_deterministic_p0_role_independent_scope_mismatch")
    if _utc("aifs_valid_from", aifs_provider_authority["valid_from_utc"]) > selected_at or _utc(
        "aifs_valid_until", aifs_provider_authority["valid_until_utc"]
    ) <= selected_at:
        raise ContractError("aifs_deterministic_p0_role_aifs_authority_stale")
    if _utc("independent_valid_from", other["valid_from_utc"]) > selected_at or _utc(
        "independent_valid_until", other["valid_until_utc"]
    ) <= selected_at:
        raise ContractError("aifs_deterministic_p0_role_independent_authority_stale")
    family_ids = (INDEPENDENCE_FAMILY_ID, str(other["independence_family_id"]))
    if len(set(family_ids)) != 2:
        raise ContractError("aifs_deterministic_p0_role_two_independent_families_required")
    family_set_sha = native_ensemble_family_set_hash(family_ids)
    role_identity = {
        "ecmwf_role": {
            "provider_id": PROVIDER_ID,
            "product_id": PRODUCT_ID,
            "model_id": MODEL_ID,
            "forecast_role": "DETERMINISTIC_SINGLE",
            "independence_family_id": INDEPENDENCE_FAMILY_ID,
            "same_independence_family_as_ifs": True,
            "counts_as_distinct_independence_family": False,
            "provider_authority_content_sha256": aifs_provider_authority[
                "content_sha256"
            ],
            "strict_oos_candidate_content_sha256": aifs_candidate[
                "content_sha256"
            ],
        },
        "independent_role": {
            "provider_id": other["provider_id"],
            "product_id": other.get("product_id"),
            "model_id": other["model_id"],
            "forecast_role": "NATIVE_ENSEMBLE",
            "independence_family_id": other["independence_family_id"],
            "formal_p0_authority_sha256": other["formal_p0_authority_sha256"],
            "formal_oos_artifact_sha256": other["formal_oos_artifact_sha256"],
            "native_member_evidence_sha256": other["native_member_evidence_sha256"],
            "provider_authority_content_sha256": other["content_sha256"],
        },
    }
    selection_seed = {
        "scope": scope,
        "selected_at_utc": _stamp(selected_at),
        "role_identity": role_identity,
        "independent_family_set_sha256": family_set_sha,
    }
    identity = {
        "schema": SELECTION_SCHEMA,
        "selection_id": "aifs-det-p0-role-" + content_sha256(selection_seed)[:32],
        "state": "P0_ROLE_SELECTION_ADAPTER_READY_NOT_COMPILED",
        **scope,
        "selected_at_utc": _stamp(selected_at),
        "role_identity": role_identity,
        "independence_family_ids": sorted(family_ids),
        "independent_family_count": 2,
        "independent_family_set_sha256": family_set_sha,
        # The selection has passed its two-family role gate; only a future
        # deterministic-aware compiler may turn it into probabilities/P0.
        "formal_p0_role_eligible": True,
        "formal_p0_probability_generated": False,
        "p0_compiler_activation_created": False,
        "requires_deterministic_aware_p0_compiler": True,
        "boundary": {
            "writes_production_database": False,
            "writes_native_ensemble_tables": False,
            "writes_probability_materialization": False,
            "creates_p0_compiler_activation": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "INDEPENDENT_FAMILY_P0_AUTHORITY_SCHEMA",
    "SELECTION_SCHEMA",
    "build_aifs_deterministic_p0_role_selection",
    "validate_independent_family_p0_authority",
]
