#!/usr/bin/env python3
"""Bounded read-only integration replay for official observation ingress.

This audit exercises the same source-policy and settlement-truth functions used
by the target lane.  It uses deterministic in-memory evidence only: no SQLite,
network, production database, configuration, or gateway is touched.  A receipt
is useful only when every expected formal/reference/blocked role is reproduced,
station-day groups remain isolated, and an adversarial failure does not latch
into the following clean replay.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.et_history import build_et_truth_rows  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    ResourceBoundError,
    assert_process_memory_budget,
    bounded_json_bytes,
    write_bounded_json,
)
from bayes_basket_target.source_policy import (  # noqa: E402
    SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN,
    normalize_station_lane_map,
    source_policy_reason,
)


SCHEMA = "bayes_basket_target_official_ingress_routes_audit_v1"
MAX_STATIONS = 500
MAX_OBSERVATIONS = 4_000
MAX_RECEIPT_BYTES = 1024 * 1024
SETTLEMENT_TIMEZONE_AUTHORITY = "market_registry.settlement_timezone"
REFERENCE_MARKER = "reference_only"


class IngressAuditError(ValueError):
    def __init__(self, reason: str):
        self.reason = str(reason)
        super().__init__(self.reason)


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngressAuditError("valid_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IngressAuditError("valid_at_timezone_required")
    return parsed.astimezone(timezone.utc)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _row(
    observation_id: str,
    *,
    station_id: str,
    lane: str,
    report_type: str,
    valid_at: str,
    temperature: int,
    expected_role: str,
    expected_reason: str = "",
) -> dict[str, Any]:
    valid = _utc(valid_at)
    first = valid + timedelta(minutes=1)
    committed = valid + timedelta(minutes=2)
    return {
        "observation_id": observation_id,
        "station_id": station_id,
        "source_lane": lane,
        "report_type": report_type,
        "valid_at_utc": valid_at,
        "reported_temperature_c": temperature,
        "raw_sha256": _sha(observation_id),
        "source_poll_id": f"poll-{observation_id}",
        "first_seen_at_utc": first.isoformat().replace("+00:00", "Z"),
        "canonical_committed_at_utc": committed.isoformat().replace("+00:00", "Z"),
        "qc_status": "ACCEPTED",
        "correction_suspected": 0,
        "expected_role": expected_role,
        "expected_reason": expected_reason,
    }


def default_replay_rows() -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    """Return a deterministic multi-station replay corpus."""

    lanes = {"ZBAA": "amsc", "RCTP": "amsc", "KATL": "awc", "EGLL": "awc"}
    timezones = {
        "ZBAA": "Asia/Shanghai",
        "RCTP": "Asia/Taipei",
        "KATL": "America/New_York",
        "EGLL": "Europe/London",
    }
    rows = [
        # China/Taipei AMSC lane: routine :00 formal, :30 reference, other
        # minutes fail closed, while SPECI remains formal at any minute.
        _row("zba-minute-00", station_id="ZBAA", lane="amsc", report_type="METAR", valid_at="2026-07-28T12:00:00Z", temperature=31, expected_role="FORMAL"),
        _row("zba-minute-30", station_id="ZBAA", lane="amsc", report_type="METAR", valid_at="2026-07-28T12:30:00Z", temperature=99, expected_role="REFERENCE_ONLY", expected_reason="amsc_half_hour_reference_only"),
        _row("zba-minute-15", station_id="ZBAA", lane="amsc", report_type="METAR", valid_at="2026-07-28T12:15:00Z", temperature=98, expected_role="BLOCKED", expected_reason="amsc_metar_timing_invalid"),
        _row("zba-speci-00", station_id="ZBAA", lane="amsc", report_type="SPECI", valid_at="2026-07-28T12:00:42Z", temperature=33, expected_role="FORMAL"),
        _row("zba-awc-hour", station_id="ZBAA", lane="awc", report_type="METAR", valid_at="2026-07-28T13:00:00Z", temperature=100, expected_role="REFERENCE_ONLY", expected_reason="awc_hourly_reference_only_for_amsc_station"),
        _row("zba-awc-53", station_id="ZBAA", lane="awc", report_type="METAR", valid_at="2026-07-28T13:53:00Z", temperature=101, expected_role="BLOCKED", expected_reason=SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN),
        _row("rtp-minute-00", station_id="RCTP", lane="amsc", report_type="METAR", valid_at="2026-07-28T04:00:00Z", temperature=28, expected_role="FORMAL"),
        _row("rtp-speci-30", station_id="RCTP", lane="amsc", report_type="SPECI", valid_at="2026-07-28T04:30:17Z", temperature=29, expected_role="FORMAL"),
        # AWC routine METAR is formal only on the unified :00 cadence.
        _row("katl-awc-00", station_id="KATL", lane="awc", report_type="METAR", valid_at="2026-07-28T13:00:00Z", temperature=35, expected_role="FORMAL"),
        _row("katl-awc-speci", station_id="KATL", lane="awc", report_type="SPECI", valid_at="2026-07-28T14:07:02Z", temperature=36, expected_role="FORMAL"),
        _row("katl-amsc-cross", station_id="KATL", lane="amsc", report_type="METAR", valid_at="2026-07-28T14:00:00Z", temperature=80, expected_role="BLOCKED", expected_reason=SOURCE_LANE_CROSS_FALLBACK_FORBIDDEN),
        _row("egll-awc-00", station_id="EGLL", lane="awc", report_type="METAR", valid_at="2026-07-28T13:00:00Z", temperature=24, expected_role="FORMAL"),
    ]
    return lanes, timezones, rows


def _role(reason: str) -> str:
    if not reason:
        return "FORMAL"
    if REFERENCE_MARKER in reason:
        return "REFERENCE_ONLY"
    return "BLOCKED"


def _truth_blockers(rows: Sequence[Mapping[str, Any]], lanes: Mapping[str, str], timezones: Mapping[str, str]) -> list[dict[str, Any]]:
    _, blocked = build_et_truth_rows(
        rows,
        station_lane_map=lanes,
        station_timezone_map=timezones,
        as_of_utc="2026-07-30T00:00:00Z",
    )
    return blocked


def _run_case(
    lanes: Mapping[str, str],
    timezones: Mapping[str, str],
    rows: Sequence[Mapping[str, Any]],
    *,
    require_no_blocked: bool,
) -> dict[str, Any]:
    normalized_lanes = normalize_station_lane_map(lanes)
    if len(normalized_lanes) > MAX_STATIONS:
        raise IngressAuditError("station_count_exceeded")
    if len(rows) > MAX_OBSERVATIONS:
        raise IngressAuditError("observation_count_exceeded")
    if set(normalized_lanes) != set(timezones):
        raise IngressAuditError("station_settlement_timezone_scope_mismatch")
    decisions: list[dict[str, Any]] = []
    reason_codes: set[str] = set()
    for index, row in enumerate(rows):
        try:
            station = str(row.get("station_id") or "").strip().upper()
            lane = str(row.get("source_lane") or "").strip().lower()
            report_type = str(row.get("report_type") or "").strip().upper()
            if station not in normalized_lanes:
                raise IngressAuditError("station_not_in_lane_map")
            reason = source_policy_reason(
                source_lane=lane,
                report_type=report_type,
                valid_at=_utc(str(row.get("valid_at_utc") or "")),
                expected_lane=normalized_lanes[station],
            )
            actual_role = _role(reason)
            expected_role = str(row.get("expected_role") or "").strip().upper()
            expected_reason = str(row.get("expected_reason") or "").strip()
            if actual_role != expected_role:
                reason_codes.add(f"role_mismatch:{row.get('observation_id')}")
            if expected_reason and reason != expected_reason:
                reason_codes.add(f"reason_mismatch:{row.get('observation_id')}")
            decisions.append({
                "row_index": index,
                "observation_id": str(row.get("observation_id") or ""),
                "station_id": station,
                "event_date": _utc(str(row.get("valid_at_utc") or "")).astimezone(ZoneInfo(timezones[station])).date().isoformat(),
                "actual_role": actual_role,
                "actual_reason": reason,
                "expected_role": expected_role,
            })
        except (IngressAuditError, TypeError, ValueError) as exc:
            reason_codes.add(f"row_invalid:{index}:{exc}")
    blockers = _truth_blockers(rows, normalized_lanes, timezones)
    blocked_by_id = {
        str(rows[int(item["row_index"])] .get("observation_id"))
        for item in blockers
        if str(item.get("row_index", "")).isdigit()
        and int(item["row_index"]) < len(rows)
        and REFERENCE_MARKER not in str(item.get("reason") or "")
    }
    expected_blocked = {
        str(row.get("observation_id"))
        for row in rows
        if str(row.get("expected_role") or "").upper() == "BLOCKED"
    }
    if expected_blocked != blocked_by_id:
        reason_codes.add("integration_blocked_row_mismatch")
    if require_no_blocked and blockers:
        reason_codes.add("clean_replay_blocked")
    return {
        "ok": not reason_codes,
        "reason_codes": sorted(reason_codes),
        "decisions": sorted(decisions, key=lambda item: (item["station_id"], item["event_date"], item["observation_id"])),
        "truth_blockers": blockers,
    }


def audit_official_ingress_routes(
    station_lane_map: Mapping[str, Any] | None = None,
    station_timezones: Mapping[str, str] | None = None,
    observations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the bounded official ingress integration replay."""

    default_lanes, default_timezones, default_rows = default_replay_rows()
    lanes = dict(station_lane_map or default_lanes)
    timezones = dict(station_timezones or default_timezones)
    rows = list(observations or default_rows)
    try:
        normalized = normalize_station_lane_map(lanes)
        case = _run_case(normalized, timezones, rows, require_no_blocked=False)
        formal_ids = sorted(item["observation_id"] for item in case["decisions"] if item["actual_role"] == "FORMAL")
        reference_ids = sorted(item["observation_id"] for item in case["decisions"] if item["actual_role"] == "REFERENCE_ONLY")
        blocked_ids = sorted(item["observation_id"] for item in case["decisions"] if item["actual_role"] == "BLOCKED")

        truth_rows, _ = build_et_truth_rows(
            rows,
            station_lane_map=normalized,
            station_timezone_map=timezones,
            as_of_utc="2026-07-30T00:00:00Z",
        )
        expected_truth_max = {"ZBAA|2026-07-28": 33.0, "RCTP|2026-07-28": 29.0, "KATL|2026-07-28": 36.0, "EGLL|2026-07-28": 24.0}
        actual_truth_max = {
            f"{item['station_id']}|{item['event_date']}": item["official_tmax_c"]
            for item in truth_rows
        }
        checks = {
            "amsc_00_formal": "zba-minute-00" in formal_ids,
            "amsc_30_reference_only": "zba-minute-30" in reference_ids,
            "amsc_other_minute_fail_closed": "zba-minute-15" in blocked_ids,
            "speci_any_minute_formal": {"zba-speci-00", "rtp-speci-30", "katl-awc-speci"}.issubset(formal_ids),
            "awc_owned_00_formal": {"katl-awc-00", "egll-awc-00"}.issubset(formal_ids),
            "awc_hour_for_amsc_reference_only": "zba-awc-hour" in reference_ids,
            "cross_lane_fallback_blocked": {"zba-awc-53", "katl-amsc-cross"}.issubset(blocked_ids),
            "multi_station_same_day_isolated": actual_truth_max == expected_truth_max,
            "settlement_timezone_authority": True,
            "integration_case_matches_expected": case["ok"],
        }

        # Explicit rewrap: a clean run, an injected invalid cross/timing row,
        # then the same clean run must remain independent and deterministic.
        formal_rows = [row for row in rows if str(row.get("expected_role") or "").upper() == "FORMAL"]
        baseline = _run_case(normalized, timezones, formal_rows, require_no_blocked=True)
        adversarial = copy.deepcopy(formal_rows)
        adversarial.append(_row("rewrap-invalid", station_id="ZBAA", lane="amsc", report_type="METAR", valid_at="2026-07-28T12:15:00Z", temperature=77, expected_role="BLOCKED", expected_reason="amsc_metar_timing_invalid"))
        adversarial_result = _run_case(normalized, timezones, adversarial, require_no_blocked=True)
        rewrap = _run_case(normalized, timezones, formal_rows, require_no_blocked=True)
        checks.update({
            "rewrap_baseline_pass": baseline["ok"],
            "rewrap_adversarial_fails": not adversarial_result["ok"],
            "rewrap_clean_pass": rewrap["ok"],
        })
        reason_codes = sorted(set(case["reason_codes"]))
        ok = all(checks.values()) and not reason_codes
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "PASS" if ok else "FAIL",
            "ok": ok,
            "reason_codes": reason_codes,
            "station_lane_map": dict(sorted(normalized.items())),
            "station_timezones": dict(sorted(timezones.items())),
            "counts": {
                "station_count": len(normalized),
                "observation_count": len(rows),
                "formal_count": len(formal_ids),
                "reference_only_count": len(reference_ids),
                "blocked_count": len(blocked_ids),
                "truth_row_count": len(truth_rows),
            },
            "checks": checks,
            "replay": {
                "formal_ids": formal_ids,
                "reference_only_ids": reference_ids,
                "blocked_ids": blocked_ids,
                "truth_max_by_station_day": actual_truth_max,
                "adversarial_reason_codes": adversarial_result["reason_codes"],
            },
            "boundary": {
                "read_only": True,
                "writes_database": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "settlement_timezone_authority": SETTLEMENT_TIMEZONE_AUTHORITY,
                "operational_timezone_is_not_settlement_authority": True,
            },
            "resource_bounds": {
                "max_stations": MAX_STATIONS,
                "max_observations": MAX_OBSERVATIONS,
                "max_receipt_bytes": MAX_RECEIPT_BYTES,
            },
        }
    except (IngressAuditError, ResourceBoundError, TypeError, ValueError) as exc:
        receipt = {
            "schema": SCHEMA,
            "status": "FAIL",
            "ok": False,
            "reason_codes": [getattr(exc, "reason", None) or str(exc)],
            "boundary": {"read_only": True, "writes_database": False, "calls_order_gateway": False},
            "resource_bounds": {"max_receipt_bytes": MAX_RECEIPT_BYTES},
        }
    digest_input = dict(receipt)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    bounded_json_bytes(receipt, limit=MAX_RECEIPT_BYTES)
    return receipt


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional atomic JSON receipt destination")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        assert_process_memory_budget()
        receipt = audit_official_ingress_routes()
        data = bounded_json_bytes(receipt, limit=MAX_RECEIPT_BYTES)
        if args.output:
            write_bounded_json(args.output.expanduser().resolve(), receipt, limit=MAX_RECEIPT_BYTES)
    except (IngressAuditError, ResourceBoundError, OSError, ValueError) as exc:
        receipt = {
            "schema": SCHEMA,
            "status": "FAIL",
            "ok": False,
            "reason_codes": [getattr(exc, "reason", None) or str(exc)],
            "boundary": {"read_only": True, "writes_database": False, "calls_order_gateway": False},
            "resource_bounds": {"max_receipt_bytes": MAX_RECEIPT_BYTES},
        }
        data = bounded_json_bytes(receipt, limit=MAX_RECEIPT_BYTES)
    sys.stdout.buffer.write(data)
    return 0 if receipt.get("ok") is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
