#!/usr/bin/env python3
"""Build one station's bounded formal calibration truth dataset.

The input is one or more historical canonical-observation evidence packets.
This command never opens a production database.  It applies the station lane
contract, settlement timezone, closed-day rule, and AMSC whole-hour rule, then
emits the compact daily-high dataset consumed by the five-model OOS builder.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402
from bayes_basket_target.source_policy import (  # noqa: E402
    AWC_HOUR_REFERENCE_FOR_AMSC,
)


SCHEMA = "bayes_basket_station_calibration_dataset_v1"
INPUT_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
REFERENCE_INPUT_SCHEMA = "bayes_basket_target_truth_reference_evidence_v1"
TEMPORARY_AWC_LABEL_AUTHORITY = "TEMPORARY_AWC_CALIBRATION_LABEL"
MAX_INPUT_FILES = 64
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OBSERVATIONS = 100_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("calibration_truth_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _window(day_text: str, timezone_name: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(day_text)
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(day, wall_time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    end = datetime.combine(
        day + timedelta(days=1), wall_time.min, tzinfo=zone
    ).astimezone(timezone.utc)
    return start, end


def build(
    *,
    payloads: Sequence[Mapping[str, Any]],
    station_id: str,
    settlement_timezone: str,
    source_lane: str,
    start_date: str,
    end_date: str,
    as_of_utc: str,
    temporary_awc_calibration_label: bool = False,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    lane = str(source_lane or "").strip().upper()
    if not station or lane not in {"AMSC", "AWC"}:
        raise ContractError("calibration_truth_scope_invalid")
    if temporary_awc_calibration_label and lane != "AWC":
        raise ContractError("temporary_awc_calibration_label_scope_invalid")
    try:
        ZoneInfo(settlement_timezone)
    except Exception as exc:
        raise ContractError("calibration_truth_timezone_invalid") from exc
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if first > last or (last - first).days >= 120:
        raise ContractError("calibration_truth_date_range_invalid")
    as_of = _utc(as_of_utc)

    deduplicated: dict[str, dict[str, Any]] = {}
    input_shas: list[str] = []
    examined = 0
    for payload in payloads:
        input_schema = str(payload.get("schema") or "")
        allowed_schema = (
            REFERENCE_INPUT_SCHEMA
            if temporary_awc_calibration_label
            else INPUT_SCHEMA
        )
        if input_schema != allowed_schema:
            raise ContractError("calibration_truth_input_schema_invalid")
        input_shas.append(
            str(payload.get("evidence_sha256") or payload.get("content_sha256") or "")
        )
        observations = payload.get(
            "reference_observations"
            if temporary_awc_calibration_label
            else "observations"
        )
        if not isinstance(observations, list):
            raise ContractError("calibration_truth_observations_invalid")
        for raw in observations:
            examined += 1
            if examined > MAX_OBSERVATIONS:
                raise ContractError("calibration_truth_observation_budget_exceeded")
            if not isinstance(raw, Mapping):
                raise ContractError("calibration_truth_observation_invalid")
            if str(raw.get("station_id") or "").strip().upper() != station:
                continue
            if str(raw.get("source_lane") or "").strip().upper() != lane:
                if temporary_awc_calibration_label:
                    # The isolated reference packet may also retain AMSC
                    # half-hour rows.  They remain reference-only and are
                    # ignored; only explicitly tagged AWC rows are consumed.
                    continue
                raise ContractError("calibration_truth_cross_lane_forbidden")
            if temporary_awc_calibration_label:
                if (
                    raw.get("evidence_role") != "REFERENCE_ONLY"
                    or raw.get("reference_reason")
                    != AWC_HOUR_REFERENCE_FOR_AMSC
                ):
                    raise ContractError(
                        "temporary_awc_calibration_label_lineage_invalid"
                    )
            elif str(raw.get("qc_status") or "") != "ACCEPTED":
                continue
            report_type = str(raw.get("report_type") or "").upper()
            valid_at = _utc(raw.get("valid_at_utc"))
            # The formal China/Taiwan truth lane is AMSC whole-hour METAR only.
            # Half-hour reports and AWC observations for these stations remain
            # reference evidence and cannot enter this dataset.
            if lane == "AMSC" and (
                report_type != "METAR"
                or valid_at.minute != 0
                or valid_at.second != 0
            ):
                continue
            if lane == "AWC" and report_type not in {"METAR", "SPECI"}:
                continue
            temperature = float(raw.get("reported_temperature_c"))
            if not math.isfinite(temperature):
                raise ContractError("calibration_truth_temperature_invalid")
            local_date = valid_at.astimezone(
                ZoneInfo(settlement_timezone)
            ).date()
            if not first <= local_date <= last:
                continue
            identity = {
                "station_id": station,
                "source_lane": lane,
                "report_type": report_type,
                "valid_at_utc": _utc_text(valid_at),
                "reported_temperature_c": temperature,
                "raw_sha256": str(raw.get("raw_sha256") or ""),
            }
            key = content_sha256(identity)
            previous = deduplicated.get(key)
            if previous is not None and previous != identity:
                raise ContractError("calibration_truth_duplicate_conflict")
            deduplicated[key] = identity

    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in deduplicated.values():
        local_day = _utc(row["valid_at_utc"]).astimezone(
            ZoneInfo(settlement_timezone)
        ).date().isoformat()
        by_date.setdefault(local_day, []).append(row)

    samples: list[dict[str, Any]] = []
    missing_dates: list[str] = []
    cursor = first
    while cursor <= last:
        target = cursor.isoformat()
        start, end = _window(target, settlement_timezone)
        rows = sorted(
            by_date.get(target, []),
            key=lambda row: (row["valid_at_utc"], row["raw_sha256"]),
        )
        if end > as_of or not rows:
            missing_dates.append(target)
            cursor += timedelta(days=1)
            continue
        maximum = max(float(row["reported_temperature_c"]) for row in rows)
        sample_identity = {
            "station_id": station,
            "event_date": target,
            "settlement_window": {
                "start_utc": _utc_text(start),
                "end_utc": _utc_text(end),
            },
            "truth": {
                "source_lane": lane,
                "tmax_c": maximum,
                "observations": rows,
            },
        }
        samples.append({
            "sample_id": "truth-" + content_sha256(sample_identity)[:32],
            **sample_identity,
        })
        cursor += timedelta(days=1)

    identity = {
        "schema": SCHEMA,
        "station_id": station,
        "settlement_timezone": settlement_timezone,
        "source_lane": lane,
        "label_authority": (
            TEMPORARY_AWC_LABEL_AUTHORITY
            if temporary_awc_calibration_label
            else "FORMAL_STATION_LANE_TRUTH"
        ),
        "start_date": start_date,
        "end_date": end_date,
        "as_of_utc": _utc_text(as_of),
        "samples": samples,
        "missing_dates": missing_dates,
        "input_evidence_sha256s": sorted(set(input_shas)),
        "checks": {
            "cross_lane_fallback_used": False,
            "closed_days_only": True,
            "amsc_whole_hour_only": lane == "AMSC",
            "temporary_awc_calibration_label":
                temporary_awc_calibration_label,
            "sample_count": len(samples),
            "complete_date_coverage": not missing_dates,
        },
        "boundary": {
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ContractError("calibration_truth_output_budget_exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path.parent).free - len(raw) < MIN_FREE_BYTES:
        raise ContractError("calibration_truth_minimum_free_disk_guard")
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation-artifact", action="append", type=Path, required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--settlement-timezone", required=True)
    parser.add_argument("--source-lane", choices=("AMSC", "AWC"), required=True)
    parser.add_argument(
        "--temporary-awc-calibration-label",
        action="store_true",
        help=(
            "Accept only isolated AWC reference evidence for an AMSC-owned "
            "station and mark it as a temporary historical calibration label."
        ),
    )
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--as-of-utc",
        default=_utc_text(datetime.now(timezone.utc)),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.observation_artifact) > MAX_INPUT_FILES:
        raise ContractError("calibration_truth_input_file_budget_exceeded")
    payloads = [
        load_bounded_json(path.expanduser().resolve(), limit=MAX_INPUT_BYTES)
        for path in args.observation_artifact
    ]
    result = build(
        payloads=payloads,
        station_id=args.station_id,
        settlement_timezone=args.settlement_timezone,
        source_lane=args.source_lane,
        start_date=args.start_date,
        end_date=args.end_date,
        as_of_utc=args.as_of_utc,
        temporary_awc_calibration_label=args.temporary_awc_calibration_label,
    )
    _write(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "schema": result["schema"],
        "station_id": result["station_id"],
        "sample_count": len(result["samples"]),
        "missing_date_count": len(result["missing_dates"]),
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["checks"]["complete_date_coverage"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
