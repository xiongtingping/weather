"""Immutable native-arrival receipts for retained DWD ICON Global/EPS raws.

ICON Global/EPS publishes one 40-member native object per lead.  The decoder
already verifies the one-object/40-member/45-station cohort, but its earlier
daily archive intentionally kept that receipt out of the formal arrival index.
This module makes the missing link explicit: a bounded index records the
official URL, provider publication timestamp, local response completion, raw
SHA-256 and decoded cohort SHA for every observed object.  The index never
creates a model sample, P0 authority, database row or order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .contracts import ContractError, content_sha256


INDEX_SCHEMA = "dwd_icon_global_eps_immutable_native_arrival_index_v1"
REPAIR_SCHEMA = "dwd_icon_global_eps_native_arrival_index_repair_receipt_v1"
ARCHIVE_SCHEMA = "dwd_icon_global_eps_official_daily_members_v1"
COHORT_SCHEMA = "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
CAPTURE_SCHEMA = "bayes_basket_target_icon_global_eps_forward_capture_v1"
PROVIDER_ID = "icon_global_eps_official"
PRODUCT_ID = "DWD_ICON_GLOBAL_EPS"
FAMILY_ID = "DWD_ICON_EPS"
MAX_OBJECTS = 26
MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_TOTAL_OBJECT_BYTES = 512 * 1024 * 1024


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_arrival_index_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"icon_eps_arrival_index_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"icon_eps_arrival_index_{name}_invalid_sha256")
    return result


def _content_identity(raw: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError(f"icon_eps_arrival_index_{label}_mapping_required")
    value = dict(raw)
    supplied = _sha(label + "_content", value.get("content_sha256"))
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError(f"icon_eps_arrival_index_{label}_content_sha_mismatch")
    return value


def _official_dwd_url(value: Any) -> str:
    result = str(value or "").strip()
    parsed = urlparse(result)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not (host == "dwd.de" or host.endswith(".dwd.de")):
        raise ContractError("icon_eps_arrival_index_source_not_official_dwd")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _raw_path(value: Any, *, raw_root: Path) -> Path:
    try:
        candidate = Path(str(value)).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ContractError("icon_eps_arrival_index_raw_missing") from exc
    try:
        candidate.relative_to(raw_root)
    except ValueError as exc:
        raise ContractError("icon_eps_arrival_index_raw_outside_authorized_root") from exc
    if (
        not candidate.is_file()
        or candidate.is_symlink()
        or candidate.stat().st_size <= 0
        or candidate.stat().st_size > MAX_OBJECT_BYTES
    ):
        raise ContractError("icon_eps_arrival_index_raw_bound_invalid")
    return candidate


def _cohort_entry(
    raw: Mapping[str, Any], *, captures_by_sha: Mapping[str, Mapping[str, Any]], raw_root: Path
) -> dict[str, Any]:
    cohort = _content_identity(raw, label="cohort")
    lineage = cohort.get("lineage")
    if not isinstance(lineage, Mapping):
        raise ContractError("icon_eps_arrival_index_cohort_lineage_missing")
    capture_sha = _sha("capture_receipt", cohort.get("capture_receipt_sha256"))
    capture = captures_by_sha.get(capture_sha)
    if capture is None:
        raise ContractError("icon_eps_arrival_index_capture_receipt_missing")
    raw_sha = _sha("raw", cohort.get("raw_sha256"))
    raw_path = _raw_path(cohort.get("raw_object_ref"), raw_root=raw_root)
    if _sha256_file(raw_path) != raw_sha:
        raise ContractError("icon_eps_arrival_index_raw_sha_mismatch")
    source_url = _official_dwd_url(lineage.get("source_url"))
    run = _stamp(_utc("run_initialization", cohort.get("run_initialization_utc")))
    valid = _stamp(_utc("valid_time", cohort.get("valid_time_utc")))
    published = _utc("provider_published", cohort.get("provider_published_at_utc"))
    received = _utc("response_completed", cohort.get("response_completed_at_utc"))
    if published > received:
        raise ContractError("icon_eps_arrival_index_timestamp_order_invalid")
    expected = {
        "schema": COHORT_SCHEMA,
        "state": "OFFICIAL_40_MEMBER_45_STATION_DECODED_CANDIDATE_ONLY",
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": FAMILY_ID,
        "member_count": 40,
        "station_count": 45,
        "member_record_count": 1_800,
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "hard_no_eligible": False,
    }
    if any(cohort.get(key) != value for key, value in expected.items()):
        raise ContractError("icon_eps_arrival_index_cohort_contract_invalid")
    if (
        capture.get("raw_sha256") != raw_sha
        or str(capture.get("raw_object_ref") or "") != str(raw_path)
        or _stamp(_utc("capture_run", capture.get("run_initialization_utc"))) != run
        or _stamp(_utc("capture_valid", capture.get("valid_time_utc"))) != valid
        or _stamp(_utc("capture_published", capture.get("provider_published_at_utc"))) != _stamp(published)
        or _stamp(_utc("capture_received", capture.get("response_completed_at_utc"))) != _stamp(received)
        or _official_dwd_url(capture.get("source_url")) != source_url
    ):
        raise ContractError("icon_eps_arrival_index_capture_cohort_mismatch")
    return {
        "cohort_content_sha256": str(cohort["content_sha256"]),
        "capture_receipt_content_sha256": capture_sha,
        "raw_sha256": raw_sha,
        "raw_object_ref": str(raw_path),
        "source_url": source_url,
        "run_initialization_utc": run,
        "valid_time_utc": valid,
        "provider_published_at_utc": _stamp(published),
        "received_at_utc": _stamp(received),
        "member_count": 40,
        "station_count": 45,
        "capture_mode": "prospective_native_http_receipt",
        "historical_arrival_time_observed": True,
    }


def _capture_index(captures: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for raw in captures:
        capture = _content_identity(raw, label="capture")
        if (
            capture.get("schema") != CAPTURE_SCHEMA
            or capture.get("state") != "OFFICIAL_RAW_CAPTURED_MEMBER_DECODE_PENDING"
            or capture.get("provider_id") != PROVIDER_ID
            or capture.get("product_id") != PRODUCT_ID
            or capture.get("independence_family_id") != FAMILY_ID
            or int(capture.get("expected_member_count") or 0) != 40
            or capture.get("formal_p0_eligible") is not False
            or capture.get("native_arrival_index_eligible") is not False
        ):
            raise ContractError("icon_eps_arrival_index_capture_contract_invalid")
        digest = str(capture["content_sha256"])
        if digest in result:
            raise ContractError("icon_eps_arrival_index_capture_duplicate")
        result[digest] = capture
    if not result or len(result) > MAX_OBJECTS:
        raise ContractError("icon_eps_arrival_index_capture_count_invalid")
    return result


def build_icon_eps_native_arrival_index(
    cohorts: Iterable[Mapping[str, Any]],
    captures: Iterable[Mapping[str, Any]],
    *,
    indexed_at_utc: str,
    raw_root: Path,
) -> dict[str, Any]:
    """Bind observed DWD object receipts to their decoded 45×40 cohorts."""

    try:
        root = raw_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ContractError("icon_eps_arrival_index_raw_root_missing") from exc
    if not root.is_dir() or root.is_symlink():
        raise ContractError("icon_eps_arrival_index_raw_root_invalid")
    indexed = _stamp(_utc("indexed_at", indexed_at_utc))
    capture_by_sha = _capture_index(captures)
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for raw in cohorts:
        entry = _cohort_entry(raw, captures_by_sha=capture_by_sha, raw_root=root)
        total_bytes += Path(entry["raw_object_ref"]).stat().st_size
        if total_bytes > MAX_TOTAL_OBJECT_BYTES:
            raise ContractError("icon_eps_arrival_index_total_raw_bound_exceeded")
        entries.append(entry)
    if not entries or len(entries) > MAX_OBJECTS:
        raise ContractError("icon_eps_arrival_index_cohort_count_invalid")
    entries.sort(key=lambda item: item["valid_time_utc"])
    if len({item["valid_time_utc"] for item in entries}) != len(entries):
        raise ContractError("icon_eps_arrival_index_valid_time_duplicate")
    if len({item["raw_sha256"] for item in entries}) != len(entries):
        raise ContractError("icon_eps_arrival_index_raw_duplicate")
    runs = {item["run_initialization_utc"] for item in entries}
    if len(runs) != 1:
        raise ContractError("icon_eps_arrival_index_mixed_run_initializations")
    identity = {
        "schema": INDEX_SCHEMA,
        "state": "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_READY",
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": FAMILY_ID,
        "run_initialization_utc": next(iter(runs)),
        "indexed_at_utc": indexed,
        "entry_count": len(entries),
        "entries": entries,
        "native_arrival_index_eligible": True,
        "formal_p0_eligible": False,
        "boundary": {
            "downloads_raw": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_icon_eps_native_arrival_index_repair_receipt(
    cohorts: Iterable[Mapping[str, Any]],
    captures: Iterable[Mapping[str, Any]],
    *,
    inspected_at_utc: str,
    raw_root: Path,
) -> dict[str, Any]:
    """Describe missing original receipts without substituting a later poll.

    A newer DWD download can have the same raw SHA-256 yet a different local
    receive time.  It must not repair an earlier cohort's native arrival.
    This receipt keeps verified objects visible while remaining wholly
    ineligible as an arrival index until every original capture receipt is
    available and validates byte-for-byte.
    """

    try:
        root = raw_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ContractError("icon_eps_arrival_index_raw_root_missing") from exc
    if not root.is_dir() or root.is_symlink():
        raise ContractError("icon_eps_arrival_index_raw_root_invalid")
    inspected = _stamp(_utc("repair_inspected_at", inspected_at_utc))
    capture_by_sha = _capture_index(captures)
    verified: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    total_bytes = 0
    for raw in cohorts:
        cohort = _content_identity(raw, label="cohort")
        capture_sha = _sha("capture_receipt", cohort.get("capture_receipt_sha256"))
        raw_sha = _sha("raw", cohort.get("raw_sha256"))
        valid = _stamp(_utc("cohort_valid", cohort.get("valid_time_utc")))
        if capture_sha not in capture_by_sha:
            unresolved.append({
                "cohort_content_sha256": str(cohort["content_sha256"]),
                "capture_receipt_content_sha256": capture_sha,
                "raw_sha256": raw_sha,
                "valid_time_utc": valid,
                "reason": "original_capture_receipt_content_missing",
            })
            continue
        entry = _cohort_entry(
            cohort, captures_by_sha=capture_by_sha, raw_root=root
        )
        total_bytes += Path(entry["raw_object_ref"]).stat().st_size
        if total_bytes > MAX_TOTAL_OBJECT_BYTES:
            raise ContractError("icon_eps_arrival_index_total_raw_bound_exceeded")
        verified.append({
            "cohort_content_sha256": entry["cohort_content_sha256"],
            "capture_receipt_content_sha256": entry["capture_receipt_content_sha256"],
            "raw_sha256": entry["raw_sha256"],
            "valid_time_utc": entry["valid_time_utc"],
        })
    if not verified and not unresolved or len(verified) + len(unresolved) > MAX_OBJECTS:
        raise ContractError("icon_eps_arrival_index_repair_scope_invalid")
    identity = {
        "schema": REPAIR_SCHEMA,
        "state": (
            "ARRIVAL_INDEX_READY"
            if not unresolved
            else "PARTIAL_NATIVE_ARRIVAL_INDEX_FAIL_CLOSED"
        ),
        "inspected_at_utc": inspected,
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": FAMILY_ID,
        "verified_object_count": len(verified),
        "verified_objects": sorted(verified, key=lambda item: item["valid_time_utc"]),
        "unresolved_receipt_count": len(unresolved),
        "unresolved_receipts": sorted(unresolved, key=lambda item: item["valid_time_utc"]),
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
        "boundary": {
            "downloads_raw": False,
            "writes_target_database": False,
            "writes_production_database": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_icon_eps_native_arrival_index(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate an index without reopening raw files; return it by cohort SHA."""

    value = _content_identity(raw, label="index")
    entries = value.get("entries")
    if (
        value.get("schema") != INDEX_SCHEMA
        or value.get("state") != "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_READY"
        or value.get("provider_id") != PROVIDER_ID
        or value.get("product_id") != PRODUCT_ID
        or value.get("independence_family_id") != FAMILY_ID
        or value.get("native_arrival_index_eligible") is not True
        or value.get("formal_p0_eligible") is not False
        or not isinstance(entries, list)
        or not entries
        or len(entries) > MAX_OBJECTS
        or int(value.get("entry_count") or -1) != len(entries)
    ):
        raise ContractError("icon_eps_arrival_index_contract_invalid")
    run = _stamp(_utc("index_run", value.get("run_initialization_utc")))
    result: dict[str, dict[str, Any]] = {}
    raw_shas: set[str] = set()
    valid_times: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            raise ContractError("icon_eps_arrival_index_entry_mapping_required")
        entry = dict(raw_entry)
        cohort_sha = _sha("entry_cohort", entry.get("cohort_content_sha256"))
        raw_sha = _sha("entry_raw", entry.get("raw_sha256"))
        _sha("entry_capture_receipt", entry.get("capture_receipt_content_sha256"))
        valid = _stamp(_utc("entry_valid", entry.get("valid_time_utc")))
        published = _utc("entry_published", entry.get("provider_published_at_utc"))
        received = _utc("entry_received", entry.get("received_at_utc"))
        if (
            cohort_sha in result
            or raw_sha in raw_shas
            or valid in valid_times
            or _stamp(_utc("entry_run", entry.get("run_initialization_utc"))) != run
            or published > received
            or _official_dwd_url(entry.get("source_url")) != str(entry.get("source_url"))
            or int(entry.get("member_count") or 0) != 40
            or int(entry.get("station_count") or 0) != 45
            or entry.get("capture_mode") != "prospective_native_http_receipt"
            or entry.get("historical_arrival_time_observed") is not True
        ):
            raise ContractError("icon_eps_arrival_index_entry_invalid")
        result[cohort_sha] = entry
        raw_shas.add(raw_sha)
        valid_times.add(valid)
    return result


