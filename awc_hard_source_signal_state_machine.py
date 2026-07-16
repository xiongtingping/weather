from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from lineage_identity_runtime import lineage_id_from_parts
from source_observation_runtime import (
    PRIMARY_METAR_SOURCE_ID,
    REFERENCE_METAR_SOURCE_IDS,
    SOURCE_CLASSES,
    build_source_observation,
    source_defaults,
)


SCHEMA_VERSION = "awc_hard_source_signal_state_machine_v2"
PRIMARY_HARD_SOURCE_IDS = frozenset({PRIMARY_METAR_SOURCE_ID})
REFERENCE_HARD_SOURCE_IDS = frozenset(REFERENCE_METAR_SOURCE_IDS)
ACCEPTED_HARD_SOURCE_IDS = PRIMARY_HARD_SOURCE_IDS
AWC_HARD_SOURCE_IDS = REFERENCE_HARD_SOURCE_IDS


def awc_hard_source_acceptance_contract() -> dict[str, Any]:
    return {
        "schema": "awc_hard_source_acceptance_contract_v1",
        "read_only": True,
        "state_machine_schema": SCHEMA_VERSION,
        "accepted_source_ids": sorted(ACCEPTED_HARD_SOURCE_IDS),
        "primary_source_ids": sorted(PRIMARY_HARD_SOURCE_IDS),
        "reference_source_ids": sorted(REFERENCE_HARD_SOURCE_IDS),
        "acceptance_states": ["accepted", "rejected", "demoted", "conflict"],
        "window_defaults": {
            "primary_amsc_acquire_window_start_min": 0.0,
            "primary_amsc_acquire_window_duration_sec": 900.0,
            "reference_awc_acquire_window_start_min": 5.0,
            "reference_awc_acquire_window_duration_sec": 120.0,
            "stale_after_sec": 900.0,
            "pre_signal_boundary": "valid_time_anchor_to_primary_hard_source_signal_window; awc_hour_plus_five_is_reference_only",
        },
        "required_observation_fields": [
            "source_id",
            "observed_at",
            "fetched_at_or_parsed_at",
            "value_c",
            "raw_or_source_event_id",
        ],
        "accepted_rules": [
            "observation_ok=true",
            "source_defaults.hard_eligible=true",
            "source_id in primary_source_ids",
            "signal_seen_at inside acceptance_window",
            "conflict_reason empty",
        ],
        "rejected_rules": [
            "observation_ok=false",
            "signal_seen_before_awc_acquire_window",
            "signal_seen_after_awc_acquire_window",
            "signal_seen_after_stale_deadline",
        ],
        "demoted_rules": [
            "source_defaults.hard_eligible=false",
            "source_id not in primary_source_ids",
            "AWC raw/cache sources are reference hard observations and cannot emit accepted primary hard source signal",
            "WU/TWC current sources remain diagnostic_current_source and cannot emit awc_hard_source_signal_at",
        ],
        "conflict_rules": [
            "conflict_reason present",
            "hard_observation_conflict_reason present",
            "conflict=true",
        ],
        "emission_boundary": {
            "awc_hard_source_signal_at": "only populated when acceptance_state=accepted",
            "emits_signal": "true only when accepted and read_only=false",
            "read_only_terminal_state": "skipped",
        },
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "historical_backfill": False,
            "restarts_launchd": False,
        },
    }


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AWCSignalWindow:
    hour_boundary_at: str
    preparation_started_at: str
    awc_pre_signal_deadline_at: str
    awc_poll_window_started_at: str
    awc_poll_window_ended_at: str
    state: str
    reason: str
    detection_lag_ms: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hour_boundary_at": self.hour_boundary_at,
            "preparation_started_at": self.preparation_started_at,
            "awc_pre_signal_deadline_at": self.awc_pre_signal_deadline_at,
            "awc_poll_window_started_at": self.awc_poll_window_started_at,
            "awc_poll_window_ended_at": self.awc_poll_window_ended_at,
            "state": self.state,
            "reason": self.reason,
            "detection_lag_ms": self.detection_lag_ms,
        }


