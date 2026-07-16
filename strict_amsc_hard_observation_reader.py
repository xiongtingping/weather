"""Read strict primary AMSC METAR/SPECI evidence without reinterpreting facts.

This is the offline/replay counterpart of the resident AMSC arrival envelope.
It only returns a source observation when the immutable ``source_observations``
row, the raw object and the signed poll manifest all bind to the same physical
report and original T1.  Legacy fact-projection rows and AWC rows are excluded:
they may help diagnosis, but cannot become an independently reconstructed
strong-observation event.

The module is read-only.  In particular it does not create canonical events,
checkpoints, posterior facts, decisions or orders.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from amsc_raw_history_archive import RAW_OBJECT_SCHEMA, raw_text_sha256, validate_amsc_raw_poll_manifest
from source_observation_replay_eligibility_runtime import classify_source_observation_replay_eligibility
from source_observation_runtime import PRIMARY_METAR_SOURCE_ID, SOURCE_CLASSES


SCHEMA_VERSION = "strict_amsc_hard_observation_reader_v1"
AMSC_PARSER_VERSION = "amsc_zuuu_metar_taf_fetcher_v1"
OBSERVED_FIRST_SEEN = "observed_first_seen"
ACCEPTED_REPORT_TYPES = frozenset({"METAR", "SPECI"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _same_time(left: Any, right: Any) -> bool:
    left_time = _parse_time(left)
    right_time = _parse_time(right)
    return left_time is not None and right_time is not None and left_time == right_time


def _float_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-12
    except (TypeError, ValueError):
        return left in (None, "") and right in (None, "")


def _bool(value: Any) -> bool | None:
    if value is True or value in (1, "1", "true", "True"):
        return True
    if value is False or value in (0, "0", "false", "False", ""):
        return False
    return None


def _load_json(value: Any) -> Mapping[str, Any]:
    try:
        decoded = json.loads(_text(value))
    except (TypeError, ValueError):
        return {}
    return _mapping(decoded)


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


def _load_raw_object(root: Path, reference: Any) -> Mapping[str, Any]:
    path = _safe_relative_file(root, reference, suffix=".json.gz")
    if path is None or not path.is_file():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return _mapping(json.load(handle))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _load_manifest(root: Path, reference: Any) -> Mapping[str, Any]:
    path = _safe_relative_file(root, reference, suffix=".json")
    if path is None or not path.is_file():
        return {}
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _table_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(row[1] or "") for row in conn.execute("PRAGMA table_info(source_observations)").fetchall()]


def _report_type(source: Mapping[str, Any], raw_object: Mapping[str, Any]) -> str:
    payload = _mapping(source.get("payload"))
    declared = _text(source.get("report_type") or payload.get("report_type")).upper()
    archived = _text(raw_object.get("report_type")).upper()
    raw_text = _text(raw_object.get("raw_text"))
    inferred = raw_text.split(None, 1)[0].upper() if raw_text else ""
    if declared and declared != archived:
        return ""
    if inferred and inferred != archived:
        return ""
    return archived


def _row_binding_reasons(stored: Mapping[str, Any], source: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field in (
        "source_event_id",
        "source_observation_id",
        "source_id",
        "source_class",
        "role",
        "station_id",
        "event_date",
        "valid_time_local",
        "fetched_at",
        "parsed_at",
        "response_completed_at",
        "raw_ref",
        "raw_sha256",
        "parser_version",
        "demotion_reason",
    ):
        if _text(stored.get(field)) != _text(source.get(field)):
            reasons.append(f"stored_source_{field}_binding_mismatch")
    for field in ("valid_time",):
        if not _same_time(stored.get(field), source.get(field)):
            reasons.append(f"stored_source_{field}_binding_mismatch")
    for field in ("temperature_c", "dewpoint_c", "observed_today_max_c", "forecast_peak_c"):
        if not _float_equal(stored.get(field), source.get(field)):
            reasons.append(f"stored_source_{field}_binding_mismatch")
    if _bool(stored.get("hard_eligible")) is not True or _bool(source.get("hard_eligible")) is not True:
        reasons.append("stored_source_hard_eligibility_invalid")
    return reasons


def _archive_reasons(
    source: Mapping[str, Any],
    *,
    archive_dir: Path,
    require_strict_asof: bool,
) -> tuple[list[str], str]:
    payload = _mapping(source.get("payload"))
    transport = _mapping(payload.get("transport"))
    reasons: list[str] = []
    raw_sha = _text(source.get("raw_sha256"))
    raw_ref = _text(payload.get("raw_object_ref"))
    manifest_ref = _text(transport.get("raw_archive_manifest_ref"))
    manifest_sha = _text(transport.get("raw_archive_manifest_sha256"))
    raw_object = _load_raw_object(archive_dir, raw_ref)
    manifest = _load_manifest(archive_dir, manifest_ref)
    if not raw_object:
        reasons.append("amsc_raw_object_missing_or_invalid")
    if not manifest:
        reasons.append("amsc_raw_manifest_missing_or_invalid")
    if reasons:
        return reasons, ""
    manifest_result = validate_amsc_raw_poll_manifest(manifest)
    if manifest_result.get("ok") is not True:
        reasons.extend(f"amsc_raw_manifest_contract_{item}" for item in manifest_result.get("issues") or [])
    if _text(manifest.get("manifest_sha256")) != manifest_sha:
        reasons.append("amsc_raw_manifest_sha_binding_mismatch")
    archived_type = _report_type(source, raw_object)
    if archived_type not in ACCEPTED_REPORT_TYPES:
        reasons.append("amsc_raw_report_type_invalid_or_mismatched")
    raw_text = _text(raw_object.get("raw_text"))
    if _text(raw_object.get("schema")) != RAW_OBJECT_SCHEMA:
        reasons.append("amsc_raw_object_schema_invalid")
    if raw_text_sha256(raw_text) != raw_sha or _text(raw_object.get("raw_sha256")) != raw_sha:
        reasons.append("amsc_raw_object_hash_binding_mismatch")
    if _text(source.get("raw_ref")) != raw_text:
        reasons.append("amsc_raw_object_text_binding_mismatch")
    if _text(payload.get("raw_text")) != raw_text:
        reasons.append("amsc_payload_raw_text_binding_mismatch")
    if _text(source.get("source_id")) != PRIMARY_METAR_SOURCE_ID or _text(raw_object.get("source_id")) != PRIMARY_METAR_SOURCE_ID:
        reasons.append("amsc_primary_source_binding_mismatch")
    if _text(source.get("station_id")).upper() != "ZUUU" or _text(raw_object.get("station_id")).upper() != "ZUUU":
        reasons.append("amsc_station_binding_mismatch")
    if not _same_time(source.get("valid_time"), raw_object.get("verified_valid_time_utc")):
        reasons.append("amsc_raw_object_valid_time_binding_mismatch")
    first_seen = _text(payload.get("first_seen_at"))
    actionable = _text(payload.get("as_of_eligible_at"))
    if _text(payload.get("arrival_time_quality")) != OBSERVED_FIRST_SEEN:
        reasons.append("amsc_arrival_quality_not_observed_first_seen")
    if not _same_time(first_seen, raw_object.get("first_seen_at")):
        reasons.append("amsc_raw_object_first_seen_binding_mismatch")
    if require_strict_asof and not _same_time(first_seen, actionable):
        reasons.append("amsc_actionable_time_not_bound_to_first_seen")
    valid = _parse_time(source.get("valid_time"))
    first = _parse_time(first_seen)
    ready = _parse_time(actionable)
    if valid is None or first is None or not valid <= first:
        reasons.append("amsc_arrival_time_order_invalid")
    elif require_strict_asof and (ready is None or first > ready):
        reasons.append("amsc_arrival_time_order_invalid")
    object_entry = next(
        (
            _mapping(item)
            for item in manifest.get("raw_objects") or []
            if _text(_mapping(item).get("raw_sha256")) == raw_sha
        ),
        {},
    )
    if raw_sha not in {_text(item) for item in manifest.get("returned_raw_sha256_list") or []}:
        reasons.append("amsc_raw_sha_not_in_manifest")
    if not object_entry:
        reasons.append("amsc_raw_object_not_indexed_by_manifest")
    else:
        if _text(object_entry.get("raw_object_ref")) != raw_ref:
            reasons.append("amsc_manifest_object_ref_binding_mismatch")
        if not _same_time(object_entry.get("first_seen_at"), first_seen):
            reasons.append("amsc_manifest_first_seen_binding_mismatch")
        if _text(object_entry.get("arrival_time_quality")) != OBSERVED_FIRST_SEEN:
            reasons.append("amsc_manifest_arrival_quality_invalid")
        if not _same_time(object_entry.get("verified_valid_time_utc"), source.get("valid_time")):
            reasons.append("amsc_manifest_valid_time_binding_mismatch")
    return sorted(set(reasons)), archived_type


def load_strict_amsc_primary_hard_observations(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    archive_dir: Path | str,
    require_strict_asof: bool = True,
    source_observation_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return independently valid primary reports for one local event date.

    Invalid source rows are quarantined independently.  They cannot turn into
    a canonical event, but one bad raw lineage never fabricates a reason to
    delay another primary report.
    """

    columns = _table_columns(conn)
    required = {
        "source_event_id", "source_observation_id", "source_id", "source_class", "role", "station_id",
        "event_date", "valid_time", "fetched_at", "raw_ref", "raw_sha256", "parser_version",
        "hard_eligible", "observation_json",
    }
    day = _text(event_date)[:10]
    root = Path(archive_dir).expanduser().resolve()
    if not required.issubset(columns) or not root.is_dir():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "source_observations": [],
            "blocked": [],
            "block_reasons": ["source_observation_schema_or_raw_archive_not_ready"],
            "read_only": True,
        }
    selected_ids = None
    if source_observation_ids is not None:
        selected_ids = sorted({
            _text(value) for value in source_observation_ids if _text(value)
        })
    where_ids = ""
    params: list[Any] = [PRIMARY_METAR_SOURCE_ID, SOURCE_CLASSES["hard_observation"], day]
    if selected_ids is not None:
        if not selected_ids:
            rows = []
        else:
            where_ids = " AND source_observation_id IN (" + ",".join("?" for _ in selected_ids) + ")"
            params.extend(selected_ids)
            rows = conn.execute(
                """
        SELECT * FROM source_observations
        WHERE source_id=? AND source_class=? AND event_date=?
        """ + where_ids + """
        ORDER BY valid_time,source_event_id
                """,
                tuple(params),
            ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM source_observations
            WHERE source_id=? AND source_class=? AND event_date=?
            ORDER BY valid_time,source_event_id
            """,
            tuple(params),
        ).fetchall()
    valid: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        stored = dict(zip(columns, row))
        source = dict(_load_json(stored.get("observation_json")))
        reasons = _row_binding_reasons(stored, source)
        replay = classify_source_observation_replay_eligibility(source)
        if require_strict_asof and replay.get("strict_source_lineage_candidate") is not True:
            reasons.append(_text(replay.get("reason")) or "source_replay_eligibility_blocked")
        if _text(source.get("parser_version")) != AMSC_PARSER_VERSION:
            reasons.append("amsc_primary_parser_version_invalid")
        if _text(source.get("source_class")) != SOURCE_CLASSES["hard_observation"]:
            reasons.append("amsc_primary_source_class_invalid")
        if _text(source.get("role")) != "metar_hard_observation_primary":
            reasons.append("amsc_primary_role_invalid")
        archive_reasons, report_type = _archive_reasons(
            source,
            archive_dir=root,
            require_strict_asof=bool(require_strict_asof),
        )
        reasons.extend(archive_reasons)
        if reasons:
            blocked.append(
                {
                    "source_event_id": _text(stored.get("source_event_id")),
                    "source_observation_id": _text(stored.get("source_observation_id")),
                    "block_reasons": sorted(set(reasons)),
                }
            )
            continue
        payload = _mapping(source.get("payload"))
        hydrated = dict(source)
        hydrated["report_type"] = report_type
        hydrated["first_transport_seen_at"] = _text(payload.get("first_seen_at"))
        hydrated["first_actionable_seen_at"] = _text(payload.get("as_of_eligible_at"))
        hydrated["strict_asof_replay_eligible"] = replay.get("strict_source_lineage_candidate") is True
        valid.append(hydrated)
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(valid),
        "state": "available_with_quarantine" if valid and blocked else "available" if valid else "blocked",
        "event_date": day,
        "source_observations": valid,
        "source_observation_count": len(valid),
        "strict_asof_required": bool(require_strict_asof),
        "source_observation_id_filter_applied": selected_ids is not None,
        "requested_source_observation_count": len(selected_ids or []),
        "blocked": blocked,
        "block_reasons": [] if valid else ["strict_amsc_primary_hard_observations_missing_or_invalid"],
        "boundary": {
            "primary_amsc_only": True,
            "awc_not_used_as_primary_or_t1": True,
            "legacy_fact_projection_excluded": True,
            "raw_object_and_manifest_revalidated": True,
            "strict_asof_replay_required": bool(require_strict_asof),
            "read_only": True,
            "creates_canonical_event": False,
            "creates_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "load_strict_amsc_primary_hard_observations"]
