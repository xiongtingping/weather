"""Immutable arrival contract for the isolated AMSC/SPECI resident lane.

The old half-hour poll result is a *batch of observations*, not an arrival
event. Re-reading an older raw METAR must never manufacture a new T1. This
module therefore accepts exactly one already parsed primary AMSC observation
and derives dispatch timestamps only from its persisted first-seen evidence.
It owns no clock, I/O, queue, or database connection.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from source_observation_runtime import (
    PRIMARY_METAR_SOURCE_ID,
    SCHEMA_VERSION as SOURCE_OBSERVATION_SCHEMA_VERSION,
    SOURCE_CLASSES,
)


SCHEMA_VERSION = "amsc_specli_resident_arrival_envelope_v1"
ACCEPTED_REPORT_TYPES = frozenset({"METAR", "SPECI"})
OBSERVED_FIRST_SEEN_QUALITY = "observed_first_seen"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _base_result(source_observation: Mapping[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(_mapping(source_observation))
    payload = _mapping(source.get("payload"))
    return {
        "schema": SCHEMA_VERSION,
        "source_observation": source,
        "source_observation_id": _text(source.get("source_observation_id")),
        "source_event_id": _text(source.get("source_event_id")),
        "source_id": _text(source.get("source_id")),
        "report_type": _text(payload.get("report_type") or source.get("report_type")).upper(),
        "boundary": {
            "single_received_observation_only": True,
            "poll_result_batch_is_not_an_arrival": True,
            "t1_from_persisted_first_seen_only": True,
            "re_fetch_must_not_reset_t1": True,
            "network_acquisition": False,
            "generic_task_queue": False,
            "subprocess_per_event": False,
            "shadow_only": True,
        },
    }


def build_amsc_specli_resident_arrival_envelope(
    source_observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one immutable host-to-ingress envelope without fabricating time.

    ``first_seen_at`` is the only source for transport receipt and T1.
    ``response_completed_at`` records a retrieval attempt, which can be later
    when an old row is re-fetched; it is never allowed to reset T1.
    """

    out = _base_result(source_observation)
    source = _mapping(out["source_observation"])
    payload = _mapping(source.get("payload"))
    blockers: list[str] = []

    if _text(source.get("schema")) != SOURCE_OBSERVATION_SCHEMA_VERSION:
        blockers.append("amsc_resident_arrival_source_schema_invalid")
    if _text(source.get("source_id")) != PRIMARY_METAR_SOURCE_ID:
        blockers.append("amsc_resident_arrival_primary_source_required")
    if _text(source.get("source_class")) != SOURCE_CLASSES["hard_observation"]:
        blockers.append("amsc_resident_arrival_hard_observation_class_required")
    if _text(source.get("role")) != "metar_hard_observation_primary":
        blockers.append("amsc_resident_arrival_primary_role_required")
    if _text(source.get("station_id")).upper() != "ZUUU":
        blockers.append("amsc_resident_arrival_station_required")
    if out["report_type"] not in ACCEPTED_REPORT_TYPES:
        blockers.append("amsc_resident_arrival_report_type_invalid")
    for field in ("source_observation_id", "source_event_id", "raw_sha256"):
        if not _text(source.get(field)):
            blockers.append(f"amsc_resident_arrival_{field}_missing")

    first_seen_at = _text(payload.get("first_seen_at"))
    as_of_eligible_at = _text(payload.get("as_of_eligible_at"))
    response_completed_at = _text(source.get("response_completed_at") or source.get("fetched_at"))
    if _text(payload.get("arrival_time_quality")) != OBSERVED_FIRST_SEEN_QUALITY:
        blockers.append("amsc_resident_arrival_first_seen_not_observed")

    parsed = {
        "valid_time": _parse_dt(source.get("valid_time")),
        "first_seen_at": _parse_dt(first_seen_at),
        "as_of_eligible_at": _parse_dt(as_of_eligible_at),
        "response_completed_at": _parse_dt(response_completed_at),
    }
    for field, value in parsed.items():
        if value is None:
            blockers.append(f"amsc_resident_arrival_{field}_missing_or_invalid")
    if all(parsed[field] is not None for field in ("valid_time", "first_seen_at", "as_of_eligible_at")):
        if not parsed["valid_time"] <= parsed["first_seen_at"] <= parsed["as_of_eligible_at"]:
            blockers.append("amsc_resident_arrival_causal_time_order_invalid")

    dispatch_source = copy.deepcopy(source)
    # This explicit field wins at ingress. A repeat fetch may have a later
    # response_completed_at than the original physical observation's T1.
    dispatch_source["first_transport_seen_at"] = first_seen_at
    dispatch_source["resident_arrival_envelope_schema"] = SCHEMA_VERSION
    out.update(
        {
            "ok": not blockers,
            "state": "ready" if not blockers else "blocked",
            "blockers": sorted(set(blockers)),
            "first_transport_seen_at": first_seen_at,
            "first_actionable_seen_at": first_seen_at,
            "as_of_eligible_at": as_of_eligible_at,
            "response_completed_at": response_completed_at,
            "dispatch_source_observation": dispatch_source,
            "timestamp_provenance": {
                "first_transport_seen_at": "source_observation.payload.first_seen_at",
                "first_actionable_seen_at": "source_observation.payload.first_seen_at",
                "as_of_eligible_at": "source_observation.payload.as_of_eligible_at",
                "response_completed_at": "source_observation.response_completed_at",
                "response_completed_at_resets_t1": False,
            },
        }
    )
    return out


def validate_amsc_specli_resident_arrival_envelope(
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild and compare an envelope before dispatching it.

    The ingress consumes recomputed source/timestamps, never caller-owned
    copies. A host therefore cannot alter T1 or source identity in transit.
    """

    supplied = _mapping(envelope)
    expected = build_amsc_specli_resident_arrival_envelope(
        _mapping(supplied.get("source_observation"))
    )
    blockers = list(expected.get("blockers") or [])
    if _text(supplied.get("schema")) != SCHEMA_VERSION:
        blockers.append("amsc_resident_arrival_envelope_schema_invalid")
    for field in (
        "source_observation_id",
        "source_event_id",
        "source_id",
        "report_type",
        "first_transport_seen_at",
        "first_actionable_seen_at",
        "as_of_eligible_at",
    ):
        if _text(supplied.get(field)) != _text(expected.get(field)):
            blockers.append(f"amsc_resident_arrival_envelope_{field}_mismatch")
    supplied_dispatch = _mapping(supplied.get("dispatch_source_observation"))
    expected_dispatch = _mapping(expected.get("dispatch_source_observation"))
    if _text(supplied_dispatch.get("source_observation_id")) != _text(expected_dispatch.get("source_observation_id")):
        blockers.append("amsc_resident_arrival_envelope_dispatch_source_mismatch")
    if _text(supplied_dispatch.get("first_transport_seen_at")) != _text(expected_dispatch.get("first_transport_seen_at")):
        blockers.append("amsc_resident_arrival_envelope_dispatch_transport_time_mismatch")
    expected["blockers"] = sorted(set(blockers))
    expected["ok"] = not expected["blockers"]
    expected["state"] = "ready" if expected["ok"] else "blocked"
    return expected


__all__ = [
    "ACCEPTED_REPORT_TYPES",
    "OBSERVED_FIRST_SEEN_QUALITY",
    "SCHEMA_VERSION",
    "build_amsc_specli_resident_arrival_envelope",
    "validate_amsc_specli_resident_arrival_envelope",
]
