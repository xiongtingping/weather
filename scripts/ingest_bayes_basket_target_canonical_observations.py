#!/usr/bin/env python3
"""Ingest canonical observation identities into the isolated target SSOT."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_fact_ingress import ingest_canonical_observation_evidence  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402


DEFAULT_EVIDENCE = ROOT / "output" / "audits" / "bayes_basket_target_canonical_observation_evidence.json"
DEFAULT_RECEIPT = ROOT / "output" / "audits" / "bayes_basket_target_canonical_fact_ingress.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        evidence = load_bounded_json(args.evidence.expanduser().resolve())
        db = resolve_target_database(args.db)
        with sqlite3.connect(db, timeout=60.0) as conn:
            result = ingest_canonical_observation_evidence(conn, evidence, commit=bool(args.apply))
    except Exception as exc:
        result = {
            "schema": "bayes_basket_target_canonical_fact_ingress_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "writes_shared_live_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
        }
    receipt = args.receipt.expanduser().resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    write_bounded_json(receipt, result)
    result["receipt"] = str(receipt)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("state") in {
        "VALIDATED_NO_WRITE",
        "MATERIALIZED_UNPUBLISHED",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
