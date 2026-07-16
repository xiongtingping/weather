"""Execution-time admission window for the dynamic second-YES settlement leg."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "second_yes_entry_window_state_v1"
WINDOW_MINUTES = 30


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "secondyeswindow_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def build_second_yes_entry_window_state(
    *, realization_state: Mapping[str, Any], decision_as_of: str,
) -> dict[str, Any]:
    """Open only during [predicted_peak-30m, predicted_peak).

    A closed or unavailable window forbids only new/additional second-YES BUYs.
    It is not a weather invalidation and therefore cannot force an existing
    same-bucket position to be sold.
    """

    realization = _mapping(realization_state)
    timing = _mapping(realization.get("peak_timing_state"))
    decision = _time(decision_as_of)
    posterior_run_id = _text(realization.get("posterior_run_id"))
    realization_state_id = _text(realization.get("realization_state_id"))
    if decision is None or not posterior_run_id or not realization_state_id:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["second_yes_entry_window_identity_or_decision_time_invalid"],
            "entry_allowed": False,
            "places_orders": False,
        }
    peak_at = _time(timing.get("entry_peak_at"))
    timing_available = (
        _text(timing.get("schema")) == "peak_timing_state_l3_v1"
        and timing.get("ok") is True
        and _text(timing.get("state")) == "available"
        and peak_at is not None
        and bool(_text(timing.get("peak_timing_state_id")))
    )
    start = peak_at - timedelta(minutes=WINDOW_MINUTES) if timing_available else None
    entry_allowed = bool(start is not None and start <= decision < peak_at)
    if not timing_available:
        reason = _text(
            timing.get("entry_peak_at_na_reason")
            or timing.get("na_reason")
            or "authoritative_peak_timing_unavailable"
        )
    elif decision < start:
        reason = "before_predicted_peak_final_30_minutes"
    elif decision >= peak_at:
        reason = "predicted_peak_time_reached_or_passed"
    else:
        reason = ""
    core = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "posterior_run_id": posterior_run_id,
        "realization_state_id": realization_state_id,
        "peak_timing_state_id": _text(timing.get("peak_timing_state_id")),
        "decision_as_of": _iso(decision),
        "predicted_peak_at": _iso(peak_at) if peak_at is not None else "",
        "entry_window_start_at": _iso(start) if start is not None else "",
        "entry_window_end_at": _iso(peak_at) if peak_at is not None else "",
        "entry_window_minutes": WINDOW_MINUTES,
        "entry_allowed": entry_allowed,
        "entry_block_reason": reason,
        "existing_same_bucket_position_may_be_retained": True,
        "window_close_is_not_target_invalidation": True,
        "uses_ev_edge_lcb": False,
        "places_orders": False,
    }
    return {**core, "second_yes_entry_window_state_id": _id(core)}


__all__ = ["SCHEMA_VERSION", "WINDOW_MINUTES", "build_second_yes_entry_window_state"]
