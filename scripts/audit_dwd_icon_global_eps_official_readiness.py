#!/usr/bin/env python3
"""Bounded, read-only readiness audit for official DWD ICON Global/EPS evidence.

This audit never contacts DWD, downloads GRIB, mutates SQLite, or promotes a
model.  It keeps deterministic ICON Global, official ICON Global EPS, and the
Open-Meteo seamless EPS construction as distinct product identities.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from datetime import datetime, timezone
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    ResourceBoundError,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.model_sources import REGIONAL_MODEL_LINEAGE  # noqa: E402


SCHEMA = "dwd_icon_global_eps_official_readiness_v1"
EXPECTED_MEMBER_COUNT = 40
EXPECTED_STATION_COUNT = 45
EXPECTED_MEMBER_STATION_RECORD_COUNT = EXPECTED_MEMBER_COUNT * EXPECTED_STATION_COUNT
STATION_MEMBER_COHORT_SCHEMA = "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
OFFICIAL_GRID_CAPTURE_SCHEMA = "bayes_basket_target_icon_eps_official_grid_capture_v1"
MAX_RECEIPT_FILES = 8_192
MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RECEIPT_BYTES = 128 * 1024 * 1024
DEFAULT_ROOTS = (
    ROOT / "data/runtime/official_nwp_sources/icon/receipts",
    ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_official/receipts",
    ROOT / "data/runtime/official_nwp_sources/icon/history",
)
DEFAULT_OUTPUT = ROOT / "output/audits/dwd_icon_global_eps_official_readiness_20260801.json"

_MEMBER_RE = re.compile(r"(?:member|mem|perturbation)[_=/.-]?(\d{1,3})(?:\D|$)", re.I)


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _time_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return text
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _scalar(record: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = record.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _canonical_timestamp(value: str) -> str:
    """Canonicalize equivalent RFC3339 spellings before immutable join keys."""

    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _urls(record: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for name in (
        "raw_request_url",
        "request_url",
        "url",
        "source_url",
        "publication_metadata_url",
    ):
        value = record.get(name)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            result.append(value)
    return result


def _is_dwd_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "dwd.de" or host.endswith(".dwd.de")


def _is_open_meteo(record: Mapping[str, Any], urls: Iterable[str]) -> bool:
    source = _scalar(record, "source_class", "source_role").lower()
    transport = _scalar(record, "transport_provider_id", "provider_id").lower()
    if source == "aggregator_republisher" or transport in {"open_meteo", "open-meteo"}:
        return True
    return any("open-meteo" in (urlparse(url).hostname or "").lower() for url in urls)


def _product_id(record: Mapping[str, Any], urls: Iterable[str]) -> str:
    explicit = _scalar(record, "ensemble_system_id", "product_id", "model_id").upper()
    normalized = explicit.replace("-", "_")
    if normalized in {"DWD_ICON_EPS_SEAMLESS", "ICON_EPS_SEAMLESS"}:
        return "DWD_ICON_EPS_SEAMLESS"
    if normalized in {"DWD_ICON_GLOBAL_EPS", "ICON_GLOBAL_EPS", "ICON_EPS"}:
        return "DWD_ICON_GLOBAL_EPS"
    if normalized in {"DWD_ICON_GLOBAL", "ICON_GLOBAL", "ICON"}:
        return "DWD_ICON_GLOBAL"
    joined = " ".join(urls).lower().replace("_", "-")
    if "icon-eps-seamless" in joined:
        return "DWD_ICON_EPS_SEAMLESS"
    if re.search(r"/(?:weather/nwp/)?icon-eps(?:/|$)", joined):
        return "DWD_ICON_GLOBAL_EPS"
    if "/icon/grib/" in joined or "icon-global" in joined:
        return "DWD_ICON_GLOBAL"
    return "UNKNOWN"


def _member_id(record: Mapping[str, Any], urls: Iterable[str]) -> str:
    value = _scalar(record, "member_id", "ensemble_member", "member", "perturbation_number")
    if value:
        try:
            return str(int(value))
        except ValueError:
            return value
    match = _MEMBER_RE.search(" ".join(urls))
    return str(int(match.group(1))) if match else ""


def _content_hash_valid(document: Mapping[str, Any]) -> bool:
    supplied = document.get("content_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64:
        return False
    identity = {key: value for key, value in document.items() if key != "content_sha256"}
    return supplied == _sha(identity)


def _station_member_cohort_summary(document: Mapping[str, Any]) -> dict[str, Any] | None:
    if document.get("schema") != STATION_MEMBER_COHORT_SCHEMA:
        return None
    rows = document.get("member_station_records")
    if not isinstance(rows, list) or len(rows) > EXPECTED_MEMBER_STATION_RECORD_COUNT:
        rows = []
    station_members: dict[str, set[int]] = defaultdict(set)
    raw_shas: set[str] = set()
    valid_records = True
    pairs: set[tuple[str, int]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            valid_records = False
            continue
        station = str(row.get("station_id") or "").strip().upper()
        try:
            member = int(row.get("member_id"))
        except (TypeError, ValueError):
            valid_records = False
            continue
        raw_sha = str(row.get("raw_sha256") or "").strip().lower()
        if not station or member not in range(1, EXPECTED_MEMBER_COUNT + 1) or len(raw_sha) != 64:
            valid_records = False
            continue
        pair = (station, member)
        if pair in pairs:
            valid_records = False
        pairs.add(pair)
        station_members[station].add(member)
        raw_shas.add(raw_sha)
    lineage = document.get("lineage")
    official_grid = lineage.get("official_grid") if isinstance(lineage, Mapping) else None
    grid = official_grid if isinstance(official_grid, Mapping) else {}
    return {
        "schema": STATION_MEMBER_COHORT_SCHEMA,
        "content_sha256": str(document.get("content_sha256") or ""),
        "content_sha256_valid": _content_hash_valid(document),
        "declared_raw_sha256": str(document.get("raw_sha256") or "").strip().lower(),
        "record_raw_sha256s": sorted(raw_shas),
        "declared_station_count": document.get("station_count"),
        "declared_member_count": document.get("member_count"),
        "declared_member_record_count": document.get("member_record_count"),
        "actual_member_record_count": len(rows),
        "actual_station_count": len(station_members),
        "per_station_member_set_complete": bool(station_members) and all(
            members == set(range(1, EXPECTED_MEMBER_COUNT + 1))
            for members in station_members.values()
        ),
        "station_member_pairs_unique": len(pairs) == len(rows),
        "record_shape_valid": valid_records,
        "source_grid_sha256": str(grid.get("source_grid_sha256") or "").strip().lower(),
        "source_grid_uuid": str(grid.get("source_grid_uuid") or "").strip().lower().replace("-", ""),
        "source_grid_cell_count": grid.get("source_grid_cell_count"),
        "source_grid_receipt_sha256": str(grid.get("source_grid_receipt_sha256") or "").strip().lower(),
        "native_arrival_index_eligible": document.get("native_arrival_index_eligible"),
        "formal_p0_eligible": document.get("formal_p0_eligible"),
    }


def _official_grid_receipt_summary(document: Mapping[str, Any]) -> dict[str, Any] | None:
    if document.get("schema") != OFFICIAL_GRID_CAPTURE_SCHEMA:
        return None
    return {
        "content_sha256": str(document.get("content_sha256") or "").strip().lower(),
        "content_sha256_valid": _content_hash_valid(document),
        "raw_sha256": str(document.get("raw_sha256") or "").strip().lower(),
        "grid_uuid": str(document.get("grid_uuid") or "").strip().lower().replace("-", ""),
        "grid_cell_count": document.get("grid_cell_count"),
        "source_url": str(document.get("source_url") or ""),
    }


def _records(document: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    children = document.get("raw_receipts")
    candidates = children if isinstance(children, list) and children else [document]
    records: list[dict[str, Any]] = []
    for child in candidates:
        if not isinstance(child, Mapping):
            continue
        merged = dict(document)
        merged.pop("raw_receipts", None)
        merged.update(child)
        urls = _urls(merged)
        product_id = _product_id(merged, urls)
        decoded_members = merged.get("decoded_member_ids")
        inventory = merged.get("member_inventory")
        if not isinstance(decoded_members, list):
            decoded_members = (
                (inventory or {}).get("member_ids")
                if isinstance(inventory, Mapping)
                else []
            )
        inventory_members = (
            inventory.get("members")
            if isinstance(inventory, Mapping)
            and isinstance(inventory.get("members"), list)
            else []
        )
        forecast_counts = {
            item.get("number_of_forecasts_in_ensemble")
            for item in inventory_members
            if isinstance(item, Mapping)
            and item.get("number_of_forecasts_in_ensemble") is not None
        }
        templates = {
            str(item.get("product_definition_template") or "")
            for item in inventory_members
            if isinstance(item, Mapping)
            and item.get("product_definition_template") is not None
        }
        station_member_cohort = _station_member_cohort_summary(merged)
        official_grid_receipt = _official_grid_receipt_summary(merged)
        records.append({
            "evidence_path": path,
            "product_id": product_id,
            "member_id": _member_id(merged, urls),
            "run_initialization_utc": _canonical_timestamp(_scalar(
                merged, "run_initialization_utc", "run_init_utc", "run_at_utc"
            )),
            "valid_time_utc": _canonical_timestamp(_scalar(merged, "valid_time_utc", "valid_at_utc")),
            "published_at_utc": _canonical_timestamp(_scalar(
                merged, "provider_published_at_utc", "published_at_utc", "publication_time_utc"
            )),
            "received_at_utc": _canonical_timestamp(_scalar(
                merged,
                "received_at_utc",
                "response_completed_at_utc",
                "response_completed_at",
                "capture_completed_at_utc",
            )),
            "raw_sha256": _scalar(merged, "raw_sha256", "object_sha256", "content_sha256"),
            "transport_evidence_present": bool(urls),
            "dwd_official_transport": bool(urls) and all(_is_dwd_url(url) for url in urls),
            "aggregator_republisher": _is_open_meteo(merged, urls),
            "decoded_member_ids": list(decoded_members or []),
            "number_of_forecasts_in_ensemble": merged.get(
                "number_of_forecasts_in_ensemble"
            ) if merged.get("number_of_forecasts_in_ensemble") is not None else (
                next(iter(forecast_counts)) if len(forecast_counts) == 1 else None
            ),
            "product_definition_template": _scalar(
                merged,
                "product_definition_template",
                "product_definition_template_number",
            ) or (next(iter(templates)) if len(templates) == 1 else ""),
            "station_member_cohort": station_member_cohort,
            "official_grid_receipt": official_grid_receipt,
        })
    return records


def _scan(roots: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths: list[Path] = []
    for root in roots:
        root = Path(root).expanduser().resolve()
        if root.is_file() and root.suffix.lower() == ".json" and not root.is_symlink():
            paths.append(root)
        elif root.is_dir():
            paths.extend(path for path in root.rglob("*.json") if path.is_file() and not path.is_symlink())
    paths = sorted(set(paths))
    if len(paths) > MAX_RECEIPT_FILES:
        raise ResourceBoundError("icon_eps_receipt_file_bound_exceeded")
    total = 0
    records: list[dict[str, Any]] = []
    for path in paths:
        size = path.stat().st_size
        if size > MAX_RECEIPT_BYTES:
            raise ResourceBoundError("icon_eps_receipt_bytes_exceeded")
        total += size
        if total > MAX_TOTAL_RECEIPT_BYTES:
            raise ResourceBoundError("icon_eps_total_receipt_bytes_exceeded")
        document = load_bounded_json(path, limit=MAX_RECEIPT_BYTES)
        if isinstance(document, Mapping):
            records.extend(_records(document, str(path)))
    return records, {
        "receipt_file_count": len(paths),
        "receipt_bytes": total,
        "record_count": len(records),
        "limits": {
            "max_receipt_files": MAX_RECEIPT_FILES,
            "max_receipt_bytes": MAX_RECEIPT_BYTES,
            "max_total_receipt_bytes": MAX_TOTAL_RECEIPT_BYTES,
        },
    }


def _coalesce_artifact_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Join complementary capture/decode receipts for one immutable raw object."""

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    unkeyed: list[dict[str, Any]] = []
    for source in records:
        row = dict(source)
        key = (
            str(row.get("product_id") or ""),
            _time_key(row.get("run_initialization_utc")),
            _time_key(row.get("valid_time_utc")),
            str(row.get("raw_sha256") or ""),
        )
        if all(key):
            grouped[key].append(row)
        else:
            unkeyed.append(row)

    merged: list[dict[str, Any]] = []
    for (product, run, valid, raw_sha), parts in sorted(grouped.items()):
        decoded_ids: list[Any] = []
        for part in parts:
            value = part.get("decoded_member_ids")
            if isinstance(value, list):
                decoded_ids.extend(value)
        transport_parts = [
            part
            for part in parts
            if bool(
                part.get("transport_evidence_present")
                or part.get("dwd_official_transport")
            )
        ]

        def first(*names: str) -> Any:
            for name in names:
                for part in parts:
                    value = part.get(name)
                    if value not in (None, "", []):
                        return value
            return ""

        station_member_parts = [
            part["station_member_cohort"]
            for part in parts
            if isinstance(part.get("station_member_cohort"), Mapping)
        ]

        merged.append({
            "evidence_path": sorted(
                {str(part.get("evidence_path") or "") for part in parts}
            ),
            "product_id": product,
            "run_initialization_utc": run,
            "valid_time_utc": valid,
            "published_at_utc": first("published_at_utc"),
            "received_at_utc": first("received_at_utc"),
            "raw_sha256": raw_sha,
            "transport_evidence_present": bool(transport_parts),
            "dwd_official_transport": bool(transport_parts) and all(
                bool(part.get("dwd_official_transport")) for part in transport_parts
            ),
            "aggregator_republisher": any(
                bool(part.get("aggregator_republisher")) for part in parts
            ),
            "decoded_member_ids": decoded_ids,
            "number_of_forecasts_in_ensemble": first(
                "number_of_forecasts_in_ensemble"
            ),
            "product_definition_template": first("product_definition_template"),
            "receipt_part_count": len(parts),
            "station_member_cohort": (
                dict(station_member_parts[0])
                if len(station_member_parts) == 1
                else None
            ),
            "station_member_cohort_part_count": len(station_member_parts),
        })
    return merged + unkeyed


