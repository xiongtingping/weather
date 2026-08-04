#!/usr/bin/env python3
"""Materialize a read-only Δv2 target-owned training-cutoff candidate."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.delta_v2_training_cutoff_candidate import (  # noqa: E402
    assert_target_owned_training_cutoff_candidate,
    build_from_connection,
)


DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output/audits/delta_v2_target_owned_training_cutoff_candidate.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--minimum-unique-events-per-phase", type=int, default=30)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    database = args.db.expanduser().resolve()
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        artifact = build_from_connection(
            conn,
            as_of_utc=args.as_of_utc,
            min_unique_events_per_phase=args.minimum_unique_events_per_phase,
        )
    finally:
        conn.close()
    assert_target_owned_training_cutoff_candidate(artifact)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": artifact["schema"],
        "state": artifact["state"],
        "settled_join_count": artifact["source"]["settled_join_count"],
        "candidate_cutoff_station_count": artifact["candidate_cutoff_station_count"],
        "candidate_ready_station_count": artifact["candidate_ready_station_count"],
        "formal_promotion_allowed": artifact["formal_promotion_allowed"],
        "orders_submitted": artifact["orders_submitted"],
        "output": str(output),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
