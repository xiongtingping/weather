"""Classify every AMSC :00/:30 fetch into strong and forecast-revision lanes."""

from __future__ import annotations

from typing import Any, Mapping

from amsc_poll_slot_runtime import assess_plan_poll_slot


SCHEMA_VERSION = "amsc_half_hour_trigger_classification_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def classify_half_hour_fetch(plan: Mapping[str, Any], *, attempt_started_at: str) -> dict[str, Any]:
    """Never turn a stale METAR into a strong event; retain newly seen TAF revisions."""

    slot = assess_plan_poll_slot(plan, attempt_started_at=attempt_started_at)
    effective = plan.get("effective_payload") if isinstance(plan.get("effective_payload"), Mapping) else {}
    fact_as_of = _text(plan.get("fact_as_of") or effective.get("fact_as_of"))
    revisions: list[dict[str, Any]] = []
    for raw in effective.get("source_observations") or []:
        row = raw if isinstance(raw, Mapping) else {}
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        if _text(row.get("source_id")) != "zuuu_taf_forecast_amsc":
            continue
        first_seen = _text(payload.get("first_seen_at"))
        fetched_at = _text(row.get("fetched_at"))
        # A provider-side first-seen timestamp can precede a delayed local
        # fetch.  Either boundary proves that this poll made the revision
        # newly actionable; the posterior writer performs checkpoint/idempotent
        # consumption checks.
        if first_seen != fact_as_of and fetched_at != fact_as_of:
            continue
        revisions.append(
            {
                "source_event_id": _text(row.get("source_event_id")),
                "source_observation_id": _text(row.get("source_observation_id")),
                "event_date": _text(row.get("event_date"))[:10],
                "first_system_seen_at": fetched_at or first_seen,
                "raw_sha256": _text(row.get("raw_sha256") or payload.get("raw_sha256")),
                "forecast_peak_c": row.get("forecast_peak_c"),
                "raw_text": _text(payload.get("raw_text") or row.get("raw_ref")),
            }
        )
    revisions.sort(key=lambda row: (row["event_date"], row["source_event_id"]))
    strong = slot.get("fresh") is True
    return {
        "schema": SCHEMA_VERSION,
        "triggered": True,
        "trigger_slot_at": slot.get("expected_slot_at"),
        "scheduled_regular_metar_slot": slot.get("scheduled_regular_metar_slot"),
        "strong_observation_candidate": strong,
        "strong_observation_na_reason": "" if strong else _text(slot.get("reason")),
        "forecast_revision_candidates": revisions,
        "forecast_revision_candidate_count": len(revisions),
        "state": "strong_and_forecast_revision" if strong and revisions else "strong_observation" if strong else "forecast_revision_only" if revisions else "no_new_event",
        "stale_metar_reconsumed": False,
        "writes_archive_db": False,
        "creates_posterior": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "classify_half_hour_fetch"]