def _station_member_cohort_blockers(
    cohort: Mapping[str, Any] | None,
    *,
    raw_sha256: str,
    grid_receipts: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    if cohort is None:
        return []
    reasons: list[str] = []
    if not bool(cohort.get("content_sha256_valid")):
        reasons.append("station_member_cohort_content_sha_invalid")
    if cohort.get("declared_station_count") != EXPECTED_STATION_COUNT or cohort.get("actual_station_count") != EXPECTED_STATION_COUNT:
        reasons.append("station_member_station_count_not_45")
    if cohort.get("declared_member_count") != EXPECTED_MEMBER_COUNT:
        reasons.append("station_member_member_count_not_40")
    if (
        cohort.get("declared_member_record_count") != EXPECTED_MEMBER_STATION_RECORD_COUNT
        or cohort.get("actual_member_record_count") != EXPECTED_MEMBER_STATION_RECORD_COUNT
    ):
        reasons.append("station_member_record_count_not_1800")
    if not bool(cohort.get("record_shape_valid")) or not bool(cohort.get("station_member_pairs_unique")):
        reasons.append("station_member_record_shape_invalid")
    if not bool(cohort.get("per_station_member_set_complete")):
        reasons.append("station_member_per_station_set_not_1_through_40")
    record_raw_shas = cohort.get("record_raw_sha256s")
    if (
        cohort.get("declared_raw_sha256") != raw_sha256
        or record_raw_shas != [raw_sha256]
    ):
        reasons.append("station_member_raw_sha_not_single_capture_raw")
    if cohort.get("native_arrival_index_eligible") is not False:
        reasons.append("station_member_native_arrival_index_must_remain_false")
    if cohort.get("formal_p0_eligible") is not False:
        reasons.append("station_member_formal_p0_must_remain_false")
    grid_receipt_sha = str(cohort.get("source_grid_receipt_sha256") or "")
    grid = grid_receipts.get(grid_receipt_sha)
    if grid is None:
        reasons.append("station_member_official_grid_receipt_missing")
    elif (
        not bool(grid.get("content_sha256_valid"))
        or grid.get("raw_sha256") != cohort.get("source_grid_sha256")
        or grid.get("grid_uuid") != cohort.get("source_grid_uuid")
        or grid.get("grid_cell_count") != cohort.get("source_grid_cell_count")
        or not _is_dwd_url(str(grid.get("source_url") or ""))
    ):
        reasons.append("station_member_official_grid_binding_invalid")
    return sorted(set(reasons))


def assess_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = _coalesce_artifact_records(records)
    grid_receipts = {
        str(grid.get("content_sha256") or ""): grid
        for row in rows
        for grid in [row.get("official_grid_receipt")]
        if isinstance(grid, Mapping) and str(grid.get("content_sha256") or "")
    }
    cohorts: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    deterministic = [row for row in rows if row.get("product_id") == "DWD_ICON_GLOBAL"]
    republisher = [row for row in rows if row.get("aggregator_republisher")]
    for row in rows:
        product = str(row.get("product_id") or "")
        if product not in {"DWD_ICON_GLOBAL_EPS", "DWD_ICON_EPS_SEAMLESS"}:
            continue
        cohorts[(product, str(row.get("run_initialization_utc") or ""), str(row.get("valid_time_utc") or ""))].append(row)

    products: dict[str, dict[str, Any]] = {}
    for product in ("DWD_ICON_GLOBAL_EPS", "DWD_ICON_EPS_SEAMLESS"):
        summaries = []
        for (cohort_product, run, valid), objects in sorted(cohorts.items()):
            if cohort_product != product:
                continue
            decoded_ids: list[str] = []
            for row in objects:
                raw_ids = row.get("decoded_member_ids")
                if isinstance(raw_ids, list):
                    decoded_ids.extend(str(item) for item in raw_ids)
            unique_ids = sorted(
                {item for item in decoded_ids if item},
                key=lambda item: (
                    not item.isdigit(),
                    int(item) if item.isdigit() else item,
                ),
            )
            reasons = []
            if not run:
                reasons.append("run_initialization_missing")
            if not valid:
                reasons.append("valid_time_missing")
            if len(objects) != 1:
                reasons.append("official_raw_object_count_not_one")
            if unique_ids != [str(index) for index in range(1, 41)]:
                reasons.append("decoded_member_set_not_1_through_40")
            if len(decoded_ids) != len(set(decoded_ids)):
                reasons.append("duplicate_member_id")
            for field, reason in (
                ("published_at_utc", "object_publication_timestamp_missing"),
                ("received_at_utc", "object_received_timestamp_missing"),
                ("raw_sha256", "object_raw_sha256_missing"),
            ):
                if any(not str(row.get(field) or "") for row in objects):
                    reasons.append(reason)
            if any(not row.get("dwd_official_transport") for row in objects):
                reasons.append("non_dwd_transport_present")
            if any(row.get("aggregator_republisher") for row in objects):
                reasons.append("aggregator_republisher_present")
            if any(
                int(row.get("number_of_forecasts_in_ensemble") or 0)
                != EXPECTED_MEMBER_COUNT
                for row in objects
            ):
                reasons.append("number_of_forecasts_not_40")
            if any(
                str(row.get("product_definition_template") or "")
                not in {"1", "4.1"}
                for row in objects
            ):
                reasons.append("product_definition_template_not_4_1")
            station_member = next(
                (
                    row.get("station_member_cohort")
                    for row in objects
                    if isinstance(row.get("station_member_cohort"), Mapping)
                ),
                None,
            )
            if sum(int(row.get("station_member_cohort_part_count") or 0) for row in objects) > 1:
                reasons.append("station_member_cohort_not_unique")
            station_reasons = _station_member_cohort_blockers(
                station_member if isinstance(station_member, Mapping) else None,
                raw_sha256=str(objects[0].get("raw_sha256") or ""),
                grid_receipts=grid_receipts,
            )
            reasons.extend(station_reasons)
            summaries.append({
                "run_initialization_utc": run,
                "valid_time_utc": valid,
                "official_raw_object_count": len(objects),
                "unique_member_count": len(unique_ids),
                "member_ids": unique_ids,
                "ready": not reasons,
                "blockers": sorted(set(reasons)),
                "station_member_cohort_present": isinstance(station_member, Mapping),
                "station_member_cohort_ready": bool(isinstance(station_member, Mapping) and not station_reasons and not reasons),
                "station_member_cohort": dict(station_member) if isinstance(station_member, Mapping) else None,
            })
        ready = [item for item in summaries if item["ready"]]
        station_member_ready = [
            item for item in summaries if item["station_member_cohort_ready"]
        ]
        products[product] = {
            "source_role": "official_native" if product == "DWD_ICON_GLOBAL_EPS" else "official_native_only_if_explicit_dwd_product_evidence",
            "cohort_count": len(summaries),
            "ready_cohort_count": len(ready),
            "native_adapter_execution_ready": bool(ready),
            "station_member_cohort_ready_count": len(station_member_ready),
            "cohorts": summaries,
            "blockers": [] if ready else ["no_complete_official_40_member_cohort"],
        }

    global_ready = products["DWD_ICON_GLOBAL_EPS"]["native_adapter_execution_ready"]
    station_member_ready = bool(
        products["DWD_ICON_GLOBAL_EPS"]["station_member_cohort_ready_count"]
    )
    state = (
        "STATION_MEMBER_COHORT_READY_STRICT_OOS_PENDING"
        if station_member_ready
        else "READY_FOR_NATIVE_ADAPTER_EXECUTION" if global_ready else "BLOCKED_FAIL_CLOSED"
    )
    return {
        "official_deterministic_global": {
            "product_id": "DWD_ICON_GLOBAL",
            "evidence_record_count": len(deterministic),
            "must_not_count_as_eps": True,
        },
        "official_eps_products": products,
        "open_meteo_republisher": {
            "source_class": "aggregator_republisher",
            "evidence_record_count": len(republisher),
            "native_arrival_index_eligible": False,
            "formal_p0_eligible_without_own_strict_oos": False,
            "must_not_be_relabelled_official_native": True,
        },
        "state": state,
        "blockers": (
            ["strict_same_day_oos_pending", "formal_p0_remains_ineligible"]
            if station_member_ready
            else [] if global_ready else [
                "DWD_ICON_GLOBAL_EPS:official_complete_40_member_cohort_absent",
                "native_arrival_and_strict_oos_must_remain_disabled",
            ]
        ),
    }


def build_audit(roots: Iterable[Path] = DEFAULT_ROOTS) -> dict[str, Any]:
    resolved_roots = tuple(Path(root).expanduser().resolve() for root in roots)
    records, scan = _scan(resolved_roots)
    assessment = assess_records(records)
    identity = {
        "schema": SCHEMA,
        **assessment,
        "regional_republisher_candidates": {
            provider: dict(lineage)
            for provider, lineage in sorted(REGIONAL_MODEL_LINEAGE.items())
            if lineage.get("model_id") in {
                "ICON-EU",
                "ICON-D2",
                "AROME-FR",
                "HARMONIE-NL",
                "HARMONIE-EU",
                "UKMO-2KM",
            }
        },
        "scan": scan,
        "input_roots": [str(root) for root in resolved_roots],
        "native_adapter_plan": [
            "discover explicit DWD official icon-eps directory and immutable run inventory",
            "capture one official object per lead with bounded streaming and content-addressed raw retention",
            "record product_id, run initialization, provider publication and local receive timestamps",
            "stream-decode GRIB template 4.1 and require perturbationNumber exactly 1..40",
            "decode per-member station curves without creating forty fake raw objects",
            "build independent strict same-day OOS artifact before any formal P0 activation",
        ],
        "boundary": {
            "network_requests": 0,
            "downloads_started": 0,
            "database_writes": 0,
            "service_changes": 0,
            "orders_submitted": 0,
            "production_activation_allowed": False,
        },
    }
    return {**identity, "content_sha256": _sha(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, dest="roots")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    audit = build_audit(args.roots or DEFAULT_ROOTS)
    wrote = write_bounded_json_if_changed(args.output.expanduser().resolve(), audit)
    print(json.dumps({
        "state": audit["state"],
        "deterministic_evidence_records": audit["official_deterministic_global"]["evidence_record_count"],
        "official_global_eps_ready_cohorts": audit["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["ready_cohort_count"],
        "output": str(args.output.expanduser().resolve()),
        "wrote": wrote,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0 if audit["state"] in {
        "READY_FOR_NATIVE_ADAPTER_EXECUTION",
        "STATION_MEMBER_COHORT_READY_STRICT_OOS_PENDING",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
