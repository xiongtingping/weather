"""Assemble a causal official-NWP availability generation.

Provider collectors own network acquisition and GRIB decoding.  This prewarm
boundary has deliberately no network capability: it verifies every available
independently captured official snapshot, records the missing-source mask, and
atomically publishes one frozen generation for the unique weak-likelihood
model.  Missing NWP sources are model inputs, not posterior-stop conditions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from official_nwp_source_catalog_runtime import (
    CATALOG_ID,
    REQUIRED_PROVIDERS,
    load_official_nwp_catalog,
    validate_official_nwp_catalog,
    validate_official_nwp_snapshot,
)


BUNDLE_SCHEMA = "official_nwp_multimodel_live_bundle_v1"
GENERATION_SCHEMA = "official_nwp_multimodel_generation_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _bundle_material(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(bundle)
    value.pop("generation", None)
    return value


def bundle_sha256(bundle: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(_bundle_material(bundle))).hexdigest()


def generation_reference(bundle: Mapping[str, Any]) -> dict[str, Any]:
    generation = _mapping(bundle.get("generation"))
    return {
        "schema": GENERATION_SCHEMA,
        "generation_id": _text(generation.get("generation_id")),
        "bundle_sha256": _text(generation.get("bundle_sha256")),
        "generation_path": _text(generation.get("generation_path")),
        "catalog_id": _text(generation.get("catalog_id")),
        "provider_set": list(generation.get("provider_set") or []),
        "response_completed_at": _text(generation.get("response_completed_at")),
    }


def _is_ordered_provider_subset(value: Any) -> bool:
    providers = tuple(value or [])
    return providers == tuple(item for item in REQUIRED_PROVIDERS if item in providers)


def validate_generation_reference(reference: Mapping[str, Any] | None, *, catalog_path: Path) -> dict[str, Any]:
    """Validate a frozen availability generation without any network access."""

    ref = _mapping(reference)
    issues: list[str] = []
    required = ("generation_id", "bundle_sha256", "generation_path", "catalog_id", "response_completed_at")
    if ref.get("schema") != GENERATION_SCHEMA or any(not _text(ref.get(key)) for key in required):
        issues.append("official_nwp_generation_reference_invalid")
    if _text(ref.get("catalog_id")) != CATALOG_ID:
        issues.append("official_nwp_generation_catalog_id_invalid")
    if not _is_ordered_provider_subset(ref.get("provider_set")):
        issues.append("official_nwp_generation_provider_set_invalid")
    path = Path(_text(ref.get("generation_path"))).expanduser().resolve() if _text(ref.get("generation_path")) else None
    bundle = _load_json(path) if path is not None else {}
    if not bundle:
        issues.append("official_nwp_generation_bundle_unreadable")
    if bundle:
        generation = _mapping(bundle.get("generation"))
        if bundle.get("schema") != BUNDLE_SCHEMA or bundle.get("state") != "available":
            issues.append("official_nwp_generation_bundle_schema_or_state_invalid")
        available_provider_ids = list(bundle.get("available_provider_ids") or [])
        missing_provider_ids = list(bundle.get("missing_provider_ids") or [])
        expected_missing = [provider for provider in REQUIRED_PROVIDERS if provider not in available_provider_ids]
        if (
            bundle.get("catalog_id") != CATALOG_ID
            or tuple(bundle.get("provider_universe") or []) != REQUIRED_PROVIDERS
            or not _is_ordered_provider_subset(available_provider_ids)
            or tuple(missing_provider_ids) != tuple(expected_missing)
            or tuple(ref.get("provider_set") or []) != tuple(available_provider_ids)
            or set(_mapping(bundle.get("providers"))) != set(available_provider_ids)
        ):
            issues.append("official_nwp_generation_bundle_catalog_or_provider_set_invalid")
        if bundle_sha256(bundle) != _text(ref.get("bundle_sha256")):
            issues.append("official_nwp_generation_bundle_sha256_mismatch")
        for key in ("generation_id", "bundle_sha256", "catalog_id", "response_completed_at"):
            if _text(generation.get(key)) != _text(ref.get(key)):
                issues.append(f"official_nwp_generation_bundle_{key}_mismatch")
        catalog_result = validate_official_nwp_catalog(load_official_nwp_catalog(catalog_path))
        if catalog_result.get("ok") is not True:
            issues.append("official_nwp_generation_catalog_validation_failed")
        else:
            catalog = _mapping(catalog_result.get("catalog"))
            for provider in available_provider_ids:
                validation = validate_official_nwp_snapshot(
                    _mapping(_mapping(bundle.get("providers")).get(provider)),
                    provider_id=provider,
                    catalog=catalog,
                )
                if validation.get("ok") is not True:
                    issues.append(f"official_nwp_generation_{provider}_snapshot_invalid")
    return {
        "schema": "official_nwp_multimodel_generation_validation_v1",
        "ok": not issues,
        "issues": sorted(set(issues)),
        "reference": ref,
        "bundle": bundle if not issues else {},
        "generation_path": str(path) if path is not None else "",
        "boundary": {"read_only": True, "network_acquisition": False, "writes_posterior": False, "places_orders": False},
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(parsed)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_paths(source_root: Path) -> dict[str, Path]:
    return {provider: source_root / provider / "current.json" for provider in REQUIRED_PROVIDERS}


def build_official_nwp_prewarm_plan(
    *,
    catalog_path: Path,
    source_root: Path,
    cache_path: Path,
    as_of: str | datetime | None = None,
    max_snapshot_age_sec: int = 1200,
) -> dict[str, Any]:
    """Read available official snapshots and freeze their missingness mask."""

    now = _parse_utc(as_of) if not isinstance(as_of, datetime) else as_of.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    catalog_result = validate_official_nwp_catalog(load_official_nwp_catalog(catalog_path))
    blockers = list(catalog_result.get("issues") or [])
    catalog = _mapping(catalog_result.get("catalog"))
    snapshots: dict[str, dict[str, Any]] = {}
    provider_validation: dict[str, dict[str, Any]] = {}
    for provider, path in _snapshot_paths(source_root).items():
        snapshot = _load_json(path)
        validation = validate_official_nwp_snapshot(snapshot, provider_id=provider, catalog=catalog)
        provider_validation[provider] = {**validation, "path": str(path)}
        if validation.get("ok") is not True:
            continue
        completed = _parse_utc(snapshot.get("response_completed_at"))
        if completed is None or (now - completed).total_seconds() < 0 or (now - completed).total_seconds() > int(max_snapshot_age_sec):
            continue
        snapshots[provider] = snapshot
    available_provider_ids = [provider for provider in REQUIRED_PROVIDERS if provider in snapshots]
    missing_provider_ids = [provider for provider in REQUIRED_PROVIDERS if provider not in snapshots]
    response_times = [_parse_utc(snapshot.get("response_completed_at")) for snapshot in snapshots.values()]
    response_completed_at = max((item for item in response_times if item is not None), default=None)
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "state": "available" if not blockers else "blocked",
        "catalog_id": CATALOG_ID,
        "provider_universe": list(REQUIRED_PROVIDERS),
        "available_provider_ids": available_provider_ids,
        "missing_provider_ids": missing_provider_ids,
        "response_completed_at": _iso(response_completed_at) if response_completed_at is not None else _iso(now),
        "providers": snapshots,
        "weak_likelihood_availability": {
            "schema": "weak_likelihood_nwp_availability_v1",
            "available_source_ids": available_provider_ids,
            "missing_source_ids": missing_provider_ids,
            "nwp_information_state": "available_subset" if available_provider_ids else "no_nwp_available",
            "requires_combination_calibration": True,
            "manual_confidence_or_weight": None,
        },
        "source_root": str(source_root),
        "hot_path_network_acquisition": False,
        "open_meteo": False,
        "aggregator_or_republisher": False,
        "boundary": {
            "network_acquisition": False,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    return {
        "schema": "official_nwp_multimodel_prewarm_plan_v1",
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "catalog_validation": catalog_result,
        "provider_validation": provider_validation,
        "bundle": bundle if not blockers else {},
        "cache_path": str(cache_path),
        "decision_as_of": _iso(now),
        "max_snapshot_age_sec": int(max_snapshot_age_sec),
        "boundary": bundle["boundary"],
    }


def persist_official_nwp_prewarm(*, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one immutable joint bundle and atomically advance current.json."""

    value = _mapping(plan)
    if value.get("ok") is not True:
        return {
            "schema": "official_nwp_multimodel_prewarm_result_v1",
            "ok": False,
            "state": "blocked",
            "blockers": list(value.get("blockers") or ["official_nwp_prewarm_plan_not_ready"]),
            "boundary": value.get("boundary") or {},
        }
    cache_path = Path(_text(value.get("cache_path"))).resolve()
    bundle = _mapping(value.get("bundle"))
    digest = bundle_sha256(bundle)
    generation_id = f"officialnwp_{digest[:24]}"
    generation_path = cache_path.parent / "generations" / f"{generation_id}.json"
    bundle["generation"] = {
        "schema": GENERATION_SCHEMA,
        "generation_id": generation_id,
        "bundle_sha256": digest,
        "generation_path": str(generation_path.resolve()),
        "catalog_id": CATALOG_ID,
        "provider_set": list(bundle.get("available_provider_ids") or []),
        "response_completed_at": _text(bundle.get("response_completed_at")),
    }
    if generation_path.is_file():
        existing = generation_path.read_bytes()
        expected = (json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if existing != expected:
            return {
                "schema": "official_nwp_multimodel_prewarm_result_v1",
                "ok": False,
                "state": "blocked",
                "blockers": ["official_nwp_generation_identity_collision"],
                "boundary": value.get("boundary") or {},
            }
    else:
        _atomic_json(generation_path, bundle)
    _atomic_json(cache_path, bundle)
    return {
        "schema": "official_nwp_multimodel_prewarm_result_v1",
        "ok": True,
        "state": "ready",
        "cache_path": str(cache_path),
        "generation": generation_reference(bundle),
        "boundary": value.get("boundary") or {},
    }


__all__ = [
    "BUNDLE_SCHEMA",
    "GENERATION_SCHEMA",
    "build_official_nwp_prewarm_plan",
    "bundle_sha256",
    "generation_reference",
    "persist_official_nwp_prewarm",
    "validate_generation_reference",
]
