#!/usr/bin/env python3
"""Validate or append one bounded calibration evidence packet to target SSOT."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_ingress import ingest_calibration_base_facts
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.resource_bounds import load_bounded_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    database = resolve_target_database(args.db)
    if not database.exists():
        raise RuntimeError("target_database_missing")
    packet = load_bounded_json(args.evidence)
    if not isinstance(packet, dict):
        raise RuntimeError("calibration_evidence_mapping_required")
    with sqlite3.connect(database, timeout=60.0, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        result = ingest_calibration_base_facts(conn, packet, commit=args.commit)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
