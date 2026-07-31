"""Training-only native-member history from Open-Meteo Previous Runs.

The Previous Runs API exposes fixed-lag forecasts, including every ensemble
member.  It is useful immediately for historical training and chronological
offline OOS, but it does not expose native publication/arrival timestamps and
therefore cannot by itself authorize formal/live P0.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .native_ensemble_history import (
    MAX_HISTORY_DAYS,
    PREVIOUS_DAY1_EXTRACTION_VERSION,
    SCHEMA,
)
from .open_meteo_ensemble import (
    ENSEMBLE_SYSTEMS,
    _bounded_get_json,
    _member_bucket_probabilities,
)
from .p0_producer import bucket_intervals_from_registry_rows


API_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
DOCUMENTATION_URL = "https://open-meteo.com/en/docs/previous-runs-api"
VARIABLE = "temperature_2m_previous_day1"
MAX_TOTAL_RESPONSE_BYTES = 32 * 1024 * 1024


def _member_keys(expected_count: int) -> list[tuple[str, str, str]]:
    return [
        (
            VARIABLE if index == 0 else f"{VARIABLE}_member{index:02d}",
            "control" if index == 0 else f"perturbed_{index:02d}",
            "CONTROL" if index == 0 else "PERTURBED",
        )
        for index in range(expected_count)
    ]


def _daily_members(
    payload: Mapping[str, Any],
    *,
    event_dates: Sequence[str],
    ensemble_system_id: str,
    expected_count: int,
) -> dict[str, list[dict[str, Any]]]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise ContractError(
            "native_previous_runs_hourly_payload_missing"
        )
    times = hourly.get("time")
    if not isinstance(times, list):
        raise ContractError(
            "native_previous_runs_hourly_time_missing"
        )
    indices_by_day = {
        day: [
            index
            for index, value in enumerate(times)
            if str(value).startswith(day + "T")
        ]
        for day in event_dates
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for day, indices in indices_by_day.items():
        if len(indices) < 20:
            continue
        members = []
        for key, member_id, member_role in _member_keys(
            expected_count
        ):
            values = hourly.get(key)
            if not isinstance(values, list) or len(values) != len(times):
                members = []
                break
            daily = []
            for index in indices:
                raw = values[index]
                if raw is None:
                    daily = []
                    break
                value = float(raw)
                if not math.isfinite(value):
                    daily = []
                    break
                daily.append(value)
            if not daily:
                members = []
                break
            members.append({
                "ensemble_system_id": ensemble_system_id,
                "member_id": member_id,
                "member_role": member_role,
                "raw_tmax_c": max(daily),
            })
        if len(members) == expected_count:
            result[day] = members
    return result


def capture_previous_runs_history(
    *,
    station_id: str,
    event_dates: Sequence[str],
    latitude: float,
    longitude: float,
    settlement_timezone: str,
    registry_rows: Sequence[Mapping[str, Any]],
    archive_query_at_utc: str,
    ensemble_system_ids: Sequence[str] | None = None,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    days = sorted(set(str(value or "").strip() for value in event_dates))
    selected = sorted(set(
        str(value)
        for value in (
            ensemble_system_ids
            if ensemble_system_ids is not None
            else ENSEMBLE_SYSTEMS
        )
    ))
    if (
        not station
        or not days
        or len(days) > MAX_HISTORY_DAYS
        or not selected
        or any(value not in ENSEMBLE_SYSTEMS for value in selected)
        or not settlement_timezone
        or not math.isfinite(float(latitude))
        or not math.isfinite(float(longitude))
    ):
        raise ContractError("native_previous_runs_scope_invalid")
    parsed_days = [date.fromisoformat(value) for value in days]
    if (parsed_days[-1] - parsed_days[0]).days >= MAX_HISTORY_DAYS:
        raise ContractError("native_previous_runs_range_too_wide")
    query_clock = datetime.fromisoformat(
        archive_query_at_utc.replace("Z", "+00:00")
    )
    local_query_day = query_clock.astimezone(
        ZoneInfo(settlement_timezone)
    ).date()
    if any(day >= local_query_day for day in parsed_days):
        raise ContractError(
            "native_previous_runs_must_precede_query_day"
        )
    intervals = bucket_intervals_from_registry_rows(registry_rows)
    frozen_intervals = {
        bucket: [
            None if lower is None else float(lower),
            None if upper is None else float(upper),
        ]
        for bucket, (lower, upper) in intervals.items()
    }
    bucket_contract_sha = content_sha256(frozen_intervals)
    systems_by_day: dict[str, list[dict[str, Any]]] = {
        day: [] for day in days
    }
    unavailable_by_day: dict[str, list[dict[str, str]]] = {
        day: [] for day in days
    }
    request_receipts = []
    total_response_bytes = 0
    for system_id in selected:
        config = ENSEMBLE_SYSTEMS[system_id]
        query = urlencode({
            "latitude": float(latitude),
            "longitude": float(longitude),
            "hourly": VARIABLE,
            "models": str(config["api_model"]),
            "timezone": settlement_timezone,
            "start_date": days[0],
            "end_date": days[-1],
        })
        url = f"{API_URL}?{query}"
        try:
            payload, response_sha = _bounded_get_json(
                url,
                timeout_seconds=timeout_seconds,
            )
            if not isinstance(payload, Mapping):
                raise ContractError(
                    "native_previous_runs_payload_mapping_required"
                )
            response_bytes = len(str(payload).encode("utf-8"))
            total_response_bytes += response_bytes
            if total_response_bytes > MAX_TOTAL_RESPONSE_BYTES:
                raise ContractError(
                    "native_previous_runs_total_response_too_large"
                )
            daily = _daily_members(
                payload,
                event_dates=days,
                ensemble_system_id=system_id,
                expected_count=int(config["expected_member_count"]),
            )
            request_receipts.append({
                "ensemble_system_id": system_id,
                "request_url": url,
                "response_sha256": response_sha,
                "response_bytes": response_bytes,
                "successful_event_date_count": len(daily),
            })
        except Exception as exc:
            daily = {}
            response_sha = ""
            request_receipts.append({
                "ensemble_system_id": system_id,
                "request_url": url,
                "response_sha256": response_sha,
                "response_bytes": 0,
                "successful_event_date_count": 0,
                "error": f"{type(exc).__name__}:{exc}",
            })
        for day in days:
            members = daily.get(day)
            if members is None:
                unavailable_by_day[day].append({
                    "ensemble_system_id": system_id,
                    "reason": "previous_day1_members_unavailable",
                })
                continue
            member_identity = [
                {
                    "member_id": row["member_id"],
                    "member_role": row["member_role"],
                    "raw_tmax_c": row["raw_tmax_c"],
                }
                for row in members
            ]
            systems_by_day[day].append({
                "ensemble_system_id": system_id,
                "independence_family_id": config[
                    "independence_family_id"
                ],
                "member_count": len(members),
                "members": members,
                "member_set_sha256": content_sha256(member_identity),
                "raw_bucket_probabilities":
                    _member_bucket_probabilities(members, intervals),
            })
    captures = []
    for day in days:
        capture_identity = {
            "station_id": station,
            "event_date": day,
            "settlement_timezone": settlement_timezone,
            "archive_valid_day_bucket": "d0",
            "systems": systems_by_day[day],
            "unavailable_products": unavailable_by_day[day],
        }
        captures.append({
            **capture_identity,
            "capture_content_sha256": content_sha256(capture_identity),
        })
    response_set_sha = content_sha256([
        {
            "ensemble_system_id": row["ensemble_system_id"],
            "response_sha256": row["response_sha256"],
        }
        for row in request_receipts
    ])
    identity = {
        "schema": SCHEMA,
        "extraction_version": PREVIOUS_DAY1_EXTRACTION_VERSION,
        "station_id": station,
        "event_dates": days,
        "event_date_count": len(days),
        "successful_event_date_count": sum(
            bool(row["systems"]) for row in captures
        ),
        "unavailable_event_date_count": sum(
            not bool(row["systems"]) for row in captures
        ),
        "settlement_timezone": settlement_timezone,
        "latitude": float(latitude),
        "longitude": float(longitude),
        "archive_query_at_utc": archive_query_at_utc,
        "request_url": (
            "open_meteo_previous_runs_request_set:" + response_set_sha
        ),
        "response_sha256": response_set_sha,
        "request_receipts": request_receipts,
        "bucket_contract_sha256": bucket_contract_sha,
        "bucket_intervals": frozen_intervals,
        "captures": captures,
        "archive_contract": {
            "documentation_url": DOCUMENTATION_URL,
            "fixed_previous_day_lag": 1,
            "provider_describes_past_days_as_archived_forecasts": True,
            "native_run_init_known": False,
            "native_publication_at_known": False,
            "native_arrival_index_eligible": False,
            "training_only": True,
            "holdout_eligible": False,
            "offline_chronological_oos_candidate": True,
            "formal_p0_eligible": False,
        },
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "API_URL",
    "capture_previous_runs_history",
]
