"""Read immutable TAF/WU/TWC captures for the live joint weak model.

The capture repository is shared with the strict-as-of training ingress, but
live scoring does not require Tier-A rows to have already been copied into P1.
Only proposals that pass the same immutable raw/arrival/source contract are
returned.  Training/OOS readiness remains an offline concern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from scripts.append_strict_asof_forecast_p1_ingress import (
    DEFAULT_ROOT,
    collect_tier_a_forecast_p1_proposals,
)


SCHEMA_VERSION = "independent_weak_live_source_snapshot_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def load_live_independent_weak_source_observations(
    *,
    event_date: str,
    station_id: str = "ZUUU",
    root: Path | str = DEFAULT_ROOT,
) -> dict[str, Any]:
    """Return valid independent forecast observations without writing P1."""

    day = _text(event_date)[:10]
    if len(day) != 10:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["independent_weak_live_event_date_invalid"],
            "source_observations": [],
            "read_only": True,
        }
    scan = collect_tier_a_forecast_p1_proposals(
        root=Path(root),
        station_id=_text(station_id).upper(),
    )
    selected: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    for item in scan.get("eligible") or []:
        if _text(item.get("event_date")) != day:
            continue
        proposal = item.get("proposal") if isinstance(item.get("proposal"), Mapping) else {}
        observation = proposal.get("source_observation") if isinstance(proposal.get("source_observation"), Mapping) else {}
        if proposal.get("ok") is True and observation:
            selected.append(dict(observation))
            trace.append({
                "capture_id": _text(item.get("capture_id")),
                "provider_source_id": _text(item.get("source_id")),
                "weak_source_id": _text(observation.get("source_id")),
                "source_event_id": _text(observation.get("source_event_id")),
                "included": True,
            })
    selected.sort(key=lambda row: (_text(row.get("fetched_at")), _text(row.get("source_event_id"))))
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available" if selected else "empty_source_set",
        "event_date": day,
        "source_observations": selected,
        "source_observation_count": len(selected),
        "source_trace": trace,
        "quarantined": list(scan.get("blocked") or []),
        "read_only": True,
        "boundary": {
            "same_contract_as_strict_p1_ingress": True,
            "p1_materialization_not_required_for_live_scoring": True,
            "strict_asof_oos_not_live_gate": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "load_live_independent_weak_source_observations"]