def bind_icon_eps_daily_archive_arrival_index(
    archive_raw: Mapping[str, Any], index_raw: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a re-signed archive whose leads are bound to the native index.

    This is an immutable derived artifact.  The candidate archive and all
    raw/cohort receipts remain unchanged; this function only supplies the
    missing arrival-index pointer needed by a later strict-OOS join.
    """

    archive = _content_identity(archive_raw, label="daily_archive")
    authority = archive.get("source_authority")
    lead_results = archive.get("lead_results")
    if (
        archive.get("schema") != ARCHIVE_SCHEMA
        or archive.get("state") != "OFFICIAL_MEMBER_DAILY_CURVES_READY_STRICT_OOS_PENDING"
        or archive.get("provider_id") != PROVIDER_ID
        or archive.get("product_id") != PRODUCT_ID
        or archive.get("independence_family_id") != FAMILY_ID
        or archive.get("native_arrival_index_eligible") is not False
        or not isinstance(authority, Mapping)
        or authority.get("source_role") != "official_native"
        or authority.get("transport_provider_id") != "DWD"
        or authority.get("native_publication_and_arrival_observed") is not True
        or authority.get("native_arrival_index_eligible") is not False
        or not isinstance(lead_results, list)
        or not lead_results
    ):
        raise ContractError("icon_eps_arrival_index_archive_contract_invalid")
    indexed = validate_icon_eps_native_arrival_index(index_raw)
    archive_run = _stamp(_utc("archive_run", archive.get("run_init_utc")))
    if archive_run != _stamp(_utc("index_run", index_raw.get("run_initialization_utc"))):
        raise ContractError("icon_eps_arrival_index_archive_run_mismatch")
    bound_cohorts: set[str] = set()
    for lead in lead_results:
        if not isinstance(lead, Mapping):
            raise ContractError("icon_eps_arrival_index_archive_lead_mapping_required")
        cohort_sha = _sha("archive_lead_cohort", lead.get("cohort_content_sha256"))
        entry = indexed.get(cohort_sha)
        if entry is None or (
            _sha("archive_lead_raw", lead.get("raw_sha256")) != entry["raw_sha256"]
            or _stamp(_utc("archive_lead_valid", lead.get("valid_time_utc")))
            != entry["valid_time_utc"]
        ):
            raise ContractError("icon_eps_arrival_index_archive_lead_mismatch")
        bound_cohorts.add(cohort_sha)
    if bound_cohorts != set(indexed):
        raise ContractError("icon_eps_arrival_index_archive_scope_mismatch")
    identity = {key: item for key, item in archive.items() if key != "content_sha256"}
    identity["native_arrival_index_eligible"] = True
    identity["source_authority"] = {
        **dict(authority),
        "native_arrival_index_eligible": True,
        "arrival_lineage_status": "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_VERIFIED",
        "native_arrival_index_evidence_sha256": str(index_raw["content_sha256"]),
    }
    identity["native_arrival_index"] = {
        "schema": INDEX_SCHEMA,
        "content_sha256": str(index_raw["content_sha256"]),
        "entry_count": len(indexed),
        "run_initialization_utc": archive_run,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "ARCHIVE_SCHEMA",
    "INDEX_SCHEMA",
    "REPAIR_SCHEMA",
    "bind_icon_eps_daily_archive_arrival_index",
    "build_icon_eps_native_arrival_index",
    "build_icon_eps_native_arrival_index_repair_receipt",
    "validate_icon_eps_native_arrival_index",
]