def classify_awc_signal_window(
    *,
    awc_valid_time: Any,
    signal_seen_at: Any,
    acquire_window_start_min: float = 5.0,
    acquire_window_duration_sec: float = 120.0,
    stale_after_sec: float = 900.0,
) -> AWCSignalWindow:
    valid_dt = _parse_dt(awc_valid_time)
    seen_dt = _parse_dt(signal_seen_at)
    if valid_dt is None or seen_dt is None:
        return AWCSignalWindow(
            hour_boundary_at="",
            preparation_started_at="",
            awc_pre_signal_deadline_at="",
            awc_poll_window_started_at="",
            awc_poll_window_ended_at="",
            state="unknown",
            reason="valid_time_or_signal_seen_at_missing",
            detection_lag_ms=None,
        )
    hour = valid_dt.replace(second=0, microsecond=0)
    start_min = max(0.0, min(59.999, float(acquire_window_start_min)))
    duration = max(0.0, float(acquire_window_duration_sec))
    poll_start = hour + timedelta(minutes=start_min)
    poll_end = poll_start + timedelta(seconds=duration)
    stale_at = poll_start + timedelta(seconds=max(duration, float(stale_after_sec)))
    lag_ms = max(0.0, (seen_dt - valid_dt).total_seconds() * 1000.0)
    if seen_dt < poll_start:
        state = "preparation_window"
        reason = "signal_seen_before_awc_acquire_window"
    elif seen_dt <= poll_end:
        state = "acceptance_window"
        reason = "signal_seen_inside_awc_acquire_window"
    elif seen_dt <= stale_at:
        state = "late_detection"
        reason = "signal_seen_after_awc_acquire_window"
    else:
        state = "stale"
        reason = "signal_seen_after_stale_deadline"
    return AWCSignalWindow(
        hour_boundary_at=_iso(hour),
        preparation_started_at=_iso(hour),
        awc_pre_signal_deadline_at=_iso(poll_start),
        awc_poll_window_started_at=_iso(poll_start),
        awc_poll_window_ended_at=_iso(poll_end),
        state=state,
        reason=reason,
        detection_lag_ms=round(lag_ms, 3),
    )


