#!/usr/bin/env python3
"""Compile and append pre-event P0 basket selections from official WS."""

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
from bayes_basket_target.p0_preselection import (
    compile_p0_preselection,
    latest_visible_p0_station_ids,
    load_market_registry_snapshot,
    load_official_ws_quotes,
)
from bayes_basket_target.resource_bounds import load_bounded_json
from bayes_basket_target.runtime_config import (
    load_target_no_live_runtime_config,
)


DEFAULT_ROOT = ROOT / "output/audits/bayes_basket_target_18z_fallback_20260726"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_ROOT / "market_registry_forward_p0_20260727.json",
    )
    parser.add_argument(
        "--ws-manifest",
        type=Path,
        default=DEFAULT_ROOT / "forward_p0_ws_manifest_20260727_0832.json",
    )
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--evaluation-at-utc", required=True)
    parser.add_argument("--season-bin", default="summer")
    parser.add_argument("--maturity-bin", default="d1")
    parser.add_argument("--liquidity-bin", default="liquid")
    parser.add_argument("--strategy-version", default="target-v1")
    parser.add_argument("--planning-budget-usdc", type=float)
    args = parser.parse_args()
    try:
        registry = load_market_registry_snapshot(args.registry)
        manifest = load_bounded_json(args.ws_manifest)
        if not isinstance(manifest, dict):
            raise RuntimeError("p0_preselection_ws_manifest_mapping_required")
        _root, quotes = load_official_ws_quotes(args.ws_manifest)
        planning_budget_usdc = (
            float(args.planning_budget_usdc)
            if args.planning_budget_usdc is not None
            else float(
                load_target_no_live_runtime_config()[
                    "target_remaining_budget_usdc"
                ]
            )
        )
        db = resolve_target_database(args.db)
        with sqlite3.connect(db, timeout=60.0) as conn:
            conn.row_factory = sqlite3.Row
            stations = latest_visible_p0_station_ids(
                conn,
                event_date=args.event_date,
                evaluation_at_utc=args.evaluation_at_utc,
            )
            compiled = [
                compile_p0_preselection(
                    conn,
                    station_id=station_id,
                    event_date=args.event_date,
                    evaluation_at_utc=args.evaluation_at_utc,
                    registry_snapshot=registry,
                    ws_manifest=manifest,
                    ws_quotes=quotes,
                    season_bin=args.season_bin,
                    maturity_bin=args.maturity_bin,
                    liquidity_bin=args.liquidity_bin,
                    strategy_version=args.strategy_version,
                    planning_budget_usdc=planning_budget_usdc,
                )
                for station_id in stations
            ]
        result = {
            "schema": "bayes_basket_target_p0_preselection_batch_v1",
            "state": "VALIDATED_NO_WRITE",
            "event_date": args.event_date,
            "station_count": len(compiled),
            "preselection_count": len(compiled),
            "written_count": 0,
            "write_authority": "TargetProductionWorker",
            "stages": sorted({str(row["probability_stage"]) for row in compiled}),
            "centers": {
                str(row["station_id"]): str(row["compiled_plan"]["plan"]["center_bucket"])
                for row in compiled
            },
            "directions": {
                str(row["station_id"]): str(row["compiled_plan"]["plan"]["direction"])
                for row in compiled
            },
            "center_stability": {
                str(row["station_id"]): {
                    "center_bucket": str(
                        row["compiled_plan"]["plan"]["center_bucket"]
                    ),
                    "runner_up_bucket": str(
                        row["compiled_plan"]["plan"]["runner_up_bucket"]
                    ),
                    "lead_probability": float(
                        row["compiled_plan"]["plan"]["center_lead_probability"]
                    ),
                    "threshold_probability": float(
                        row["compiled_plan"]["plan"][
                            "center_lead_threshold_probability"
                        ]
                    ),
                    "center_stable": bool(
                        row["compiled_plan"]["plan"]["center_stable"]
                    ),
                }
                for row in compiled
            },
            "model_envelopes": {
                str(row["station_id"]): {
                    "lower_bucket": str(
                        row["compiled_plan"]["probability_envelope"]["lineage"].get(
                            "model_envelope_lower_bucket", ""
                        )
                    ),
                    "upper_bucket": str(
                        row["compiled_plan"]["probability_envelope"]["lineage"].get(
                            "model_envelope_upper_bucket", ""
                        )
                    ),
                }
                for row in compiled
            },
            "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
            "final_authority": "P1",
            "capital_authorized": False,
            "intents_written": 0,
            "reservations_written": 0,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_p0_preselection_batch_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "VALIDATED_NO_WRITE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
