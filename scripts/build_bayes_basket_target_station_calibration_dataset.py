#!/usr/bin/env python3
"""Build one station's bounded formal calibration truth dataset.

The input is one or more historical canonical-observation evidence packets.
This command never opens a production database.  It applies the station lane
contract, settlement timezone, closed-day rule, and official AWC report-type
rule, then emits the compact daily-high dataset consumed by the five-model OOS
builder.
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

from bayes_basket_target.calibration_ingress import (  # noqa: E402
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
)
from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    assert_timestamp_not_future,
    content_sha256,
)
from bayes_basket_target.resource_bounds import load_bounded_json  # noqa: E402
SCHEMA = "bayes_basket_station_calibration_dataset_v1"
INPUT_SCHEMA = "bayes_basket_target_historical_awc_replay_evidence_v4"
MAX_INPUT_FILES = 64
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OBSERVATIONS = 100_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
FORMAL_EVIDENCE_ROLE = "FORMAL"


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


def _sha256(value: Any, *, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{label}_invalid")
    return result


def _verify_historical_awc_evidence(
    payload: Mapping[str, Any],
    *,
    settlement_timezone: str,
) -> str:
    """Require one exact historical-AWC evidence envelope.

    Historical replay is usable only as an explicit no-live fact.  In
    particular, a caller cannot relabel a reference packet as AWC merely by
    supplying the requested lane on this CLI.
    """

    supplied = _sha256(
        payload.get("evidence_sha256"),
        label="calibration_truth_input_evidence_sha",
    )
    identity = {
        str(key): value
        for key, value in payload.items()
        if key != "evidence_sha256"
    }
    if supplied != content_sha256(identity):
        raise ContractError("calibration_truth_input_evidence_sha_mismatch")
    boundary = payload.get("boundary")
    checks = payload.get("checks")
    if (
        payload.get("target_timezone") != settlement_timezone
        or payload.get("historical_only") is not True
        or payload.get("canonical_ingress") is not True
        or payload.get("source_lane") != "AWC"
        or payload.get("source_role") != HISTORICAL_AWC_REPLAY
        or payload.get("authority_scope") != NO_LIVE_REPLAY
        or not isinstance(boundary, Mapping)
        or not isinstance(checks, Mapping)
        or checks.get("station_day_awc_report_hour_coverage_explicit") is not True
        or any(
            boundary.get(key) is not False
            for key in (
                "writes_production_db",
                "calls_order_gateway",
                "places_orders",
                "live_arrival_evidence",
                "enters_realtime_truth",
                "enters_p1",
                "enters_hard_no",
            )
        )
    ):
        raise ContractError("calibration_truth_input_lineage_invalid")
    return supplied


def build(
    *,
    payloads: Sequence[Mapping[str, Any]],
    station_id: str,
    settlement_timezone: str,
    source_lane: str,
    start_date: str,
    end_date: str,
    as_of_utc: str,
) -> dict[str, Any]:
    station = str(station_id or "").strip().upper()
    lane = str(source_lane or "").strip().upper()
    if not station or lane != "AWC":
        raise ContractError("calibration_truth_scope_invalid")
    try:
        ZoneInfo(settlement_timezone)
    except Exception as exc:
        raise ContractError("calibration_truth_timezone_invalid") from exc
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if first > last or (last - first).days >= 120:
        raise ContractError("calibration_truth_date_range_invalid")
    as_of = _utc(assert_timestamp_not_future(as_of_utc))

    deduplicated: dict[str, dict[str, Any]] = {}
    truth_ready_dates: set[str] = set()
    input_shas: list[str] = []
    examined = 0
    for payload in payloads:
        input_schema = str(payload.get("schema") or "")
        if input_schema != INPUT_SCHEMA:
            raise ContractError("calibration_truth_input_schema_invalid")
        input_sha = _verify_historical_awc_evidence(
            payload,
            settlement_timezone=settlement_timezone,
        )
        input_shas.append(input_sha)
        ready = payload.get("truth_ready_dates")
        blocked = payload.get("truth_blocked_dates")
        if (
            not isinstance(ready, list)
            or any(not isinstance(item, str) for item in ready)
            or not isinstance(blocked, list)
            or any(not isinstance(item, str) for item in blocked)
            or set(ready) & set(blocked)
        ):
            raise ContractError("calibration_truth_day_readiness_invalid")
        truth_ready_dates.update(ready)
        observations = payload.get("observations")
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
                raise ContractError("calibration_truth_cross_lane_forbidden")
            if (
                raw.get("source_role") != HISTORICAL_AWC_REPLAY
                or raw.get("authority_scope") != NO_LIVE_REPLAY
                or raw.get("settlement_timezone") != settlement_timezone
                or raw.get("evidence_role") != FORMAL_EVIDENCE_ROLE
                or raw.get("reference_only") is not False
            ):
                raise ContractError("calibration_truth_observation_lineage_invalid")
            if str(raw.get("qc_status") or "") != "ACCEPTED":
                continue
            report_type = str(raw.get("report_type") or "").upper()
            valid_at = _utc(raw.get("valid_at_utc"))
            if report_type not in {"METAR", "SPECI"}:
                continue
            if (
                report_type == "METAR"
                and (valid_at.minute != 0 or valid_at.second != 0)
            ):
                continue
            try:
                temperature = float(raw.get("reported_temperature_c"))
            except (TypeError, ValueError) as exc:
                raise ContractError("calibration_truth_temperature_invalid") from exc
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
                "source_role": HISTORICAL_AWC_REPLAY,
                "authority_scope": NO_LIVE_REPLAY,
                "settlement_timezone": settlement_timezone,
                "evidence_role": FORMAL_EVIDENCE_ROLE,
                "reference_only": False,
                "qc_status": "ACCEPTED",
                "report_type": report_type,
                "valid_at_utc": _utc_text(valid_at),
                "reported_temperature_c": temperature,
                "raw_sha256": str(raw.get("raw_sha256") or "").lower(),
            }
            if (
                len(identity["raw_sha256"]) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in identity["raw_sha256"]
                )
            ):
                raise ContractError("calibration_truth_raw_sha_invalid")
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
        if end > as_of or not rows or target not in truth_ready_dates:
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
            "settlement_timezone": settlement_timezone,
            "truth": {
                "source_lane": lane,
                "source_role": HISTORICAL_AWC_REPLAY,
                "authority_scope": NO_LIVE_REPLAY,
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
        "source_role": HISTORICAL_AWC_REPLAY,
        "authority_scope": NO_LIVE_REPLAY,
        "label_authority": "FORMAL_STATION_LANE_TRUTH",
        "start_date": start_date,
        "end_date": end_date,
        "as_of_utc": _utc_text(as_of),
        "samples": samples,
        "missing_dates": missing_dates,
        "input_evidence_sha256s": sorted(set(input_shas)),
        "checks": {
            "cross_lane_fallback_used": False,
            "closed_days_only": True,
            "formal_awc_truth_only": True,
            "awc_hourly_metar_and_speci_only": True,
            "historical_awc_replay_only": True,
            "input_evidence_content_hashes_verified": True,
            "settlement_timezone_exact_match": True,
            "reference_only_excluded": True,
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
    parser.add_argument("--source-lane", choices=("AWC",), required=True)
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
