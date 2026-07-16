#!/usr/bin/env python3
"""Read-only X/S bridge candidate fit; never promotes or writes production."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_authority_contract_runtime import load_resolution_authority_contract  # noqa: E402
from resolution_bridge_calibration_runtime import fit_resolution_bridge_candidate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data/shadow/resolution_final_labels.db",
    )
    parser.add_argument(
        "--authority-contract",
        type=Path,
        default=ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json",
    )
    args = parser.parse_args()
    authority, audit = load_resolution_authority_contract(args.authority_contract)
    if audit.get("ok") is not True:
        result = {
            "schema": "resolution_bridge_calibration_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": list(audit.get("blockers") or ["resolution_authority_contract_invalid"]),
            "read_only": True,
            "places_orders": False,
        }
    else:
        with sqlite3.connect(f"file:{args.db.expanduser().resolve()}?mode=ro", uri=True) as conn:
            pairs = []
            for (raw,) in conn.execute(
                "SELECT pair_json FROM resolution_observed_x_s_pairs ORDER BY event_date,target_contract_sha256"
            ):
                try:
                    value = json.loads(str(raw or ""))
                except (TypeError, ValueError):
                    value = {}
                if isinstance(value, dict):
                    pairs.append(value)
        result = fit_resolution_bridge_candidate(
            pairs,
            authority_contract_sha256=str(audit.get("contract_sha256") or ""),
        )
        result["authority_contract_id"] = authority.get("contract_id")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
