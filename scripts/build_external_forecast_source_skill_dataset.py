#!/usr/bin/env python3
"""Build strict as-of external source score rows without reading local history."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from external_forecast_snapshot_runtime import default_provider_family
from forecast_source_bucket_distribution_runtime import build_forecast_source_bucket_distribution
from forecast_source_scoring_runtime import score_forecast_source_event


SCHEMA_VERSION = "external_forecast_source_skill_dataset_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        output = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if output.tzinfo is None:
        output = output.replace(tzinfo=timezone.utc)
    return output.astimezone(timezone.utc)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _snapshot_id(row: Mapping[str, Any]) -> str:
    return str(
        row.get("forecast_snapshot_id")
        or row.get("source_event_id")
        or row.get("response_sha256")
        or ""
    ).strip()


def _immutable_lineage(value: Any) -> bool:
    return str(value or "").startswith("immutable_")


def _curve_temperatures(source: Mapping[str, Any]) -> list[float]:
    rows = source.get("source_rows") if isinstance(source.get("source_rows"), Sequence) else []
    values: list[float] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        curve = row.get("hourly_curve")
        if isinstance(curve, Sequence) and not isinstance(curve, (str, bytes, bytearray)):
            for point in curve:
                if isinstance(point, Mapping):
                    value = _float(point.get("temperature_c"))
                    if value is not None:
                        values.append(value)
        else:
            value = _float(row.get("temperature_c"))
            if value is not None:
                values.append(value)
    return values


def build_cutoff_weather_context(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive smooth, ex-ante context only from forecasts selected at the cutoff."""

    peaks = [value for value in (_float(row.get("forecast_peak_c")) for row in selected) if value is not None]
    ranges: list[float] = []
    warming: list[float] = []
    for source in selected:
        values = _curve_temperatures(source)
        if not values:
            continue
        ranges.append(max(values) - min(values))
        warming.append(max(values) - values[0])
    context: dict[str, Any] = {
        "forecast_source_count": float(len(peaks)),
        "forecast_consensus_peak_c": statistics.fmean(peaks) if peaks else None,
        "forecast_peak_spread_c": statistics.pstdev(peaks) if len(peaks) >= 2 else 0.0 if peaks else None,
        "forecast_consensus_diurnal_range_c": statistics.fmean(ranges) if ranges else None,
        "forecast_consensus_warming_from_day_start_c": statistics.fmean(warming) if warming else None,
    }
    return {
        key: float(value)
        for key, value in context.items()
        if value is not None and math.isfinite(float(value))
    }


def _eligible_row(
    row: Mapping[str, Any],
    *,
    market_event: Mapping[str, Any],
    cutoff: datetime,
    cutoff_id: str = "",
    allow_prior_named_cutoff: bool = False,
) -> tuple[bool, str]:
    if str(row.get("source_class") or "forecast_weak_source") != "forecast_weak_source":
        return False, "source_class_not_forecast_weak_source"
    if str(row.get("station_id") or "") != str(market_event.get("station_id") or ""):
        return False, "station_id_mismatch"
    if str(row.get("target_event_date_local") or "") != str(market_event.get("event_date_local") or ""):
        return False, "event_date_mismatch"
    snapshot_cutoff_id = str(row.get("cutoff_id") or "")
    if snapshot_cutoff_id and cutoff_id and snapshot_cutoff_id != cutoff_id:
        snapshot_cutoff_at = _parse_datetime(row.get("cutoff_at"))
        if not allow_prior_named_cutoff or (snapshot_cutoff_at is not None and snapshot_cutoff_at > cutoff):
            return False, "forecast_cutoff_id_mismatch"
    eligible_at = _parse_datetime(row.get("as_of_eligible_at"))
    if eligible_at is None:
        return False, "as_of_eligible_at_missing"
    if eligible_at > cutoff:
        return False, "forecast_future_at_cutoff"
    if not _snapshot_id(row):
        return False, "forecast_snapshot_id_missing"
    return True, ""


