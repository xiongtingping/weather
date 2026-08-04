#!/usr/bin/env python3
"""Emit a bounded, read-only P1 basket atomic-chain acceptance receipt."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.p1_atomic_chain_smoke import (  # noqa: E402
    SCHEMA,
    audit_p1_atomic_chain,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _blocked_receipt(error: Exception) -> dict[str, object]:
    core: dict[str, object] = {
        "schema": SCHEMA,
        "state": "BLOCKED",
        "blockers": [str(error)],
        "boundary": {
            "read_only": True,
            "query_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "network_access": False,
        },
    }
    return {**core, "content_sha256": content_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--station-id")
    parser.add_argument("--event-date")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.db.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not database.is_file() or database.is_symlink():
        receipt = _blocked_receipt(ContractError("p1_atomic_chain_database_invalid"))
        write_bounded_json(output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 2
    try:
        with sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=30,
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            receipt = audit_p1_atomic_chain(
                conn,
                station_id=args.station_id,
                event_date=args.event_date,
            )
    except ContractError as exc:
        receipt = _blocked_receipt(exc)
    write_bounded_json(output, receipt)
    print(json.dumps({
        "state": receipt["state"],
        "station_id": receipt.get("station_id"),
        "event_date": receipt.get("event_date"),
        "replayed_fact_count": receipt.get("replayed_fact_count", 0),
        "orders_submitted": receipt["boundary"]["orders_submitted"],
        "content_sha256": receipt["content_sha256"],
    }, sort_keys=True))
    return 0 if receipt["state"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
