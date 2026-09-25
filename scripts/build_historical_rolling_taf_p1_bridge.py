#!/usr/bin/env python3
"""Index the historical rolling native candidate into the TAF/P1 lane.

This is deliberately a read-only bridge.  The rolling native artifact is a
single 20-train/8-holdout historical candidate (cut off at a closed event
day); it is not a current P0 root.  The bridge only records overlap with
already retained official AWC TAF/truth rows and the current P1 audit.  It
never creates a P0/P0-M/P1 row, writes the target database, or treats the
overlap as the formal TAF-24 gate.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_ROLLING = ROOT / "output/audits/native_ensemble_historical_oos_rolling_candidate_20260803_0728.json"
DEFAULT_TAF_AUDIT = ROOT / "output/audits/taf_p0m_exact_join_45station_20260803_stable_reaudit.json"
DEFAULT_P1_AUDIT = ROOT / "output/audits/p1_trajectory_45station_audit_20260802_recheck.json"
DEFAULT_OUTPUT = ROOT / "output/audits/historical_rolling_taf_p1_bridge_20260803_0728.json"
SCHEMA = "bayes_basket_target_historical_rolling_taf_p1_bridge_v1"
ROLLING_SCHEMA = "native_ensemble_forward_oos_manifest_v1"
TAF_AUDIT_SCHEMA = "bayes_basket_target_taf_p0m_exact_join_45station_audit_v1"
P1_AUDIT_SCHEMA = "bayes_basket_target_p1_trajectory_45station_audit_v1"
TAF_MIN = 24
MAX_STATIONS = expected_station_count()
MIN_HISTORICAL_CUTOFF = date(2026, 7, 28)


def _load(path: Path) -> dict[str, Any]:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("rolling_bridge_symlink_input_forbidden")
    payload = load_bounded_json(requested.resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(payload, Mapping):
        raise ContractError("rolling_bridge_mapping_required")
    return dict(payload)


def _verify_hash(payload: Mapping[str, Any], *, name: str) -> str:
    supplied = str(payload.get("content_sha256") or "").strip().lower()
    if len(supplied) != 64 or any(ch not in "0123456789abcdef" for ch in supplied):
        raise ContractError(f"rolling_bridge_{name}_hash_invalid")
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    if content_sha256(identity) != supplied:
        raise ContractError(f"rolling_bridge_{name}_hash_mismatch")
    return supplied


def _day(value: Any, *, name: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ContractError(f"rolling_bridge_{name}_invalid") from exc


def _db(path: Path) -> sqlite3.Connection:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ContractError("rolling_bridge_database_symlink_forbidden")
    conn = sqlite3.connect(f"file:{requested.resolve(strict=True)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _taf_counts(
    conn: sqlite3.Connection,
    *,
    station: str,
    cutoff: str,
    holdout_days: set[str],
    decision_by_day: Mapping[str, str],
    settlement_timezone: str,
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT event_date,tx_c,provider_available_at_utc,content_sha256 "
        "FROM calibration_historical_taf_capture "
        "WHERE station_id=? AND event_date<=? "
        "AND authority_scope='CALIBRATION_TRAIN_ONLY' "
        "AND truth_label_authority='FORMAL_STATION_LANE_TRUTH' "
        "ORDER BY event_date,provider_available_at_utc,content_sha256",
        (station, cutoff),
    ).fetchall()
    tx_days = {str(row[0]) for row in rows if row[1] is not None}
    overlap = sorted(holdout_days & tx_days)
    try:
        tz = ZoneInfo(settlement_timezone)
    except Exception as exc:  # pragma: no cover - registry supplies IANA zones
        raise ContractError("rolling_bridge_settlement_timezone_invalid") from exc
    strict_days: set[str] = set()
    for row in rows:
        event_day = str(row[0])
        if row[1] is None or event_day not in decision_by_day:
            continue
        try:
            available = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00")).astimezone(timezone.utc)
            decision = datetime.fromisoformat(str(decision_by_day[event_day]).replace("Z", "+00:00")).astimezone(timezone.utc)
            day_end = datetime.combine(
                date.fromisoformat(event_day) + timedelta(days=1),
                time.min,
                tzinfo=tz,
            ).astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ContractError("rolling_bridge_taf_timestamp_invalid") from exc
        # Same strict-as-of shape as the formal exact join: TAF must arrive
        # after the native decision and before the local settlement day ends.
        if decision < available < day_end:
            strict_days.add(event_day)
    return {
        "row_count": len(rows),
        "distinct_event_day_count": len({str(row[0]) for row in rows}),
        "tx_row_count": sum(row[1] is not None for row in rows),
        "tx_event_day_count": len(tx_days),
        "rolling_holdout_tx_overlap_days": overlap,
        "rolling_holdout_tx_overlap_count": len(overlap),
        "strict_asof_tx_overlap_days": sorted(strict_days & holdout_days),
        "strict_asof_tx_overlap_count": len(strict_days & holdout_days),
        "source": "target_ssot.calibration_historical_taf_capture",
    }


def _truth_counts(
    conn: sqlite3.Connection,
    *,
    station: str,
    cutoff: str,
    holdout_days: set[str],
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT local_date,revision_no,canonical_committed_at_utc "
        "FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND local_date<=? AND source_lane IN " + FORMAL_TRUTH_LANES_SQL + " "
        "AND qc_status='ACCEPTED' ORDER BY local_date,revision_no",
        (station, cutoff),
    ).fetchall()
    days = {str(row[0]) for row in rows}
    return {
        "accepted_awc_row_count": len(rows),
        "accepted_awc_event_day_count": len(days),
        "rolling_holdout_truth_overlap_count": len(days & holdout_days),
        "source": "target_ssot.calibration_station_daily_high_truth",
    }


def _existing_join_map(taf_audit: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in taf_audit.get("entries") or []:
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").strip().upper()
        join = raw.get("exact_join")
        if not station or not isinstance(join, Mapping):
            continue
        result[station] = {
            "sample_count": int(join.get("sample_count") or 0),
            "state": str(raw.get("state") or ""),
            "blockers": [str(x) for x in raw.get("blockers") or []],
            "path": str(join.get("path") or ""),
            "current_p0_scope_present": isinstance(raw.get("current_p0_scope"), Mapping),
        }
    return result


def _p1_map(p1_audit: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in p1_audit.get("scopes") or []:
        if not isinstance(raw, Mapping):
            continue
        station = str(raw.get("station_id") or "").strip().upper()
        if not station:
            continue
        row = result.setdefault(station, {"scope_count": 0, "formal_eligible_count": 0, "blockers": Counter()})
        row["scope_count"] += 1
        row["formal_eligible_count"] += int(raw.get("formal_eligible") is True)
        for blocker in raw.get("blockers") or []:
            row["blockers"][str(blocker)] += 1
    return result


def build_bridge(
    *,
    db_path: Path,
    rolling_path: Path,
    taf_audit_path: Path,
    p1_audit_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    rolling = _load(rolling_path)
    rolling_sha = _verify_hash(rolling, name="rolling")
    if rolling.get("schema") != ROLLING_SCHEMA:
        raise ContractError("rolling_bridge_rolling_schema_invalid")
    if rolling.get("oos_data_policy") != "HISTORICAL_CLOSED_DAY_ONLY":
        raise ContractError("rolling_bridge_historical_policy_required")
    if rolling.get("future_data_policy") != "OBSERVATIONAL_CANDIDATE_NOT_OOS":
        raise ContractError("rolling_bridge_future_policy_required")
    if int(rolling.get("future_event_count") or 0) != 0:
        raise ContractError("rolling_bridge_future_events_forbidden")
    cutoff_text = str(rolling.get("historical_event_date_cutoff") or "").strip()
    cutoff = _day(cutoff_text, name="historical_cutoff")
    if cutoff < MIN_HISTORICAL_CUTOFF:
        raise ContractError("rolling_bridge_historical_cutoff_below_2026_07_28")

    taf_audit = _load(taf_audit_path)
    taf_sha = _verify_hash(taf_audit, name="taf_audit")
    if taf_audit.get("schema") != TAF_AUDIT_SCHEMA:
        raise ContractError("rolling_bridge_taf_audit_schema_invalid")
    p1_audit = _load(p1_audit_path)
    p1_sha = _verify_hash(p1_audit, name="p1_audit")
    if p1_audit.get("schema") != P1_AUDIT_SCHEMA:
        raise ContractError("rolling_bridge_p1_audit_schema_invalid")
    if int(taf_audit.get("canonical_station_count") or 0) != MAX_STATIONS:
        raise ContractError("rolling_bridge_taf_station_scope_invalid")
    if int(p1_audit.get("station_scope", {}).get("canonical_station_count") or 0) != MAX_STATIONS:
        raise ContractError("rolling_bridge_p1_station_scope_invalid")

    existing_joins = _existing_join_map(taf_audit)
    p1_by_station = _p1_map(p1_audit)
    reports = rolling.get("reports")
    if not isinstance(reports, list) or len(reports) != MAX_STATIONS:
        raise ContractError("rolling_bridge_rolling_station_scope_invalid")
    stations = sorted({str(item.get("station_id") or "").upper() for item in reports if isinstance(item, Mapping)})
    if len(stations) != MAX_STATIONS:
        raise ContractError("rolling_bridge_station_set_invalid")

    station_rows: list[dict[str, Any]] = []
    with _db(db_path) as conn:
        for raw in sorted(reports, key=lambda item: str(item.get("station_id") or "")):
            station = str(raw.get("station_id") or "").upper()
            candidates = [item for item in raw.get("cross_candidates") or [] if isinstance(item, Mapping)]
            passed = [item for item in candidates if str(item.get("state") or "") == "PRODUCTION_NO_LIVE_OOS_PASS"]
            rejected = [item for item in candidates if str(item.get("state") or "") == "OOS_REJECTED"]
            holdout_days: set[str] = set()
            decision_by_day: dict[str, str] = {}
            settlement_timezones: set[str] = set()
            selected_systems: set[str] = set()
            families: set[str] = set()
            train_counts: set[int] = set()
            for candidate in candidates:
                selected_systems.update(str(value) for value in candidate.get("selected_system_ids") or [])
                families.update(str(value) for value in candidate.get("independence_family_ids") or [])
                train_counts.add(int(candidate.get("min_train_days") or 0))
                block = candidate.get("rolling_origin_block") or {}
                holdout_days.update(str(value) for value in block.get("holdout_dates") or [])
                for pred in candidate.get("predictions") or []:
                    if not isinstance(pred, Mapping):
                        continue
                    day = str(pred.get("event_date") or "")
                    if day:
                        holdout_days.add(day)
                        decision_by_day[day] = str(pred.get("decision_at_utc") or "")
                if candidate.get("settlement_timezone"):
                    settlement_timezones.add(str(candidate["settlement_timezone"]))
            invalid_future = [day for day in holdout_days if _day(day, name="holdout_day") > cutoff]
            if invalid_future:
                raise ContractError("rolling_bridge_future_holdout_forbidden")
            if len(settlement_timezones) > 1:
                raise ContractError("rolling_bridge_settlement_timezone_ambiguous")
            settlement_timezone = next(iter(settlement_timezones), "UTC")
            taf = _taf_counts(
                conn,
                station=station,
                cutoff=cutoff_text,
                holdout_days=holdout_days,
                decision_by_day=decision_by_day,
                settlement_timezone=settlement_timezone,
            )
            truth = _truth_counts(conn, station=station, cutoff=cutoff_text, holdout_days=holdout_days)
            p1 = p1_by_station.get(station, {"scope_count": 0, "formal_eligible_count": 0, "blockers": Counter()})
            existing = existing_joins.get(station, {})
            blockers: list[str] = []
            if not passed:
                blockers.append("rolling_native_candidate_missing_or_rejected")
            if taf["strict_asof_tx_overlap_count"] == 0:
                blockers.append("awc_taf_tx_overlap_missing")
            elif taf["strict_asof_tx_overlap_count"] < TAF_MIN:
                blockers.append("taf_native_oos_sample_n_insufficient")
            # The 7/28 rolling artifact is a candidate input only.  It is not
            # an activation and must never be treated as the current P0 root.
            blockers.append("rolling_candidate_not_bound_to_current_p0_root")
            if int(p1.get("formal_eligible_count") or 0) == 0:
                blockers.append("p1_formal_trajectory_unbound")
            station_rows.append({
                "station_id": station,
                "rolling_candidate_count": len(candidates),
                "rolling_oos_pass_count": len(passed),
                "rolling_oos_rejected_count": len(rejected),
                "rolling_holdout_days": sorted(holdout_days),
                "rolling_holdout_count": len(holdout_days),
                "rolling_min_train_days": sorted(train_counts),
                "rolling_selected_system_ids": sorted(selected_systems),
                "rolling_independence_families": sorted(families),
                "candidate_only": True,
                "current_p0_root_bound": False,
                "taf_capture": taf,
                "truth": truth,
                "existing_current_p0_exact_join": existing,
                "p1_audit": {
                    "scope_count": int(p1.get("scope_count") or 0),
                    "formal_eligible_count": int(p1.get("formal_eligible_count") or 0),
                    "blocker_counts": dict(sorted((p1.get("blockers") or Counter()).items())),
                },
                "blockers": sorted(set(blockers)),
                "state": "CANDIDATE_ONLY_BLOCKED" if blockers else "CANDIDATE_ONLY_OVERLAP",
            })

    summary = {
        "station_count": len(station_rows),
        "rolling_oos_pass_station_count": sum(row["rolling_oos_pass_count"] > 0 for row in station_rows),
        "rolling_oos_rejected_station_count": sum(row["rolling_oos_rejected_count"] > 0 for row in station_rows),
        "rolling_candidate_missing_station_count": sum(row["rolling_candidate_count"] == 0 for row in station_rows),
        "taf_tx_overlap_station_count": sum(row["taf_capture"]["strict_asof_tx_overlap_count"] > 0 for row in station_rows),
        "taf_tx_overlap_sample_count": sum(row["taf_capture"]["strict_asof_tx_overlap_count"] for row in station_rows),
        "taf_qualified_scope_count_at_24": sum(row["taf_capture"]["strict_asof_tx_overlap_count"] >= TAF_MIN for row in station_rows),
        "max_taf_tx_overlap_count": max(row["taf_capture"]["strict_asof_tx_overlap_count"] for row in station_rows),
        "p1_formal_eligible_scope_count": sum(row["p1_audit"]["formal_eligible_count"] for row in station_rows),
        "current_p0_root_bound_count": sum(row["current_p0_root_bound"] for row in station_rows),
    }
    identity = {
        "schema": SCHEMA,
        "as_of_utc": str(rolling.get("as_of_utc") or ""),
        "historical_event_date_cutoff": cutoff_text,
        "oos_data_policy": "HISTORICAL_CLOSED_DAY_ONLY",
        "future_data_policy": "OBSERVATIONAL_CANDIDATE_NOT_OOS",
        "taf_minimum_sample_n": TAF_MIN,
        "rolling_input": {"path": str(rolling_path.resolve()), "content_sha256": rolling_sha},
        "taf_audit_input": {"path": str(taf_audit_path.resolve()), "content_sha256": taf_sha},
        "p1_audit_input": {"path": str(p1_audit_path.resolve()), "content_sha256": p1_sha},
        "summary": summary,
        "stations": station_rows,
        "state": "CANDIDATE_ONLY_BLOCKED",
        "boundary": {
            "read_only": True,
            "network_fetches": 0,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "writes_target_database": False,
            "writes_production_database": False,
            "production_p0_materialized": False,
            "production_p0m_materialized": False,
            "p1_materialized": False,
            "current_p0_root_mutated": False,
            "candidate_only": True,
            "future_data_used_as_oos": False,
        },
        "resolution": {
            "next_step": "accumulate two more disjoint historical 8-day blocks from closed AWC truth and bind each to immutable P0 candidate roots before any TAF-24 decision",
            "taf_action": "reuse existing official AWC captures only; do not synthesize revision/TX or promote this bridge",
            "p1_action": "complete strict-as-of AWC prefix and root-bound trajectory folds; no current P1 materialization from this candidate",
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json_if_changed(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--rolling", type=Path, default=DEFAULT_ROLLING)
    parser.add_argument("--taf-audit", type=Path, default=DEFAULT_TAF_AUDIT)
    parser.add_argument("--p1-audit", type=Path, default=DEFAULT_P1_AUDIT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_bridge(
        db_path=args.db,
        rolling_path=args.rolling,
        taf_audit_path=args.taf_audit,
        p1_audit_path=args.p1_audit,
        output_path=args.output,
    )
    print(json.dumps({
        "schema": result["schema"],
        "output": str(args.output.resolve()),
        "content_sha256": result["content_sha256"],
        "summary": result["summary"],
        "state": result["state"],
        "boundary": result["boundary"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
