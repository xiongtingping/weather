#!/usr/bin/env python3
"""Build and stage substitute observation identity facts into the target SSOT.

The substitute lanes' analogue of the PR12 apply step: a scheduled ASOS slot
fill is formal P1 evidence the moment the materializer writes it, but until
its identity fact is staged the atomic chain fails closed on
``target_atomic_revision_fact_not_staged`` and the stage cannot act.  This
script reads the recent substitute rows from the canonical archive, builds
the contract, and stages it through the single public write seam.  On a
healthy AWC day the window is empty and the run is a no-op receipt.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.asos_substitute_observation_contract import (  # noqa: E402
    build_substitute_observation_contract,
)
from bayes_basket_target.canonical_ingress import (  # noqa: E402
    stage_substitute_observation_contract,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "asos_substitute_observation_stage_receipt_v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-database", type=Path, default=ROOT / "data" / "archive.db"
    )
    parser.add_argument("--target-database", type=Path, required=True)
    parser.add_argument("--window-days", type=int, default=3)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ingested_at = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "applied": bool(args.apply),
        "ingested_at_utc": ingested_at,
        "boundary": {
            "single_public_substitute_write_seam": True,
            "writes_truth_lane": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    archive = sqlite3.connect(
        f"file:{args.archive_database.expanduser().resolve(strict=True)}?mode=ro",
        uri=True,
    )
    try:
        contract = build_substitute_observation_contract(
            archive,
            as_of_utc=ingested_at,
            window_days=int(args.window_days),
        )
    finally:
        archive.close()
    if contract is None:
        receipt["state"] = "NO_SUBSTITUTE_ROWS"
        receipt["revision_fact_count"] = 0
    else:
        receipt["contract_content_sha256"] = str(contract["content_sha256"])
        receipt["revision_fact_count"] = len(contract["revision_facts"])
        if not args.apply:
            receipt["state"] = "DRY_RUN"
        else:
            target = args.target_database.expanduser().resolve(strict=True)
            conn = sqlite3.connect(str(target), timeout=120.0)
            try:
                conn.execute("BEGIN IMMEDIATE")
                staged = stage_substitute_observation_contract(
                    conn, contract, ingested_at_utc=ingested_at
                )
                conn.commit()
                receipt["state"] = "STAGED"
                receipt["stage_receipt"] = staged
            except (ContractError, sqlite3.Error) as exc:
                conn.rollback()
                receipt["state"] = "REFUSED"
                receipt["reason"] = f"{type(exc).__name__}:{str(exc)[:200]}"
            finally:
                conn.close()
    write_bounded_json(args.receipt.expanduser().resolve(), receipt)
    print(json.dumps(
        {k: v for k, v in receipt.items() if k != "stage_receipt"},
        ensure_ascii=False,
    ))
    return 0 if receipt.get("state") in {
        "STAGED", "DRY_RUN", "NO_SUBSTITUTE_ROWS"
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
