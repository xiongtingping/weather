"""Pure AMSC/AWC canonical-event replay contract.

This is the M2 replay boundary before a canonical observation is allowed to
feed M3 materialization.  It handles source routing, duplicate identity,
strict sequence/time monotonicity, and correction halts without reading raw
weather fields or writing SQLite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_canonical_event_replay_v1"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed


def _normalize(raw: Mapping[str, Any], *, station_id: str, source_lane: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError("canonical_event_mapping_required")
    event = {
        "observation_id": _text("canonical_event_observation_id", raw.get("observation_id")),
        "station_id": _text("canonical_event_station_id", raw.get("station_id")),
        "source_lane": _text("canonical_event_source_lane", raw.get("source_lane")).upper(),
        "report_type": _text("canonical_event_report_type", raw.get("report_type")).upper(),
        "event_sequence": raw.get("event_sequence"),
        "observed_at_utc": _text("canonical_event_observed_at_utc", raw.get("observed_at_utc")),
        "raw_sha256": _text("canonical_event_raw_sha256", raw.get("raw_sha256")).lower(),
        "correction_suspected": raw.get("correction_suspected", False),
    }
    if event["station_id"] != station_id:
        raise ContractError("canonical_event_station_scope_mismatch")
    if event["source_lane"] != source_lane:
        raise ContractError("canonical_event_source_route_mismatch")
    if event["source_lane"] not in {"AMSC", "AWC"}:
        raise ContractError("canonical_event_source_lane_invalid")
    if event["report_type"] not in {"METAR", "SPECI"}:
        raise ContractError("canonical_event_report_type_invalid")
    try:
        sequence = int(event["event_sequence"])
    except (TypeError, ValueError) as exc:
        raise ContractError("canonical_event_sequence_invalid") from exc
    if sequence < 0:
        raise ContractError("canonical_event_sequence_negative")
    if len(event["raw_sha256"]) != 64 or any(char not in "0123456789abcdef" for char in event["raw_sha256"]):
        raise ContractError("canonical_event_raw_sha256_invalid")
    if not isinstance(event["correction_suspected"], bool):
        raise ContractError("canonical_event_correction_flag_invalid")
    _timestamp("canonical_event_observed_at_utc", event["observed_at_utc"])
    event["event_sequence"] = sequence
    event["content_sha256"] = content_sha256({"schema": SCHEMA, **event})
    return event


def replay_canonical_events(
    events: Sequence[Mapping[str, Any]],
    *,
    station_id: str,
    source_lane: str,
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay events exactly once and fail closed on rewind or correction."""

    station = _text("canonical_replay_station_id", station_id)
    lane = _text("canonical_replay_source_lane", source_lane).upper()
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
        raise ContractError("canonical_replay_events_list_required")
    prior = dict(initial_state or {})
    last_sequence = int(prior.get("last_sequence", -1))
    last_observed = _timestamp("canonical_replay_initial_observed_at", prior["last_observed_at_utc"]) if prior.get("last_observed_at_utc") else None
    seen = {str(key): str(value) for key, value in (prior.get("seen_event_ids") or {}).items()}
    accepted: list[dict[str, Any]] = []
    duplicates: list[str] = []
    halted = bool(prior.get("halted", False))
    for raw in events:
        event = _normalize(raw, station_id=station, source_lane=lane)
        event_id = event["observation_id"]
        known = seen.get(event_id)
        if known is not None:
            if known != event["content_sha256"]:
                raise ContractError("canonical_event_id_content_conflict")
            duplicates.append(event_id)
            continue
        if halted:
            raise ContractError("canonical_event_after_correction_halted")
        if event["event_sequence"] <= last_sequence:
            raise ContractError("canonical_event_sequence_rewind")
        observed = _timestamp("canonical_event_observed_at_utc", event["observed_at_utc"])
        if last_observed is not None and observed < last_observed:
            raise ContractError("canonical_event_timestamp_rewind")
        seen[event_id] = event["content_sha256"]
        accepted.append(event)
        last_sequence = event["event_sequence"]
        last_observed = observed
        if event["correction_suspected"]:
            halted = True

    core = {
        "schema": SCHEMA,
        "station_id": station,
        "source_lane": lane,
        "accepted_event_ids": [row["observation_id"] for row in accepted],
        "duplicate_event_ids": duplicates,
        "last_sequence": last_sequence,
        "last_observed_at_utc": last_observed.isoformat().replace("+00:00", "Z") if last_observed else None,
        "seen_event_ids": dict(sorted(seen.items())),
        "halted": halted,
        "halt_reason": "correction_or_revision_requires_review" if halted else None,
        "accepted_content_shas": [row["content_sha256"] for row in accepted],
        "boundary": {
            "probability_consumed": False,
            "weather_payload_consumed": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    return {**core, "replay_sha256": content_sha256(core)}


__all__ = ["SCHEMA", "replay_canonical_events"]
