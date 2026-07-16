"""Read one closed ZUUU station day directly from immutable AMSC raw lineage.

The physical daily-high label belongs to the raw archive, not to archive.db.
This reader therefore validates the selected coverage-v2 membership against
raw objects and signed poll manifests, then parses only the METAR temperature
needed by the final-X builder.  It never creates or repairs fact rows.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from amsc_raw_history_archive import (
    POLL_MANIFEST_SCHEMA,
    RAW_OBJECT_SCHEMA,
    raw_text_sha256,
    validate_amsc_daily_coverage_snapshot,
    validate_amsc_raw_poll_manifest,
)
from zuuu_metar_reporting_schedule import validate_regular_metar_slot_coverage


SCHEMA_VERSION = "amsc_primary_raw_coverage_reader_v1"
STATION_TZ = ZoneInfo("Asia/Shanghai")
TEMPERATURE_RE = re.compile(r"(?<!TX)(?<!TN)\b(M?\d{2}|//)/(M?\d{2}|//)\b")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _same_time(left: Any, right: Any) -> bool:
    left_at = _parse_time(left)
    right_at = _parse_time(right)
    return left_at is not None and right_at is not None and left_at == right_at


def _temperature_c(raw_text: str) -> float | None:
    current = re.split(r"\s+(?=(?:BECMG|TEMPO)\b)", raw_text, maxsplit=1)[0]
    match = TEMPERATURE_RE.search(current)
    if not match or match.group(1) == "//":
        return None
    token = match.group(1)
    return -float(token[1:]) if token.startswith("M") else float(token)


def _read_gzip_json(path: Path) -> Mapping[str, Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _valid_manifest_memberships(root: Path) -> dict[str, list[dict[str, Any]]]:
    memberships: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((root / "manifests").glob("**/*.json")) if (root / "manifests").exists() else ():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, Mapping) or manifest.get("schema") != POLL_MANIFEST_SCHEMA:
            continue
        if validate_amsc_raw_poll_manifest(manifest).get("ok") is not True:
            continue
        manifest_sha = _text(manifest.get("manifest_sha256"))
        returned_hashes = {_text(value) for value in manifest.get("returned_raw_sha256_list") or []}
        for item in manifest.get("raw_objects") or []:
            if not isinstance(item, Mapping):
                continue
            raw_sha = _text(item.get("raw_sha256"))
            if raw_sha and raw_sha in returned_hashes:
                memberships.setdefault(raw_sha, []).append({
                    **dict(item),
                    "manifest_ref": str(path.relative_to(root)),
                    "manifest_sha256": manifest_sha,
                })
    return memberships


def load_amsc_primary_raw_coverage_observations(
    archive_dir: str | Path,
    *,
    event_date: str,
    coverage_manifest: Mapping[str, Any],
    late_arrival_tolerance_seconds: int = 5 * 60,
) -> dict[str, Any]:
    """Return exact coverage members after raw-object and manifest revalidation."""

    root = Path(archive_dir).expanduser().resolve()
    day = _text(event_date)[:10]
    hashes = [_text(value) for value in coverage_manifest.get("raw_object_sha256_list") or []]
    reasons: list[str] = []
    blocked: list[dict[str, Any]] = []
    if _text(coverage_manifest.get("event_date")) != day:
        reasons.append("raw_coverage_reader_event_date_mismatch")
    if _text(coverage_manifest.get("station_id")).upper() != "ZUUU":
        reasons.append("raw_coverage_reader_station_mismatch")
    if not root.is_dir() or not hashes or len(hashes) != len(set(hashes)):
        reasons.append("raw_coverage_reader_membership_missing_or_duplicate")
    coverage_contract = validate_amsc_daily_coverage_snapshot(coverage_manifest)
    if coverage_contract.get("ok") is not True:
        reasons.extend(f"raw_coverage_reader:{item}" for item in coverage_contract.get("issues") or [])
    memberships = _valid_manifest_memberships(root) if not reasons else {}
    observations: list[dict[str, Any]] = []
    for raw_sha in hashes:
        path = root / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
        raw_object = _read_gzip_json(path)
        item_reasons: list[str] = []
        raw_text = _text(raw_object.get("raw_text"))
        valid_at = _parse_time(raw_object.get("verified_valid_time_utc"))
        first_seen_at = _parse_time(raw_object.get("first_seen_at"))
        if raw_object.get("schema") != RAW_OBJECT_SCHEMA:
            item_reasons.append("raw_object_schema_invalid")
        if raw_text_sha256(raw_text) != raw_sha or _text(raw_object.get("raw_sha256")) != raw_sha:
            item_reasons.append("raw_object_hash_invalid")
        if _text(raw_object.get("source_id")) != "zuuu_metar_raw_amsc":
            item_reasons.append("raw_object_source_invalid")
        if _text(raw_object.get("station_id")).upper() != "ZUUU":
            item_reasons.append("raw_object_station_invalid")
        if _text(raw_object.get("report_type")).upper() != "METAR":
            item_reasons.append("raw_object_report_type_invalid")
        if valid_at is None or valid_at.astimezone(STATION_TZ).date().isoformat() != day:
            item_reasons.append("raw_object_valid_time_invalid")
        if first_seen_at is None or (valid_at is not None and first_seen_at < valid_at):
            item_reasons.append("raw_object_first_seen_invalid")
        temperature = _temperature_c(raw_text)
        if temperature is None:
            item_reasons.append("raw_object_temperature_missing")
        matching_membership = next(
            (
                entry
                for entry in memberships.get(raw_sha, [])
                if _text(entry.get("raw_object_ref")) == str(path.relative_to(root))
                and _same_time(entry.get("first_seen_at"), raw_object.get("first_seen_at"))
                and _same_time(entry.get("verified_valid_time_utc"), raw_object.get("verified_valid_time_utc"))
                and _text(entry.get("arrival_time_quality")) == "observed_first_seen"
            ),
            None,
        )
        if matching_membership is None:
            item_reasons.append("raw_object_signed_poll_membership_missing")
        if item_reasons:
            blocked.append({"raw_sha256": raw_sha, "block_reasons": sorted(set(item_reasons))})
            continue
        lag_seconds = (first_seen_at - valid_at).total_seconds()
        observations.append({
            "source_id": "zuuu_metar_raw_amsc",
            "role": "metar_hard_observation_primary",
            "report_type": "METAR",
            "station_id": "ZUUU",
            "event_date": day,
            "valid_time": valid_at.isoformat().replace("+00:00", "Z"),
            "temperature_c": temperature,
            "source_observation_id": f"amscraw_{raw_sha[:32]}",
            "raw_sha256": raw_sha,
            "raw_object_ref": str(path.relative_to(root)),
            "raw_manifest_ref": _text(matching_membership.get("manifest_ref")),
            "raw_manifest_sha256": _text(matching_membership.get("manifest_sha256")),
            "first_transport_seen_at": first_seen_at.isoformat().replace("+00:00", "Z"),
            "strict_asof_replay_eligible": 0 <= lag_seconds <= max(0, int(late_arrival_tolerance_seconds)),
        })
    observations.sort(key=lambda row: (row["valid_time"], row["raw_sha256"]))
    slot_coverage = validate_regular_metar_slot_coverage(
        event_date=day,
        observed_valid_times_utc=[row["valid_time"] for row in observations],
    )
    if set(row["raw_sha256"] for row in observations) != set(hashes):
        reasons.append("raw_coverage_reader_membership_incomplete")
    if slot_coverage.get("complete") is not True:
        reasons.append("raw_coverage_reader_schedule_incomplete")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not reasons and not blocked,
        "state": "available" if not reasons and not blocked else "blocked",
        "event_date": day,
        "source_observations": observations,
        "source_observation_count": len(observations),
        "blocked": blocked,
        "block_reasons": sorted(set(reasons)),
        "boundary": {
            "coverage_v2_membership_only": True,
            "raw_objects_revalidated": True,
            "signed_poll_membership_revalidated": True,
            "reads_archive_db": False,
            "reads_legacy_coverage": False,
            "writes_facts": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "load_amsc_primary_raw_coverage_observations"]
