#!/usr/bin/env python3
"""Build a read-only P0/P0-M/P1 station-policy family scorecard."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.station_policy_exact_stage_scorecard import (  # noqa: E402
    build_exact_stage_scorecard,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_LANES = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_BUNDLES = {
    "P0": ROOT / "output/audits/station_policy_candidate_bundle_45station_p0_20260801.json",
    "P0-M": ROOT / "output/audits/station_policy_candidate_bundle_45station_p0m_20260801.json",
    "P1": ROOT / "output/audits/station_policy_candidate_bundle_45station_p1_20260801.json",
}
DEFAULT_OUTPUT = ROOT / "output/audits/station_policy_exact_stage_scorecard_45station_utility_contract_v2_20260802.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--lanes", type=Path, default=DEFAULT_LANES)
    parser.add_argument("--p0-bundle", type=Path, default=DEFAULT_BUNDLES["P0"])
    parser.add_argument("--p0m-bundle", type=Path, default=DEFAULT_BUNDLES["P0-M"])
    parser.add_argument("--p1-bundle", type=Path, default=DEFAULT_BUNDLES["P1"])
    parser.add_argument("--as-of", required=True, dest="as_of_utc")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    lanes = json.loads(args.lanes.read_text(encoding="utf-8"))
    stations = sorted((lanes.get("stations") or {}).keys())
    db = args.db.resolve()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        receipt = build_exact_stage_scorecard(
            conn,
            as_of_utc=args.as_of_utc,
            station_ids=stations,
            bundle_paths={"P0": args.p0_bundle, "P0-M": args.p0m_bundle, "P1": args.p1_bundle},
        )
    finally:
        conn.close()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": receipt["schema"],
        "station_count": receipt["station_count"],
        "facts_read": receipt["input_counts"]["facts_read"],
        "frozen_labels": receipt["input_counts"]["latest_frozen_label_count"],
        "closed_awc_labels": receipt["input_counts"].get(
            "official_awc_frozen_label_count", 0
        ),
        "observational_candidate_not_oos": receipt["input_counts"].get(
            "observational_candidate_not_oos_count", 0
        ),
        "historical_utility_binding_state": (
            receipt.get("historical_utility_binding_inventory") or {}
        ).get("state"),
        "score_rows_emitted": receipt["checks"].get(
            "policy_utility_score_rows_emitted"
        ),
        "promotion_permitted": receipt["checks"]["promotion_permitted"],
        "orders_submitted": receipt["orders_submitted"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
