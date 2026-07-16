"""Read-only liveness contract for the strict-as-of forward forecast capture.

This reports whether the hourly research collector is producing a current,
complete three-model capture report.  It is operational evidence only: a
healthy collector does not make its short history eligible for model training.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "forecast_source_forward_capture_health_audit_v1"
CAPTURE_SCHEMA_VERSION = "current_forecast_source_forward_capture_v1"
DEFAULT_REQUIRED_SOURCE_IDS = (
    "ecmwf_model_peak",
    "gfs_model_peak",
    "icon_model_peak",
)
DEFAULT_MAX_AGE_SECONDS = 7200


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


def build_forecast_source_forward_capture_health_audit(
    capture_report: Mapping[str, Any] | None,
    *,
    now: str,
    expected_event_date: str,
    required_source_ids: Iterable[str] = DEFAULT_REQUIRED_SOURCE_IDS,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Check one latest-capture report without creating any new evidence."""

    report = _mapping(capture_report)
    required = tuple(sorted({_text(source) for source in required_source_ids if _text(source)}))
    blockers: list[str] = []
    if _text(report.get("schema")) != CAPTURE_SCHEMA_VERSION:
        blockers.append("forward_capture_report_schema_invalid")
    if _text(report.get("source_data_policy")) != "external_only":
        blockers.append("forward_capture_source_data_policy_invalid")
    if _text(report.get("event_date")) != _text(expected_event_date)[:10]:
        blockers.append("forward_capture_event_date_not_current")
    observed_at = _parse_time(report.get("observed_at"))
    audit_now = _parse_time(now)
    if observed_at is None or audit_now is None:
        blockers.append("forward_capture_observed_at_or_audit_now_invalid")
        age_seconds: float | None = None
    elif observed_at > audit_now:
        blockers.append("forward_capture_observed_at_in_future")
        age_seconds = (audit_now - observed_at).total_seconds()
    else:
        age_seconds = (audit_now - observed_at).total_seconds()
        if age_seconds > max(1, int(max_age_seconds)):
            blockers.append("forward_capture_report_stale")
    results = _mapping(report.get("results"))
    source_states: list[dict[str, Any]] = []
    for source_id in required:
        result = _mapping(results.get(source_id))
        available = result.get("ok") is True
        response_sha256 = _text(result.get("response_sha256"))
        state = _text(result.get("state"))
        reason = ""
        if not result:
            reason = "forward_capture_required_source_result_missing"
        elif not available:
            reason = "forward_capture_required_source_unavailable"
        elif not response_sha256:
            reason = "forward_capture_required_source_raw_hash_missing"
        elif state not in {"written", "already_present", "already_current"}:
            reason = "forward_capture_required_source_write_state_invalid"
        if reason:
            blockers.append(f"{reason}:{source_id}")
        source_states.append(
            {
                "source_id": source_id,
                "ok": not reason,
                "state": state,
                "response_sha256": response_sha256,
                "reason": reason,
            }
        )
    guards = _mapping(report.get("guards"))
    if any(guards.get(name) is not False for name in ("writes_archive_db", "writes_facts_sqlite", "places_orders", "restarts_launchd")):
        blockers.append("forward_capture_production_guard_invalid")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "healthy" if not blockers else "blocked",
        "expected_event_date": _text(expected_event_date)[:10],
        "observed_at": _text(report.get("observed_at")),
        "audit_now": _text(now),
        "age_seconds": age_seconds,
        "max_age_seconds": max(1, int(max_age_seconds)),
        "required_source_ids": list(required),
        "source_states": source_states,
        "blockers": sorted(set(blockers)),
        "boundary": {
            "operational_health_not_training_readiness": True,
            "does_not_promote_tier_b_or_tier_c": True,
            "does_not_create_forecast_revision": True,
        },
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "read_only": True,
    }


__all__ = [
    "CAPTURE_SCHEMA_VERSION",
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_REQUIRED_SOURCE_IDS",
    "SCHEMA_VERSION",
    "build_forecast_source_forward_capture_health_audit",
]
