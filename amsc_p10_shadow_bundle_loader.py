"""Load immutable AMSC input bundles for the resident P10 shadow lane.

The resident P10 host deliberately has no network or file-system acquisition
responsibility.  This module is the narrow boundary immediately before that
host: it turns an already persisted AMSC bundle (or the existing fact-plan
envelope that contains one) into an immutable, manifest-verified input.

It never creates an observation, rewrites ``first_seen_at``, writes
``archive.db``, or calls a queue/signer/CLOB.  In particular, a historical
fact plan remains useful only for replay.  It must not be mistaken for a new
live arrival merely because it is read by this process now.
"""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from amsc_p10_shadow_source_adapter import AMSC_BUNDLE_SCHEMA
from amsc_raw_history_archive import RAW_OBJECT_SCHEMA, raw_text_sha256, validate_amsc_raw_poll_manifest
from amsc_specli_resident_arrival_contract import ACCEPTED_REPORT_TYPES
from source_observation_runtime import PRIMARY_METAR_SOURCE_ID


SCHEMA_VERSION = "amsc_p10_shadow_bundle_loader_v1"
FACT_PLAN_SCHEMA = "weather_fact_snapshot_writer_plan_v1"
FORWARD_NEW_ONLY = "forward_new_primary_only"
REPLAY_ALL = "replay_all_archived_primary"
SUPPORTED_LOAD_MODES = frozenset({FORWARD_NEW_ONLY, REPLAY_ALL})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_relative_file(root: Path, reference: Any, *, suffix: str) -> Path | None:
    raw = _text(reference)
    if not raw or Path(raw).is_absolute() or not raw.endswith(suffix):
        return None
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _load_manifest(root: Path, reference: Any) -> dict[str, Any]:
    path = _safe_relative_file(root, reference, suffix=".json")
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def _load_raw_object(root: Path, reference: Any) -> dict[str, Any]:
    path = _safe_relative_file(root, reference, suffix=".json.gz")
    if path is None or not path.is_file():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def _bundle_payload(input_payload: Mapping[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    """Return the embedded bundle without repairing the caller's data."""

    value = _mapping(input_payload)
    if _text(value.get("schema")) == AMSC_BUNDLE_SCHEMA:
        return value, "amsc_observation_bundle", []
    effective = _mapping(value.get("effective_payload"))
    if _text(value.get("schema")) != FACT_PLAN_SCHEMA:
        return {}, "", ["amsc_p10_loader_input_schema_invalid"]
    if _text(effective.get("amsc_observation_bundle_schema")) != AMSC_BUNDLE_SCHEMA:
        return {}, "", ["amsc_p10_loader_fact_plan_bundle_schema_invalid"]
    return {
        "schema": effective.get("amsc_observation_bundle_schema"),
        "read_only": True,
        "station_id": effective.get("station_id"),
        "fetched_at": effective.get("fact_as_of"),
        "source_observations": effective.get("source_observations"),
        "source_observation_ids": effective.get("source_observation_ids"),
        "raw_history_archive": effective.get("amsc_raw_history_archive"),
    }, "amsc_fact_snapshot_write_plan", []


def _manifest_object_index(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for raw in manifest.get("raw_objects") or []:
        entry = _mapping(raw)
        raw_sha = _text(entry.get("raw_sha256"))
        if raw_sha and raw_sha not in entries:
            entries[raw_sha] = entry
    return entries


def _source_is_primary(source: Mapping[str, Any]) -> bool:
    payload = _mapping(source.get("payload"))
    return (
        _text(source.get("source_id")) == PRIMARY_METAR_SOURCE_ID
        and _text(source.get("station_id")).upper() == "ZUUU"
        and _text(payload.get("report_type") or source.get("report_type")).upper() in ACCEPTED_REPORT_TYPES
    )


def _source_archive_issues(
    *,
    source: Mapping[str, Any],
    manifest_object: Mapping[str, Any],
    raw_object: Mapping[str, Any],
) -> list[str]:
    """Prove the selected primary row is bound to the immutable archive."""

    payload = _mapping(source.get("payload"))
    transport = _mapping(payload.get("transport"))
    issues: list[str] = []
    raw_sha = _text(source.get("raw_sha256"))
    raw_ref = _text(payload.get("raw_object_ref"))
    first_seen = _text(payload.get("first_seen_at"))
    if not raw_sha:
        issues.append("amsc_p10_loader_source_raw_sha_missing")
    if not raw_ref:
        issues.append("amsc_p10_loader_source_raw_object_ref_missing")
    if _text(payload.get("arrival_time_quality")) != "observed_first_seen":
        issues.append("amsc_p10_loader_source_arrival_quality_invalid")
    if not first_seen:
        issues.append("amsc_p10_loader_source_first_seen_missing")
    if _text(transport.get("raw_object_ref")) != raw_ref:
        issues.append("amsc_p10_loader_source_transport_object_ref_mismatch")
    if _text(manifest_object.get("raw_sha256")) != raw_sha:
        issues.append("amsc_p10_loader_manifest_raw_sha_mismatch")
    if _text(manifest_object.get("raw_object_ref")) != raw_ref:
        issues.append("amsc_p10_loader_manifest_object_ref_mismatch")
    if _text(manifest_object.get("first_seen_at")) != first_seen:
        issues.append("amsc_p10_loader_manifest_first_seen_mismatch")
    if _text(manifest_object.get("arrival_time_quality")) != "observed_first_seen":
        issues.append("amsc_p10_loader_manifest_arrival_quality_invalid")
    if _text(raw_object.get("schema")) != RAW_OBJECT_SCHEMA:
        issues.append("amsc_p10_loader_raw_object_schema_invalid")
    if _text(raw_object.get("raw_sha256")) != raw_sha:
        issues.append("amsc_p10_loader_raw_object_sha_mismatch")
    if raw_text_sha256(raw_object.get("raw_text")) != raw_sha:
        issues.append("amsc_p10_loader_raw_object_content_hash_mismatch")
    if _text(raw_object.get("raw_text")) != _text(source.get("raw_ref")):
        issues.append("amsc_p10_loader_raw_object_text_mismatch")
    if _text(raw_object.get("raw_text")) != _text(payload.get("raw_text")):
        issues.append("amsc_p10_loader_payload_raw_text_mismatch")
    if _text(raw_object.get("first_seen_at")) != first_seen:
        issues.append("amsc_p10_loader_raw_object_first_seen_mismatch")
    if _text(raw_object.get("arrival_time_quality")) != "observed_first_seen":
        issues.append("amsc_p10_loader_raw_object_arrival_quality_invalid")
    if _text(raw_object.get("source_id")) != PRIMARY_METAR_SOURCE_ID:
        issues.append("amsc_p10_loader_raw_object_source_invalid")
    if _text(raw_object.get("station_id")).upper() != "ZUUU":
        issues.append("amsc_p10_loader_raw_object_station_invalid")
    report_type = _text(payload.get("report_type") or source.get("report_type")).upper()
    if _text(raw_object.get("report_type")).upper() != report_type:
        issues.append("amsc_p10_loader_raw_object_report_type_mismatch")
    if _text(raw_object.get("verified_valid_time_utc")) != _text(source.get("valid_time")):
        issues.append("amsc_p10_loader_raw_object_valid_time_mismatch")
    return sorted(set(issues))


def load_amsc_p10_shadow_bundle(
    input_payload: Mapping[str, Any] | None,
    *,
    raw_archive_dir: str | Path,
    mode: str = FORWARD_NEW_ONLY,
) -> dict[str, Any]:
    """Load a manifest-bound P10 bundle from an immutable persisted payload.

    ``forward_new_primary_only`` accepts only primary reports whose immutable
    raw object was first created by this exact manifest.  It is the only mode
    a future forward inbox sidecar may use.  ``replay_all_archived_primary``
    is intentionally named for replay and may include older rows; it never
    transforms their historic T1 into a fresh arrival.
    """

    selected_mode = _text(mode)
    root = Path(raw_archive_dir).expanduser().resolve()
    bundle, input_kind, blockers = _bundle_payload(_mapping(input_payload))
    if selected_mode not in SUPPORTED_LOAD_MODES:
        blockers.append("amsc_p10_loader_mode_invalid")
    if _text(bundle.get("schema")) != AMSC_BUNDLE_SCHEMA:
        blockers.append("amsc_p10_loader_bundle_schema_invalid")
    if bundle.get("read_only") is not True:
        blockers.append("amsc_p10_loader_bundle_read_only_required")
    if _text(bundle.get("station_id")).upper() != "ZUUU":
        blockers.append("amsc_p10_loader_station_invalid")
    source_rows = bundle.get("source_observations")
    if not isinstance(source_rows, list):
        blockers.append("amsc_p10_loader_source_observations_invalid")
    archive = _mapping(bundle.get("raw_history_archive"))
    manifest_ref = _text(archive.get("manifest_ref"))
    manifest_sha = _text(archive.get("manifest_sha256"))
    manifest = _load_manifest(root, manifest_ref)
    manifest_validation = validate_amsc_raw_poll_manifest(manifest) if manifest else {"ok": False, "issues": ["manifest_missing"]}
    if not manifest:
        blockers.append("amsc_p10_loader_manifest_missing_or_invalid")
    elif manifest_validation.get("ok") is not True:
        blockers.extend(f"amsc_p10_loader_manifest_contract:{item}" for item in manifest_validation.get("issues") or ["invalid"])
    if manifest and _text(manifest.get("manifest_sha256")) != manifest_sha:
        blockers.append("amsc_p10_loader_manifest_sha_binding_mismatch")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "blockers": sorted(set(blockers)),
            "input_kind": input_kind,
            "mode": selected_mode,
            "read_only": True,
            "shadow_only": True,
            "places_orders": False,
        }

    manifest_index = _manifest_object_index(manifest)
    manifest_completed_at = _text(manifest.get("response_completed_at"))
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    ignored_non_primary: list[str] = []
    excluded_preexisting: list[str] = []
    for raw in source_rows:
        source = _mapping(raw)
        source_id = _text(source.get("source_observation_id"))
        if not _source_is_primary(source):
            ignored_non_primary.append(source_id)
            continue
        payload = _mapping(source.get("payload"))
        raw_sha = _text(source.get("raw_sha256"))
        manifest_object = _mapping(manifest_index.get(raw_sha))
        raw_object = _load_raw_object(root, manifest_object.get("raw_object_ref")) if manifest_object else {}
        issues = _source_archive_issues(
            source=source,
            manifest_object=manifest_object,
            raw_object=raw_object,
        )
        if issues:
            rejected.append({"source_observation_id": source_id, "blockers": issues})
            continue
        if selected_mode == FORWARD_NEW_ONLY and _text(manifest_object.get("first_seen_at")) != manifest_completed_at:
            excluded_preexisting.append(source_id)
            continue
        selected.append(copy.deepcopy(source))

    rebuilt_archive = {
        "schema": archive.get("schema"),
        "manifest_ref": manifest_ref,
        "manifest_sha256": manifest_sha,
        "raw_objects_by_sha256": manifest_index,
    }
    output_bundle = {
        "schema": AMSC_BUNDLE_SCHEMA,
        "read_only": True,
        "station_id": "ZUUU",
        "fetched_at": bundle.get("fetched_at"),
        "source_observations": selected,
        "source_observation_ids": [
            _text(source.get("source_observation_id")) for source in selected if _text(source.get("source_observation_id"))
        ],
        "raw_history_archive": rebuilt_archive,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "ready_with_rejections" if rejected else "ready",
        "input_kind": input_kind,
        "mode": selected_mode,
        "bundle": output_bundle,
        "manifest": {
            "manifest_ref": manifest_ref,
            "manifest_sha256": manifest_sha,
            "response_completed_at": manifest_completed_at,
        },
        "selected_primary_count": len(selected),
        "rejected_primary_rows": rejected,
        "ignored_non_primary_source_observation_ids": [value for value in ignored_non_primary if value],
        "excluded_preexisting_primary_source_observation_ids": [value for value in excluded_preexisting if value],
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "network_acquisition": False,
            "production_archive_write": False,
            "generic_task_queue": False,
            "t1_synthesized_from_file_read": False,
            "forward_mode_only_uses_new_raw_objects_from_exact_manifest": True,
        },
    }


__all__ = [
    "FACT_PLAN_SCHEMA",
    "FORWARD_NEW_ONLY",
    "REPLAY_ALL",
    "SCHEMA_VERSION",
    "SUPPORTED_LOAD_MODES",
    "load_amsc_p10_shadow_bundle",
]
