"""Read-only 7/30/90-day measurement and keep/rollback recommendation."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "forecast_source_weight_measurement_v1"


def _date(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def build_source_weight_measurement(
    settled_rows: Sequence[Mapping[str, Any]],
    *,
    applied_at: str,
    as_of_date: str = "",
    windows: Sequence[int] = (7, 30, 90),
    minimum_conclusive_event_days: int = 20,
    relative_harm_tolerance: float = 0.0,
) -> dict[str, Any]:
    applied_date = _date(applied_at)
    if applied_date is None:
        raise ValueError("forecast_source_weight_applied_at_invalid")
    parsed = [(row, _date(row.get("event_date_local") or row.get("event_date"))) for row in settled_rows]
    available_dates = [value for _row, value in parsed if value is not None]
    as_of = _date(as_of_date) or (max(available_dates) if available_dates else date.today())
    payloads: dict[str, Any] = {}
    rollback_reasons: list[str] = []
    for raw_window in windows:
        window = max(1, int(raw_window))
        end = min(as_of, applied_date + timedelta(days=window - 1))
        rows = [row for row, event_date in parsed if event_date is not None and applied_date <= event_date <= end]
        days = {str(row.get("event_date_local") or row.get("event_date") or "")[:10] for row in rows}
        metrics: dict[str, Any] = {}
        conclusive = len(days) >= int(minimum_conclusive_event_days)
        harmed = False
        for metric in ("nll", "rps"):
            champion_values = [_float(row.get("champion", {}).get(metric)) for row in rows if isinstance(row.get("champion"), Mapping)]
            challenger_values = [_float(row.get("challenger", {}).get(metric)) for row in rows if isinstance(row.get("challenger"), Mapping)]
            paired = [(left, right) for left, right in zip(champion_values, challenger_values) if left is not None and right is not None]
            champion = sum(left for left, _right in paired) / len(paired) if paired else None
            challenger = sum(right for _left, right in paired) / len(paired) if paired else None
            relative_harm = (
                (challenger - champion) / max(abs(champion), 1e-12)
                if champion is not None and challenger is not None
                else None
            )
            metric_harmed = bool(conclusive and relative_harm is not None and relative_harm > float(relative_harm_tolerance))
            harmed = harmed or metric_harmed
            metrics[metric] = {
                "paired_count": len(paired),
                "champion": champion,
                "challenger": challenger,
                "relative_harm": relative_harm,
                "harm_tolerance": float(relative_harm_tolerance),
                "harmed": metric_harmed,
            }
        key = f"{window}d"
        if harmed:
            rollback_reasons.append(f"{key}_nll_or_rps_regression")
        payloads[key] = {
            "window_days": window,
            "window_start_date": applied_date.isoformat(),
            "window_end_date": end.isoformat(),
            "event_day_count": len(days),
            "conclusive": conclusive,
            "metrics": metrics,
            "recommendation": "rollback" if harmed else "keep" if conclusive else "continue_measurement",
        }
    return {
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "applied_at": applied_at,
        "as_of_date": as_of.isoformat(),
        "windows": payloads,
        "recommendation": "rollback" if rollback_reasons else "keep" if all(row["conclusive"] for row in payloads.values()) else "continue_measurement",
        "rollback_reasons": rollback_reasons,
        "rollback_requires_retraining": False,
        "guards": {
            "writes_archive_db": False,
            "changes_parameters": False,
            "performs_rollback": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_source_weight_measurement"]
