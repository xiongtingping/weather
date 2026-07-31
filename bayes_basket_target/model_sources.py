"""Canonical five-model roles and explicitly allowed transport variants.

The candidate pool contains ECMWF, GFS, ICON, GEM and CMA.  A model role may
have more than one evidence transport, but transport provenance is never
promoted into native publication or arrival evidence.  Native provider
receipts remain preferred whenever they overlap an aggregator republisher.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .contracts import ContractError


CANONICAL_MODEL_PROVIDERS = (
    "ecmwf_ifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
    "gem_global_open_meteo",
    "cma_grapes_global_open_meteo",
)

# Order is meaningful: native DWD ICON wins when a complete native curve and
# an aggregator curve overlap the same station/day/run.  The aggregator is
# only a completion path, never a silent replacement of native evidence.
MODEL_PROVIDER_VARIANTS: Mapping[str, tuple[str, ...]] = {
    "ecmwf_ifs025_official": ("ecmwf_ifs025_official",),
    "gfs_0p25_official": ("gfs_0p25_official",),
    "icon_global_official": (
        "icon_global_official",
        "icon_global_open_meteo",
    ),
    "gem_global_open_meteo": (
        "gem_gdps_official",
        "gem_global_open_meteo",
    ),
    "cma_grapes_global_open_meteo": ("cma_grapes_global_open_meteo",),
}

HISTORICAL_REPLAY_ONLY_PROVIDERS: Mapping[str, str] = {
    "ecmwf_ifs_open_meteo_previous_runs": "ecmwf_ifs025_official",
    "gfs_global_open_meteo_previous_runs": "gfs_0p25_official",
}

MODEL_ROLE_BY_PROVIDER = {
    provider: role
    for role, providers in MODEL_PROVIDER_VARIANTS.items()
    for provider in providers
}
MODEL_ROLE_BY_PROVIDER.update(HISTORICAL_REPLAY_ONLY_PROVIDERS)
MODEL_ROLE_BY_PROVIDER["gem_gdps_official"] = "gem_global_open_meteo"
REGIONAL_MODEL_ID_BY_PROVIDER = {
    "ncep_hrrr_conus_open_meteo": "HRRR",
    "ncep_nbm_conus_open_meteo": "NBM",
    "ncep_nam_conus_open_meteo": "NAM",
    "icon_d2_open_meteo": "ICON-D2",
    "meteofrance_arome_france0025_open_meteo": "AROME-FR",
    "ukmo_uk_deterministic_2km_open_meteo": "UKMO-2KM",
    "knmi_harmonie_arome_netherlands_open_meteo": "HARMONIE-NL",
    "knmi_harmonie_arome_europe_open_meteo": "HARMONIE-EU",
    "jma_msm_open_meteo": "JMA-MSM",
}
REGIONAL_MODEL_ROLE_BY_PROVIDER = {
    provider: f"regional_{model_id.lower().replace('-', '_')}"
    for provider, model_id in REGIONAL_MODEL_ID_BY_PROVIDER.items()
}
MODEL_ROLE_BY_PROVIDER.update(REGIONAL_MODEL_ROLE_BY_PROVIDER)
MODEL_ROLES = frozenset(CANONICAL_MODEL_PROVIDERS)
ALLOWED_MODEL_PROVIDERS = frozenset(MODEL_ROLE_BY_PROVIDER)


def provider_role(provider_id: Any) -> str:
    provider = str(provider_id or "").strip().lower()
    try:
        return MODEL_ROLE_BY_PROVIDER[provider]
    except KeyError as exc:
        raise ContractError("model_provider_not_allowed") from exc


def model_id_for_provider(provider_id: Any) -> str:
    provider = str(provider_id or "").strip().lower()
    if provider in REGIONAL_MODEL_ID_BY_PROVIDER:
        return REGIONAL_MODEL_ID_BY_PROVIDER[provider]
    role = provider_role(provider_id)
    return {
        "ecmwf_ifs025_official": "ECMWF",
        "gfs_0p25_official": "GFS",
        "icon_global_official": "ICON",
        "gem_global_open_meteo": "GEM",
        "cma_grapes_global_open_meteo": "CMA",
    }[role]


def provider_roles(providers: Iterable[Any]) -> set[str]:
    roles: set[str] = set()
    for provider in providers:
        roles.add(provider_role(provider))
    return roles


def has_one_provider_per_model_role(providers: Iterable[Any]) -> bool:
    values = [str(provider or "").strip().lower() for provider in providers]
    if len(values) != len(set(values)):
        return False
    if any(value in HISTORICAL_REPLAY_ONLY_PROVIDERS for value in values):
        return False
    try:
        roles = provider_roles(values)
    except ContractError:
        return False
    return roles == MODEL_ROLES and len(values) == len(MODEL_ROLES)


def missing_model_roles(providers: Iterable[Any]) -> list[str]:
    try:
        present = provider_roles(providers)
    except ContractError:
        present = set()
    return sorted(MODEL_ROLES - present)


def preferred_provider_for_role(providers: Iterable[Any], role: str) -> str:
    role_key = str(role or "").strip().lower()
    allowed = set(MODEL_PROVIDER_VARIANTS.get(role_key, ()))
    candidates = sorted(
        {
            str(provider or "").strip().lower()
            for provider in providers
            if str(provider or "").strip().lower() in allowed
        },
        key=lambda value: MODEL_PROVIDER_VARIANTS[role_key].index(value),
    )
    if not candidates:
        raise ContractError("model_role_provider_missing")
    return candidates[0]


def provider_source_class(provider_id: Any) -> str:
    provider = str(provider_id or "").strip().lower()
    if provider in {
        "icon_global_open_meteo",
        "gem_global_open_meteo",
        "cma_grapes_global_open_meteo",
    } or provider in REGIONAL_MODEL_ID_BY_PROVIDER or provider in HISTORICAL_REPLAY_ONLY_PROVIDERS:
        return "aggregator_republisher"
    if provider in ALLOWED_MODEL_PROVIDERS:
        return "native_or_official_cloud_replica"
    raise ContractError("model_provider_not_allowed")


__all__ = [
    "ALLOWED_MODEL_PROVIDERS",
    "CANONICAL_MODEL_PROVIDERS",
    "HISTORICAL_REPLAY_ONLY_PROVIDERS",
    "MODEL_PROVIDER_VARIANTS",
    "MODEL_ROLE_BY_PROVIDER",
    "MODEL_ROLES",
    "REGIONAL_MODEL_ID_BY_PROVIDER",
    "has_one_provider_per_model_role",
    "missing_model_roles",
    "model_id_for_provider",
    "preferred_provider_for_role",
    "provider_role",
    "provider_roles",
    "provider_source_class",
]
