"""Build station-local DWD ICON Global/EPS member daily curves.

The input is a bounded set of official one-lead cohort receipts.  Each
receipt represents one DWD raw object containing all 40 members.  This module
joins those cohorts across valid times and computes one local-day maximum per
station/member without creating member-shaped raw files or P0 authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256
from .station_registry import coordinate_registry_manifest


SCHEMA = "dwd_icon_global_eps_official_daily_members_v1"
COHORT_SCHEMA = "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
ENSEMBLE_SYSTEM_ID = "DWD_ICON_GLOBAL_EPS"
FAMILY_ID = "DWD_ICON_EPS"
PROVIDER_ID = "icon_global_eps_official"
EXPECTED_MEMBERS = tuple(range(1, 41))
MAX_COHORTS = 64
MAX_GAP_HOURS = 3.0
ENDPOINT_TOLERANCE_HOURS = 3.0
MIN_POINTS = 8


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"icon_eps_daily_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"icon_eps_daily_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _verified_cohort(raw: Mapping[str, Any]) -> dict[str, Any]:
    cohort = dict(raw)
    identity = {key: value for key, value in cohort.items() if key != "content_sha256"}
    lineage = cohort.get("lineage")
    grid = lineage.get("official_grid") if isinstance(lineage, Mapping) else None
    records = cohort.get("member_station_records")
    if (
        cohort.get("schema") != COHORT_SCHEMA
        or cohort.get("provider_id") != PROVIDER_ID
        or cohort.get("product_id") != ENSEMBLE_SYSTEM_ID
        or cohort.get("independence_family_id") != FAMILY_ID
        or cohort.get("state")
        != "OFFICIAL_40_MEMBER_45_STATION_DECODED_CANDIDATE_ONLY"
        or cohort.get("content_sha256") != content_sha256(identity)
        or cohort.get("member_count") != len(EXPECTED_MEMBERS)
        or cohort.get("station_count") != 45
        or cohort.get("member_record_count") != 45 * len(EXPECTED_MEMBERS)
        or not isinstance(records, list)
        or len(records) != 45 * len(EXPECTED_MEMBERS)
        or not isinstance(lineage, Mapping)
        or lineage.get("source_role") != "official_native"
        or lineage.get("transport_provider_id") != "DWD"
        or not isinstance(grid, Mapping)
        or grid.get("source_grid_uuid") != "ae487d14fe2e11e4af85e50a2a56a360"
        or grid.get("source_grid_cell_count") != 737_280
        or cohort.get("formal_p0_eligible") is not False
        or cohort.get("hard_no_eligible") is not False
    ):
        raise ContractError("icon_eps_daily_cohort_invalid")
    return cohort


def _station_scope(station_timezones: Mapping[str, str]) -> tuple[str, ...]:
    expected = tuple(str(value) for value in coordinate_registry_manifest()["station_ids"])
    normalized = {
        str(station).strip().upper(): str(zone).strip()
        for station, zone in station_timezones.items()
    }
    if set(normalized) != set(expected) or any(not value for value in normalized.values()):
        raise ContractError("icon_eps_daily_station_timezone_scope_invalid")
    for zone in normalized.values():
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ContractError("icon_eps_daily_station_timezone_invalid") from exc
    return tuple(sorted(expected))


def build_icon_eps_daily_archive(
    *,
    cohorts: Sequence[Mapping[str, Any]],
    event_date: str,
    station_timezones: Mapping[str, str],
    built_at_utc: str,
) -> dict[str, Any]:
    """Materialize complete 45-station, 40-member local-day curves."""

    if (
        not isinstance(cohorts, Sequence)
        or isinstance(cohorts, (str, bytes, bytearray))
        or not 1 <= len(cohorts) <= MAX_COHORTS
    ):
        raise ContractError("icon_eps_daily_cohort_count_invalid")
    try:
        target_day = date.fromisoformat(str(event_date))
    except ValueError as exc:
        raise ContractError("icon_eps_daily_event_date_invalid") from exc
    built_at = _utc("built_at_utc", built_at_utc)
    stations = _station_scope(station_timezones)
    normalized_timezones = {
        station: str(station_timezones[station]).strip() for station in stations
    }
    verified = [_verified_cohort(row) for row in cohorts]
    run_values = {
        _iso(_utc("run_initialization_utc", row["run_initialization_utc"]))
        for row in verified
    }
    if len(run_values) != 1:
        raise ContractError("icon_eps_daily_mixed_runs")
    run_init = next(iter(run_values))
    by_valid: dict[str, dict[str, Any]] = {}
    grid_indices_sha: set[str] = set()
    for cohort in verified:
        valid = _iso(_utc("valid_time_utc", cohort["valid_time_utc"]))
        if valid in by_valid:
            raise ContractError("icon_eps_daily_duplicate_valid_time")
        by_valid[valid] = cohort
        indices = cohort["lineage"]["official_grid"].get(
            "station_nearest_cell_indices"
        )
        if not isinstance(indices, Mapping) or set(indices) != set(stations):
            raise ContractError("icon_eps_daily_station_indices_invalid")
        grid_indices_sha.add(content_sha256(dict(sorted(indices.items()))))
    if len(grid_indices_sha) != 1:
        raise ContractError("icon_eps_daily_station_indices_drift")

    values: dict[tuple[str, int], list[dict[str, Any]]] = {
        (station, member): []
        for station in stations
        for member in EXPECTED_MEMBERS
    }
    cohort_sha_by_valid: dict[str, str] = {}
    for valid, cohort in sorted(by_valid.items()):
        cohort_sha_by_valid[valid] = str(cohort["content_sha256"])
        seen: set[tuple[str, int]] = set()
        for record in cohort["member_station_records"]:
            if not isinstance(record, Mapping):
                raise ContractError("icon_eps_daily_member_record_invalid")
            station = str(record.get("station_id") or "").strip().upper()
            try:
                member = int(record.get("member_id"))
                temperature = float(record.get("temperature_2m_c"))
            except (TypeError, ValueError) as exc:
                raise ContractError("icon_eps_daily_member_record_invalid") from exc
            key = (station, member)
            if (
                key not in values
                or key in seen
                or not math.isfinite(temperature)
                or not -100.0 <= temperature <= 100.0
                or record.get("raw_sha256") != cohort.get("raw_sha256")
            ):
                raise ContractError("icon_eps_daily_member_record_invalid")
            seen.add(key)
            zone = ZoneInfo(normalized_timezones[station])
            if _utc("valid_time_utc", valid).astimezone(zone).date() == target_day:
                values[key].append({
                    "valid_time_utc": valid,
                    "temperature_2m_c": round(temperature, 6),
                    "raw_sha256": str(cohort["raw_sha256"]),
                    "cohort_content_sha256": str(cohort["content_sha256"]),
                })
        if seen != set(values):
            raise ContractError("icon_eps_daily_member_record_scope_invalid")

    captures: list[dict[str, Any]] = []
    all_component_shas: set[str] = set()
    for station in stations:
        zone = ZoneInfo(normalized_timezones[station])
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=zone).astimezone(
            timezone.utc
        )
        end = start + timedelta(days=1)
        members: list[dict[str, Any]] = []
        common_valid_times: tuple[str, ...] | None = None
        for member in EXPECTED_MEMBERS:
            points = sorted(values[(station, member)], key=lambda row: row["valid_time_utc"])
            times = [_utc("valid_time_utc", row["valid_time_utc"]) for row in points]
            gaps = [
                (later - earlier).total_seconds() / 3600.0
                for earlier, later in zip(times, times[1:])
            ]
            if (
                len(points) < MIN_POINTS
                or not times
                or (times[0] - start).total_seconds() / 3600.0
                > ENDPOINT_TOLERANCE_HOURS
                or (end - times[-1]).total_seconds() / 3600.0
                > ENDPOINT_TOLERANCE_HOURS
                or (gaps and max(gaps) > MAX_GAP_HOURS)
            ):
                raise ContractError(
                    f"icon_eps_daily_curve_incomplete:{station}:member_{member:02d}"
                )
            valid_times = tuple(row["valid_time_utc"] for row in points)
            if common_valid_times is None:
                common_valid_times = valid_times
            elif common_valid_times != valid_times:
                raise ContractError("icon_eps_daily_member_valid_time_drift")
            peak = max(points, key=lambda row: row["temperature_2m_c"])
            component_shas = sorted({row["cohort_content_sha256"] for row in points})
            all_component_shas.update(component_shas)
            members.append({
                "member_id": f"member_{member:02d}",
                "member_role": "PERTURBED",
                "raw_tmax_c": float(peak["temperature_2m_c"]),
                "peak_valid_time_utc": peak["valid_time_utc"],
                "point_count": len(points),
                "component_cohort_sha256": component_shas,
                "curve_sha256": content_sha256(points),
            })
        member_identity = [
            {
                "member_id": row["member_id"],
                "member_role": row["member_role"],
                "raw_tmax_c": row["raw_tmax_c"],
            }
            for row in members
        ]
        capture_identity = {
            "station_id": station,
            "event_date": str(target_day),
            "settlement_timezone": normalized_timezones[station],
            "run_cycle": run_init[11:13] + "Z",
            "run_init_utc": run_init,
            "ensemble_system_id": ENSEMBLE_SYSTEM_ID,
            "independence_family_id": FAMILY_ID,
            "member_count": len(members),
            "members": members,
            "member_set_sha256": content_sha256(member_identity),
            "valid_times_utc": list(common_valid_times or ()),
            "daily_curve_complete": True,
            "max_gap_hours": MAX_GAP_HOURS,
            "endpoint_tolerance_hours": ENDPOINT_TOLERANCE_HOURS,
        }
        captures.append({
            **capture_identity,
            "content_sha256": content_sha256(capture_identity),
        })

    latest_received = max(
        _utc("response_completed_at_utc", row["response_completed_at_utc"])
        for row in verified
    )
    identity = {
        "schema": SCHEMA,
        "state": "OFFICIAL_MEMBER_DAILY_CURVES_READY_STRICT_OOS_PENDING",
        "provider_id": PROVIDER_ID,
        "product_id": ENSEMBLE_SYSTEM_ID,
        "event_date": str(target_day),
        "run_cycle": run_init[11:13] + "Z",
        "run_init_utc": run_init,
        "archive_query_at_utc": _iso(latest_received),
        "built_at_utc": _iso(built_at),
        "ensemble_system_id": ENSEMBLE_SYSTEM_ID,
        "independence_family_id": FAMILY_ID,
        "member_count": len(EXPECTED_MEMBERS),
        "station_count": len(stations),
        "capture_count": len(captures),
        "captures": captures,
        "component_cohort_count": len(verified),
        "component_cohort_sha256": sorted(all_component_shas),
        "component_cohort_sha256_by_valid_time": cohort_sha_by_valid,
        "lead_results": [
            {
                "forecast_hour": int(cohort["forecast_hour"]),
                "valid_time_utc": valid,
                "raw_sha256": str(cohort["raw_sha256"]),
                "cohort_content_sha256": str(cohort["content_sha256"]),
            }
            for valid, cohort in sorted(by_valid.items())
        ],
        "station_nearest_cell_indices_sha256": next(iter(grid_indices_sha)),
        "source_authority": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "native_publication_and_arrival_observed": True,
            # Provider publication and HTTP response completion are retained,
            # but this archive is not the immutable native-arrival index used
            # by formal strict-OOS promotion.  Keep the distinction explicit
            # so a republisher or a retrospective timestamp cannot be treated
            # as native arrival by downstream gates.
            "native_arrival_index_eligible": False,
            "arrival_lineage_status": (
                "PROVIDER_PUBLICATION_AND_LOCAL_RECEIPT_OBSERVED_INDEX_PENDING"
            ),
            "raw_member_fanout": False,
            "official_grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
            "official_grid_cell_count": 737_280,
        },
        "holdout_eligible": True,
        "training_fact_eligible_after_closed_awc_truth": True,
        "strict_oos_state": "PENDING_CLOSED_AWC_TRUTH_AND_ROLLING_OOS",
        "formal_p0_eligible": False,
        "native_arrival_index_eligible": False,
        "hard_no_eligible": False,
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = ["SCHEMA", "build_icon_eps_daily_archive"]
