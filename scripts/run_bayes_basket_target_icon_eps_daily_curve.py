#!/usr/bin/env python3
"""Capture missing ICON Global/EPS leads and build 45-station daily members.

The command is plan-only unless ``--execute`` is supplied.  Execution reuses
content-addressed raw objects and per-lead receipts, decodes one 40-member raw
at a time, and reuses the already verified R03B06 nearest-cell index map.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.icon_eps_daily_curve import (  # noqa: E402
    build_icon_eps_daily_archive,
)
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from scripts.decode_bayes_basket_target_icon_eps_station_members import (  # noqa: E402
    DEFAULT_GRID_RECEIPT,
    OfficialDwdIconEpsAdapter,
    decode_station_members,
)
from scripts.run_bayes_basket_target_icon_eps_forward_capture import (  # noqa: E402
    DEFAULT_OUTPUT_ROOT,
    build_plan,
    execute_plan,
)


PLAN_SCHEMA = "dwd_icon_global_eps_daily_curve_capture_plan_v1"
DEFAULT_REGISTRY = (
    ROOT / "data/runtime/bayes_basket_target_nwp/market_registry_forward.json"
)
DEFAULT_REFERENCE = ROOT / "output/audits/icon_global_eps_station_members_r03b06_20260801.json"
DEFAULT_WORK_ROOT = ROOT / "output/audits/icon_global_eps_daily_20260802"
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_eps_daily_members_20260802.json"
CADENCE_HOURS = 3
MAX_LEADS = 32


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("icon_eps_daily_runner_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_eps_daily_runner_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _station_timezones(registry_path: Path, *, event_date: str) -> dict[str, str]:
    registry = load_market_registry_snapshot(registry_path.expanduser().resolve(strict=True))
    expected = {
        str(value) for value in coordinate_registry_manifest()["station_ids"]
    }
    zones: dict[str, set[str]] = {station: set() for station in expected}
    for row in registry["rows"]:
        station = str(row.get("station_id") or "").strip().upper()
        if station in zones and str(row.get("event_date") or "") == event_date:
            zone = str(row.get("settlement_timezone") or "").strip()
            if zone:
                zones[station].add(zone)
    if any(len(values) != 1 for values in zones.values()):
        missing = sorted(station for station, values in zones.items() if len(values) != 1)
        raise ContractError(
            "icon_eps_daily_runner_registry_timezone_invalid:" + ",".join(missing)
        )
    return {station: next(iter(values)) for station, values in sorted(zones.items())}


def _required_leads(
    *, run_init: datetime, event_date: str, station_timezones: Mapping[str, str]
) -> list[int]:
    target = date.fromisoformat(event_date)
    starts: list[datetime] = []
    ends: list[datetime] = []
    for zone_name in station_timezones.values():
        start = datetime.combine(target, datetime.min.time(), tzinfo=ZoneInfo(zone_name)).astimezone(
            timezone.utc
        )
        starts.append(start)
        ends.append(start + timedelta(days=1))
    first_delta = (min(starts) - run_init).total_seconds() / 3600.0
    last_delta = (max(ends) - run_init).total_seconds() / 3600.0
    first = max(0, int(math.ceil(first_delta / CADENCE_HOURS) * CADENCE_HOURS))
    # The curve interval is half-open.  Its last point must precede the latest
    # local-day end while staying within one native sampling interval.
    last = int(math.floor((last_delta - 1e-9) / CADENCE_HOURS) * CADENCE_HOURS)
    leads = list(range(first, last + 1, CADENCE_HOURS))
    if not leads or len(leads) > MAX_LEADS or any(not 0 <= lead <= 180 for lead in leads):
        raise ContractError("icon_eps_daily_runner_lead_scope_invalid")
    for start, end in zip(starts, ends):
        points = [run_init + timedelta(hours=lead) for lead in leads if start <= run_init + timedelta(hours=lead) < end]
        if (
            not points
            or (points[0] - start).total_seconds() / 3600.0 > CADENCE_HOURS
            or (end - points[-1]).total_seconds() / 3600.0 > CADENCE_HOURS
            or any((b - a).total_seconds() / 3600.0 > CADENCE_HOURS for a, b in zip(points, points[1:]))
        ):
            raise ContractError("icon_eps_daily_runner_lead_coverage_invalid")
    return leads


def _reference_indices(path: Path, *, grid_receipt: Mapping[str, Any]) -> dict[str, int]:
    raw = load_bounded_json(path.expanduser().resolve(strict=True))
    if not isinstance(raw, Mapping):
        raise ContractError("icon_eps_daily_runner_reference_invalid")
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    lineage = raw.get("lineage")
    grid = lineage.get("official_grid") if isinstance(lineage, Mapping) else None
    indices = grid.get("station_nearest_cell_indices") if isinstance(grid, Mapping) else None
    if (
        raw.get("content_sha256") != content_sha256(identity)
        or raw.get("schema")
        != "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
        or not isinstance(indices, Mapping)
        or grid.get("source_grid_receipt_sha256") != grid_receipt.get("content_sha256")
        or grid.get("source_grid_uuid") != grid_receipt.get("grid_uuid")
        or grid.get("source_grid_cell_count") != grid_receipt.get("grid_cell_count")
    ):
        raise ContractError("icon_eps_daily_runner_reference_invalid")
    return {str(station): int(index) for station, index in indices.items()}


def _reusable_cohort(
    path: Path,
    *,
    capture: Mapping[str, Any],
    forecast_hour: int,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = load_bounded_json(path)
    if not isinstance(raw, dict):
        return None
    identity = {key: value for key, value in raw.items() if key != "content_sha256"}
    if (
        raw.get("content_sha256") != content_sha256(identity)
        or raw.get("schema")
        != "bayes_basket_target_icon_global_eps_station_member_cohort_v1"
        or raw.get("forecast_hour") != forecast_hour
        or raw.get("capture_receipt_sha256") != capture.get("content_sha256")
        or raw.get("raw_sha256") != capture.get("raw_sha256")
        or raw.get("member_count") != 40
        or raw.get("station_count") != 45
        or raw.get("member_record_count") != 1800
    ):
        return None
    return raw


def build_capture_plan(
    *,
    run_initialization_utc: str,
    event_date: str,
    station_timezones: Mapping[str, str],
    as_of_utc: str,
) -> dict[str, Any]:
    run = _utc(run_initialization_utc)
    leads = _required_leads(
        run_init=run,
        event_date=event_date,
        station_timezones=station_timezones,
    )
    identity = {
        "schema": PLAN_SCHEMA,
        "run_initialization_utc": _iso(run),
        "event_date": event_date,
        "station_count": len(station_timezones),
        "station_timezones": dict(sorted(station_timezones.items())),
        "cadence_hours": CADENCE_HOURS,
        "forecast_hours": leads,
        "lead_count": len(leads),
        "as_of_utc": _iso(_utc(as_of_utc)),
        "reuse_policy": "CONTENT_ADDRESSED_RAW_AND_EXACT_LEAD_RECEIPT",
        "raw_member_fanout": False,
        "formal_p0_eligible": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", default=_iso(datetime.now(timezone.utc)))
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--grid-receipt", type=Path, default=DEFAULT_GRID_RECEIPT)
    parser.add_argument("--reference-cohort", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    zones = _station_timezones(args.registry, event_date=args.event_date)
    plan = build_capture_plan(
        run_initialization_utc=args.run_initialization_utc,
        event_date=args.event_date,
        station_timezones=zones,
        as_of_utc=args.as_of_utc,
    )
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0

    grid_receipt = load_bounded_json(args.grid_receipt.expanduser().resolve(strict=True))
    if not isinstance(grid_receipt, Mapping):
        raise ContractError("icon_eps_daily_runner_grid_receipt_invalid")
    indices = _reference_indices(args.reference_cohort, grid_receipt=grid_receipt)
    adapter = OfficialDwdIconEpsAdapter(
        grid_receipt_path=args.grid_receipt,
        precomputed_station_indices=indices,
    )
    work_root = args.work_root.expanduser().resolve()
    capture_root = work_root / "captures"
    cohort_root = work_root / "cohorts"
    capture_root.mkdir(parents=True, exist_ok=True)
    cohort_root.mkdir(parents=True, exist_ok=True)
    cohorts: list[dict[str, Any]] = []
    lead_results: list[dict[str, Any]] = []
    for lead in plan["forecast_hours"]:
        capture_path = capture_root / f"capture_f{lead:03d}.json"
        cohort_path = cohort_root / f"cohort_f{lead:03d}.json"
        capture_plan = build_plan(
            run_initialization_utc=plan["run_initialization_utc"],
            forecast_hour=int(lead),
            as_of_utc=plan["as_of_utc"],
        )
        capture = execute_plan(
            capture_plan,
            output_root=args.raw_root,
            receipt_path=capture_path,
            timeout_sec=args.timeout_sec,
            prefer_curl=True,
        )
        cohort = _reusable_cohort(
            cohort_path,
            capture=capture,
            forecast_hour=int(lead),
        )
        cohort_state = "REUSED_VERIFIED"
        if cohort is None:
            cohort = decode_station_members(capture_path, adapter=adapter)
            write_bounded_json(cohort_path, cohort)
            cohort_state = "DECODED"
        cohorts.append(cohort)
        lead_results.append({
            "forecast_hour": lead,
            "valid_time_utc": cohort["valid_time_utc"],
            "capture_state": capture["state"],
            "cohort_state": cohort_state,
            "raw_sha256": capture["raw_sha256"],
            "cohort_content_sha256": cohort["content_sha256"],
            "capture_path": str(capture_path),
            "cohort_path": str(cohort_path),
        })
    archive = build_icon_eps_daily_archive(
        cohorts=cohorts,
        event_date=args.event_date,
        station_timezones=zones,
        built_at_utc=args.as_of_utc,
    )
    result_identity = {
        **archive,
        "capture_plan_sha256": plan["content_sha256"],
        "lead_results": lead_results,
    }
    result_identity.pop("content_sha256", None)
    result = {**result_identity, "content_sha256": content_sha256(result_identity)}
    output = args.output.expanduser().resolve()
    write_bounded_json(output, result)
    print(json.dumps({
        "state": result["state"],
        "output": str(output),
        "lead_count": len(lead_results),
        "station_count": result["station_count"],
        "member_count": result["member_count"],
        "capture_count": result["capture_count"],
        "formal_p0_eligible": result["formal_p0_eligible"],
        "orders_submitted": result["boundary"]["orders_submitted"],
        "content_sha256": result["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
