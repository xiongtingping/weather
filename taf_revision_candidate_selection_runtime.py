"""Pure selection helpers for newly visible AMSC TAF/AMD revisions.

This module deliberately owns no database connection and no production write
authority.  Both retired and current probability epochs may reuse the parsing
logic without making the retired writer reachable from the active runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Collection, Iterable, Mapping

from amsc_taf_revision_runtime import taf_revision_order_key


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("payload")
    return value if isinstance(value, Mapping) else {}


def _instant(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _system_visible_at(row: Mapping[str, Any]) -> datetime | None:
    """Return when this installation could first consume the revision.

    Provider ``first_seen_at`` may precede a delayed local fetch.  The later of
    provider visibility and local persistence is therefore the strict as-of
    boundary for live consumption.
    """

    payload = _payload(row)
    values = [
        instant
        for instant in (
            _instant(payload.get("first_seen_at")),
            _instant(row.get("fetched_at")),
        )
        if instant is not None
    ]
    return max(values) if values else None


def dedupe_source_observations(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        identity = _text(row.get("source_observation_id") or row.get("source_event_id"))
        if identity:
            selected[identity] = row
    return list(selected.values())


def select_new_taf_revision_candidates(
    source_observations: Iterable[Mapping[str, Any]],
    *,
    event_date: str,
    decision_as_of: str,
    visible_after: str = "",
    applied_revision_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Select the newest unconsumed revision visible in a checkpoint window.

    The full visible batch is sorted before applied-id filtering.  If its newest
    revision is already applied, every older revision is already represented by
    that replacement snapshot and must not be replayed individually.
    """

    day = _text(event_date)[:10]
    upper = _instant(decision_as_of)
    lower = _instant(visible_after)
    if upper is None:
        return []
    candidates: list[dict[str, Any]] = []
    for raw in source_observations:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        payload = _payload(row)
        if _text(row.get("source_id")) != "zuuu_taf_forecast_amsc":
            continue
        revision_day = _text(row.get("event_date") or payload.get("event_date"))[:10]
        if revision_day != day:
            continue
        visible_at = _system_visible_at(row)
        if visible_at is None or visible_at > upper:
            continue
        if lower is not None and visible_at <= lower:
            continue
        candidates.append(row)
    candidates.sort(
        key=lambda row: taf_revision_order_key(
            issue_time_utc=_text(
                row.get("issue_time_utc") or _payload(row).get("issue_time_utc")
            ),
            revision_kind=_text(
                row.get("forecast_revision_kind")
                or _payload(row).get("forecast_revision_kind")
            ),
            first_seen_at=_text(
                _payload(row).get("first_seen_at") or row.get("fetched_at")
            ),
            revision_id=_text(
                _payload(row).get("source_revision_id") or row.get("source_event_id")
            ),
        )
    )
    if not candidates:
        return []
    latest = candidates[-1]
    latest_payload = _payload(latest)
    latest_id = _text(
        latest_payload.get("source_revision_id") or latest.get("source_event_id")
    )
    if latest_id in {_text(value) for value in applied_revision_ids}:
        return []
    return candidates


__all__ = ["dedupe_source_observations", "select_new_taf_revision_candidates"]