def build_awc_hard_source_signal_candidate(
    *,
    slug: str,
    event_date: str,
    observation: Mapping[str, Any],
    candidate_seen_at: str = "",
    station_id: str = "ZUUU",
    read_only: bool = True,
    acquire_window_start_min: float = 5.0,
    acquire_window_duration_sec: float = 120.0,
    stale_after_sec: float = 900.0,
) -> dict[str, Any]:
    source_id = str(observation.get("source_id") or "").strip()
    observed_at = str(observation.get("observed_at") or "").strip()
    fetched_at = str(observation.get("fetched_at") or "").strip()
    parsed_at = str(observation.get("parsed_at") or "").strip()
    signal_seen_at = str(candidate_seen_at or parsed_at or fetched_at or "").strip()
    value_c = observation.get("value_c")
    defaults = source_defaults(source_id)
    source_hard_eligible = bool(defaults.get("hard_eligible"))
    is_primary_source = source_id in PRIMARY_HARD_SOURCE_IDS
    is_reference_source = source_id in REFERENCE_HARD_SOURCE_IDS
    is_awc_source = is_reference_source
    is_accepted_signal_source = source_id in ACCEPTED_HARD_SOURCE_IDS
    if is_primary_source and acquire_window_start_min == 5.0 and acquire_window_duration_sec == 120.0:
        acquire_window_start_min = 0.0
        acquire_window_duration_sec = 900.0
    source_observation = build_source_observation(
        source_id=source_id,
        station_id=station_id,
        event_date=event_date,
        valid_time=observed_at,
        fetched_at=fetched_at,
        parsed_at=parsed_at,
        response_completed_at=fetched_at,
        response_duration_ms=observation.get("fetch_duration_ms"),
        temperature_c=float(value_c) if isinstance(value_c, (int, float)) else None,
        observed_today_max_c=float(value_c) if isinstance(value_c, (int, float)) else None,
        raw_payload=observation.get("raw") or "",
        quality_flags=tuple(observation.get("quality_flags") or ()),
        payload={"source_observation_input": dict(observation)},
    )
    window = classify_awc_signal_window(
        awc_valid_time=observed_at,
        signal_seen_at=signal_seen_at,
        acquire_window_start_min=acquire_window_start_min,
        acquire_window_duration_sec=acquire_window_duration_sec,
        stale_after_sec=stale_after_sec,
    )
    conflict_reason = str(
        observation.get("conflict_reason")
        or observation.get("hard_observation_conflict_reason")
        or ""
    ).strip()
    if not conflict_reason and bool(observation.get("conflict")):
        conflict_reason = "hard_observation_conflict"
    if conflict_reason:
        acceptance_state = "conflict"
        acceptance_reason = conflict_reason
    elif not observation.get("ok"):
        acceptance_state = "rejected"
        acceptance_reason = str(observation.get("reason") or "observation_not_ok")
    elif not source_hard_eligible:
        acceptance_state = "demoted"
        acceptance_reason = str(defaults.get("demotion_reason") or "source_not_hard_eligible")
    elif not is_accepted_signal_source:
        acceptance_state = "demoted"
        acceptance_reason = (
            "hard_source_reference_not_primary_signal_source"
            if is_reference_source
            else "hard_source_not_primary_signal_source"
        )
    elif window.state != "acceptance_window":
        acceptance_state = "rejected"
        acceptance_reason = window.reason
    else:
        acceptance_state = "accepted"
        acceptance_reason = "primary_hard_source_signal_accepted"
    signal_eligible = acceptance_state == "accepted"
    signal_id = lineage_id_from_parts(
        "signal_id",
        {
            "slug": slug,
            "event_date": event_date,
            "source_event_id": source_observation.source_event_id,
            "hard_source_valid_time": observed_at,
            "signal_seen_at": signal_seen_at,
        },
    )
    terminal_state = "skipped" if read_only else ("no_action" if not signal_eligible else "accepted_pending_execution")
    terminal_reason = "read_only_candidate_not_emitted" if read_only else (
        "" if signal_eligible else acceptance_reason
    )
    return {
        "schema": SCHEMA_VERSION,
        "read_only": bool(read_only),
        "signal_id": signal_id,
        "source_event_id": source_observation.source_event_id,
        "slug": str(slug or "").strip(),
        "event_date": str(event_date or "").strip(),
        "station_id": station_id,
        "source_id": source_id,
        "source_class": source_observation.source_class,
        "source_role": source_observation.role,
        "source_hard_eligible": source_hard_eligible,
        "primary_hard_source": is_primary_source,
        "reference_hard_source": is_reference_source,
        "accepted_signal_source": is_accepted_signal_source,
        "awc_source": is_awc_source,
        "value_c": value_c,
        "awc_valid_time": observed_at,
        "hard_source_valid_time": observed_at,
        "source_fetched_at": fetched_at,
        "source_parsed_at": parsed_at,
        "signal_seen_at": signal_seen_at,
        "awc_hard_source_signal_at": signal_seen_at if signal_eligible else "",
        "window": window.as_dict(),
        "acceptance_state": acceptance_state,
        "acceptance_reason": acceptance_reason,
        "signal_eligible": signal_eligible,
        "emits_signal": bool(signal_eligible and not read_only),
        "terminal_state": terminal_state,
        "terminal_reason": terminal_reason,
        "source_observation": source_observation.as_dict(include_payload=False),
    }
