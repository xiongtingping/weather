"""Exact schedule-bound AMSC poll-slot freshness contract.

The acquisition process can spend several minutes retrying a blocked SQLite
write.  Freshness must therefore be evaluated against the time of the *current
fetch attempt*, not the time that the long-lived launcher process started.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from zuuu_metar_reporting_schedule import SCHEDULE_ID, expected_regular_metar_slots_utc


SCHEMA_VERSION = "amsc_poll_slot_freshness_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_utc(value: Any) -> datetime | None:
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


def poll_slot_utc_for_attempt(attempt_started_at: str) -> str:
    """Return the half-hour slot which the named attempt is allowed to accept."""

    parsed = _parse_utc(attempt_started_at)
    if parsed is None:
        raise ValueError("poll_attempt_started_at_invalid")
    slot = parsed.replace(minute=(parsed.minute // 30) * 30, second=0, microsecond=0)
    return slot.isoformat().replace("+00:00", "Z")


def assess_zuuu_regular_metar_schedule(attempt_started_at: str) -> dict[str, Any]:
    """Classify the half-hour attempt against the frozen ZUUU 44-slot plan."""

    expected_at = poll_slot_utc_for_attempt(attempt_started_at)
    slot = _parse_utc(expected_at)
    if slot is None:  # defensive; poll_slot_utc_for_attempt already validates.
        raise ValueError("poll_attempt_slot_invalid")
    local_day = slot.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    scheduled = expected_at in set(expected_regular_metar_slots_utc(local_day))
    return {
        "schema": SCHEMA_VERSION,
        "attempt_started_at": _text(attempt_started_at),
        "expected_slot_at": expected_at,
        "station_id": "ZUUU",
        "reporting_schedule_id": SCHEDULE_ID,
        "scheduled_regular_metar_slot": scheduled,
        "reason": "scheduled_regular_metar_slot" if scheduled else "unscheduled_regular_metar_slot",
    }


def assess_plan_poll_slot(
    plan: Mapping[str, Any],
    *,
    attempt_started_at: str,
) -> dict[str, Any]:
    """Accept only the exact scheduled report for this actual fetch attempt.

    ``>=`` is intentionally not used: a stale prior report must not close a
    later slot, and a later/future report must not silently stand in for the
    missing scheduled report.  Both cases preserve the retry/audit trail.
    """

    schedule = assess_zuuu_regular_metar_schedule(attempt_started_at)
    expected_at = _text(schedule.get("expected_slot_at"))
    features = (
        plan.get("effective_payload", {})
        if isinstance(plan.get("effective_payload"), Mapping)
        else {}
    )
    features = (
        features.get("weather_likelihood_features", {})
        if isinstance(features.get("weather_likelihood_features"), Mapping)
        else {}
    )
    latest_at = _text(features.get("zuuu_metar_raw_amsc_latest_at"))
    latest = _parse_utc(latest_at)
    expected = _parse_utc(expected_at)
    if schedule.get("scheduled_regular_metar_slot") is not True:
        reason = "unscheduled_regular_metar_slot"
    elif latest is None:
        reason = "latest_metar_at_missing_or_invalid"
    elif latest < expected:
        reason = "latest_metar_before_poll_slot"
    elif latest > expected:
        reason = "latest_metar_after_poll_slot"
    else:
        reason = "exact_poll_slot_match"
    return {
        "schema": SCHEMA_VERSION,
        "fresh": reason == "exact_poll_slot_match",
        "reason": reason,
        "attempt_started_at": _text(attempt_started_at),
        "expected_slot_at": expected_at,
        "latest_metar_at": latest_at,
        "scheduled_regular_metar_slot": schedule.get("scheduled_regular_metar_slot"),
        "reporting_schedule_id": schedule.get("reporting_schedule_id"),
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate exact AMSC half-hour poll freshness")
    parser.add_argument("--plan", required=True, help="AMSC write-plan JSON")
    parser.add_argument("--attempt-started-at", required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    print(json.dumps(assess_plan_poll_slot(plan, attempt_started_at=args.attempt_started_at), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["SCHEMA_VERSION", "assess_plan_poll_slot", "assess_zuuu_regular_metar_schedule", "poll_slot_utc_for_attempt"]
