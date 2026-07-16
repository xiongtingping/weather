"""Authoritative execution-time window for the strongly-excluded NO leg.

This state is deliberately separate from the posterior/RealizationState.  A
wall-clock boundary can change without a new weather observation, so freezing
it into an observation-triggered posterior would leave the execution state
stale.  The builder consumes only a frozen resolution-local date, an approved
policy artifact, and the decision clock; it never consumes weather, prices,
positions, EV, Edge, or fair value.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = "settlement_window_state_v1"
POLICY_SCHEMA_VERSION = "strongly_excluded_no_settlement_window_policy_v1"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "config" / "trading" / "strongly_excluded_no_settlement_window_v1.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _parse_local_time(value: Any) -> time | None:
    try:
        return time.fromisoformat(_text(value))
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def load_approved_settlement_window_policy(path: str | Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Load and self-hash the immutable approved policy surface."""

    policy = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(policy, Mapping):
        raise ValueError("settlement_window_policy_not_mapping")
    core = dict(policy)
    supplied = _text(core.pop("artifact_sha256", ""))
    artifact = _sha(core)
    if supplied and supplied != artifact:
        raise ValueError("settlement_window_policy_artifact_sha256_mismatch")
    return {**core, "artifact_sha256": artifact}


def build_settlement_window_state(
    *,
    resolution_local_date: str,
    decision_as_of: str,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one frozen, auditable NO execution window state."""

    raw_policy = dict(_mapping(policy))
    supplied_artifact = _text(raw_policy.pop("artifact_sha256", ""))
    computed_artifact = _sha(raw_policy)
    issues: list[str] = []
    if raw_policy.get("schema") != POLICY_SCHEMA_VERSION:
        issues.append("settlement_window_policy_schema_invalid")
    if _text(raw_policy.get("approval_state")) != "approved":
        issues.append("settlement_window_policy_not_approved")
    if _text(raw_policy.get("station_id")).upper() != "ZUUU":
        issues.append("settlement_window_policy_station_invalid")
    if _text(raw_policy.get("applies_only_to_strategy_leg")) != "strongly_excluded_no":
        issues.append("settlement_window_policy_leg_scope_invalid")
    if not supplied_artifact or supplied_artifact != computed_artifact:
        issues.append("settlement_window_policy_artifact_invalid")
    day_text = _text(resolution_local_date)[:10]
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        day = None
        issues.append("settlement_window_resolution_local_date_invalid")
    decision = _parse_utc(decision_as_of)
    if decision is None:
        issues.append("settlement_window_decision_as_of_invalid")
    opens_time = _parse_local_time(raw_policy.get("window_open_local_time"))
    closes_time = _parse_local_time(raw_policy.get("window_close_local_time"))
    if opens_time is None or closes_time is None or opens_time >= closes_time:
        issues.append("settlement_window_policy_local_times_invalid")
    try:
        zone = ZoneInfo(_text(raw_policy.get("resolution_timezone")))
    except ZoneInfoNotFoundError:
        zone = None
        issues.append("settlement_window_policy_timezone_invalid")
    if issues:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(issues)),
            "read_only": True,
            "places_orders": False,
        }
    assert day is not None and decision is not None and opens_time is not None and closes_time is not None and zone is not None
    opens_at = datetime.combine(day, opens_time, tzinfo=zone).astimezone(timezone.utc)
    closes_at = datetime.combine(day, closes_time, tzinfo=zone).astimezone(timezone.utc)
    is_open = opens_at <= decision <= closes_at
    phase = "before_window" if decision < opens_at else "open" if is_open else "closed"
    identity = {
        "resolution_local_date": day_text,
        "decision_as_of": _iso(decision),
        "policy_artifact_sha256": supplied_artifact,
        "window_opens_at": _iso(opens_at),
        "window_closes_at": _iso(closes_at),
        "phase": phase,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "settlement_window_state_id": "setwin_" + _sha(identity)[:24],
        **identity,
        "settlement_window_open": is_open,
        "station_id": "ZUUU",
        "resolution_timezone": _text(raw_policy.get("resolution_timezone")),
        "strategy_leg": "strongly_excluded_no",
        "boundary": {
            "separate_from_posterior_and_realization_state": True,
            "decision_clock_is_frozen": True,
            "weather_or_market_price_consumed": False,
            "ev_edge_or_fair_value_consumed": False,
            "top_yes_and_second_yes_not_time_gated": True,
        },
        "read_only": True,
        "places_orders": False,
    }


__all__ = [
    "DEFAULT_POLICY_PATH",
    "POLICY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_settlement_window_state",
    "load_approved_settlement_window_policy",
]
