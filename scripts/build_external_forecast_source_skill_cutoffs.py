#!/usr/bin/env python3
"""Build deterministic, leakage-free cutoffs for external source-skill replay."""

from __future__ import annotations

import argparse
import json
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "external_forecast_source_skill_cutoffs_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def _event_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_event_day_start_cutoffs(
    events: Sequence[Mapping[str, Any]],
    *,
    forecast_snapshots: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    cutoffs: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    snapshots_by_date: dict[str, list[Mapping[str, Any]]] = {}
    for snapshot in forecast_snapshots:
        snapshots_by_date.setdefault(str(snapshot.get("target_event_date_local") or ""), []).append(snapshot)
    for event in events:
        event_id = str(event.get("event_id") or "")
        target_date = _event_date(event.get("event_date_local"))
        if not event_id or target_date is None:
            trace.append(
                {
                    "event_id": event_id,
                    "state": "unavailable",
                    "state_reason": "event_id_or_event_date_missing",
                }
            )
            continue
        # Asia/Shanghai is UTC+08 without DST. Local 00:00 is prior-day 16:00Z.
        cutoff_at = datetime.combine(target_date - timedelta(days=1), time(16), tzinfo=timezone.utc)
        event_end = datetime.combine(target_date, time(16), tzinfo=timezone.utc)
        settlement_at = _time(event.get("resolved_at") or event.get("settlement_at"))
        event_cutoffs = [cutoff_at]
        for snapshot in snapshots_by_date.get(target_date.isoformat(), []):
            available = _time(snapshot.get("as_of_eligible_at"))
            if available is None or available <= cutoff_at or available >= event_end:
                continue
            if settlement_at is not None and available >= settlement_at:
                continue
            event_cutoffs.append(available)
        for current_cutoff in sorted(set(event_cutoffs)):
            is_start = current_cutoff == cutoff_at
            digest = hashlib.sha256(current_cutoff.isoformat().encode("utf-8")).hexdigest()[:12]
            cutoffs.append({
                "event_id": event_id,
                "event_date_local": target_date.isoformat(),
                "cutoff_id": "event_day_start_local" if is_start else f"forecast_availability_{digest}",
                "cutoff_at": current_cutoff.isoformat().replace("+00:00", "Z"),
                "lead_hours": (event_end - current_cutoff).total_seconds() / 3600.0,
                "allow_prior_named_cutoff": not is_start,
                "context": {},
                "context_contract": {
                    "known_at_cutoff_only": True,
                    "settlement_fields_consumed": False,
                    "weather_features_added_from_selected_forecast_curves": True,
                    "lead_hours_definition": "hours_from_cutoff_to_local_event_day_end_boundary",
                    "cutoff_selection": "event_day_start_or_external_forecast_first_availability",
                },
            })
        trace.append({"event_id": event_id, "state": "available", "cutoff_count": len(set(event_cutoffs))})
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "cutoff_count": len(cutoffs),
        "cutoffs": cutoffs,
        "trace": trace,
        "guards": {
            "reads_existing_local_history": False,
            "uses_settlement_outcome_as_feature": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _load_events(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("events") if isinstance(value, Mapping) else value
    if not isinstance(rows, list):
        raise ValueError("market_events_missing")
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("cutoff_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-events", type=Path, required=True)
    parser.add_argument("--forecast-snapshots", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    snapshots = []
    for path in args.forecast_snapshots:
        value = json.loads(path.read_text(encoding="utf-8"))
        rows = value.get("forecast_snapshots") if isinstance(value, Mapping) else value
        if not isinstance(rows, list):
            raise ValueError(f"forecast_snapshots_missing:{path}")
        snapshots.extend(dict(row) for row in rows if isinstance(row, Mapping))
    report = build_event_day_start_cutoffs(_load_events(args.market_events), forecast_snapshots=snapshots)
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
