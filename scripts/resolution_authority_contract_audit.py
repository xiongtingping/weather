#!/usr/bin/env python3
"""Read-only audit for the accepted ZUUU X/S source-authority contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_authority_contract_runtime import load_resolution_authority_contract


DEFAULT_CONTRACT = ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    args = parser.parse_args()
    _, report = load_resolution_authority_contract(args.contract)
    report["contract_path"] = str(Path(args.contract))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
