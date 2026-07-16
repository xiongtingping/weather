"""Deterministic, canonical-observation trajectory features for strong evidence.

The owner derives temperature interval slopes and bidirectional path features
from accepted canonical events only.  It is not a probability model, strategy,
or order signal; a trained strong-observation artifact may consume it once.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "temperature_trajectory_feature_state_v1"
OWNER_ID = "canonical_temperature_trajectory_owner_v1"
WINDOW_MINUTES = (30, 60, 120, 180)
CB_CLOUD_GROUP_RE = re.compile(r"(?:^|\s)(?:FEW|SCT|BKN|OVC)\d{3}CB(?:\s|$)", re.IGNORECASE)


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
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cloud_text_contains_cb(value: Any) -> bool:
    """Detect an explicit METAR CB cloud group without conflating TCU."""

    return bool(CB_CLOUD_GROUP_RE.search(_text(value).upper()))


def _first_cb_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for index, row in enumerate(rows):
        cloud_text = _mapping(row.get("physical_evidence")).get("cloud_text")
        if not cloud_text_contains_cb(cloud_text):
            continue
        first_temperature = float(row["temperature_c"])
        observed_peak = max(float(item["temperature_c"]) for item in rows[: index + 1])
        return {
            "cb_seen": True,
            "first_cb_canonical_observation_event_id": row["canonical_observation_event_id"],
            "first_cb_valid_time": row["valid_time"].isoformat().replace("+00:00", "Z"),
            "first_cb_temperature_c": first_temperature,
            "observed_peak_at_first_cb_c": observed_peak,
            "first_cb_at_observed_peak": first_temperature >= observed_peak,
            "detector": "explicit_metar_cb_cloud_group_only",
            "tcu_is_not_cb": True,
        }
    return {
        "cb_seen": False,
        "first_cb_canonical_observation_event_id": "",
        "first_cb_valid_time": "",
        "first_cb_temperature_c": None,
        "observed_peak_at_first_cb_c": None,
        "first_cb_at_observed_peak": None,
        "detector": "explicit_metar_cb_cloud_group_only",
        "tcu_is_not_cb": True,
    }


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "owner_id": OWNER_ID,
        "read_only": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
    }


def _event_rows(
    canonical_events: Iterable[Mapping[str, Any]],
    *,
    decision_as_of: str,
    quantization_c: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    as_of = _parse_time(decision_as_of)
    if as_of is None:
        return [], ["trajectory_decision_asof_missing_or_invalid"]
    issues: list[str] = []
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    target_identity = ""
    station_id = ""
    for raw in canonical_events:
        event = _mapping(raw)
        event_id = _text(event.get("canonical_observation_event_id"))
        canonical_key = _text(event.get("canonical_observation_key"))
        action_time = _parse_time(event.get("first_actionable_seen_at"))
        valid_time = _parse_time(event.get("valid_time"))
        parsed = _mapping(event.get("parsed_observation"))
        temperature_c = _finite(parsed.get("temperature_c") if "temperature_c" in parsed else event.get("temperature_c"))
        if _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}:
            issues.append("trajectory_canonical_event_not_accepted")
            continue
        if not event_id or action_time is None or valid_time is None or temperature_c is None:
            issues.append("trajectory_canonical_event_identity_time_or_temperature_missing")
            continue
        if action_time > as_of:
            issues.append("trajectory_canonical_event_after_asof")
            continue
        if event_id in seen_ids:
            issues.append("trajectory_canonical_event_duplicate")
            continue
        if canonical_key and canonical_key in seen_keys:
            issues.append("trajectory_canonical_key_duplicate")
            continue
        event_target = _text(event.get("target_identity"))
        event_station = _text(event.get("station_id")).upper()
        if target_identity and event_target != target_identity:
            issues.append("trajectory_target_identity_mismatch")
            continue
        if station_id and event_station != station_id:
            issues.append("trajectory_station_id_mismatch")
            continue
        target_identity = event_target or target_identity
        station_id = event_station or station_id
        seen_ids.add(event_id)
        if canonical_key:
            seen_keys.add(canonical_key)
        rows.append(
            {
                "canonical_observation_event_id": event_id,
                "canonical_observation_key": canonical_key,
                "valid_time": valid_time,
                "first_actionable_seen_at": action_time,
                "temperature_c": temperature_c,
                "temperature_interval_lower_c": temperature_c - quantization_c / 2.0,
                "temperature_interval_upper_c": temperature_c + quantization_c / 2.0,
                "target_identity": event_target,
                "station_id": event_station,
                "physical_evidence": {
                    key: parsed.get(key)
                    # Source observations normalize wind speed in m/s.  Keep
                    # the legacy kt spelling only when an older archival row
                    # genuinely supplied it; never silently drop the current
                    # physical evidence from a trajectory state.
                    for key in (
                        "dewpoint_c",
                        "cloud_text",
                        "weather_text",
                        "wind_speed_mps",
                        "wind_speed_kt",
                        "pressure_hpa",
                    )
                    if parsed.get(key) not in (None, "")
                },
            }
        )
    rows.sort(key=lambda item: (item["valid_time"], item["first_actionable_seen_at"], item["canonical_observation_event_id"]))
    if not rows:
        issues.append("trajectory_canonical_sequence_empty")
    return rows, sorted(set(issues))


def _window_slope(rows: list[dict[str, Any]], *, window_minutes: int) -> dict[str, Any]:
    latest = rows[-1]
    latest_time = latest["valid_time"]
    candidates = [
        row
        for row in rows[:-1]
        if 0.0 < (latest_time - row["valid_time"]).total_seconds() / 60.0 <= window_minutes
    ]
    if not candidates:
        return {"window_minutes": window_minutes, "state": "na", "na_reason": "insufficient_canonical_points_in_window"}
    previous = candidates[0]
    elapsed_hours = (latest_time - previous["valid_time"]).total_seconds() / 3600.0
    display_rate = (latest["temperature_c"] - previous["temperature_c"]) / elapsed_hours
    lower_rate = (latest["temperature_interval_lower_c"] - previous["temperature_interval_upper_c"]) / elapsed_hours
    upper_rate = (latest["temperature_interval_upper_c"] - previous["temperature_interval_lower_c"]) / elapsed_hours
    return {
        "window_minutes": window_minutes,
        "state": "available",
        "from_canonical_observation_event_id": previous["canonical_observation_event_id"],
        "to_canonical_observation_event_id": latest["canonical_observation_event_id"],
        "elapsed_minutes": elapsed_hours * 60.0,
        "display_rate_c_per_hour": display_rate,
        "interval_rate_lower_c_per_hour": lower_rate,
        "interval_rate_upper_c_per_hour": upper_rate,
    }


def _path_state(rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    temperatures = [float(row["temperature_c"]) for row in rows]
    recent = temperatures[-4:]
    latest = recent[-1]
    peak = max(temperatures)
    peak_index = max(index for index, value in enumerate(temperatures) if value == peak)
    cooling_depth = max(0.0, peak - latest)
    recent_deltas = [right - left for left, right in zip(recent, recent[1:])]
    sustained_display_warming = len(recent) == 4 and all(delta >= 1.0 for delta in recent_deltas)
    confirmed_display_cooling = (
        len(recent) >= 3
        and recent_deltas[-1] <= -1.0
        and recent_deltas[-2] <= -1.0
        and cooling_depth >= 2.0
    )
    rebound = len(recent) >= 3 and recent_deltas[-2] <= -1.0 and recent_deltas[-1] >= 1.0
    temporary_platform = (
        len(recent) >= 3
        and not sustained_display_warming
        and not confirmed_display_cooling
        and max(recent[-3:]) - min(recent[-3:]) <= 1.0
    )
    if rebound:
        state = "rebound"
    elif confirmed_display_cooling:
        state = "cooling"
    elif sustained_display_warming:
        state = "sustained_warming"
    elif temporary_platform:
        state = "temporary_platform"
    elif len(recent) >= 2 and recent_deltas[-1] <= -1.0:
        state = "pullback_watch"
    else:
        state = "mixed_or_insufficient"
    latest_time = rows[-1]["valid_time"]
    peak_time = rows[peak_index]["valid_time"]
    return state, {
        "display_temperatures": recent,
        "recent_display_deltas_c": recent_deltas,
        "sustained_display_warming": sustained_display_warming,
        "confirmed_display_cooling": confirmed_display_cooling,
        "rebound": rebound,
        "temporary_platform": temporary_platform,
        "latest_temperature_c": latest,
        "observed_peak_display_c": peak,
        "observed_peak_valid_time": peak_time.isoformat().replace("+00:00", "Z"),
        "cooling_depth_display_c": cooling_depth,
        "cooling_duration_minutes": max(0.0, (latest_time - peak_time).total_seconds() / 60.0) if cooling_depth > 0.0 else 0.0,
    }


def build_temperature_trajectory_feature_state(
    *,
    canonical_events: Iterable[Mapping[str, Any]],
    decision_as_of: str,
    temperature_quantization_c: float = 1.0,
    temporal_role: str = "post_event",
) -> dict[str, Any]:
    """Create one deterministic trajectory feature state at a strict cutoff.

    ``pre_event`` is the only role valid as an input to an event's likelihood.
    ``post_event`` includes the just-consumed event and belongs to the next
    checkpoint only.  The feature math is identical; the caller owns the
    canonical sequence cut, making the Bayes temporal boundary explicit.
    """

    quantization = _finite(temperature_quantization_c)
    if quantization is None or quantization <= 0.0:
        return _blocked("trajectory_temperature_quantization_invalid")
    if temporal_role not in {"pre_event", "post_event"}:
        return _blocked("trajectory_temporal_role_invalid")
    rows, issues = _event_rows(
        canonical_events,
        decision_as_of=decision_as_of,
        quantization_c=quantization,
    )
    if issues:
        return _blocked(*issues)
    slopes = {f"{window}m": _window_slope(rows, window_minutes=window) for window in WINDOW_MINUTES}
    state, state_evidence = _path_state(rows)
    first_cb_evidence = _first_cb_evidence(rows)
    latest = rows[-1]
    observed_max_lower_bound_c = max(row["temperature_interval_lower_c"] for row in rows)
    sequence_identity = [row["canonical_observation_event_id"] for row in rows]
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "owner_id": OWNER_ID,
        "target_identity": latest["target_identity"],
        "station_id": latest["station_id"],
        "temporal_role": temporal_role,
        "decision_as_of": _text(decision_as_of),
        "canonical_sequence_event_ids": sequence_identity,
        "canonical_sequence_sha256": _sha256(sequence_identity),
        "temperature_quantization_c": quantization,
        "latest_canonical_observation_event_id": latest["canonical_observation_event_id"],
        "latest_valid_time": latest["valid_time"].isoformat().replace("+00:00", "Z"),
        "latest_temperature_c": latest["temperature_c"],
        "latest_temperature_interval_c": {
            "lower": latest["temperature_interval_lower_c"],
            "upper": latest["temperature_interval_upper_c"],
        },
        "observed_max_lower_bound_c": observed_max_lower_bound_c,
        "slopes": slopes,
        "acceleration_c_per_hour_squared": (
            slopes["30m"].get("display_rate_c_per_hour") - slopes["120m"].get("display_rate_c_per_hour")
            if slopes["30m"].get("state") == "available" and slopes["120m"].get("state") == "available"
            else None
        ),
        "path_state_candidate": state,
        "path_state_evidence": state_evidence,
        "latest_physical_evidence": latest["physical_evidence"],
        "first_cb_evidence": first_cb_evidence,
        "boundary": {
            "canonical_events_only": True,
            "interval_aware_temperature": True,
            "single_deterministic_owner": True,
            "not_independent_likelihood": True,
            "not_a_strategy_signal": True,
            "must_be_consumed_once_by_strong_artifact": True,
            "pre_event_only_for_current_likelihood": True,
        },
        "read_only": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
    }


def build_empty_pre_event_temperature_trajectory_feature_state(
    *,
    target_identity: str,
    station_id: str,
    decision_as_of: str,
    temperature_quantization_c: float = 1.0,
) -> dict[str, Any]:
    """Represent the first D0 report's legal, explicit no-history state.

    An empty predecessor is not a reason to fabricate momentum or skip the
    first report.  It is an explicit feature state consumed by the approved
    artifact's ``__missing__``/``__other__`` handling, while the report itself
    remains the current observation.  No post-event state may use this helper.
    """

    quantization = _finite(temperature_quantization_c)
    if quantization is None or quantization <= 0.0:
        return _blocked("trajectory_temperature_quantization_invalid")
    if not _text(target_identity) or not _text(station_id) or _parse_time(decision_as_of) is None:
        return _blocked("trajectory_empty_pre_event_identity_or_asof_invalid")
    slopes = {
        f"{window}m": {
            "window_minutes": window,
            "state": "na",
            "na_reason": "no_prior_canonical_observation",
        }
        for window in WINDOW_MINUTES
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "owner_id": OWNER_ID,
        "temporal_role": "pre_event",
        "target_identity": _text(target_identity),
        "station_id": _text(station_id).upper(),
        "decision_as_of": _text(decision_as_of),
        "canonical_sequence_event_ids": [],
        "canonical_sequence_sha256": _sha256([]),
        "temperature_quantization_c": quantization,
        "latest_canonical_observation_event_id": "",
        "latest_valid_time": "",
        "latest_temperature_c": None,
        "latest_temperature_interval_c": {"lower": None, "upper": None},
        "observed_max_lower_bound_c": None,
        "slopes": slopes,
        "acceleration_c_per_hour_squared": None,
        "path_state_candidate": "no_prior_observation",
        "path_state_evidence": {"na_reason": "no_prior_canonical_observation"},
        "latest_physical_evidence": {},
        "first_cb_evidence": {
            "cb_seen": False,
            "first_cb_canonical_observation_event_id": "",
            "first_cb_valid_time": "",
            "first_cb_temperature_c": None,
            "observed_peak_at_first_cb_c": None,
            "first_cb_at_observed_peak": None,
            "detector": "explicit_metar_cb_cloud_group_only",
            "tcu_is_not_cb": True,
        },
        "boundary": {
            "canonical_events_only": True,
            "interval_aware_temperature": True,
            "single_deterministic_owner": True,
            "not_independent_likelihood": True,
            "not_a_strategy_signal": True,
            "must_be_consumed_once_by_strong_artifact": True,
            "pre_event_only_for_current_likelihood": True,
            "explicit_no_history_not_fabricated_momentum": True,
        },
        "read_only": True,
        "creates_probability": False,
        "creates_strategy_signal": False,
    }


__all__ = [
    "OWNER_ID",
    "SCHEMA_VERSION",
    "cloud_text_contains_cb",
    "build_empty_pre_event_temperature_trajectory_feature_state",
    "build_temperature_trajectory_feature_state",
]
