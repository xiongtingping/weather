"""Pure canonicalization of transport observations for sequential Bayes shadow.

It produces an append-only event/relation proposal.  Persistence, posterior
updates, and orders deliberately belong to later owners.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from lineage_identity_runtime import (
    build_canonical_observation_event_id,
    canonical_observation_key_from_parts,
)
from source_observation_runtime import (
    PRIMARY_METAR_SOURCE_ID,
    REFERENCE_METAR_SOURCE_IDS,
    SOURCE_CLASSES,
)


SCHEMA_VERSION = "canonical_observation_event_v1"
DEFAULT_SELECTION_POLICY_VERSION = "amsc_primary_awc_confirmation_substitute_v1"
_ALLOWED_REPORT_TYPES = {"METAR", "SPECI"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_time(value: Any) -> datetime | None:
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


def _temporal_identity_reason(*, valid_time: str, transport_seen_at: str, actionable_seen_at: str, decision_as_of: str) -> str:
    """Validate the causal timestamps before an observation becomes canonical.

    This deliberately does not invent an upstream publication-delay ceiling:
    T0→T1 is a separately measured source-discovery SLI.  It does reject any
    event whose valid/transport/actionable clocks cannot establish a causal
    strict-as-of identity.
    """

    valid = _parse_time(valid_time)
    transport = _parse_time(transport_seen_at)
    actionable = _parse_time(actionable_seen_at)
    cutoff = _parse_time(decision_as_of)
    if valid is None:
        return "valid_time_missing_or_invalid"
    if transport is None:
        return "first_transport_seen_at_missing_or_invalid"
    if actionable is None:
        return "first_actionable_seen_at_missing_or_invalid"
    if cutoff is None:
        return "decision_as_of_missing_or_invalid"
    if valid > transport:
        return "valid_time_after_first_transport"
    if valid > actionable:
        return "valid_time_after_first_actionable"
    if transport > actionable:
        return "first_transport_seen_at_after_first_actionable"
    if actionable > cutoff:
        return "strict_as_of_future"
    return ""


def _report_type(row: Mapping[str, Any]) -> str:
    payload = _mapping(row.get("payload"))
    explicit = _text(row.get("report_type") or payload.get("report_type")).upper()
    # Some early normalized AMSC rows predate the explicit ``report_type``
    # payload field.  The immutable report text still carries the identity.
    # Never silently collapse an archived SPECI into a METAR merely because
    # that optional normalization field was absent.
    raw_text = _text(row.get("raw_ref") or payload.get("raw_text") or payload.get("raw_ref"))
    inferred = raw_text.split(None, 1)[0].upper() if raw_text else ""
    if inferred in _ALLOWED_REPORT_TYPES:
        return inferred
    return explicit or "METAR"


def _seen_at(row: Mapping[str, Any]) -> str:
    return _text(
        row.get("first_actionable_seen_at")
        or row.get("response_completed_at")
        or row.get("fetched_at")
        or row.get("parsed_at")
    )


def _first_transport_seen_at(row: Mapping[str, Any]) -> str:
    return _text(
        row.get("first_transport_seen_at")
        or row.get("response_completed_at")
        or row.get("fetched_at")
        or row.get("parsed_at")
    )


def _normalized_physical(row: Mapping[str, Any], *, report_type: str) -> dict[str, Any]:
    payload = _mapping(row.get("payload"))
    def pick(*keys: str) -> Any:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return value
            value = payload.get(key)
            if value not in (None, ""):
                return value
        return None

    return {
        "station_id": _text(row.get("station_id")).upper(),
        "report_type": report_type,
        "valid_time": _text(row.get("valid_time")),
        "temperature_c": pick("temperature_c", "temp_c", "metar_temperature_c"),
        "dewpoint_c": pick("dewpoint_c", "metar_dewpoint_c"),
        "wind_direction_deg": pick("wind_direction_deg", "wind_dir_deg"),
        "wind_speed_kt": pick("wind_speed_kt", "wind_kt"),
        "visibility_m": pick("visibility_m"),
        "cloud_text": _text(pick("cloud_text", "clouds")),
        "weather_text": _text(pick("weather_text", "weather")),
        "pressure_hpa": pick("pressure_hpa", "altimeter_hpa"),
    }


def _physical_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_hard_candidate(row: Mapping[str, Any]) -> bool:
    return (
        _text(row.get("source_class")) == SOURCE_CLASSES["hard_observation"]
        and bool(row.get("hard_eligible"))
        and _text(row.get("source_id")) in {PRIMARY_METAR_SOURCE_ID, *REFERENCE_METAR_SOURCE_IDS}
    )


def _source_priority(row: Mapping[str, Any]) -> tuple[int, str, str]:
    source_id = _text(row.get("source_id"))
    rank = 0 if source_id == PRIMARY_METAR_SOURCE_ID else 1
    return (rank, _seen_at(row), _text(row.get("source_observation_id")))


def _revision_authorized(row: Mapping[str, Any]) -> bool:
    payload = _mapping(row.get("payload"))
    return bool(
        row.get("is_correction")
        or row.get("cor")
        or payload.get("is_correction")
        or payload.get("cor")
        or payload.get("correction_of")
    )


def _existing_by_physical(existing_events: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for event in existing_events:
        if not isinstance(event, Mapping):
            continue
        key = _text(event.get("canonical_observation_key"))
        physical = _text(event.get("normalized_physical_sha256"))
        event_id = _text(event.get("canonical_observation_event_id"))
        if key and physical and event_id:
            result[(key, physical)] = event
    return result


def _existing_by_key(existing_events: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in existing_events:
        if not isinstance(event, Mapping):
            continue
        key = _text(event.get("canonical_observation_key"))
        if key:
            result[key].append(event)
    return result


def canonicalize_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    target_identity: str,
    event_date: str,
    decision_as_of: str,
    allow_awc_substitute: bool = False,
    selection_policy_version: str = DEFAULT_SELECTION_POLICY_VERSION,
    existing_events: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Propose canonical events/relation rows without writes or posterior math."""

    target = _text(target_identity)
    event_day = _text(event_date)[:10]
    rejected: list[dict[str, str]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in observations:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        source_observation_id = _text(row.get("source_observation_id"))
        source_event_id = _text(row.get("source_event_id"))
        source_id = _text(row.get("source_id"))
        if not _is_hard_candidate(row):
            rejected.append({"source_observation_id": source_observation_id, "reason": "not_hard_observation_candidate"})
            continue
        if _text(row.get("event_date"))[:10] != event_day:
            rejected.append({"source_observation_id": source_observation_id, "reason": "event_date_mismatch"})
            continue
        report_type = _report_type(row)
        if report_type not in _ALLOWED_REPORT_TYPES:
            rejected.append({"source_observation_id": source_observation_id, "reason": "report_type_unsupported"})
            continue
        valid_time = _text(row.get("valid_time"))
        seen_at = _seen_at(row)
        temporal_reason = _temporal_identity_reason(
            valid_time=valid_time,
            transport_seen_at=_first_transport_seen_at(row),
            actionable_seen_at=seen_at,
            decision_as_of=decision_as_of,
        )
        if temporal_reason:
            rejected.append({"source_observation_id": source_observation_id, "reason": temporal_reason})
            continue
        if not source_observation_id or not source_event_id or not source_id or not _text(row.get("station_id")):
            rejected.append({"source_observation_id": source_observation_id, "reason": "lineage_or_station_missing"})
            continue
        physical = _normalized_physical(row, report_type=report_type)
        physical_sha = _physical_sha256(physical)
        canonical_key = canonical_observation_key_from_parts(
            target_identity=target,
            station_id=_text(row.get("station_id")),
            report_type=report_type,
            valid_time=valid_time,
        )
        grouped[canonical_key].append(
            {
                "row": row,
                "physical": physical,
                "physical_sha": physical_sha,
                "report_type": report_type,
                "canonical_key": canonical_key,
            }
        )

    existing_events = list(existing_events)
    existing = _existing_by_physical(existing_events)
    existing_by_key = _existing_by_key(existing_events)
    events: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for canonical_key in sorted(grouped):
        physical_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in grouped[canonical_key]:
            physical_groups[candidate["physical_sha"]].append(candidate)
        ordered_groups = sorted(
            physical_groups.values(),
            key=lambda values: min(_source_priority(item["row"]) for item in values),
        )
        known_events = existing_by_key.get(canonical_key, [])
        existing_revision_max = max(
            (int(event.get("revision_no") or 0) for event in known_events),
            default=-1,
        )
        existing_accepted = sorted(
            (
                event
                for event in known_events
                if _text(event.get("state")).startswith("accepted")
            ),
            key=lambda event: (int(event.get("revision_no") or 0), _text(event.get("accepted_at"))),
        )
        selected_physical_sha = _text(existing_accepted[-1].get("normalized_physical_sha256")) if existing_accepted else ""
        selected_event_id = _text(existing_accepted[-1].get("canonical_observation_event_id")) if existing_accepted else ""
        next_revision_no = existing_revision_max + 1
        for values in ordered_groups:
            values.sort(key=lambda item: _source_priority(item["row"]))
            preferred = values[0]
            row = preferred["row"]
            physical_sha = preferred["physical_sha"]
            existing_event = existing.get((canonical_key, physical_sha))
            if existing_event:
                existing_id = _text(existing_event.get("canonical_observation_event_id"))
                for item in values:
                    relation = "confirmation" if _text(item["row"].get("source_id")) != _text(existing_event.get("selected_source_id")) else "selected"
                    relations.append(
                        {
                            "canonical_observation_event_id": existing_id,
                            "source_observation_id": _text(item["row"].get("source_observation_id")),
                            "source_event_id": _text(item["row"].get("source_event_id")),
                            "source_id": _text(item["row"].get("source_id")),
                            "relation": relation,
                            "seen_at": _seen_at(item["row"]),
                            "raw_sha256": _text(item["row"].get("raw_sha256")),
                        }
                    )
                if _text(existing_event.get("state")).startswith("accepted") and not selected_event_id:
                    selected_physical_sha = physical_sha
                    selected_event_id = existing_id
                continue
            has_primary = any(_text(item["row"].get("source_id")) == PRIMARY_METAR_SOURCE_ID for item in values)
            is_first_physical = not selected_physical_sha
            correction_allowed = any(_revision_authorized(item["row"]) for item in values)
            if has_primary and (is_first_physical or correction_allowed):
                state = "accepted_primary"
                selection_role = "primary"
            elif not has_primary and is_first_physical and allow_awc_substitute:
                state = "accepted_substitute"
                selection_role = "substitute"
            elif not is_first_physical and correction_allowed:
                state = "accepted_primary" if has_primary else "accepted_substitute"
                selection_role = "primary" if has_primary else "substitute"
            elif is_first_physical:
                state = "candidate"
                selection_role = "substitute"
            else:
                state = "conflict_blocked"
                selection_role = "substitute"
            revision_no = next_revision_no
            next_revision_no += 1
            event_id = build_canonical_observation_event_id(
                canonical_observation_key=canonical_key,
                revision_no=revision_no,
                normalized_physical_sha256=physical_sha,
            )
            first_seen = min(_first_transport_seen_at(item["row"]) for item in values)
            first_actionable = min(_seen_at(item["row"]) for item in values)
            event = {
                "schema": SCHEMA_VERSION,
                "canonical_observation_event_id": event_id,
                "canonical_observation_key": canonical_key,
                "target_identity": target,
                "station_id": _text(row.get("station_id")).upper(),
                "report_type": preferred["report_type"],
                "valid_time": _text(row.get("valid_time")),
                "revision_no": revision_no,
                "normalized_physical_sha256": physical_sha,
                "selected_source_observation_id": _text(row.get("source_observation_id")),
                "selected_source_id": _text(row.get("source_id")),
                "selection_role": selection_role,
                "selection_policy_version": _text(selection_policy_version),
                "first_transport_seen_at": first_seen,
                "first_actionable_seen_at": first_actionable,
                "accepted_at": first_actionable,
                "state": state,
                "supersedes_event_id": selected_event_id if correction_allowed and selected_event_id else "",
                "conflict_group_id": canonical_key if state == "conflict_blocked" else "",
                "parsed_observation": preferred["physical"],
                "quality_flags": sorted({str(flag) for item in values for flag in item["row"].get("quality_flags") or []}),
                "created_at": first_actionable,
            }
            events.append(event)
            for item in values:
                relation = "selected" if _text(item["row"].get("source_observation_id")) == event["selected_source_observation_id"] else "confirmation"
                if state == "conflict_blocked":
                    relation = "conflicting"
                relations.append(
                    {
                        "canonical_observation_event_id": event_id,
                        "source_observation_id": _text(item["row"].get("source_observation_id")),
                        "source_event_id": _text(item["row"].get("source_event_id")),
                        "source_id": _text(item["row"].get("source_id")),
                        "relation": relation,
                        "seen_at": _seen_at(item["row"]),
                        "raw_sha256": _text(item["row"].get("raw_sha256")),
                    }
                )
            if state.startswith("accepted"):
                selected_physical_sha = physical_sha
                selected_event_id = event_id

    events.sort(key=lambda event: (event["valid_time"], event["revision_no"], event["canonical_observation_event_id"]))
    return {
        "schema": SCHEMA_VERSION,
        "target_identity": target,
        "event_date": event_day,
        "decision_as_of": _text(decision_as_of),
        "selection_policy_version": _text(selection_policy_version),
        "read_only": True,
        "events": events,
        "relations": relations,
        "rejected": rejected,
        "accepted_event_count": sum(1 for event in events if str(event["state"]).startswith("accepted")),
    }
