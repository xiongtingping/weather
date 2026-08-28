"""Bounded PR02.C retrieval for official historical AWC truth gaps.

The module has two deliberately separate phases:

* freeze an exact target inventory from the current canonical truth-gap
  receipt; and
* resolve every target to one terminal, content-addressed receipt state.

It never creates truth, residuals, artifacts, activations, P0/P0-M/P1, or
orders.  Newly retained official raw objects must still pass the canonical
historical replay and the single canonical ingress.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .contracts import ContractError, content_sha256
from .historical_awc_replay import LEGACY_OBSERVATION_SCHEMA
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    MAX_TARGET_RESEARCH_RAW_FILES,
    ResourceBoundError,
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)
from .station_registry import coordinate_registry_manifest
from .fleet import expected_station_count


INVENTORY_SCHEMA = "awc_historical_truth_gap_inventory_v1"
RECEIPT_SCHEMA = "awc_historical_truth_gap_fetch_receipt_v1"
TRUTH_GAP_SCHEMA = "bayes_basket_target_historical_awc_truth_contract_build_v1"
REPLAY_MANIFEST_SCHEMA = "bayes_basket_target_historical_awc_replay_45station_manifest_v2"
REPLAY_EVIDENCE_SCHEMA = "bayes_basket_target_historical_awc_replay_evidence_v4"
ENDPOINT = "https://aviationweather.gov/api/data/metar"
DEFAULT_PROXY_URL = "http://127.0.0.1:1082"
USER_AGENT = "weather-pr02c-awc-history/1.0"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CUMULATIVE_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_QUERY_STATIONS = 8
MAX_TARGETS = expected_station_count() * 32 * 25
MAX_QUERY_COUNT = 4096
MAX_TIMEOUT_SECONDS = 30
MAX_BATCH_SECONDS = 15 * 60
MIN_QUERY_INTERVAL_SECONDS = 0.75
AWC_RETENTION_HOURS = 360
_METAR = re.compile(r"\b(?:METAR|SPECI)\s+([A-Z0-9]{4})\s+\d{6}Z\b")
_TEMP = re.compile(r"(?<!TX)(?<!TN)\b(M?\d{2}|//)/(M?\d{2}|//)\b")
_PRECISE_TEMP = re.compile(r"\bT([01])(\d{3})([01])(\d{3})\b")


class HistoricalAwcGapError(ContractError):
    """Raised when a PR02.C inventory or receipt cannot be trusted."""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: Any, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HistoricalAwcGapError(f"{label}_invalid") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise HistoricalAwcGapError(f"{label}_timezone_required")
    return result.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _hour_text(value: Any) -> str:
    return _utc(value, "awc_gap_hour").replace(
        minute=0, second=0, microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise HistoricalAwcGapError(f"{label}_invalid")
    return result


def _verify_content_hash(value: Mapping[str, Any], *, label: str) -> str:
    supplied = _sha(value.get("content_sha256"), f"{label}_content_sha256")
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise HistoricalAwcGapError(f"{label}_content_sha256_mismatch")
    return supplied


def _resolve_bound_path(value: Any, *, label: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if path.is_symlink():
        raise HistoricalAwcGapError(f"{label}_symlink_forbidden")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise HistoricalAwcGapError(f"{label}_missing") from exc


def _expected_stations(expected_station_ids: Iterable[str] | None) -> list[str]:
    if expected_station_ids is None:
        expected_station_ids = coordinate_registry_manifest()["station_ids"]
    result = sorted({str(item).strip().upper() for item in expected_station_ids})
    if not result:
        raise HistoricalAwcGapError("awc_gap_expected_station_scope_empty")
    return result


def load_truth_gap_bundle(
    receipt_path: Path | str,
    *,
    expected_station_ids: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Open and verify the receipt, replay manifest, and station leaves."""

    receipt_resolved = _resolve_bound_path(receipt_path, label="awc_gap_truth_receipt")
    receipt = load_bounded_json(receipt_resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(receipt, Mapping) or receipt.get("schema") != TRUTH_GAP_SCHEMA:
        raise HistoricalAwcGapError("awc_gap_truth_receipt_schema_invalid")
    receipt = dict(receipt)
    receipt_content_sha = _verify_content_hash(receipt, label="awc_gap_truth_receipt")
    expected = _expected_stations(expected_station_ids)
    rows = receipt.get("stations")
    if (
        not isinstance(rows, list)
        or len(rows) != len(expected)
        or sorted(str(row.get("station_id") or "").upper() for row in rows if isinstance(row, Mapping))
        != expected
        or int(receipt.get("station_count") or -1) != len(expected)
    ):
        raise HistoricalAwcGapError("awc_gap_truth_receipt_station_scope_mismatch")

    source = receipt.get("source_manifest")
    if not isinstance(source, Mapping):
        raise HistoricalAwcGapError("awc_gap_source_manifest_pointer_missing")
    manifest_path = _resolve_bound_path(source.get("path"), label="awc_gap_source_manifest")
    if _file_sha256(manifest_path) != _sha(
        source.get("file_sha256"), "awc_gap_source_manifest_file_sha256"
    ):
        raise HistoricalAwcGapError("awc_gap_source_manifest_file_sha256_mismatch")
    manifest = load_bounded_json(manifest_path, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(manifest, Mapping) or manifest.get("schema") != REPLAY_MANIFEST_SCHEMA:
        raise HistoricalAwcGapError("awc_gap_source_manifest_schema_invalid")
    manifest = dict(manifest)
    manifest_content_sha = _verify_content_hash(manifest, label="awc_gap_source_manifest")
    manifest_rows = manifest.get("stations")
    if (
        not isinstance(manifest_rows, list)
        or len(manifest_rows) != len(expected)
        or sorted(str(row.get("station_id") or "").upper() for row in manifest_rows if isinstance(row, Mapping))
        != expected
    ):
        raise HistoricalAwcGapError("awc_gap_source_manifest_station_scope_mismatch")

    pointer_rows = source.get("station_evidence")
    if not isinstance(pointer_rows, list):
        raise HistoricalAwcGapError("awc_gap_station_evidence_pointers_missing")
    pointers = {
        str(row.get("station_id") or "").strip().upper(): row
        for row in pointer_rows
        if isinstance(row, Mapping)
    }
    manifest_pointers = {
        str(row.get("station_id") or "").strip().upper(): row
        for row in manifest_rows
        if isinstance(row, Mapping)
    }
    if sorted(pointers) != expected or sorted(manifest_pointers) != expected:
        raise HistoricalAwcGapError("awc_gap_station_evidence_pointer_scope_mismatch")

    evidences: list[dict[str, Any]] = []
    for station in expected:
        pointer = pointers[station]
        manifest_pointer = manifest_pointers[station]
        evidence_path = _resolve_bound_path(
            pointer.get("path"), label="awc_gap_station_evidence"
        )
        file_sha = _file_sha256(evidence_path)
        expected_file_sha = _sha(
            pointer.get("file_sha256"), "awc_gap_station_evidence_file_sha256"
        )
        if file_sha != expected_file_sha or file_sha != manifest_pointer.get("file_sha256"):
            raise HistoricalAwcGapError("awc_gap_station_evidence_file_sha256_mismatch")
        evidence = load_bounded_json(evidence_path, limit=MAX_JSON_INPUT_BYTES)
        if not isinstance(evidence, Mapping) or evidence.get("schema") != REPLAY_EVIDENCE_SCHEMA:
            raise HistoricalAwcGapError("awc_gap_station_evidence_schema_invalid")
        evidence = dict(evidence)
        evidence_sha = _sha(
            evidence.get("evidence_sha256"), "awc_gap_station_evidence_content_sha256"
        )
        identity = {key: item for key, item in evidence.items() if key != "evidence_sha256"}
        if (
            evidence_sha != content_sha256(identity)
            or evidence_sha != pointer.get("content_sha256")
            or evidence_sha != manifest_pointer.get("content_sha256")
            or str(evidence.get("station_id") or "").upper() != station
            or evidence.get("source_lane") != "AWC"
            or evidence.get("source_role") != "AWC_HISTORICAL_REPLAY_INGRESS"
            or evidence.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
            or evidence.get("historical_only") is not True
            or evidence.get("canonical_ingress") is not True
        ):
            raise HistoricalAwcGapError("awc_gap_station_evidence_binding_invalid")
        evidences.append(evidence)
    return (
        receipt,
        manifest,
        evidences,
        {
            "receipt_path": str(receipt_resolved),
            "receipt_file_sha256": _file_sha256(receipt_resolved),
            "receipt_content_sha256": receipt_content_sha,
            "manifest_path": str(manifest_path),
            "manifest_file_sha256": _file_sha256(manifest_path),
            "manifest_content_sha256": manifest_content_sha,
        },
    )


def build_gap_inventory(
    *,
    truth_gap_receipt: Mapping[str, Any],
    replay_manifest: Mapping[str, Any],
    station_evidences: Sequence[Mapping[str, Any]],
    source_binding: Mapping[str, str],
) -> dict[str, Any]:
    """Freeze one target per missing METAR-covered hour/revision policy."""

    receipt_stations = {
        str(row.get("station_id") or "").upper(): row
        for row in truth_gap_receipt.get("stations", [])
        if isinstance(row, Mapping)
    }
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for evidence in sorted(station_evidences, key=lambda row: str(row["station_id"])):
        station = str(evidence["station_id"]).upper()
        receipt_row = receipt_stations.get(station)
        if receipt_row is None:
            raise HistoricalAwcGapError("awc_gap_receipt_station_evidence_missing")
        if sorted(receipt_row.get("truth_blocked_dates") or []) != sorted(
            evidence.get("truth_blocked_dates") or []
        ):
            raise HistoricalAwcGapError("awc_gap_blocked_date_binding_mismatch")
        blocked_dates = set(str(item) for item in evidence.get("truth_blocked_dates") or [])
        completeness = evidence.get("station_day_completeness")
        if not isinstance(completeness, list):
            raise HistoricalAwcGapError("awc_gap_completeness_rows_invalid")
        for day in completeness:
            if not isinstance(day, Mapping):
                raise HistoricalAwcGapError("awc_gap_completeness_row_invalid")
            event_date = str(day.get("event_date") or "")
            if event_date not in blocked_dates:
                continue
            missing_slots = sorted({str(item) for item in day.get("missing_awc_report_hour_slots") or []})
            conflict_slots = sorted({str(item) for item in day.get("quarantined_metar_revision_slots") or []})
            for expected_slot, action in [
                *((slot, "FETCH_REQUIRED") for slot in missing_slots),
                *((slot, "REVISION_REVIEW_REQUIRED") for slot in conflict_slots),
            ]:
                _utc(expected_slot, "awc_gap_expected_valid_at_utc")
                # The retained AWC rows do not expose a provider revision
                # number.  Request the complete official revision set and
                # quarantine multiple raw hashes; never invent revision=0.
                requested_revision_key = "ALL_OFFICIAL_REVISIONS"
                natural_key = (
                    station,
                    event_date,
                    expected_slot,
                    requested_revision_key,
                )
                if natural_key in seen:
                    raise HistoricalAwcGapError("awc_gap_duplicate_natural_key")
                seen.add(natural_key)
                target_identity = {
                    "station_id": station,
                    "event_date": event_date,
                    "expected_valid_at_utc": expected_slot,
                    "expected_report_type": "AWC_REPORT",
                    "requested_revision_key": requested_revision_key,
                }
                targets.append(
                    {
                        "target_id": content_sha256(target_identity),
                        **target_identity,
                        "settlement_timezone": str(evidence.get("target_timezone") or ""),
                        "station_day_start_utc": str(day.get("station_day_start_utc") or ""),
                        "station_day_end_utc": str(day.get("station_day_end_utc") or ""),
                        "action": action,
                    }
                )
    targets.sort(
        key=lambda row: (
            row["event_date"],
            row["station_day_end_utc"],
            row["station_id"],
            row["expected_valid_at_utc"],
            row["action"],
        )
    )
    if not targets or len(targets) > MAX_TARGETS:
        raise HistoricalAwcGapError("awc_gap_target_count_invalid")
    identity = {
        "schema": INVENTORY_SCHEMA,
        "as_of_utc": str(truth_gap_receipt.get("as_of_utc") or ""),
        "source_truth_gap": dict(source_binding),
        "source_replay_manifest_content_sha256": _sha(
            replay_manifest.get("content_sha256"), "awc_gap_replay_manifest_content_sha256"
        ),
        "station_count": len(station_evidences),
        "target_count": len(targets),
        "targets": targets,
        "natural_key": [
            "station_id",
            "event_date",
            "expected_valid_at_utc",
            "requested_revision_key",
        ],
        "checks": {
            "exact_truth_gap_receipt_bound": True,
            "exact_replay_station_leaves_bound": True,
            "closed_historical_days_only": True,
            "hour_bucket_awc_report_coverage": True,
            "provider_revision_number_invented": False,
            "forward_data_counted_as_oos": False,
        },
        "boundary": {
            "production_no_live": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "creates_truth_or_activation": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_gap_inventory_from_receipt(
    receipt_path: Path | str,
    *,
    expected_station_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    receipt, manifest, evidences, binding = load_truth_gap_bundle(
        receipt_path, expected_station_ids=expected_station_ids
    )
    return build_gap_inventory(
        truth_gap_receipt=receipt,
        replay_manifest=manifest,
        station_evidences=evidences,
        source_binding=binding,
    )


def verify_gap_inventory(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema") != INVENTORY_SCHEMA:
        raise HistoricalAwcGapError("awc_gap_inventory_schema_invalid")
    inventory = dict(value)
    _verify_content_hash(inventory, label="awc_gap_inventory")
    targets = inventory.get("targets")
    if not isinstance(targets, list) or len(targets) != int(inventory.get("target_count") or -1):
        raise HistoricalAwcGapError("awc_gap_inventory_target_count_mismatch")
    seen: set[tuple[str, str, str, str]] = set()
    for target in targets:
        if not isinstance(target, Mapping):
            raise HistoricalAwcGapError("awc_gap_inventory_target_invalid")
        identity = {
            "station_id": str(target.get("station_id") or "").upper(),
            "event_date": str(target.get("event_date") or ""),
            "expected_valid_at_utc": str(target.get("expected_valid_at_utc") or ""),
            "expected_report_type": str(target.get("expected_report_type") or ""),
            "requested_revision_key": str(target.get("requested_revision_key") or ""),
        }
        key = (
            identity["station_id"],
            identity["event_date"],
            identity["expected_valid_at_utc"],
            identity["requested_revision_key"],
        )
        if (
            target.get("target_id") != content_sha256(identity)
            or key in seen
            or identity["expected_report_type"] != "AWC_REPORT"
            or identity["requested_revision_key"] != "ALL_OFFICIAL_REVISIONS"
            or target.get("action") not in {"FETCH_REQUIRED", "REVISION_REVIEW_REQUIRED"}
        ):
            raise HistoricalAwcGapError("awc_gap_inventory_target_binding_invalid")
        _utc(identity["expected_valid_at_utc"], "awc_gap_expected_valid_at_utc")
        seen.add(key)
    return inventory


def verify_gap_fetch_receipt(
    value: Mapping[str, Any], *, inventory: Mapping[str, Any]
) -> dict[str, Any]:
    if value.get("schema") != RECEIPT_SCHEMA:
        raise HistoricalAwcGapError("awc_gap_prior_receipt_schema_invalid")
    receipt = dict(value)
    _verify_content_hash(receipt, label="awc_gap_prior_receipt")
    if (
        receipt.get("inventory_content_sha256") != inventory.get("content_sha256")
        or int(receipt.get("inventory_target_count") or -1)
        != int(inventory.get("target_count") or -2)
        or int(receipt.get("terminal_count") or -1)
        != int(inventory.get("target_count") or -2)
    ):
        raise HistoricalAwcGapError("awc_gap_prior_receipt_inventory_binding_mismatch")
    rows = receipt.get("terminals")
    if not isinstance(rows, list):
        raise HistoricalAwcGapError("awc_gap_prior_receipt_terminals_invalid")
    expected = {str(row["target_id"]) for row in inventory["targets"]}
    actual = [str(row.get("target_id") or "") for row in rows if isinstance(row, Mapping)]
    if len(actual) != len(rows) or len(set(actual)) != len(actual) or set(actual) != expected:
        raise HistoricalAwcGapError("awc_gap_prior_receipt_terminal_scope_mismatch")
    return receipt


def _parse_row_time(row: Mapping[str, Any]) -> datetime | None:
    try:
        if row.get("obsTime") is not None:
            return datetime.fromtimestamp(float(row["obsTime"]), tz=timezone.utc)
        for key in ("reportTime", "observation_time", "receiptTime"):
            if row.get(key):
                return _utc(row[key], "awc_gap_provider_report_time")
    except (HistoricalAwcGapError, TypeError, ValueError, OverflowError, OSError):
        return None
    return None


def _row_station(row: Mapping[str, Any]) -> str:
    return str(row.get("icaoId") or row.get("station_id") or row.get("station") or "").strip().upper()


def _row_report_type(row: Mapping[str, Any]) -> str:
    value = str(row.get("metarType") or row.get("report_type") or "METAR").strip().upper()
    return value if value in {"METAR", "SPECI"} else ""


def _row_raw_text(row: Mapping[str, Any]) -> str:
    return str(row.get("rawOb") or row.get("raw_text") or row.get("raw") or "").strip()


def _raw_temperature(raw_text: str) -> float:
    precise = _PRECISE_TEMP.search(raw_text.replace("\n", " "))
    if precise is not None:
        value = int(precise.group(2)) / 10.0
        return -value if precise.group(1) == "1" else value
    match = _TEMP.search(raw_text.replace("\n", " "))
    if match is None or match.group(1) == "//":
        raise HistoricalAwcGapError("awc_gap_temperature_missing")
    value = match.group(1)
    return float(-int(value[1:]) if value.startswith("M") else int(value))


def _row_temperature(row: Mapping[str, Any], raw_text: str) -> float:
    value = row.get("temp")
    if value is None:
        value = row.get("reported_temperature_c")
    try:
        result = float(value) if value is not None else _raw_temperature(raw_text)
    except (TypeError, ValueError) as exc:
        raise HistoricalAwcGapError("awc_gap_temperature_invalid") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise HistoricalAwcGapError("awc_gap_temperature_invalid")
    return result


def _canonical_raw_object(
    row: Mapping[str, Any],
    *,
    station_id: str,
    valid_at: datetime,
    source_url: str,
    source_poll_id: str,
    received_at_utc: str,
) -> dict[str, Any]:
    raw_text = _row_raw_text(row)
    report_type = _row_report_type(row)
    if not raw_text or report_type not in {"METAR", "SPECI"}:
        raise HistoricalAwcGapError("awc_gap_provider_row_invalid")
    match = _METAR.search(raw_text.replace("\n", " "))
    if match is None or match.group(1).upper() != station_id:
        raise HistoricalAwcGapError("awc_gap_raw_station_mismatch")
    raw_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    return {
        "station_id": station_id,
        "source_lane": "awc",
        "report_type": report_type,
        "valid_at_utc": _utc_text(valid_at),
        "reported_temperature_c": _row_temperature(row, raw_text),
        "raw_sha256": raw_sha,
        "raw_text": raw_text,
        "source_poll_id": source_poll_id,
        "first_seen_at_utc": received_at_utc,
        "canonical_committed_at_utc": received_at_utc,
        "source_url": source_url,
        "historical_data_class": "H",
    }


def _gzip_json(value: Mapping[str, Any]) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    from io import BytesIO

    output = BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as handle:
        handle.write(raw)
    return output.getvalue()


def _persist_raw_object(raw_root: Path, value: Mapping[str, Any]) -> tuple[Path, bool]:
    raw_sha = _sha(value.get("raw_sha256"), "awc_gap_raw_sha256")
    destination = raw_root / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
    if destination.is_symlink():
        raise HistoricalAwcGapError("awc_gap_raw_object_symlink_forbidden")
    if destination.is_file():
        try:
            with gzip.open(destination, "rt", encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HistoricalAwcGapError("awc_gap_existing_raw_object_invalid") from exc
        if (
            not isinstance(current, Mapping)
            or current.get("raw_sha256") != raw_sha
            or current.get("raw_text") != value.get("raw_text")
        ):
            raise HistoricalAwcGapError("awc_gap_existing_raw_object_conflict")
        return destination.resolve(), False
    payload = _gzip_json(value)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ResourceBoundError("awc_gap_raw_object_bytes_exceeded")
    if shutil.disk_usage(raw_root).free < len(payload) + 512 * 1024 * 1024:
        raise ResourceBoundError("awc_gap_raw_min_free_space_guard")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve(), True


def _load_retained_exact_index(raw_root: Path, wanted: set[tuple[str, str]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    object_root = raw_root / "objects"
    if not object_root.is_dir():
        return result
    file_count = 0
    for path in object_root.rglob("*.json.gz"):
        if path.is_symlink():
            continue
        file_count += 1
        if file_count > MAX_TARGET_RESEARCH_RAW_FILES:
            raise ResourceBoundError("target_raw_retention_file_count_exceeded")
        assert_process_memory_budget()
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping) or str(value.get("source_lane") or "").lower() != "awc":
            continue
        station = str(value.get("station_id") or "").upper()
        stamp = str(value.get("valid_at_utc") or "")
        try:
            key = (station, _hour_text(stamp))
        except HistoricalAwcGapError:
            continue
        if key not in wanted or str(value.get("report_type") or "").upper() not in {"METAR", "SPECI"}:
            continue
        raw_text = str(value.get("raw_text") or "")
        raw_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        parsed = urllib.parse.urlparse(str(value.get("source_url") or ""))
        if (
            raw_sha != value.get("raw_sha256")
            or parsed.scheme != "https"
            or parsed.hostname != "aviationweather.gov"
            or parsed.path != "/api/data/metar"
            or not str(value.get("source_poll_id") or "").startswith("awc-historical-")
        ):
            continue
        result[key].append(dict(value))
    return result


def _observation_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "station_id": str(raw["station_id"]),
        "source_lane": "awc",
        "report_type": str(raw["report_type"]),
        "valid_at_utc": str(raw["valid_at_utc"]),
        "reported_temperature_c": float(raw["reported_temperature_c"]),
        "raw_sha256": str(raw["raw_sha256"]),
        "source_poll_id": str(raw["source_poll_id"]),
        "first_seen_at_utc": str(raw["first_seen_at_utc"]),
        "canonical_committed_at_utc": str(raw["canonical_committed_at_utc"]),
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }


def _build_url(stations: Sequence[str], query_end_utc: str) -> str:
    query_end = _utc(query_end_utc, "awc_gap_query_end_utc")
    return ENDPOINT + "?" + urllib.parse.urlencode(
        {
            "ids": ",".join(stations),
            "format": "json",
            "taf": "false",
            "hours": "24",
            "date": query_end.strftime("%Y%m%d_%H%M"),
        }
    )


Transport = Callable[[str, int, int, str], tuple[int, bytes, Mapping[str, str]]]


def official_awc_transport(
    url: str,
    timeout_seconds: int,
    max_response_bytes: int,
    proxy_url: str,
) -> tuple[int, bytes, Mapping[str, str]]:
    parsed_proxy = urllib.parse.urlparse(proxy_url)
    if (
        parsed_proxy.scheme not in {"http", "https"}
        or parsed_proxy.hostname != "127.0.0.1"
        or parsed_proxy.port != 1082
    ):
        raise HistoricalAwcGapError("awc_gap_proxy_contract_invalid")
    # The local 1082 proxy's trusted inspection CA is installed in the macOS
    # system keychain, not certifi.  Use the platform curl trust store while
    # keeping certificate and hostname verification enabled.  stdout contains
    # only the three-byte HTTP status; response/header bodies go to bounded
    # temporary files and are removed on every path.
    with tempfile.TemporaryDirectory(prefix="pr02c-awc-") as temporary_root:
        temporary = Path(temporary_root)
        if shutil.disk_usage(temporary).free < max_response_bytes + 512 * 1024 * 1024:
            raise ResourceBoundError("awc_gap_transport_min_free_space_guard")
        body_path = temporary / "body"
        header_path = temporary / "headers"
        command = [
            "/usr/bin/curl",
            "--silent",
            "--show-error",
            "--location",
            "--max-redirs",
            "2",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--tlsv1.2",
            "--connect-timeout",
            str(timeout_seconds),
            "--max-time",
            str(timeout_seconds),
            "--max-filesize",
            str(max_response_bytes),
            "--proxy",
            proxy_url,
            "--user-agent",
            USER_AGENT,
            "--header",
            "Accept: application/json",
            "--dump-header",
            str(header_path),
            "--output",
            str(body_path),
            "--write-out",
            "%{http_code}",
            url,
        ]
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 2,
        )
        if len(completed.stdout) > 16 or len(completed.stderr) > 64 * 1024:
            raise ResourceBoundError("awc_gap_transport_diagnostic_bytes_exceeded")
        if completed.returncode != 0:
            if completed.returncode == 63:
                raise ResourceBoundError("awc_gap_response_bytes_exceeded")
            message = completed.stderr.decode("utf-8", "replace").strip()[:320]
            raise OSError(f"curl_exit_{completed.returncode}:{message}")
        try:
            status = int(completed.stdout.decode("ascii").strip())
        except (UnicodeError, ValueError) as exc:
            raise HistoricalAwcGapError("awc_gap_http_status_invalid") from exc
        body_size = int(body_path.stat().st_size) if body_path.is_file() else 0
        header_size = int(header_path.stat().st_size) if header_path.is_file() else 0
        if body_size > max_response_bytes or header_size > 64 * 1024:
            raise ResourceBoundError("awc_gap_response_bytes_exceeded")
        body = body_path.read_bytes() if body_path.is_file() else b""
        header_text = header_path.read_text(encoding="iso-8859-1") if header_path.is_file() else ""
        headers: dict[str, str] = {}
        for line in header_text.splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip()] = value.strip()
        return status, body, headers


def _decode_rows(body: bytes) -> list[dict[str, Any]]:
    if not body.strip():
        return []
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalAwcGapError("awc_gap_response_json_invalid") from exc
    if isinstance(value, Mapping) and isinstance(value.get("data"), list):
        value = value["data"]
    if not isinstance(value, list):
        raise HistoricalAwcGapError("awc_gap_response_shape_invalid")
    if len(value) > 100_000:
        raise ResourceBoundError("awc_gap_response_row_count_exceeded")
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _terminal(
    target: Mapping[str, Any],
    *,
    state: str,
    subtype: str,
    query_id: str | None = None,
    raw_sha256s: Sequence[str] = (),
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "target_id": str(target["target_id"]),
        "station_id": str(target["station_id"]),
        "event_date": str(target["event_date"]),
        "expected_valid_at_utc": str(target["expected_valid_at_utc"]),
        "requested_revision_key": str(target["requested_revision_key"]),
        "state": state,
        "subtype": subtype,
        "query_id": query_id,
        "raw_sha256s": sorted(set(raw_sha256s)),
        "retryable": bool(retryable),
    }


def execute_gap_fetch(
    inventory: Mapping[str, Any],
    *,
    raw_root: Path | str,
    requested_at_utc: str,
    proxy_url: str = DEFAULT_PROXY_URL,
    timeout_seconds: int = 15,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    max_cumulative_response_bytes: int = MAX_CUMULATIVE_RESPONSE_BYTES,
    min_query_interval_seconds: float = MIN_QUERY_INTERVAL_SECONDS,
    max_batch_seconds: float = MAX_BATCH_SECONDS,
    prior_receipt: Mapping[str, Any] | None = None,
    transport: Transport = official_awc_transport,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve every inventory target without writing canonical facts."""

    inventory = verify_gap_inventory(inventory)
    verified_prior = (
        verify_gap_fetch_receipt(prior_receipt, inventory=inventory)
        if prior_receipt is not None
        else None
    )
    requested_at = _utc(requested_at_utc, "awc_gap_requested_at_utc")
    if timeout_seconds < 1 or timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise HistoricalAwcGapError("awc_gap_timeout_invalid")
    if max_response_bytes < 1 or max_response_bytes > MAX_RESPONSE_BYTES:
        raise HistoricalAwcGapError("awc_gap_max_response_bytes_invalid")
    if (
        max_cumulative_response_bytes < max_response_bytes
        or max_cumulative_response_bytes > MAX_CUMULATIVE_RESPONSE_BYTES
    ):
        raise HistoricalAwcGapError("awc_gap_cumulative_response_bytes_invalid")
    if not 0 <= min_query_interval_seconds <= 5:
        raise HistoricalAwcGapError("awc_gap_query_interval_invalid")
    if not 1 <= max_batch_seconds <= MAX_BATCH_SECONDS:
        raise HistoricalAwcGapError("awc_gap_batch_seconds_invalid")
    root = Path(raw_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    assert_process_memory_budget()
    retained_bytes_before = assert_target_raw_retention_budget(
        root, bytes_needed=max_cumulative_response_bytes + MAX_JSON_OUTPUT_BYTES
    )

    targets = [dict(item) for item in inventory["targets"]]
    wanted = {
        (str(item["station_id"]), _hour_text(item["expected_valid_at_utc"]))
        for item in targets
    }
    retained = _load_retained_exact_index(root, wanted)
    terminals: dict[str, dict[str, Any]] = {}
    if verified_prior is not None:
        for row in verified_prior["terminals"]:
            # Non-retryable terminal gaps and review blockers are immutable
            # for this inventory.  Successful raw states are re-proved from
            # the content-addressed store so the new observation packet stays
            # complete; retryable network/provider blockers alone may query.
            if (
                row.get("state") in {"DATA_MISSING", "BLOCKED"}
                and row.get("retryable") is False
            ):
                terminals[str(row["target_id"])] = dict(row)
    observations: dict[tuple[str, str, str], dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for target in targets:
        target_id = str(target["target_id"])
        if target_id in terminals:
            continue
        if target["action"] == "REVISION_REVIEW_REQUIRED":
            terminals[target_id] = _terminal(
                target,
                state="BLOCKED",
                subtype="CORRECTION_REVISION_REVIEW_REQUIRED",
            )
            continue
        hour_rows = retained.get((
            str(target["station_id"]),
            _hour_text(target["expected_valid_at_utc"]),
        ), [])
        hashes_by_valid: dict[str, set[str]] = defaultdict(set)
        for row in hour_rows:
            hashes_by_valid[str(row["valid_at_utc"])].add(str(row["raw_sha256"]))
        hashes = sorted({value for values in hashes_by_valid.values() for value in values})
        if hour_rows and all(len(values) == 1 for values in hashes_by_valid.values()):
            terminals[target_id] = _terminal(
                target,
                state="ALREADY_RETAINED",
                subtype="OFFICIAL_HOUR_COVERAGE_ALREADY_RETAINED",
                raw_sha256s=hashes,
            )
            for raw in hour_rows:
                raw_sha = str(raw["raw_sha256"])
                observations[(str(raw["station_id"]), str(raw["valid_at_utc"]), raw_sha)] = _observation_row(raw)
        elif any(len(values) > 1 for values in hashes_by_valid.values()):
            terminals[target_id] = _terminal(
                target,
                state="BLOCKED",
                subtype="CORRECTION_REVISION_REVIEW_REQUIRED",
                raw_sha256s=hashes,
            )
        else:
            pending.append(target)

    cutoff = requested_at - timedelta(hours=AWC_RETENTION_HOURS)
    query_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for target in pending:
        target_id = str(target["target_id"])
        day_end = _utc(target["station_day_end_utc"], "awc_gap_station_day_end_utc")
        if day_end <= cutoff:
            terminals[target_id] = _terminal(
                target,
                state="DATA_MISSING",
                subtype="OFFICIAL_RETENTION_WINDOW_EXPIRED",
            )
        else:
            query_groups[_utc_text(day_end)].append(target)

    query_specs: list[tuple[str, list[str], list[dict[str, Any]]]] = []
    for query_end, group in sorted(query_groups.items()):
        by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for target in group:
            by_station[str(target["station_id"])].append(target)
        stations = sorted(by_station)
        for index in range(0, len(stations), MAX_QUERY_STATIONS):
            chunk = stations[index : index + MAX_QUERY_STATIONS]
            chunk_targets = [item for station in chunk for item in by_station[station]]
            query_specs.append((query_end, chunk, chunk_targets))
    if len(query_specs) > MAX_QUERY_COUNT:
        raise ResourceBoundError("awc_gap_query_count_exceeded")

    query_receipts: list[dict[str, Any]] = []
    cumulative_bytes = 0
    newly_retained_count = 0
    batch_started = time.monotonic()
    last_query_started: float | None = None
    for query_end, stations, group_targets in query_specs:
        if time.monotonic() - batch_started >= max_batch_seconds:
            for target in group_targets:
                terminals[str(target["target_id"])] = _terminal(
                    target,
                    state="BLOCKED",
                    subtype="BATCH_BUDGET_EXCEEDED",
                    retryable=True,
                )
            continue
        if last_query_started is not None:
            wait_seconds = min_query_interval_seconds - (
                time.monotonic() - last_query_started
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        assert_process_memory_budget(projected_bytes=max_response_bytes * 3)
        url = _build_url(stations, query_end)
        requested = _utc_text(datetime.now(timezone.utc))
        query_identity = {
            "query_end_utc": query_end,
            "stations": stations,
            "url": url,
            "requested_at_utc": requested,
        }
        query_id = content_sha256(query_identity)
        last_query_started = time.monotonic()
        try:
            status, body, headers = transport(
                url, timeout_seconds, max_response_bytes, proxy_url
            )
            received = _utc_text(datetime.now(timezone.utc))
            cumulative_bytes += len(body)
            if cumulative_bytes > max_cumulative_response_bytes:
                raise ResourceBoundError("awc_gap_cumulative_response_bytes_exceeded")
            body_sha = hashlib.sha256(body).hexdigest()
            rows = _decode_rows(body) if status == 200 else []
            query_receipts.append(
                {
                    "query_id": query_id,
                    **query_identity,
                    "received_at_utc": received,
                    "http_status": int(status),
                    "response_bytes": len(body),
                    "response_body_sha256": body_sha,
                    "response_date_header": str(headers.get("Date") or ""),
                    "returned_row_count": len(rows),
                    "error_subtype": None,
                }
            )
        except ResourceBoundError as exc:
            received = _utc_text(datetime.now(timezone.utc))
            query_receipts.append(
                {
                    "query_id": query_id,
                    **query_identity,
                    "received_at_utc": received,
                    "http_status": None,
                    "response_bytes": 0,
                    "response_body_sha256": None,
                    "response_date_header": "",
                    "returned_row_count": 0,
                    "error_subtype": str(exc),
                }
            )
            for target in group_targets:
                terminals[str(target["target_id"])] = _terminal(
                    target,
                    state="BLOCKED",
                    subtype="RESOURCE_GUARD_BLOCKED",
                    query_id=query_id,
                )
            continue
        except Exception as exc:  # transport/network failure becomes durable evidence
            received = _utc_text(datetime.now(timezone.utc))
            subtype = f"{type(exc).__name__}:{str(exc)[:160]}"
            query_receipts.append(
                {
                    "query_id": query_id,
                    **query_identity,
                    "received_at_utc": received,
                    "http_status": None,
                    "response_bytes": 0,
                    "response_body_sha256": None,
                    "response_date_header": "",
                    "returned_row_count": 0,
                    "error_subtype": subtype,
                }
            )
            for target in group_targets:
                terminals[str(target["target_id"])] = _terminal(
                    target,
                    state="BLOCKED",
                    subtype="HTTP_OR_NETWORK_FAILURE",
                    query_id=query_id,
                    retryable=True,
                )
            continue

        rows_by_key: dict[tuple[str, str], list[tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
        if status == 200:
            for row in rows:
                station = _row_station(row)
                valid_at = _parse_row_time(row)
                if not station or valid_at is None or _row_report_type(row) not in {"METAR", "SPECI"}:
                    continue
                rows_by_key[(station, _hour_text(valid_at))].append((valid_at, row))
        for target in group_targets:
            target_id = str(target["target_id"])
            if status in {204, 404}:
                terminals[target_id] = _terminal(
                    target,
                    state="DATA_MISSING",
                    subtype="OFFICIAL_PRODUCT_NOT_FOUND",
                    query_id=query_id,
                )
                continue
            if status != 200:
                terminals[target_id] = _terminal(
                    target,
                    state="BLOCKED",
                    subtype="HTTP_OR_NETWORK_FAILURE",
                    query_id=query_id,
                    retryable=status >= 500 or status in {408, 429},
                )
                continue
            matches = rows_by_key.get(
                (
                    str(target["station_id"]),
                    _hour_text(target["expected_valid_at_utc"]),
                ), []
            )
            raw_candidates: dict[tuple[str, str], dict[str, Any]] = {}
            for valid_at, row in matches:
                try:
                    raw = _canonical_raw_object(
                        row,
                        station_id=str(target["station_id"]),
                        valid_at=valid_at,
                        source_url=url,
                        source_poll_id=f"awc-historical-pr02c-{query_receipts[-1]['response_body_sha256']}",
                        received_at_utc=str(query_receipts[-1]["received_at_utc"]),
                    )
                except HistoricalAwcGapError:
                    continue
                raw_candidates[(str(raw["valid_at_utc"]), str(raw["raw_sha256"]))] = raw
            hashes_by_valid = defaultdict(set)
            for valid_at, raw_sha in raw_candidates:
                hashes_by_valid[valid_at].add(raw_sha)
            hashes = sorted({raw_sha for _valid_at, raw_sha in raw_candidates})
            if not hashes:
                terminals[target_id] = _terminal(
                    target,
                    state="DATA_MISSING",
                    subtype="HOUR_COVERAGE_MISSING_FROM_OFFICIAL_RESPONSE",
                    query_id=query_id,
                )
                continue
            for raw in raw_candidates.values():
                _path, created = _persist_raw_object(root, raw)
                newly_retained_count += int(created)
            if any(len(values) > 1 for values in hashes_by_valid.values()):
                terminals[target_id] = _terminal(
                    target,
                    state="BLOCKED",
                    subtype="CORRECTION_REVISION_REVIEW_REQUIRED",
                    query_id=query_id,
                    raw_sha256s=hashes,
                )
                continue
            for raw in raw_candidates.values():
                raw_sha = str(raw["raw_sha256"])
                observations[(str(raw["station_id"]), str(raw["valid_at_utc"]), raw_sha)] = _observation_row(raw)
            terminals[target_id] = _terminal(
                target,
                state="FETCHED_OFFICIAL_RAW",
                subtype="OFFICIAL_HOUR_COVERAGE_RETAINED",
                query_id=query_id,
                raw_sha256s=hashes,
            )

    missing_terminal_ids = sorted(
        set(str(item["target_id"]) for item in targets) - set(terminals)
    )
    if missing_terminal_ids:
        raise HistoricalAwcGapError("awc_gap_terminal_coverage_incomplete")
    terminal_rows = [terminals[str(item["target_id"])] for item in targets]
    state_counts: dict[str, int] = defaultdict(int)
    subtype_counts: dict[str, int] = defaultdict(int)
    for row in terminal_rows:
        state_counts[str(row["state"])] += 1
        subtype_counts[str(row["subtype"])] += 1
    observation_identity = {
        "schema": LEGACY_OBSERVATION_SCHEMA,
        "target_timezone": "PER_STATION_CANONICAL",
        "historical_only": True,
        "canonical_ingress": True,
        "generated_at_utc": requested_at_utc,
        "observations": sorted(
            observations.values(),
            key=lambda row: (
                row["station_id"], row["valid_at_utc"], row["report_type"], row["raw_sha256"]
            ),
        ),
        "observation_count": len(observations),
        "boundary": {
            "production_no_live": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_arrival_evidence": False,
        },
    }
    observation_artifact = {
        **observation_identity,
        "evidence_sha256": content_sha256(observation_identity),
    }
    receipt_identity = {
        "schema": RECEIPT_SCHEMA,
        "requested_at_utc": requested_at_utc,
        "inventory_content_sha256": str(inventory["content_sha256"]),
        "prior_receipt_content_sha256": (
            str(verified_prior["content_sha256"])
            if verified_prior is not None
            else None
        ),
        "inventory_target_count": len(targets),
        "terminal_count": len(terminal_rows),
        "terminal_state_counts": dict(sorted(state_counts.items())),
        "terminal_subtype_counts": dict(sorted(subtype_counts.items())),
        "terminals": terminal_rows,
        "queries": query_receipts,
        "query_count": len(query_receipts),
        "response_bytes_total": cumulative_bytes,
        "newly_retained_raw_count": newly_retained_count,
        "observation_count": len(observations),
        "observation_artifact_evidence_sha256": observation_artifact["evidence_sha256"],
        "raw_root": str(root),
        "resource_guard": {
            "proxy_url": proxy_url,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "max_cumulative_response_bytes": max_cumulative_response_bytes,
            "min_query_interval_seconds": min_query_interval_seconds,
            "max_batch_seconds": max_batch_seconds,
            "retained_bytes_before": retained_bytes_before,
            "minimum_free_bytes": 512 * 1024 * 1024,
            "process_rss_guard": True,
        },
        "boundary": {
            "production_no_live": True,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "creates_truth_residual_artifact_activation_or_p0": False,
        },
    }
    receipt = {**receipt_identity, "content_sha256": content_sha256(receipt_identity)}
    return receipt, observation_artifact


def write_gap_fetch_bundle(
    *,
    receipt: Mapping[str, Any],
    observation_artifact: Mapping[str, Any],
    receipt_output: Path | str,
    observation_output: Path | str,
) -> None:
    write_bounded_json(receipt_output, receipt, limit=MAX_JSON_OUTPUT_BYTES)
    write_bounded_json(observation_output, observation_artifact, limit=MAX_JSON_OUTPUT_BYTES)


__all__ = [
    "AWC_RETENTION_HOURS",
    "DEFAULT_PROXY_URL",
    "ENDPOINT",
    "HistoricalAwcGapError",
    "INVENTORY_SCHEMA",
    "MAX_CUMULATIVE_RESPONSE_BYTES",
    "MAX_RESPONSE_BYTES",
    "RECEIPT_SCHEMA",
    "build_gap_inventory",
    "build_gap_inventory_from_receipt",
    "execute_gap_fetch",
    "load_truth_gap_bundle",
    "official_awc_transport",
    "verify_gap_inventory",
    "verify_gap_fetch_receipt",
    "write_gap_fetch_bundle",
]