def select_strict_as_of_source_forecasts(
    *,
    market_event: Mapping[str, Any],
    cutoff_at: str,
    cutoff_id: str = "",
    forecast_snapshots: Sequence[Mapping[str, Any]],
    allow_prior_named_cutoff: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cutoff = _parse_datetime(cutoff_at)
    if cutoff is None:
        return [], {"state": "unavailable", "state_reason": "cutoff_at_invalid", "excluded_counts": {}}
    excluded_counts: dict[str, int] = {}
    eligible: list[dict[str, Any]] = []
    for raw in forecast_snapshots:
        row = dict(raw)
        ok, reason = _eligible_row(
            row,
            market_event=market_event,
            cutoff=cutoff,
            cutoff_id=cutoff_id,
            allow_prior_named_cutoff=allow_prior_named_cutoff,
        )
        if not ok:
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            continue
        eligible.append(row)

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in eligible:
        groups.setdefault((str(row.get("source_id") or ""), _snapshot_id(row)), []).append(row)
    chosen_by_source: dict[str, tuple[tuple[datetime, datetime, str], list[dict[str, Any]]]] = {}
    for (source_id, snapshot_id), rows in groups.items():
        eligible_at = max(_parse_datetime(row.get("as_of_eligible_at")) for row in rows)
        issued_values = [_parse_datetime(row.get("issued_at")) for row in rows]
        issued_at = max((value for value in issued_values if value is not None), default=eligible_at)
        assert eligible_at is not None
        key = (eligible_at, issued_at, snapshot_id)
        previous = chosen_by_source.get(source_id)
        if previous is None or key > previous[0]:
            chosen_by_source[source_id] = (key, rows)

    selected = []
    used = {}
    for source_id, (selection_key, rows) in sorted(chosen_by_source.items()):
        temperatures = [
            value
            for value in (_float(row.get("temperature_c")) for row in rows)
            if value is not None
        ]
        first = rows[0]
        explicit = next(
            (row.get("bucket_probabilities") for row in rows if isinstance(row.get("bucket_probabilities"), Mapping)),
            None,
        )
        members = next(
            (
                row.get("member_daily_peaks_c")
                for row in rows
                if isinstance(row.get("member_daily_peaks_c"), Sequence)
                and not isinstance(row.get("member_daily_peaks_c"), (str, bytes, bytearray))
            ),
            None,
        )
        source_forecast = {
            "source_id": source_id,
            "provider_family": str(first.get("provider_family") or default_provider_family(source_id)),
            "forecast_snapshot_id": _snapshot_id(first),
            "forecast_snapshot_ids": sorted({_snapshot_id(row) for row in rows}),
            "forecast_peak_c": _float(first.get("forecast_peak_c")) if first.get("forecast_peak_c") is not None else (max(temperatures) if temperatures else None),
            "temperature_unit": str(first.get("temperature_unit") or "C"),
            "bucket_probabilities": explicit,
            "member_daily_peaks_c": members,
            "member_prior_strength": _float(first.get("member_prior_strength")) or 0.25,
            "bias": _float(first.get("bias")) or 0.0,
            "residual_sigma": _float(first.get("residual_sigma")) or 1.5,
            "lineage_quality": (
                str(first.get("lineage_quality") or "immutable_external_evidence")
                if all(_immutable_lineage(row.get("lineage_quality")) for row in rows)
                and len({str(row.get("lineage_quality") or "") for row in rows}) == 1
                else "immutable_external_evidence"
                if all(_immutable_lineage(row.get("lineage_quality")) for row in rows)
                else "weak_or_mixed_lineage"
            ),
            "source_rows": rows,
        }
        selected.append(source_forecast)
        used[source_id] = {
            "forecast_snapshot_id": source_forecast["forecast_snapshot_id"],
            "as_of_eligible_at": selection_key[0].isoformat().replace("+00:00", "Z"),
            "issued_at": selection_key[1].isoformat().replace("+00:00", "Z"),
            "row_count": len(rows),
            "provider_family": source_forecast["provider_family"],
            "lineage_quality": source_forecast["lineage_quality"],
        }
    return selected, {
        "state": "available" if selected else "unavailable",
        "state_reason": "" if selected else "no_strict_as_of_forecast_snapshots",
        "cutoff_at": cutoff_at,
        "eligible_row_count": len(eligible),
        "selected_source_count": len(selected),
        "used_sources": used,
        "excluded_counts": dict(sorted(excluded_counts.items())),
    }


def build_external_forecast_source_skill_dataset(
    *,
    market_events: Sequence[Mapping[str, Any]],
    forecast_snapshots: Sequence[Mapping[str, Any]],
    cutoffs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    events = {str(event.get("event_id") or ""): dict(event) for event in market_events if event.get("event_id")}
    score_rows: list[dict[str, Any]] = []
    pairing_trace: list[dict[str, Any]] = []
    for cutoff in cutoffs:
        event_id = str(cutoff.get("event_id") or "")
        event = events.get(event_id)
        if event is None:
            pairing_trace.append({"event_id": event_id, "state": "unavailable", "state_reason": "market_event_missing"})
            continue
        cutoff_at = str(cutoff.get("cutoff_at") or "")
        cutoff_id = str(cutoff.get("cutoff_id") or "")
        selected, trace = select_strict_as_of_source_forecasts(
            market_event=event,
            cutoff_at=cutoff_at,
            cutoff_id=cutoff_id,
            forecast_snapshots=forecast_snapshots,
            allow_prior_named_cutoff=bool(cutoff.get("allow_prior_named_cutoff")),
        )
        base_context = dict(cutoff.get("context") or {}) if isinstance(cutoff.get("context"), Mapping) else {}
        derived_context = build_cutoff_weather_context(selected)
        overlap = set(base_context) & set(derived_context)
        if overlap:
            raise ValueError(f"derived_context_conflicts_with_cutoff_context:{','.join(sorted(overlap))}")
        context = {**base_context, **derived_context}
        pairing_trace.append(
            {
                "event_id": event_id,
                "cutoff_id": cutoff_id,
                **trace,
                "context": context,
                "context_lineage": "selected_strict_as_of_forecast_curves_only",
            }
        )
        for source in selected:
            distribution = build_forecast_source_bucket_distribution(event, source)
            if distribution.get("state") != "available":
                score_rows.append(
                    {
                        "schema": "forecast_source_score_event_v1",
                        "state": "unavailable",
                        "event_id": event_id,
                        "cutoff_id": cutoff_id,
                        "source_id": source["source_id"],
                        "provider_family": source["provider_family"],
                        "score_na_reason": distribution.get("state_reason"),
                    }
                )
                continue
            score_row = score_forecast_source_event(
                market_event=event,
                bucket_probabilities=distribution["bucket_probabilities"],
                source_id=source["source_id"],
                provider_family=source["provider_family"],
                cutoff_id=cutoff_id,
                cutoff_at=cutoff_at,
                forecast_peak=source.get("forecast_peak_c"),
                forecast_snapshot_ids=source.get("forecast_snapshot_ids"),
                lineage_quality=source["lineage_quality"],
                coverage_quality="complete",
            )
            score_row["lead_hours"] = _float(cutoff.get("lead_hours"))
            score_row["context"] = context
            score_row["context_lineage"] = "selected_strict_as_of_forecast_curves_only"
            score_row["settlement_at"] = str(event.get("settlement_at") or event.get("resolved_at") or "")
            score_rows.append(score_row)
    available = [row for row in score_rows if row.get("state") == "available"]
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "market_event_count": len(events),
        "forecast_snapshot_row_count": len(forecast_snapshots),
        "cutoff_count": len(cutoffs),
        "score_row_count": len(score_rows),
        "available_score_row_count": len(available),
        "paired_event_count": len({str(row.get("event_id") or "") for row in available}),
        "score_rows": score_rows,
        "pairing_trace": pairing_trace,
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


def _load_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    raw = json.loads(text)
    if isinstance(raw, list):
        return [dict(row) for row in raw if isinstance(row, Mapping)]
    if isinstance(raw, Mapping):
        for key in ("events", "forecast_snapshots", "cutoffs"):
            if isinstance(raw.get(key), list):
                return [dict(row) for row in raw[key] if isinstance(row, Mapping)]
    raise ValueError(f"unsupported row file: {path}")


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("skill_dataset_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-events", type=Path, required=True)
    parser.add_argument("--forecast-snapshots", type=Path, action="append", required=True)
    parser.add_argument("--cutoffs", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    report = build_external_forecast_source_skill_dataset(
        market_events=_load_rows(args.market_events),
        forecast_snapshots=[row for path in args.forecast_snapshots for row in _load_rows(path)],
        cutoffs=_load_rows(args.cutoffs),
    )
    if args.output:
        if not args.allow_research_write:
            raise SystemExit("--output requires --allow-research-write")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["output_path"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
