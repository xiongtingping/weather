#!/usr/bin/env python3
"""Audit the retained historical AWC METAR chain without rebinding P0 roots.

The output is a one-shot, content-addressed gap ledger.  It deliberately
separates (a) retained official AWC raw/HTTP-200 receipt coverage, (b) closed
AWC truth timing, and (c) the non-negotiable same-capture future-temperature
curve bound to the original P0/P0-M root.  A later archive curve must never
be attached to an old root, so this tool never writes a P0/P0-M/P1 row and
never emits an appendable P1 root when that original binding is absent.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, SHARED_LIVE_DB  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_OUTPUT_BYTES,
    assert_process_memory_budget,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from scripts.build_bayes_basket_target_metar_trajectory_training_packet import (  # noqa: E402
    _poll_receipt_verified,
)
from bayes_basket_target.settlement_time import settlement_day_window  # noqa: E402


SCHEMA = "bayes_basket_target_p1_historical_metar_chain_gap_ledger_v1"
MAX_ROOTS = 4096
MAX_RAW_ROWS_PER_ROOT = 512
MAX_POLL_JSON_BYTES = 2 * 1024 * 1024


def _utc(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"p1_history_chain_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p1_history_chain_{field}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _curve_state(root: Mapping[str, Any]) -> str:
    """Validate only an existing binding; this function never fills one."""
    try:
        lineage = json.loads(str(root.get("lineage_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "ROOT_LINEAGE_INVALID"
    if not isinstance(lineage, Mapping):
        return "ROOT_LINEAGE_INVALID"
    raw = lineage.get("conditional_future_temperature_curve_json")
    declared = str(lineage.get("conditional_future_temperature_curve_sha256") or "").lower()
    if not raw or not declared:
        return "ROOT_FUTURE_CURVE_MISSING"
    try:
        curve = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "ROOT_FUTURE_CURVE_INVALID"
    if not isinstance(curve, Mapping) or str(curve.get("content_sha256") or "").lower() != declared:
        return "ROOT_FUTURE_CURVE_INVALID"
    stage = str(root.get("stage") or "")
    checkpoint = str(root.get("checkpoint_id") or "")
    posterior = str(root.get("posterior_sha256") or "")
    curve_stage = str(lineage.get("conditional_future_temperature_curve_root_stage") or stage)
    curve_checkpoint = str(lineage.get("conditional_future_temperature_curve_root_checkpoint_id") or checkpoint)
    curve_probability = str(lineage.get("conditional_future_temperature_curve_root_probability_sha256") or posterior)
    direct = curve_stage == stage and curve_checkpoint == checkpoint and curve_probability == posterior
    ancestor = (
        stage == "P0-M" and curve_stage == "P0"
        and curve_checkpoint == str(lineage.get("p0_root_checkpoint_id") or "")
        and curve_probability == str(lineage.get("p0_root_posterior_sha256") or "")
    )
    return "ROOT_FUTURE_CURVE_BOUND" if direct or ancestor else "ROOT_FUTURE_CURVE_BINDING_INVALID"


def _raw_receipt_inventory(
    conn: sqlite3.Connection, *, station_id: str, event_date: str,
    timezone_name: str, decision_at: datetime,
) -> dict[str, Any]:
    window = settlement_day_window(event_date, timezone_name)
    begin = str(window["target_day_start_utc"])
    end = str(window["target_day_end_utc"])
    rows = conn.execute(
        "SELECT o.observation_id,o.source_lane,o.station_id,o.report_type,o.valid_at,"
        "o.reported_temperature_c,o.raw_sha256,o.raw_text,o.canonical_committed_at,"
        "o.correction_suspected,o.source_poll_id,p.source_lane AS poll_source_lane,"
        "p.response_status AS poll_response_status,p.response_completed_at AS poll_response_completed_at,"
        "CASE WHEN length(p.poll_json)<=? THEN p.poll_json ELSE NULL END AS poll_json "
        "FROM reported_trajectory_observations o LEFT JOIN reported_trajectory_source_polls p "
        "ON p.poll_id=o.source_poll_id WHERE o.station_id=? AND o.source_lane='awc' "
        "AND o.report_type IN ('METAR','SPECI') AND o.valid_at>=? AND o.valid_at<? "
        "AND o.valid_at<=? AND o.canonical_committed_at<=? "
        "ORDER BY o.valid_at,o.canonical_committed_at,o.observation_id LIMIT ?",
        (MAX_POLL_JSON_BYTES, station_id, begin, end, _stamp(decision_at), _stamp(decision_at), MAX_RAW_ROWS_PER_ROOT + 1),
    ).fetchall()
    if len(rows) > MAX_RAW_ROWS_PER_ROOT:
        raise ContractError("p1_history_chain_raw_receipt_limit_exceeded")
    verified = 0
    corrections = 0
    for row in rows:
        assert_process_memory_budget()
        value = dict(row)
        corrections += int(bool(value.get("correction_suspected")))
        verified += int(_poll_receipt_verified(value))
    state = (
        "RAW_RECEIPT_NONE_BEFORE_DECISION" if not rows else
        "RAW_RECEIPT_ALL_EXACT_AWC_BOUND" if verified == len(rows) and corrections == 0 else
        "RAW_RECEIPT_PARTIAL_OR_UNVERIFIED"
    )
    return {
        "state": state,
        "pre_decision_observation_count": len(rows),
        "exact_awc_http200_receipt_count": verified,
        "unverified_or_nonofficial_count": len(rows) - verified,
        "correction_suspected_count": corrections,
    }


def _truth_state(conn: sqlite3.Connection, *, station_id: str, event_date: str, decision_at: datetime, as_of: datetime, timezone_name: str) -> str:
    row = conn.execute(
        "SELECT canonical_committed_at_utc FROM calibration_station_daily_high_truth "
        "WHERE station_id=? AND local_date=? AND source_lane='AWC' AND qc_status='ACCEPTED' "
        "AND timestamps_complete=1 ORDER BY revision_no DESC,canonical_committed_at_utc DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    if row is None:
        return "CLOSED_AWC_TRUTH_MISSING"
    committed = _utc(row[0], field="truth_committed_at")
    day_end = _utc(settlement_day_window(event_date, timezone_name)["target_day_end_utc"], field="day_end")
    if day_end > as_of:
        return "FUTURE_EVENT_NOT_OOS"
    if committed > as_of:
        return "TRUTH_NOT_AVAILABLE_AT_AS_OF"
    if committed <= decision_at:
        return "TRUTH_VISIBLE_AT_DECISION_NOT_OOS"
    return "CLOSED_AWC_TRUTH_STRICT_OOS"


def build(*, target_db: Path, archive_db: Path, as_of_utc: str) -> dict[str, Any]:
    as_of = _utc(as_of_utc, field="as_of_utc")
    canonical = coordinate_registry_manifest()
    stations = set(canonical["station_ids"])
    with sqlite3.connect(f"file:{target_db.expanduser().resolve(strict=True)}?mode=ro", uri=True) as target, sqlite3.connect(f"file:{archive_db.expanduser().resolve(strict=True)}?mode=ro", uri=True) as archive:
        target.row_factory = sqlite3.Row
        archive.row_factory = sqlite3.Row
        target.execute("PRAGMA query_only=ON")
        archive.execute("PRAGMA query_only=ON")
        zones = {
            str(row["station_id"]): str(row["settlement_timezone"])
            for row in target.execute(
                "SELECT station_id,settlement_timezone FROM basket_target_market_registry "
                "WHERE source_eligible=1 GROUP BY station_id HAVING MAX(refreshed_at)"
            )
        }
        roots = target.execute(
            "SELECT stage,checkpoint_id,station_id,event_date,settlement_timezone,decision_at_utc,"
            "posterior_sha256,lineage_json,content_sha256 FROM basket_probability_envelope "
            "WHERE state='COMMITTED' AND stage IN ('P0','P0-M') AND event_date<? "
            "ORDER BY station_id,event_date,decision_at_utc,stage,checkpoint_id",
            (_stamp(as_of),),
        ).fetchall()
        if len(roots) > MAX_ROOTS:
            raise ContractError("p1_history_chain_root_limit_exceeded")
        rows: list[dict[str, Any]] = []
        for root in roots:
            assert_process_memory_budget()
            item = dict(root)
            station = str(item["station_id"] or "").upper()
            event_date = str(item["event_date"] or "")
            timezone_name = str(item["settlement_timezone"] or zones.get(station) or "")
            if station not in stations or not timezone_name:
                raise ContractError("p1_history_chain_root_scope_invalid")
            decision = _utc(item["decision_at_utc"], field="root_decision_at")
            event_closed = _utc(
                settlement_day_window(event_date, timezone_name)["target_day_end_utc"],
                field="event_day_end",
            ) <= as_of
            curve = _curve_state(item)
            raw = _raw_receipt_inventory(archive, station_id=station, event_date=event_date, timezone_name=timezone_name, decision_at=decision)
            truth = _truth_state(target, station_id=station, event_date=event_date, decision_at=decision, as_of=as_of, timezone_name=timezone_name)
            blockers = []
            diagnostic_blockers = []
            if curve != "ROOT_FUTURE_CURVE_BOUND":
                diagnostic_blockers.append("original_p0_future_curve_binding_missing_or_invalid")
            if raw["state"] != "RAW_RECEIPT_ALL_EXACT_AWC_BOUND":
                diagnostic_blockers.append("official_awc_raw_receipt_chain_incomplete")
            if truth != "CLOSED_AWC_TRUTH_STRICT_OOS":
                diagnostic_blockers.append("closed_awc_truth_not_strict_oos")
            if event_closed:
                blockers = list(diagnostic_blockers)
                state = "BLOCKED_NO_APPENDABLE_ROOT" if blockers else "STRICT_ASOF_CHAIN_READY_FOR_CANDIDATE_ONLY_APPEND"
            else:
                # A forward/global-day row can be valuable for arrival and
                # interface observation, but must never inflate an OOS gate
                # or become a root repair target before its day closes.
                state = "OBSERVATIONAL_CANDIDATE_NOT_OOS"
            rows.append({
                "station_id": station,
                "event_date": event_date,
                "stage": str(item["stage"]),
                "checkpoint_id": str(item["checkpoint_id"]),
                "root_content_sha256": str(item["content_sha256"]),
                "decision_at_utc": _stamp(decision),
                "future_curve_binding": curve,
                "retained_awc_raw_receipts": raw,
                "truth": {"state": truth},
                "event_closed_at_as_of": event_closed,
                "evidence_role": "historical_strict_oos" if event_closed else "observational_candidate_not_oos",
                "strict_oos_gate_applies": event_closed,
                "state": state,
                "blockers": blockers,
                "diagnostic_blockers": diagnostic_blockers,
                "old_root_rebind_authorized": False,
            })
    state_counts = Counter(str(row["state"]) for row in rows)
    curve_counts = Counter(str(row["future_curve_binding"]) for row in rows)
    raw_counts = Counter(str(row["retained_awc_raw_receipts"]["state"]) for row in rows)
    truth_counts = Counter(str(row["truth"]["state"]) for row in rows)
    appendable = [row for row in rows if row["state"] == "STRICT_ASOF_CHAIN_READY_FOR_CANDIDATE_ONLY_APPEND"]
    identity = {
        "schema": SCHEMA,
        "generated_at_utc": _stamp(datetime.now(timezone.utc)),
        "as_of_utc": _stamp(as_of),
        "station_registry": canonical,
        "root_count": len(rows),
        "roots": rows,
        "summary": {
            "state_counts": dict(sorted(state_counts.items())),
            "future_curve_binding_counts": dict(sorted(curve_counts.items())),
            "retained_awc_raw_receipt_counts": dict(sorted(raw_counts.items())),
            "truth_counts": dict(sorted(truth_counts.items())),
            "historical_closed_root_count": sum(row["event_closed_at_as_of"] for row in rows),
            "observational_root_count": sum(not row["event_closed_at_as_of"] for row in rows),
            "appendable_root_count": len(appendable),
            "old_root_rebind_authorized": False,
        },
        "appendable_roots": appendable,
        "repair_policy": {
            "future_curve": "only a same-capture original binding may support P1; late archive bytes cannot be rebound",
            "raw_receipts": "AWC HTTP-200 raw/issue/receipt proof is necessary but never substitutes for a P0 future curve",
            "oos": "only closed AWC truth committed after the decision and by this as-of is strict OOS; future days are excluded",
        },
        "boundary": {
            "database_mode": "read_only_query_only",
            "writes_target_database": False,
            "writes_archive_database": False,
            "writes_p0": False,
            "writes_p0m": False,
            "writes_p1": False,
            "calls_network": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--archive-db", type=Path, default=SHARED_LIVE_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ledger = build(target_db=args.target_db, archive_db=args.archive_db, as_of_utc=args.as_of_utc)
    wrote = write_bounded_json_if_changed(args.output.expanduser().resolve(), ledger, limit=MAX_JSON_OUTPUT_BYTES)
    print(json.dumps({"schema": SCHEMA, "summary": ledger["summary"], "content_sha256": ledger["content_sha256"], "wrote": wrote}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
