"""Strict as-of, mutually-exclusive window replay for calibration evidence.

This module is an offline evidence boundary.  It accepts frozen official-model
hourly rows and frozen observation rows, materializes one station/day sample,
checks the run/observation lineage and local-day boundary, then evaluates
disjoint 3/7/15/30-day windows plus a cross-season/cross-station view.

It intentionally does not read the production database, probability state,
market state or order gateway.  A passing receipt is evidence for calibration
replay only; it is never an activation or order-authorization signal.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    MIN_FORMAL_ENSEMBLE_MODELS,
    ContractError,
    content_sha256,
)
from .model_run_contract import ALLOWED_RUN_HOURS_UTC
from .model_sources import ALLOWED_MODEL_PROVIDERS, MODEL_ROLES, provider_role


SCHEMA = "bayes_basket_target_strict_asof_replay_v1"
WINDOW_DAYS = (3, 7, 15, 30)
# Operational/reporting callers may still use this constant explicitly.
# Formal calibration APIs below deliberately have no timezone default: their
# caller must bind the market registry's settlement timezone.
DEFAULT_TARGET_TIMEZONE = "America/New_York"
MODEL_PROVIDERS = ("ecmwf_ifs025_official", "gfs_0p25_official", "icon_global_official")
MATURITY_D_MINUS_1_UTC_DATE = "D-1_UTC_DATE_TO_TARGET_ET_DAY"
MATURITY_D_MINUS_2_UTC_DATE_18Z = "D-2_UTC_DATE_18Z_TO_TARGET_DAY"
MATURITY_D_MINUS_2_UTC_DATE_12Z = "D-2_UTC_DATE_12Z_TO_TARGET_DAY"
MATURITY_PRE_ET_SAME_UTC_DATE_00Z = "PRE_ET_SAME_UTC_DATE_00Z"
MATURITY_D0_TARGET_ET_DAY = "D0_WITHIN_TARGET_ET_DAY"
MATURITY_D2_PREVIOUS_RELEASE_FALLBACK = "D2_PREVIOUS_RELEASE_FALLBACK"
# ``maturity_key`` is the transport/time-proof identity. ``lead_bucket`` is
# the stable calibration/policy key. Keep them separate so a verbose maturity
# proof cannot leak into an artifact scope that expects d1/d2.
LEAD_BUCKET_D1 = "d1"
LEAD_BUCKET_D2 = "d2"
LEAD_BUCKET_D0 = "d0"
LEAD_BUCKET_D2_FALLBACK = "d2_fallback"
LEAD_BUCKET_BY_MATURITY = {
    # The same-UTC-date 00Z run is the latest run still before the ET target
    # day starts, so it is d1. The prior UTC-date run is the earlier d2 bucket.
    MATURITY_PRE_ET_SAME_UTC_DATE_00Z: LEAD_BUCKET_D1,
    MATURITY_D_MINUS_1_UTC_DATE: LEAD_BUCKET_D2,
    MATURITY_D_MINUS_2_UTC_DATE_18Z: LEAD_BUCKET_D2,
    MATURITY_D_MINUS_2_UTC_DATE_12Z: LEAD_BUCKET_D2,
    MATURITY_D0_TARGET_ET_DAY: LEAD_BUCKET_D0,
    MATURITY_D2_PREVIOUS_RELEASE_FALLBACK: LEAD_BUCKET_D2_FALLBACK,
}


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid_number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid_number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid_number")
    return result


def _timestamp(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return result.astimezone(timezone.utc)


def _local_date(name: str, value: Any) -> date:
    raw = _text(name, value)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_date") from exc


def _season(event_date: date) -> str:
    if event_date.month in (12, 1, 2):
        return "winter"
    if event_date.month in (3, 4, 5):
        return "spring"
    if event_date.month in (6, 7, 8):
        return "summer"
    return "autumn"


def season_bin_for_event_date(
    event_date: str | date,
    *,
    latitude: float | None = None,
) -> str:
    """Return the meteorological season, flipped south of the equator."""

    parsed = event_date if isinstance(event_date, date) else _local_date("event_date", event_date)
    season = _season(parsed)
    if latitude is None or float(latitude) >= 0.0:
        return season
    return {
        "winter": "summer",
        "spring": "autumn",
        "summer": "winter",
        "autumn": "spring",
    }[season]


def _target_bounds(event_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("target_timezone_invalid") from exc
    start = datetime.combine(event_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _maturity_key(run_initialization_utc: datetime, event_date: date, timezone_name: str) -> str:
    target_start, _ = _target_bounds(event_date, timezone_name)
    _, target_end = _target_bounds(event_date, timezone_name)
    if run_initialization_utc >= target_end:
        raise ContractError("model_run_after_target_day")
    # A same UTC-date 00Z run is valid whenever it landed before the ET day
    # began; the old UTC-calendar-date-only rule incorrectly rejected that
    # case. Prior UTC-date official cycles remain a separate maturity key.
    if (
        run_initialization_utc.hour not in ALLOWED_RUN_HOURS_UTC
        or run_initialization_utc.minute
        or run_initialization_utc.second
        or run_initialization_utc.microsecond
    ):
        raise ContractError("model_run_maturity_drift")
    if run_initialization_utc >= target_start:
        return MATURITY_D0_TARGET_ET_DAY
    if run_initialization_utc.date() == event_date - timedelta(days=1):
        return MATURITY_D_MINUS_1_UTC_DATE
    # Explicit 12Z and 18Z cycles two UTC dates before the target are stable
    # d2 scopes.  GEM/GDPS has only 00Z/12Z runs, so requiring 18Z for every
    # model would fabricate a nonexistent cycle.
    if (
        run_initialization_utc.date() == event_date - timedelta(days=2)
        and run_initialization_utc.hour == 12
    ):
        return MATURITY_D_MINUS_2_UTC_DATE_12Z
    if (
        run_initialization_utc.date() == event_date - timedelta(days=2)
        and run_initialization_utc.hour == 18
    ):
        return MATURITY_D_MINUS_2_UTC_DATE_18Z
    if run_initialization_utc.date() == event_date:
        return MATURITY_PRE_ET_SAME_UTC_DATE_00Z
    if 0.0 < (target_start - run_initialization_utc).total_seconds() / 3600.0 <= 42.0:
        return MATURITY_D2_PREVIOUS_RELEASE_FALLBACK
    raise ContractError("model_run_maturity_drift")


def maturity_key_for_model_run(
    run_initialization_utc: str | datetime,
    event_date: str | date,
    *,
    target_timezone: str,
) -> str:
    """Classify a real model initialization into its immutable maturity scope."""

    run_init = (
        run_initialization_utc.astimezone(timezone.utc)
        if isinstance(run_initialization_utc, datetime)
        else _timestamp("run_initialization_utc", run_initialization_utc)
    )
    parsed_date = event_date if isinstance(event_date, date) else _local_date("event_date", event_date)
    return _maturity_key(run_init, parsed_date, target_timezone)


def lead_bucket_for_maturity(maturity_key: str) -> str:
    """Bind calibration scope to the exact strict-as-of maturity contract."""

    try:
        return LEAD_BUCKET_BY_MATURITY[str(maturity_key).strip()]
    except KeyError as exc:
        raise ContractError("model_run_maturity_key_invalid") from exc


def _lineage_sha(value: Mapping[str, Any]) -> str:
    return content_sha256(value)


def materialize_model_station_days(
    hourly_rows: Iterable[Mapping[str, Any]],
    *,
    target_timezone: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate official hourly model rows into strict-as-of station/day runs.

    Every input row must carry a raw model message SHA and receipt SHA.  The
    target local-day check prevents a forecast row from another day being
    silently folded into the label.  Multiple providers are retained as
    separate forecast columns, while the maturity key must be identical for
    every accepted run.
    """

    grouped: dict[tuple[str, date, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in hourly_rows:
        if not isinstance(raw, Mapping):
            raise ContractError("model_row_mapping_required")
        station_id = _text("station_id", raw.get("station_id"))
        event_date = _local_date("event_date", raw.get("event_date"))
        provider_id = _text("provider_id", raw.get("provider_id"))
        if provider_id not in ALLOWED_MODEL_PROVIDERS:
            raise ContractError("model_provider_not_allowed")
        capture_mode = str(raw.get("capture_mode") or "prospective").strip().lower()
        if provider_id == "icon_global_open_meteo" and capture_mode != "historical_rebuild":
            raise ContractError("aggregator_prospective_not_allowed")
        run_init = _timestamp("model_run_initialization_utc", raw.get("model_run_initialization_utc"))
        valid_time = _timestamp("valid_time_utc", raw.get("valid_time_utc"))
        target_start, target_end = _target_bounds(event_date, target_timezone)
        if not target_start <= valid_time < target_end:
            raise ContractError("forecast_valid_time_outside_target_local_day")
        maturity_key = _maturity_key(run_init, event_date, target_timezone)
        _number("temperature_2m_c", raw.get("temperature_2m_c"))
        raw_sha = _sha("raw_message_sha256", raw.get("raw_message_sha256"))
        receipt_sha = _sha("receipt_sha256", raw.get("receipt_sha256"))
        published_raw = str(raw.get("published_at_utc") or "").strip()
        received_raw = str(raw.get("received_at_utc") or "").strip()
        published_at = _timestamp("published_at_utc", published_raw) if published_raw else None
        received_at = _timestamp("received_at_utc", received_raw) if received_raw else None
        if published_at is not None and received_at is not None and published_at > received_at:
            raise ContractError("model_published_after_received")
        grouped[(station_id, event_date, provider_id)].append(
            {
                "station_id": station_id,
                "event_date": event_date.isoformat(),
                "provider_id": provider_id,
                "run_initialization_utc": run_init.isoformat().replace("+00:00", "Z"),
                "valid_time_utc": valid_time.isoformat().replace("+00:00", "Z"),
                "temperature_2m_c": _number("temperature_2m_c", raw.get("temperature_2m_c")),
                "raw_message_sha256": raw_sha,
                "receipt_sha256": receipt_sha,
                "maturity_key": maturity_key,
                "published_at_utc": published_at.isoformat().replace("+00:00", "Z") if published_at else "",
                "received_at_utc": received_at.isoformat().replace("+00:00", "Z") if received_at else "",
                "provider_role": provider_role(provider_id),
                "source_class": str(raw.get("source_class") or "").strip(),
                "aggregator_or_republisher": bool(raw.get("aggregator_or_republisher", False)),
                "open_meteo": bool(raw.get("open_meteo", False)),
                "capture_mode": capture_mode,
                "source_url": str(raw.get("source_url") or "").strip(),
                "raw_object_ref": str(raw.get("raw_object_ref") or "").strip(),
            }
        )

    output: list[dict[str, Any]] = []
    for (station_id, event_date, provider_id), rows in sorted(grouped.items()):
        run_inits = {row["run_initialization_utc"] for row in rows}
        maturities = {row["maturity_key"] for row in rows}
        if len(run_inits) != 1:
            raise ContractError("model_run_generation_drift")
        if len(maturities) != 1:
            raise ContractError("model_run_maturity_drift")
        raw_shas = sorted({row["raw_message_sha256"] for row in rows})
        receipt_shas = sorted({row["receipt_sha256"] for row in rows})
        published_times = sorted({row["published_at_utc"] for row in rows if row["published_at_utc"]})
        received_times = sorted({row["received_at_utc"] for row in rows if row["received_at_utc"]})
        published_complete = all(row["published_at_utc"] for row in rows)
        received_complete = all(row["received_at_utc"] for row in rows)
        if not raw_shas or not receipt_shas:
            raise ContractError("model_run_lineage_missing")
        output.append(
            {
                "station_id": station_id,
                "event_date": event_date.isoformat(),
                "season": _season(event_date),
                "provider_id": provider_id,
                "run_initialization_utc": next(iter(run_inits)),
                "run_cycle": f"{_timestamp('run_initialization_utc', next(iter(run_inits))).hour:02d}Z",
                "maturity_key": next(iter(maturities)),
                "lead_bucket": lead_bucket_for_maturity(next(iter(maturities))),
                "forecast_daily_max_c": max(row["temperature_2m_c"] for row in rows),
                "sample_count": len(rows),
                "raw_message_sha256": raw_shas,
                "receipt_sha256": receipt_shas,
                "model_run_lineage_sha256": _lineage_sha(
                    {
                        "station_id": station_id,
                        "event_date": event_date.isoformat(),
                        "provider_id": provider_id,
                        "run_initialization_utc": next(iter(run_inits)),
                        "raw_message_sha256": raw_shas,
                        "receipt_sha256": receipt_shas,
                    }
                ),
                # A daily curve may be assembled from several official raw
                # messages.  The run-level as-of timestamp is the latest
                # message timestamp, but only when every message carries it;
                # a partial timestamp set remains incomplete and fail-closed.
                "published_at_utc": published_times[-1] if published_complete else "",
                "received_at_utc": received_times[-1] if received_complete else "",
                "capture_modes": sorted({str(row.get("capture_mode") or "prospective").strip().lower() for row in rows}),
                "source_classes": sorted({
                    str(row.get("source_class") or "").strip()
                    for row in rows
                    if str(row.get("source_class") or "").strip()
                }),
                "aggregator_or_republisher": any(
                    bool(row.get("aggregator_or_republisher", False)) for row in rows
                ),
                "open_meteo": any(bool(row.get("open_meteo", False)) for row in rows),
                "source_urls": sorted({
                    str(row.get("source_url") or "").strip()
                    for row in rows
                    if str(row.get("source_url") or "").strip()
                }),
                "raw_object_refs": sorted({
                    str(row.get("raw_object_ref") or "").strip()
                    for row in rows
                    if str(row.get("raw_object_ref") or "").strip()
                }),
                "historical_replay_eligible": all(
                    bool(row.get("historical_replay_eligible", True)) for row in rows
                ),
                "prospective_strict_asof_eligible": all(
                    bool(row.get(
                        "prospective_strict_asof_eligible",
                        bool(row["published_at_utc"] and row["received_at_utc"]),
                    ))
                    for row in rows
                ),
            }
        )

    maturities = sorted({row["maturity_key"] for row in output})
    return output, {
        "input_rows": sum(len(rows) for rows in grouped.values()),
        "station_day_provider_runs": len(output),
        "maturity_keys": maturities,
        "lead_buckets": sorted({row["lead_bucket"] for row in output}),
        "all_model_runs_lineage_complete": bool(output)
        and all(row["model_run_lineage_sha256"] for row in output),
    }


def materialize_observation_station_days(
    observation_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Aggregate raw official-observation mirror rows into labeled station days.

    The observation source is explicitly offline research evidence.  Raw
    report hashes and source-object hashes are required so a final label cannot
    be mistaken for an untraceable daily summary.
    """

    grouped: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for raw in observation_rows:
        if not isinstance(raw, Mapping):
            raise ContractError("observation_row_mapping_required")
        station_id = _text("station_id", raw.get("station_id"))
        event_date = _local_date("event_date", raw.get("event_date"))
        valid_time = _timestamp("valid_time_utc", raw.get("valid_time_utc"))
        temperature_raw = str(raw.get("temperature_c", "")).strip()
        if not temperature_raw:
            continue
        temperature = _number("temperature_c", temperature_raw)
        raw_sha = _sha("raw_sha256", raw.get("raw_sha256"))
        source_raw_sha = _sha("source_raw_sha256", raw.get("source_raw_sha256"))
        grouped[(station_id, event_date)].append(
            {
                "station_id": station_id,
                "event_date": event_date.isoformat(),
                "valid_time_utc": valid_time.isoformat().replace("+00:00", "Z"),
                "temperature_c": temperature,
                "raw_sha256": raw_sha,
                "source_raw_sha256": source_raw_sha,
                "source_lane": str(raw.get("source_lane") or "").strip().upper(),
                "source_role": str(raw.get("source_role") or "").strip(),
                "observation_id": str(raw.get("observation_id") or "").strip(),
                "truth_id": str(raw.get("truth_id") or "").strip(),
                "received_at_utc": str(raw.get("received_at_utc") or "").strip(),
                "canonical_committed_at_utc": str(raw.get("canonical_committed_at_utc") or "").strip(),
            }
        )

    output: dict[tuple[str, str], dict[str, Any]] = {}
    for (station_id, event_date), rows in sorted(grouped.items()):
        raw_shas = sorted({row["raw_sha256"] for row in rows})
        source_shas = sorted({row["source_raw_sha256"] for row in rows})
        if not raw_shas or not source_shas:
            raise ContractError("observation_lineage_missing")
        key = (station_id, event_date.isoformat())
        output[key] = {
            "station_id": station_id,
            "event_date": event_date.isoformat(),
            "season": _season(event_date),
            "observed_daily_max_c": max(row["temperature_c"] for row in rows),
            "report_count": len(rows),
            "raw_sha256": raw_shas,
            "source_raw_sha256": source_shas,
            "sample_lineage_sha256": _lineage_sha(
                {
                    "station_id": station_id,
                    "event_date": event_date.isoformat(),
                    "raw_sha256": raw_shas,
                    "source_raw_sha256": source_shas,
                }
            ),
        }
        source_lanes = sorted({row["source_lane"] for row in rows if row["source_lane"]})
        source_roles = sorted({row["source_role"] for row in rows if row["source_role"]})
        observation_ids = sorted({row["observation_id"] for row in rows if row["observation_id"]})
        truth_ids = sorted({row["truth_id"] for row in rows if row["truth_id"]})
        received_at = sorted({row["received_at_utc"] for row in rows if row["received_at_utc"]})
        committed_at = sorted({row["canonical_committed_at_utc"] for row in rows if row["canonical_committed_at_utc"]})
        if source_lanes:
            output[key]["source_lane"] = source_lanes[0] if len(source_lanes) == 1 else "MIXED"
            output[key]["source_lanes"] = source_lanes
        if source_roles:
            output[key]["source_role"] = source_roles[0] if len(source_roles) == 1 else "MIXED"
        if observation_ids:
            output[key]["observation_ids"] = observation_ids
        if truth_ids:
            output[key]["truth_id"] = truth_ids[0] if len(truth_ids) == 1 else "MIXED"
        if received_at:
            output[key]["received_at_utc"] = max(received_at)
        if committed_at:
            output[key]["canonical_committed_at_utc"] = max(committed_at)
    return output, {
        "input_rows": sum(len(rows) for rows in grouped.values()),
        "station_days": len(output),
        "all_samples_lineage_complete": bool(output)
        and all(row["sample_lineage_sha256"] for row in output.values()),
    }


def build_disjoint_windows(anchor_local_date: date) -> list[dict[str, Any]]:
    """Build trailing windows with no shared event date or station-day key."""

    windows: list[dict[str, Any]] = []
    cursor_days = 0
    for width in WINDOW_DAYS:
        end = anchor_local_date - timedelta(days=cursor_days)
        start = end - timedelta(days=width)
        windows.append(
            {
                "window_id": f"prior_{width}d",
                "days": width,
                "start_event_date_inclusive": start.isoformat(),
                "end_event_date_exclusive": end.isoformat(),
                "anchor_local_date": anchor_local_date.isoformat(),
            }
        )
        cursor_days += width
    return windows


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _metric(rows: Sequence[dict[str, Any]], provider_id: str) -> dict[str, Any]:
    scoped = [row for row in rows if provider_id in row["forecasts"]]
    errors = [row["observed_daily_max_c"] - row["forecasts"][provider_id] for row in scoped]
    return {
        "n_station_days": len(errors),
        "mae_c": _mean([abs(value) for value in errors]),
        "mean_error_actual_minus_forecast_c": _mean(errors),
    }


def _summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    providers = sorted({provider for row in rows for provider in row["forecasts"]})
    by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_season: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_station[row["station_id"]].append(row)
        by_season[row["season"]].append(row)
    return {
        "station_days": len(rows),
        "event_dates": sorted({row["event_date"] for row in rows}),
        "stations": sorted(by_station),
        "seasons": sorted(by_season),
        "models": {provider: _metric(rows, provider) for provider in providers},
        "by_station": {
            station: {provider: _metric(station_rows, provider) for provider in providers}
            for station, station_rows in sorted(by_station.items())
        },
        "by_season": {
            season: {provider: _metric(season_rows, provider) for provider in providers}
            for season, season_rows in sorted(by_season.items())
        },
        "independence_unit": "station_day",
        "n_eff": len(rows),
    }


def evaluate_strict_asof_replay(
    model_hourly_rows: Iterable[Mapping[str, Any]],
    observation_rows: Iterable[Mapping[str, Any]],
    *,
    as_of_utc: str,
    target_timezone: str,
    fail_on_incomplete_model_set: bool = True,
) -> dict[str, Any]:
    """Evaluate strict-as-of windows and cross-season/cross-station evidence.

    The default is a hard contract failure for a partial model set.  The
    calibration evidence builder uses ``fail_on_incomplete_model_set=False``
    so it can emit a durable blocked evidence receipt instead of leaving a
    stale prior audit behind.
    """

    as_of = _timestamp("as_of_utc", as_of_utc)
    zone = ZoneInfo(target_timezone)
    anchor_local_date = as_of.astimezone(zone).date()
    model_days, model_quality = materialize_model_station_days(
        model_hourly_rows, target_timezone=target_timezone
    )
    observations, observation_quality = materialize_observation_station_days(observation_rows)

    maturity_keys = sorted({row["maturity_key"] for row in model_days})
    if len(maturity_keys) != 1:
        raise ContractError("strict_asof_maturity_not_uniform")
    maturity_key = maturity_keys[0]
    run_cycles = sorted({row["run_cycle"] for row in model_days})
    if len(run_cycles) != 1:
        raise ContractError("strict_asof_run_cycle_not_uniform")
    run_cycle = run_cycles[0]

    joined: dict[tuple[str, str], dict[str, Any]] = {}
    missing_observation_keys: list[tuple[str, str]] = []
    incomplete_model_keys: list[dict[str, Any]] = []
    model_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in model_days:
        event_date = _local_date("event_date", row["event_date"])
        run_init = _timestamp("run_initialization_utc", row["run_initialization_utc"])
        if row.get("published_at_utc") and row.get("received_at_utc"):
            published_at = _timestamp("published_at_utc", row["published_at_utc"])
            received_at = _timestamp("received_at_utc", row["received_at_utc"])
            if published_at > received_at:
                raise ContractError("model_published_after_received")
            if received_at > as_of:
                raise ContractError("model_received_after_asof")
            _, target_end = _target_bounds(event_date, target_timezone)
            if not run_init < target_end:
                raise ContractError("strict_asof_future_model_run")
        if not event_date < anchor_local_date:
            continue
        key = (row["station_id"], row["event_date"])
        model_by_key[key].append(row)

    for key, rows in sorted(model_by_key.items()):
        sample = observations.get(key)
        if sample is None:
            missing_observation_keys.append(key)
            continue
        providers = {row["provider_id"] for row in rows}
        provider_roles_present = {provider_role(provider) for provider in providers}
        if len(provider_roles_present) < MIN_FORMAL_ENSEMBLE_MODELS:
            if fail_on_incomplete_model_set:
                raise ContractError("strict_asof_model_provider_set_incomplete")
            incomplete_model_keys.append({
                "station_id": key[0],
                "event_date": key[1],
                "available_providers": sorted(providers),
                "missing_providers": sorted(MODEL_ROLES - provider_roles_present),
            })
            continue
        selected_rows: dict[str, dict[str, Any]] = {}
        for row in rows:
            role = provider_role(row["provider_id"])
            previous = selected_rows.get(role)
            if previous is None or row["provider_id"] == "icon_global_official":
                selected_rows[role] = row
        rows = list(selected_rows.values())
        if len({row["run_initialization_utc"] for row in rows}) != 1:
            raise ContractError("strict_asof_model_run_generation_mismatch")
        joined[key] = {
            "station_id": key[0],
            "event_date": key[1],
            "season": sample["season"],
            "maturity_key": maturity_key,
            "run_cycle": run_cycle,
            "observed_daily_max_c": sample["observed_daily_max_c"],
            "forecasts": {
                row["provider_id"]: row["forecast_daily_max_c"] for row in rows
            },
            "model_roles": {
                provider_role(row["provider_id"]): row["provider_id"] for row in rows
            },
            "model_run_lineage_sha256": sorted(
                row["model_run_lineage_sha256"] for row in rows
            ),
            "sample_lineage_sha256": sample["sample_lineage_sha256"],
            "run_initialization_utc": rows[0]["run_initialization_utc"],
        }

    if not joined and incomplete_model_keys and not fail_on_incomplete_model_set:
        windows = build_disjoint_windows(anchor_local_date)
        blocked = {
            "schema": SCHEMA,
            "state": "BLOCKED_EVIDENCE",
            "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
            "target_timezone": target_timezone,
            "anchor_local_date": anchor_local_date.isoformat(),
            "maturity_key": maturity_key,
            "run_cycle": run_cycle,
            "model_quality": model_quality,
            "observation_quality": observation_quality,
            "joined_quality": {
                "joined_station_days": 0,
                "missing_observation_station_days": len(missing_observation_keys),
                "missing_observation_keys_sample": [list(key) for key in missing_observation_keys[:20]],
                "incomplete_model_provider_sets": incomplete_model_keys[:100],
                "all_joined_samples_have_lineage": False,
                "all_joined_model_runs_have_lineage": False,
            },
            "windows": {
                "window_days": list(WINDOW_DAYS),
                "mutually_exclusive": False,
                "overlap_station_day_keys": [],
                "same_maturity": False,
                "reports": [
                    {
                        **_summarize_rows([]),
                        "window_id": window["window_id"],
                        "start_event_date_inclusive": window["start_event_date_inclusive"],
                        "end_event_date_exclusive": window["end_event_date_exclusive"],
                        "maturity_key": maturity_key,
                        "run_cycle": run_cycle,
                        "strict_asof": False,
                        "lineage_complete": False,
                        "promotion_grade": False,
                    }
                    for window in windows
                ],
            },
            "crossseason": {
                **_summarize_rows([]),
                "maturity_key": maturity_key,
                "run_cycle": run_cycle,
                "strict_asof": False,
                "lineage_complete": False,
                "all_four_seasons_present": False,
                "cross_station": False,
                "promotion_grade": False,
            },
            "boundary": {
                "offline_research_only": True,
                "uses_production_database": False,
                "writes_production_state": False,
                "reads_probability_state": False,
                "reads_market_state": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "promotion_allowed": False,
            },
            "join_blockers": incomplete_model_keys[:100],
        }
        blocked["replay_sha256"] = content_sha256(blocked)
        return blocked
    if not joined:
        raise ContractError("strict_asof_no_joined_station_days")

    all_rows = list(joined.values())
    windows = build_disjoint_windows(anchor_local_date)
    window_results: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    overlap_keys: list[tuple[str, str]] = []
    for window in windows:
        start = _local_date("window_start", window["start_event_date_inclusive"])
        end = _local_date("window_end", window["end_event_date_exclusive"])
        scoped = [
            row for row in all_rows if start <= _local_date("event_date", row["event_date"]) < end
        ]
        scoped_keys = {(row["station_id"], row["event_date"]) for row in scoped}
        overlap_keys.extend(sorted(seen_keys & scoped_keys))
        seen_keys.update(scoped_keys)
        summary = _summarize_rows(scoped)
        summary.update(
            {
                "window_id": window["window_id"],
                "start_event_date_inclusive": window["start_event_date_inclusive"],
                "end_event_date_exclusive": window["end_event_date_exclusive"],
                "maturity_key": maturity_key,
                "run_cycle": run_cycle,
                "strict_asof": all(
                    _timestamp("run_initialization_utc", row["run_initialization_utc"])
                    < _target_bounds(
                        _local_date("event_date", row["event_date"]), target_timezone
                    )[0]
                    for row in scoped
                ),
                "lineage_complete": all(
                    row["sample_lineage_sha256"] and row["model_run_lineage_sha256"]
                    for row in scoped
                ),
                "promotion_grade": False,
            }
        )
        window_results.append(summary)

    crossseason = _summarize_rows(all_rows)
    crossseason.update(
        {
            "maturity_key": maturity_key,
            "run_cycle": run_cycle,
            "strict_asof": all(
                _timestamp("run_initialization_utc", row["run_initialization_utc"])
                < _target_bounds(
                    _local_date("event_date", row["event_date"]), target_timezone
                )[0]
                for row in all_rows
            ),
            "lineage_complete": all(
                row["sample_lineage_sha256"] and row["model_run_lineage_sha256"]
                for row in all_rows
            ),
            "all_four_seasons_present": set(crossseason["seasons"])
            == {"spring", "summer", "autumn", "winter"},
            "cross_station": len(crossseason["stations"]) > 1,
            "promotion_grade": False,
        }
    )

    result = {
        "schema": SCHEMA,
        "state": "BLOCKED_EVIDENCE" if incomplete_model_keys else "EVALUATED",
        "as_of_utc": as_of.isoformat().replace("+00:00", "Z"),
        "target_timezone": target_timezone,
        "anchor_local_date": anchor_local_date.isoformat(),
        "maturity_key": maturity_key,
        "lead_bucket": lead_bucket_for_maturity(maturity_key),
        "run_cycle": run_cycle,
        "model_quality": model_quality,
        "observation_quality": observation_quality,
        "joined_quality": {
            "joined_station_days": len(all_rows),
            "missing_observation_station_days": len(missing_observation_keys),
            "missing_observation_keys_sample": [list(key) for key in missing_observation_keys[:20]],
            "all_joined_samples_have_lineage": all(
                row["sample_lineage_sha256"] for row in all_rows
            ),
            "all_joined_model_runs_have_lineage": all(
                row["model_run_lineage_sha256"] for row in all_rows
            ),
            "incomplete_model_provider_sets": incomplete_model_keys[:100],
        },
        "windows": {
            "window_days": list(WINDOW_DAYS),
            "mutually_exclusive": not overlap_keys,
            "overlap_station_day_keys": [list(key) for key in overlap_keys],
            "same_maturity": all(row["maturity_key"] == maturity_key for row in window_results),
            "reports": window_results,
        },
        "crossseason": crossseason,
        "boundary": {
            "offline_research_only": True,
            "uses_production_database": False,
            "writes_production_state": False,
            "reads_probability_state": False,
            "reads_market_state": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "promotion_allowed": False,
        },
        "join_blockers": incomplete_model_keys[:100],
    }
    result["replay_sha256"] = content_sha256(result)
    return result
