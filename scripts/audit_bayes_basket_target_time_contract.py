#!/usr/bin/env python3
"""Build the bounded M2-09 three-clock contract receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.settlement_time import (  # noqa: E402
    normalize_event_clock_utc,
    settlement_day_window,
    time_contract_snapshot,
)


SCHEMA = "bayes_basket_target_time_contract_audit_v1"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_time_contract.json"
MAX_RECEIPT_BYTES = 1024 * 1024


def build_time_contract_audit() -> dict[str, Any]:
    rkpk = time_contract_snapshot(
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        event_at_utc="2026-07-26T15:30:00Z",
    )
    spring = settlement_day_window("2026-03-08", "America/New_York")
    fall = settlement_day_window("2026-11-01", "America/New_York")
    half_hour = settlement_day_window("2026-07-27", "Asia/Kolkata")
    checks = {
        "registry_timezone_controls_resolution_window": (
            rkpk["target_day_start_utc"] == "2026-07-26T15:00:00Z"
            and rkpk["target_day_end_utc"] == "2026-07-27T15:00:00Z"
        ),
        "resolution_day_and_et_operational_day_are_independent": (
            rkpk["resolution_local_date_at_event"] == "2026-07-27"
            and rkpk["operational_day"] == "2026-07-26"
        ),
        "utc_event_clock_is_explicit": (
            rkpk["event_clock_utc"] == "2026-07-26T15:30:00Z"
        ),
        "spring_dst_window_is_23_hours": (
            spring["target_day_start_utc"] == "2026-03-08T05:00:00Z"
            and spring["target_day_end_utc"] == "2026-03-09T04:00:00Z"
        ),
        "fall_dst_window_is_25_hours": (
            fall["target_day_start_utc"] == "2026-11-01T04:00:00Z"
            and fall["target_day_end_utc"] == "2026-11-02T05:00:00Z"
            and normalize_event_clock_utc("2026-11-01T01:30:00-04:00")
            == "2026-11-01T05:30:00Z"
            and normalize_event_clock_utc("2026-11-01T01:30:00-05:00")
            == "2026-11-01T06:30:00Z"
        ),
        "half_hour_timezone_boundary_is_exact": (
            half_hour["target_day_start_utc"] == "2026-07-26T18:30:00Z"
            and half_hour["target_day_end_utc"] == "2026-07-27T18:30:00Z"
        ),
    }
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "PASS" if all(checks.values()) else "BLOCKED",
        "checks": checks,
        "samples": {
            "rkpk_cross_day": rkpk,
            "new_york_spring_dst": spring,
            "new_york_fall_dst": fall,
            "kolkata_half_hour": half_hour,
        },
        "boundary": {
            "market_timezone_authority": "market_registry.settlement_timezone",
            "operational_timezone": "America/New_York",
            "event_ordering_clock": "UTC",
            "et_may_override_market_timezone": False,
            "read_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {**identity, "receipt_sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    receipt = build_time_contract_audit()
    write_bounded_json(args.output, receipt, limit=MAX_RECEIPT_BYTES)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "output": str(args.output),
                "receipt_sha256": receipt["receipt_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
