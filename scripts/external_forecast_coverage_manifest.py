#!/usr/bin/env python3
"""Build a no-download request and coverage manifest for external source skill data."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"
SCHEMA_VERSION = "external_forecast_coverage_manifest_v1"
OPEN_METEO_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
OPEN_METEO_SINGLE_RUNS_URL = "https://single-runs-api.open-meteo.com/v1/forecast"

DEFAULT_MODELS = (
    ("ecmwf", "ecmwf_ifs025", "nwp_ecmwf"),
    ("gfs", "gfs_seamless", "nwp_gfs"),
    ("icon", "icon_seamless", "nwp_icon"),
)

SINGLE_RUN_AVAILABLE_FROM = {
    "ecmwf_ifs025": date(2024, 3, 14),
    "gfs_seamless": date(2026, 4, 2),
    "icon_seamless": date(2026, 4, 2),
}
CANONICAL_RUN_CYCLE_UTC_HOUR = 6
GLOBAL_RUN_CONSERVATIVE_AVAILABILITY_DELAY = timedelta(hours=6, minutes=10)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date(value: Any) -> date | None:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _date_chunks(values: Sequence[date], *, max_span_days: int = 31) -> list[tuple[date, date]]:
    if not values:
        return []
    ordered = sorted(set(values))
    chunks: list[tuple[date, date]] = []
    start = ordered[0]
    previous = ordered[0]
    for current in ordered[1:]:
        if (current - start).days >= max(1, int(max_span_days)) or (current - previous).days > 1:
            chunks.append((start, previous))
            start = current
        previous = current
    chunks.append((start, previous))
    return chunks


def _previous_runs_url(*, model: str, start: date, end: date, latitude: float, longitude: float) -> str:
    hourly = ",".join(f"temperature_2m_previous_day{day}" for day in range(0, 8))
    parameters = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": hourly,
        "models": model,
        "timezone": "Asia/Shanghai",
        "temperature_unit": "celsius",
    }
    return f"{OPEN_METEO_PREVIOUS_RUNS_URL}?{urlencode(parameters)}"


def _single_run_url(*, model: str, run: str, latitude: float, longitude: float) -> str:
    parameters = {
        "latitude": latitude,
        "longitude": longitude,
        "run": run,
        "hourly": "temperature_2m",
        "models": model,
        "timezone": "Asia/Shanghai",
        "temperature_unit": "celsius",
        "forecast_days": 3,
    }
    return f"{OPEN_METEO_SINGLE_RUNS_URL}?{urlencode(parameters)}"


def _exact_single_run_requests(
    *,
    event_dates: Sequence[date],
    models: Sequence[tuple[str, str, str]],
    latitude: float,
    longitude: float,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for event_date in event_dates:
        run_date = event_date - timedelta(days=1)
        run_at = datetime.combine(run_date, time(CANONICAL_RUN_CYCLE_UTC_HOUR), tzinfo=timezone.utc)
        conservative_available_at = run_at + GLOBAL_RUN_CONSERVATIVE_AVAILABILITY_DELAY
        cutoff_at = datetime.combine(run_date, time(16), tzinfo=timezone.utc)  # event-day 00:00 Asia/Shanghai
        for source_id, model, provider_family in models:
            available_from = SINGLE_RUN_AVAILABLE_FROM.get(model)
            if available_from is not None and run_at.date() < available_from:
                continue
            run = run_at.strftime("%Y-%m-%dT%H:%M")
            requests.append(
                {
                    "source_id": f"{source_id}_model_peak",
                    "provider_family": provider_family,
                    "model": model,
                    "target_event_date_local": event_date.isoformat(),
                    "cutoff_id": "event_day_start_local",
                    "cutoff_at": cutoff_at.isoformat().replace("+00:00", "Z"),
                    "run": run,
                    "run_initialized_at": run_at.isoformat().replace("+00:00", "Z"),
                    "provider_available_at_conservative": conservative_available_at.isoformat().replace("+00:00", "Z"),
                    "availability_quality": "documented_global_upper_bound_plus_eventual_consistency_buffer",
                    "eligible_for_exact_cutoff_pairing": conservative_available_at <= cutoff_at,
                    "url": _single_run_url(
                        model=model,
                        run=run,
                        latitude=latitude,
                        longitude=longitude,
                    ),
                }
            )
    return requests


def _event_dates(discovery: Mapping[str, Any]) -> list[date]:
    dates = []
    for event in discovery.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        parsed = _date(event.get("event_date_local"))
        if parsed:
            dates.append(parsed)
    return sorted(set(dates))


def build_external_forecast_coverage_manifest(
    discovery: Mapping[str, Any],
    *,
    as_of_date: date,
    latitude: float = 30.5785,
    longitude: float = 103.9471,
    models: Sequence[tuple[str, str, str]] = DEFAULT_MODELS,
    chunk_days: int = 31,
    estimated_previous_run_response_mb: float = 0.35,
    estimated_single_run_response_mb: float = 0.08,
    twc_api_key_available: bool = False,
) -> dict[str, Any]:
    event_dates = _event_dates(discovery)
    chunks = _date_chunks(event_dates, max_span_days=chunk_days)
    previous_requests = []
    for source_id, model, provider_family in models:
        for start, end in chunks:
            previous_requests.append(
                {
                    "source_id": f"{source_id}_model_peak",
                    "provider_family": provider_family,
                    "model": model,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "url": _previous_runs_url(
                        model=model,
                        start=start,
                        end=end,
                        latitude=latitude,
                        longitude=longitude,
                    ),
                    "evidence_tier": "fixed_24h_lead_diagnostic",
                    "eligible_for_exact_cutoff_pairing": False,
                    "exact_cutoff_na_reason": "previous_runs_has_fixed_lead_offset_not_individual_run_identity",
                }
            )

    exact_single_requests = _exact_single_run_requests(
        event_dates=event_dates,
        models=models,
        latitude=latitude,
        longitude=longitude,
    )
    exact_sources_by_day: dict[str, set[str]] = {}
    for row in exact_single_requests:
        if row.get("eligible_for_exact_cutoff_pairing"):
            exact_sources_by_day.setdefault(str(row["target_event_date_local"]), set()).add(str(row["source_id"]))
    required_source_count = len(models)
    exact_triplet_days = sum(len(sources) == required_source_count for sources in exact_sources_by_day.values())

    single_run_examples = []
    for event_date in event_dates[:1]:
        run_day = event_date - timedelta(days=1)
        for source_id, model, provider_family in models:
            for cycle in ("00:00", "06:00", "12:00", "18:00"):
                single_run_examples.append(
                    {
                        "source_id": f"{source_id}_model_peak",
                        "provider_family": provider_family,
                        "model": model,
                        "run": f"{run_day.isoformat()}T{cycle}",
                        "url": _single_run_url(
                            model=model,
                            run=f"{run_day.isoformat()}T{cycle}",
                            latitude=latitude,
                            longitude=longitude,
                        ),
                    }
                )
    single_run_request_upper_bound = len(event_dates) * len(models) * 4
    previous_mb = len(previous_requests) * max(0.0, float(estimated_previous_run_response_mb))
    single_mb = single_run_request_upper_bound * max(0.0, float(estimated_single_run_response_mb))

    awc_cutoff = as_of_date - timedelta(days=15)
    taf_official_window = [value for value in event_dates if value >= awc_cutoff]
    taf_forward_only = [value for value in event_dates if value < awc_cutoff]
    source_coverage = {
        "polymarket": {
            "state": "available" if event_dates else "unavailable",
            "resolved_event_day_count": len(event_dates),
            "source": "Gamma public-search/list events",
        },
        "three_model_previous_runs": {
            "state": "planned" if event_dates else "blocked_no_market_dates",
            "model_count": len(models),
            "request_count": len(previous_requests),
            "historical_download_executed": False,
        },
        "single_runs_replay": {
            "state": "exact_run_required_for_strict_cutoff_pairing",
            "exact_minimum_request_count": len(exact_single_requests),
            "exact_triplet_event_day_count": exact_triplet_days,
            "exact_request_plan": "one conservative 06Z run per model per event-day; available before local event-day start",
            "request_upper_bound": single_run_request_upper_bound,
            "example_requests": single_run_examples,
            "historical_download_executed": False,
        },
        "taf": {
            "state": "official_15_day_window_plus_forward_only",
            "official_window_event_days": len(taf_official_window),
            "older_event_days_forward_only": len(taf_forward_only),
            "older_history_not_backfilled": True,
        },
        "wu_forecast": {
            "state": "forward_only_until_issued_forecast_archive_is_authoritative",
            "historical_download_executed": False,
        },
        "twc_forecast": {
            "state": "forward_snapshot_ready" if twc_api_key_available else "blocked_missing_api_credential",
            "api_key_value_persisted": False,
            "historical_forecast_archive_required_for_backfill": True,
        },
    }
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "mode": "dry_run_no_download",
        "source_data_policy": "external_only",
        "station_id": "ZUUU",
        "latitude": latitude,
        "longitude": longitude,
        "market_event_day_count": len(event_dates),
        "event_date_min": event_dates[0].isoformat() if event_dates else None,
        "event_date_max": event_dates[-1].isoformat() if event_dates else None,
        "event_dates": [value.isoformat() for value in event_dates],
        "date_chunk_count": len(chunks),
        "previous_runs_requests": previous_requests,
        "single_runs_exact_requests": exact_single_requests,
        "source_coverage": source_coverage,
        "estimated_impact": {
            "previous_runs_request_count": len(previous_requests),
            "single_runs_request_upper_bound": single_run_request_upper_bound,
            "single_runs_exact_request_count": len(exact_single_requests),
            "estimated_previous_runs_mb": round(previous_mb, 3),
            "estimated_single_runs_exact_mb": round(len(exact_single_requests) * max(0.0, float(estimated_single_run_response_mb)), 3),
            "estimated_single_runs_upper_bound_mb": round(single_mb, 3),
            "estimated_recommended_exact_total_mb": round(len(exact_single_requests) * max(0.0, float(estimated_single_run_response_mb)), 3),
            "estimated_total_upper_bound_mb": round(previous_mb + single_mb, 3),
            "research_root": str(DEFAULT_RESEARCH_ROOT),
        },
        "confirmation": {
            "historical_download_requires_confirmation": False,
            "bounded_fetch_flags_required": True,
            "scope": "Open-Meteo exact Single Runs raw writes under research namespace; Previous Runs optional diagnostics only",
        },
        "guards": {
            "downloads_historical_forecasts": False,
            "writes_research_files": False,
            "reads_existing_local_history": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "causal_contract": {
            "previous_runs_exact_cutoff_eligible": False,
            "single_runs_exact_cutoff_required": True,
            "run_initialization_is_not_provider_availability": True,
            "conservative_global_availability_delay_minutes": int(GLOBAL_RUN_CONSERVATIVE_AVAILABILITY_DELAY.total_seconds() / 60),
            "eventual_consistency_buffer_included": True,
            "source": "Open-Meteo official Previous Runs, Single Runs, and model updates documentation",
        },
        "next_boundary": "execute_explicit_bounded_fetch_when_requested",
    }


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("coverage_manifest_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-json", type=Path, required=True)
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--chunk-days", type=int, default=31)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    discovery = json.loads(args.discovery_json.read_text(encoding="utf-8"))
    as_of = _date(args.as_of_date)
    if as_of is None:
        raise SystemExit("invalid --as-of-date")
    report = build_external_forecast_coverage_manifest(
        discovery,
        as_of_date=as_of,
        chunk_days=max(1, min(366, int(args.chunk_days))),
        twc_api_key_available=bool(os.environ.get("TWC_API_KEY") or os.environ.get("WEATHER_COM_API_KEY")),
    )
    if args.output:
        if not args.allow_research_write:
            raise SystemExit("--output requires --allow-research-write")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report["guards"]["writes_research_files"] = True
        report["output_path"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
