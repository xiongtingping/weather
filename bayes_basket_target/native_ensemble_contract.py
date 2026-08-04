"""Contracts for the calibrated cross-native-ensemble P0 authority.

Member multiplicity expresses uncertainty inside one forecast system.  It
never increases the number of independent systems admitted by the P0 gate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ContractError


MIN_CROSS_ENSEMBLE_FAMILIES = 2
P0_PROBABILITY_PATHWAY = "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA = (
    "bayes_basket_taf_native_ensemble_live_compatibility_fingerprint_v1"
)
# The native-system registry describes the upstream model, not an arbitrary
# transport that happens to expose a similarly named product.  Keep this
# mapping deliberately small: adding an official provider is a SSOT change,
# never an implicit side effect of registering a model name.
OFFICIAL_NATIVE_TRANSPORT_BY_PROVIDER = {
    "ECMWF": "ECMWF_OPEN_DATA_S3",
    "NOAA": "NOAA_GEFS_PDS_S3",
    "ECCC": "ECCC_DD_WEATHER",
    "DWD": "DWD",
}


def native_ensemble_family_set_hash(family_ids: Sequence[str]) -> str:
    normalized = tuple(str(item).strip() for item in family_ids)
    canonical = tuple(sorted(set(normalized)))
    if not canonical or any(not item for item in canonical):
        raise ContractError("native_ensemble_family_id_missing")
    return hashlib.sha256("|".join(canonical).encode("utf-8")).hexdigest()


def _selected_native_systems(
    *,
    selected_system_ids: Sequence[str],
    systems_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, ...], list[Mapping[str, Any]]]:
    selected = tuple(sorted(str(item).strip() for item in selected_system_ids))
    if any(not item for item in selected) or len(set(selected)) != len(selected):
        raise ContractError("native_ensemble_system_set_invalid")

    systems: list[Mapping[str, Any]] = []
    for system_id in selected:
        system = systems_by_id.get(system_id)
        if system is None:
            raise ContractError("native_ensemble_system_unregistered")
        if str(system.get("source_class")) != "NATIVE_ENSEMBLE_PROVIDER":
            raise ContractError("native_ensemble_source_class_invalid")
        if bool(system.get("aggregator_or_republisher")):
            raise ContractError("native_ensemble_republisher_forbidden")
        provider_id = str(system.get("provider_id") or "").strip()
        if provider_id not in OFFICIAL_NATIVE_TRANSPORT_BY_PROVIDER:
            raise ContractError("native_ensemble_system_provider_not_official")
        systems.append(system)
    return selected, systems


def validate_native_ensemble_transport_gate(
    *,
    selected_system_ids: Sequence[str],
    systems_by_id: Mapping[str, Mapping[str, Any]],
    transport_provider_by_system: Mapping[str, Any],
) -> dict[str, str]:
    """Bind each native system to its sole official transport.

    This deliberately validates the transport separately from member/OOS
    completeness so official forward P0 compilation can reuse the same
    lineage rule without inventing member publication fields from an archive
    receipt.  Republisher packets never call this native-only gate.
    """

    selected, systems = _selected_native_systems(
        selected_system_ids=selected_system_ids,
        systems_by_id=systems_by_id,
    )
    resolved: dict[str, str] = {}
    for system_id, system in zip(selected, systems):
        provider_id = str(system["provider_id"]).strip()
        expected_transport = OFFICIAL_NATIVE_TRANSPORT_BY_PROVIDER[
            provider_id
        ]
        observed_transport = str(
            transport_provider_by_system.get(system_id) or ""
        ).strip()
        if observed_transport != expected_transport:
            raise ContractError("native_ensemble_transport_not_official")
        resolved[system_id] = observed_transport
    return dict(sorted(resolved.items()))


def validate_cross_native_ensemble_gate(
    *,
    selected_system_ids: Sequence[str],
    systems_by_id: Mapping[str, Mapping[str, Any]],
    member_runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate that a cross-model gate counts systems/families, not members."""

    selected, systems = _selected_native_systems(
        selected_system_ids=selected_system_ids,
        systems_by_id=systems_by_id,
    )

    family_ids = tuple(
        str(system.get("independence_family_id", "")).strip()
        for system in systems
    )
    if any(not item for item in family_ids):
        raise ContractError("native_ensemble_family_id_missing")
    independent_families = set(family_ids)
    if len(independent_families) < MIN_CROSS_ENSEMBLE_FAMILIES:
        raise ContractError("cross_native_ensemble_requires_two_families")

    member_ids_by_system: dict[str, set[str]] = {
        system_id: set() for system_id in selected
    }
    transport_provider_by_system: dict[str, str] = {}
    for member in member_runs:
        system_id = str(member.get("ensemble_system_id", "")).strip()
        if system_id not in member_ids_by_system:
            raise ContractError("native_ensemble_member_outside_selected_systems")
        system = systems_by_id[system_id]
        if (
            str(member.get("provider_id") or "").strip()
            != str(system.get("provider_id") or "").strip()
        ):
            raise ContractError("native_ensemble_member_provider_mismatch")
        member_transport = str(
            member.get("transport_provider_id") or ""
        ).strip()
        existing_transport = transport_provider_by_system.get(system_id)
        if (
            existing_transport is not None
            and existing_transport != member_transport
        ):
            raise ContractError("native_ensemble_transport_not_official")
        transport_provider_by_system[system_id] = member_transport

    official_transport_by_system = validate_native_ensemble_transport_gate(
        selected_system_ids=selected,
        systems_by_id=systems_by_id,
        transport_provider_by_system=transport_provider_by_system,
    )

    for member in member_runs:
        system_id = str(member.get("ensemble_system_id", "")).strip()
        if (
            str(member.get("authority_scope") or "")
            != "FORMAL_NATIVE_ELIGIBLE"
            or member.get("native_timestamps_complete") not in (1, True)
            or not str(member.get("run_init_utc") or "").strip()
            or not str(member.get("published_at_utc") or "").strip()
        ):
            raise ContractError(
                "native_ensemble_member_not_formal_native"
            )
        member_id = str(member.get("member_id", "")).strip()
        if not member_id:
            raise ContractError("native_ensemble_member_id_missing")
        if member_id in member_ids_by_system[system_id]:
            raise ContractError("native_ensemble_member_duplicate")
        member_ids_by_system[system_id].add(member_id)

    for system_id, member_ids in member_ids_by_system.items():
        expected = int(systems_by_id[system_id].get("expected_member_count", 0))
        if expected < 2:
            raise ContractError("native_ensemble_expected_member_count_invalid")
        if len(member_ids) != expected:
            raise ContractError("native_ensemble_member_set_incomplete")

    return {
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "ensemble_system_ids": list(selected),
        "ensemble_system_count": len(selected),
        "independence_family_ids": sorted(independent_families),
        "independent_family_count": len(independent_families),
        "available_ensemble_family_set_hash": native_ensemble_family_set_hash(
            family_ids
        ),
        "member_count_by_system": {
            system_id: len(member_ids_by_system[system_id])
            for system_id in selected
        },
        "official_transport_provider_by_system": (
            official_transport_by_system
        ),
    }


