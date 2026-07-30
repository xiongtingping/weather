#!/usr/bin/env python3
"""Prepare one registry-scoped three-model run manifest.

The manifest is the only supported binding between ECMWF, GFS, and ICON for
the formal target ``production_no_live`` lane. It does not download data,
write SQLite, start a service, or authorize live orders.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.model_run_contract import build_model_run_contract  # noqa: E402
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.settlement_time import registry_scope_settlement_timezone  # noqa: E402


DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_model_run_contract.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-local-date", required=True)
    parser.add_argument("--run-initialization-utc", required=True)
    parser.add_argument(
        "--market-registry-snapshot",
        type=Path,
        required=True,
        help="Content-verified registry scope that owns settlement_timezone",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    registry = load_market_registry_snapshot(args.market_registry_snapshot)
    rows = [
        row
        for row in registry["rows"]
        if str(row.get("event_date") or "") == args.target_local_date
    ]
    station_ids = sorted({
        str(row.get("station_id") or "").strip().upper()
        for row in rows
        if str(row.get("station_id") or "").strip()
    })
    target_timezone = registry_scope_settlement_timezone(
        rows,
        station_ids=station_ids,
        event_dates=[args.target_local_date],
    )
    contract = build_model_run_contract(
        target_local_date=args.target_local_date,
        run_initialization_utc=args.run_initialization_utc,
        station_ids=station_ids,
        target_timezone=target_timezone,
        market_registry_snapshot_sha256=registry["snapshot_sha256"],
    )
    output = args.output.expanduser().resolve()
    write_bounded_json(output, contract)
    print(json.dumps({
        "schema": contract["schema"],
        "output": str(output),
        "target_local_date": contract["target_local_date"],
        "target_timezone": contract["target_timezone"],
        "run_initialization_utc": contract["run_initialization_utc"],
        "provider_count": len(contract["providers"]),
        "station_count": len(contract["station_ids"]),
        "live_order_authorized": contract["boundary"]["live_order_authorized"],
        "contract_sha256": contract["contract_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
