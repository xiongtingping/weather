#!/usr/bin/env python3
"""Stage a built PR12 AWC observation contract into the target SSOT.

``canonical_ingress.stage_awc_observation_contract`` is the only public PR12
write seam -- the legacy projection ingress is retired and fail-closed -- but
its sole caller has been the atomic ledger's own commit path.  That made the
facts a by-product of trading, and P1 cannot trade without them: its atomic
root is the observation contract and its lineage must carry the revision
hash.  A stage that only runs inside the commit it gates is a deadlock, and
the ledger has not reached a P1 commit since 2026-08-01.

This gives the build its own apply step.  It stages exactly what the builder
produced, inside one transaction it owns, and writes nothing else.
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

from bayes_basket_target.canonical_ingress import (  # noqa: E402
    stage_awc_observation_contract,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402


SCHEMA = "pr12_awc_observation_contract_stage_receipt_v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--target-database", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    contract = json.loads(
        args.contract.expanduser().resolve(strict=True).read_text(encoding="utf-8")
    )
    ingested_at = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    target = args.target_database.expanduser().resolve(strict=True)
    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "applied": bool(args.apply),
        "contract_content_sha256": str(contract.get("content_sha256") or ""),
        "revision_fact_count": len(contract.get("revision_facts") or []),
        "day_bounds_fact_count": len(contract.get("day_bounds_facts") or []),
        "ingested_at_utc": ingested_at,
        "boundary": {
            "single_public_pr12_write_seam": True,
            "writes_legacy_observation_projection": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    if not args.apply:
        receipt["state"] = "DRY_RUN"
    else:
        conn = sqlite3.connect(str(target), timeout=120.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            staged = stage_awc_observation_contract(
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
    return 0 if receipt.get("state") in {"STAGED", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
