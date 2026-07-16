"""Strict-as-of canonical forecast revision selection for v2 weak evidence."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "forecast_revision_canonical_snapshot_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


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


def _sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _first(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def canonicalize_forecast_revisions(
    source_observations: Iterable[Mapping[str, Any]],
    *,
    target_identity: str,
    event_date: str,
    decision_as_of: str,
) -> dict[str, Any]:
    """Select only latest *actually arrived* forecast revision per source.

    ``fetched_at`` / first actionable arrival is the availability clock.  A
    provider-declared availability timestamp can be retained as metadata but
    may never make a revision visible before this system captured it.
    """

    target = _text(target_identity)
    event_day = _text(event_date)[:10]
    cutoff = _parse_time(decision_as_of)
    if not target or not event_day or cutoff is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["target_event_date_or_decision_asof_missing_or_invalid"],
            "read_only": True,
        }
    selected: dict[str, tuple[datetime, str, dict[str, Any]]] = {}
    trace: list[dict[str, Any]] = []
    for raw in source_observations:
        row = dict(raw) if isinstance(raw, Mapping) else {}
        payload = _mapping(row.get("payload"))
        source_id = _text(_first(row, payload, "source_id", "source"))
        revision_id = _text(_first(row, payload, "source_event_id", "source_revision_id"))
        source_observation_id = _text(_first(row, payload, "source_observation_id"))
        source_class = _text(_first(row, payload, "source_class"))
        source_event_day = _text(_first(row, payload, "event_date", "target_event_date"))[:10]
        valid_time = _text(_first(row, payload, "valid_time", "forecast_valid_time", "forecast_peak_at"))
        arrived_at = _text(_first(row, payload, "fetched_at", "first_actionable_seen_at", "response_completed_at"))
        arrived = _parse_time(arrived_at)
        peak = _finite(_first(row, payload, "forecast_peak_c", "peak_c", "value_c"))
        sigma = _finite(_first(row, payload, "forecast_sigma_c", "sigma_c", "std_c"))
        trace_row = {
            "source_id": source_id,
            "source_revision_id": revision_id,
            "source_observation_id": source_observation_id,
            "event_date": source_event_day,
            "valid_time": valid_time,
            "first_actionable_seen_at": arrived_at,
            "included": False,
            "exclusion_reason": "",
        }
        if source_class != "forecast_weak_source":
            trace_row["exclusion_reason"] = "source_class_not_forecast_weak"
            trace.append(trace_row)
            continue
        if not source_id or not revision_id or not source_observation_id:
            trace_row["exclusion_reason"] = "forecast_revision_lineage_missing"
            trace.append(trace_row)
            continue
        if source_event_day != event_day:
            trace_row["exclusion_reason"] = "forecast_revision_event_date_mismatch"
            trace.append(trace_row)
            continue
        if not valid_time:
            trace_row["exclusion_reason"] = "forecast_revision_valid_time_missing"
            trace.append(trace_row)
            continue
        if arrived is None:
            trace_row["exclusion_reason"] = "forecast_revision_first_actionable_seen_at_missing_or_invalid"
            trace.append(trace_row)
            continue
        if arrived > cutoff:
            trace_row["exclusion_reason"] = "forecast_revision_after_asof"
            trace.append(trace_row)
            continue
        if peak is None:
            trace_row["exclusion_reason"] = "forecast_revision_peak_missing_or_invalid"
            trace.append(trace_row)
            continue
        candidate = {
            "source_id": source_id,
            "source_revision_id": revision_id,
            "source_observation_id": source_observation_id,
            "source_class": source_class,
            "event_date": source_event_day,
            "valid_time": valid_time,
            "available_at": arrived_at,
            "forecast_peak_c": peak,
            "forecast_sigma_c": sigma,
            "forecast_value_kind": _text(_first(row, payload, "forecast_value_kind")) or "peak_point",
            "forecast_record_valid_time": _text(_first(row, payload, "forecast_record_valid_time")),
            "trajectory_eligible": _first(row, payload, "trajectory_eligible") is not False,
            "provider_declared_available_at": _text(_first(row, payload, "provider_available_at", "as_of_eligible_at")),
            "raw_sha256": _text(_first(row, payload, "raw_sha256")),
            "raw_ref": _text(_first(row, payload, "raw_ref")),
            "trace": trace_row,
        }
        previous = selected.get(source_id)
        if previous is not None and (arrived < previous[0] or (arrived == previous[0] and revision_id <= previous[1])):
            trace_row["exclusion_reason"] = "forecast_revision_superseded_by_latest_visible"
            trace.append(trace_row)
            continue
        if previous is not None:
            previous[2]["trace"]["included"] = False
            previous[2]["trace"]["exclusion_reason"] = "forecast_revision_superseded_by_latest_visible"
        selected[source_id] = (arrived, revision_id, candidate)
        trace.append(trace_row)
    revisions: list[dict[str, Any]] = []
    for source_id, (_arrived, _revision, candidate) in sorted(selected.items()):
        candidate["trace"]["included"] = True
        revisions.append({key: value for key, value in candidate.items() if key != "trace"})
    identity = {
        "target_identity": target,
        "event_date": event_day,
        "decision_as_of": _text(decision_as_of),
        "source_revisions": revisions,
    }
    return {
        "schema": SCHEMA_VERSION,
        # Zero visible forecasts is a real availability state.  The unique
        # weak model may select its approved neutral ``empty`` surface, so it
        # must not be converted into an upstream posterior-stop condition.
        "ok": True,
        "state": "available",
        "target_identity": target,
        "event_date": event_day,
        "decision_as_of": _text(decision_as_of),
        "forecast_evidence_snapshot_id": f"forev_{_sha256(identity)[:24]}",
        "forecast_evidence_snapshot_sha256": _sha256(identity),
        "source_revisions": revisions,
        "source_revision_ids": [row["source_revision_id"] for row in revisions],
        "source_trace": trace,
        "block_reasons": [],
        "weak_evidence_state": "forecast_revisions_available" if revisions else "no_forecast_revisions_available",
        "boundary": {
            "first_system_arrival_is_availability_clock": True,
            "provider_declared_availability_cannot_backdate_system_visibility": True,
            "latest_visible_revision_per_source": True,
            "empty_forecast_source_set_is_explicit_model_input": True,
            "empty_forecast_source_set_requires_approved_neutral_surface_downstream": True,
            "forecast_only": True,
            "creates_likelihood": False,
            "creates_posterior": False,
            "creates_strategy_signal": False,
        },
        "read_only": True,
    }


__all__ = ["SCHEMA_VERSION", "canonicalize_forecast_revisions"]
