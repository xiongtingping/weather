#!/usr/bin/env python3
"""Build a minimal no-live candidate policy packet from current target scopes."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.no_live_candidate_artifacts import (
    build_no_live_candidate_artifact_packet,
)
from bayes_basket_target.market_registry import load_market_registry_snapshot
from bayes_basket_target.resource_bounds import (
    load_bounded_json,
    write_bounded_json,
)


DEFAULT_BUNDLE = ROOT / "output/audits/station_policy_candidate_bundle_v1.json"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_no_live_candidate_artifacts_v1.json"
PROBABILITY_STAGES = frozenset({"P0", "P0-M", "P1"})


def _registry_station_scope(
    payload: dict,
    *,
    event_date: str,
    stages: set[str],
    maturity_bin: str,
) -> tuple[
    set[str],
    dict[str, set[str]],
    dict[tuple[str, str], str],
]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("candidate_policy_registry_rows_missing")
    invalid_stages = sorted(stages - PROBABILITY_STAGES)
    if invalid_stages:
        raise ValueError(
            "candidate_policy_stage_invalid:" + ",".join(invalid_stages)
        )
    if not stages:
        raise ValueError("candidate_policy_stage_scope_empty")
    maturity = str(maturity_bin or "").strip()
    if not maturity:
        raise ValueError("candidate_policy_maturity_bin_missing")
    station_ids = {
        str(row.get("station_id") or "").strip().upper()
        for row in rows
        if isinstance(row, dict)
        and str(row.get("event_date") or "").strip() == event_date
        and int(row.get("source_eligible") or 0) == 1
        and int(row.get("market_active") or 0) == 1
        and int(row.get("market_accepting_orders") or 0) == 1
        and str(row.get("station_id") or "").strip()
    }
    if not station_ids:
        raise ValueError("candidate_policy_registry_station_scope_empty")
    stages_by_station = {
        station_id: set(stages)
        for station_id in station_ids
    }
    maturity_by_scope = {
        (station_id, stage): maturity
        for station_id in station_ids
        for stage in stages
    }
    return station_ids, stages_by_station, maturity_by_scope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--candidate-bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--market-registry-snapshot", type=Path)
    parser.add_argument(
        "--stage",
        action="append",
        choices=sorted(PROBABILITY_STAGES),
        help="stage coverage when station scope comes from the registry; repeatable",
    )
    parser.add_argument("--maturity-bin", default="d1")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        bundle = load_bounded_json(args.candidate_bundle)
        with sqlite3.connect(
            f"file:{resolve_target_database(args.db)}?mode=ro",
            uri=True,
            timeout=60.0,
        ) as conn:
            if args.market_registry_snapshot is not None:
                station_ids, stages_by_station, maturity_bins_by_station_stage = (
                    _registry_station_scope(
                        load_market_registry_snapshot(
                            args.market_registry_snapshot
                        ),
                        event_date=args.event_date,
                        stages=set(args.stage or PROBABILITY_STAGES),
                        maturity_bin=args.maturity_bin,
                    )
                )
            else:
                scope_rows = conn.execute(
                    "SELECT DISTINCT station_id,stage FROM basket_probability_envelope "
                    "WHERE event_date=? AND state='COMMITTED' ORDER BY station_id,stage",
                    (args.event_date,),
                ).fetchall()
                station_ids = {
                    str(row[0]) for row in scope_rows if str(row[0] or "").strip()
                }
                stages_by_station: dict[str, set[str]] = {}
                for station_id, stage in scope_rows:
                    stages_by_station.setdefault(str(station_id), set()).add(str(stage))
                maturity_bins_by_station_stage: dict[tuple[str, str], str] = {}
                for station_id, stage, lineage_json in conn.execute(
                    "SELECT station_id,stage,lineage_json "
                    "FROM basket_probability_envelope "
                    "WHERE event_date=? AND state='COMMITTED' "
                    "ORDER BY decision_at_utc,content_sha256",
                    (args.event_date,),
                ):
                    try:
                        lineage = json.loads(str(lineage_json))
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError(
                            "candidate_policy_probability_lineage_invalid"
                        ) from exc
                    maturity_bin = str(lineage.get("lead_bucket") or "").strip()
                    if not maturity_bin:
                        raise ValueError(
                            "candidate_policy_probability_maturity_missing"
                        )
                    key = (str(station_id), str(stage))
                    previous = maturity_bins_by_station_stage.setdefault(
                        key, maturity_bin
                    )
                    if previous != maturity_bin:
                        raise ValueError(
                            "candidate_policy_probability_maturity_conflict"
                        )
            packet = build_no_live_candidate_artifact_packet(
                conn,
                candidate_bundle=bundle,
                station_ids=station_ids,
                stages_by_station=stages_by_station,
                maturity_bins_by_station_stage=(
                    maturity_bins_by_station_stage
                ),
            )
        output = args.output.expanduser().resolve()
        write_bounded_json(output, packet)
        result = {
            "schema": packet["producer_schema"],
            "state": "BUILT",
            "station_count": len(packet["station_ids"]),
            "policy_artifact_count": len(packet["policy_artifacts"]),
            "cash_phase_policy_count": len(packet["cash_phase_policies"]),
            "output": str(output),
            "policy_authority_scope": packet["policy_authority_scope"],
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_no_live_candidate_artifacts_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "BUILT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