def validate_cross_native_ensemble_weights(
    *,
    system_weights: Mapping[str, float],
    systems_by_id: Mapping[str, Mapping[str, Any]],
    family_weight_caps: Mapping[str, float],
) -> dict[str, Any]:
    if not system_weights:
        raise ContractError("cross_native_ensemble_weights_missing")
    weights: dict[str, float] = {}
    family_weights: dict[str, float] = {}
    for system_id, raw_weight in system_weights.items():
        system = systems_by_id.get(str(system_id))
        if system is None:
            raise ContractError("native_ensemble_system_unregistered")
        weight = float(raw_weight)
        if not 0.0 < weight <= 1.0:
            raise ContractError("cross_native_ensemble_weight_invalid")
        family_id = str(
            system.get("independence_family_id", "")
        ).strip()
        if not family_id:
            raise ContractError("native_ensemble_family_id_missing")
        weights[str(system_id)] = weight
        family_weights[family_id] = (
            family_weights.get(family_id, 0.0) + weight
        )
    if abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ContractError("cross_native_ensemble_weights_not_normalized")
    if len(family_weights) < MIN_CROSS_ENSEMBLE_FAMILIES:
        raise ContractError("cross_native_ensemble_requires_two_families")
    caps: dict[str, float] = {}
    for family_id in family_weights:
        if family_id not in family_weight_caps:
            raise ContractError(
                "cross_native_ensemble_family_cap_missing"
            )
        cap = float(family_weight_caps[family_id])
        if not 0.0 < cap <= 1.0:
            raise ContractError(
                "cross_native_ensemble_family_cap_invalid"
            )
        caps[family_id] = cap
        if family_weights[family_id] > cap + 1e-12:
            raise ContractError(
                "cross_native_ensemble_family_cap_exceeded"
            )
    return {
        "system_weights": dict(sorted(weights.items())),
        "family_weights": dict(sorted(family_weights.items())),
        "family_weight_caps": dict(sorted(caps.items())),
    }
