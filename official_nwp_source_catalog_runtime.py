"""Validation boundary for the four independent official NWP weak sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


CATALOG_SCHEMA = "official_nwp_source_catalog_v1"
CATALOG_ID = "official_nwp_zuuu_v1"
REQUIRED_PROVIDERS = ("ecmwf", "icon", "gfs", "jma")
NORMALIZED_SNAPSHOT_SCHEMA = "official_nwp_normalized_snapshot_v1"
RAW_ENVELOPE_SCHEMA = "official_nwp_raw_envelope_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def load_official_nwp_catalog(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(parsed)


def validate_official_nwp_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(catalog)
    issues: list[str] = []
    if value.get("schema") != CATALOG_SCHEMA:
        issues.append("official_nwp_catalog_schema_invalid")
    if _text(value.get("catalog_id")) != CATALOG_ID:
        issues.append("official_nwp_catalog_id_invalid")
    providers = _mapping(value.get("providers"))
    for provider_id in REQUIRED_PROVIDERS:
        provider = _mapping(providers.get(provider_id))
        if not provider:
            issues.append(f"official_nwp_catalog_{provider_id}_missing")
            continue
        if not _text(provider.get("source_id")) or not _text(provider.get("model_id")):
            issues.append(f"official_nwp_catalog_{provider_id}_identity_missing")
        if not _text(provider.get("collector_adapter")):
            issues.append(f"official_nwp_catalog_{provider_id}_collector_adapter_missing")
        hosts = [str(host).strip().lower() for host in provider.get("allowed_hosts") or [] if str(host).strip()]
        if not hosts:
            issues.append(f"official_nwp_catalog_{provider_id}_allowed_hosts_missing")
        fields = [str(field).strip() for field in provider.get("required_normalized_fields") or [] if str(field).strip()]
        if not fields:
            issues.append(f"official_nwp_catalog_{provider_id}_normalized_fields_missing")
    policy = _mapping(value.get("joint_snapshot_policy"))
    if tuple(policy.get("provider_universe") or []) != REQUIRED_PROVIDERS:
        issues.append("official_nwp_catalog_provider_universe_invalid")
    if policy.get("missing_provider_policy") != "propagate_availability_mask_to_unique_weak_likelihood_model":
        issues.append("official_nwp_catalog_missing_provider_policy_invalid")
    if policy.get("empty_nwp_set_policy") != "emit_explicit_empty_nwp_generation_for_taf_wu_twc_or_prior_strong_combination":
        issues.append("official_nwp_catalog_empty_nwp_set_policy_invalid")
    if policy.get("source_combination_calibration_is_model_artifact_parameter") is not True:
        issues.append("official_nwp_catalog_combination_calibration_policy_invalid")
    boundaries = _mapping(value.get("boundaries"))
    if boundaries.get("open_meteo_retired") is not True:
        issues.append("official_nwp_catalog_open_meteo_retirement_missing")
    if boundaries.get("no_aggregator_or_republisher_source") is not True:
        issues.append("official_nwp_catalog_aggregator_boundary_missing")
    return {
        "schema": "official_nwp_source_catalog_validation_v1",
        "ok": not issues,
        "issues": sorted(set(issues)),
        "catalog": value if not issues else {},
        "boundary": {"read_only": True, "network_acquisition": False, "writes_posterior": False, "places_orders": False},
    }


def validate_official_nwp_snapshot(
    snapshot: Mapping[str, Any],
    *,
    provider_id: str,
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate decoded provider output against its immutable raw receipt."""

    source = _mapping(snapshot)
    provider = _mapping(_mapping(catalog).get("providers")).get(provider_id)
    provider = _mapping(provider)
    issues: list[str] = []
    if source.get("schema") != NORMALIZED_SNAPSHOT_SCHEMA:
        issues.append("official_nwp_snapshot_schema_invalid")
    if _text(source.get("provider_id")) != provider_id:
        issues.append("official_nwp_snapshot_provider_identity_invalid")
    for key in ("source_id", "model_id", "run_at_utc", "valid_time_utc", "response_completed_at", "raw_object_ref", "raw_sha256", "decoder_id", "decoder_version"):
        if not _text(source.get(key)):
            issues.append(f"official_nwp_snapshot_{key}_missing")
    if _text(source.get("source_id")) != _text(provider.get("source_id")):
        issues.append("official_nwp_snapshot_source_id_mismatch")
    if _text(source.get("model_id")) != _text(provider.get("model_id")):
        issues.append("official_nwp_snapshot_model_id_mismatch")
    raw_urls = source.get("raw_request_urls")
    raw_url_list = [str(item).strip() for item in raw_urls] if isinstance(raw_urls, list) else [_text(source.get("raw_request_url"))]
    allowed_hosts = {str(item).strip().lower() for item in provider.get("allowed_hosts") or []}
    if not raw_url_list or any(not url or (urlsplit(url).hostname or "").lower() not in allowed_hosts for url in raw_url_list):
        issues.append("official_nwp_snapshot_raw_request_host_invalid")
    fields = _mapping(source.get("normalized_features"))
    for field in provider.get("required_normalized_fields") or []:
        if field not in fields:
            issues.append(f"official_nwp_snapshot_required_field_missing:{field}")
    if source.get("aggregator_or_republisher") is not False:
        issues.append("official_nwp_snapshot_aggregator_not_forbidden")
    if source.get("open_meteo") is not False:
        issues.append("official_nwp_snapshot_open_meteo_retirement_violation")
    return {
        "schema": "official_nwp_normalized_snapshot_validation_v1",
        "ok": not issues,
        "issues": sorted(set(issues)),
        "snapshot": source if not issues else {},
        "boundary": {"read_only": True, "network_acquisition": False, "writes_posterior": False, "places_orders": False},
    }


__all__ = [
    "CATALOG_ID",
    "CATALOG_SCHEMA",
    "NORMALIZED_SNAPSHOT_SCHEMA",
    "RAW_ENVELOPE_SCHEMA",
    "REQUIRED_PROVIDERS",
    "load_official_nwp_catalog",
    "validate_official_nwp_catalog",
    "validate_official_nwp_snapshot",
]
