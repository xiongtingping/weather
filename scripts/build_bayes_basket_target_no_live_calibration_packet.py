#!/usr/bin/env python3
"""Compile reviewed calibration evidence into a no-live-only artifact packet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.no_live_calibration_authority import (  # noqa: E402
    build_no_live_calibration_artifact_packet,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--oos-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--calibration-generation", type=int, required=True)
    parser.add_argument("--ensemble-generation", type=int, required=True)
    args = parser.parse_args()
    evidence = load_bounded_json(args.input.expanduser().resolve())
    oos_audit = load_bounded_json(args.oos_audit.expanduser().resolve())
    packet = build_no_live_calibration_artifact_packet(
        evidence,
        oos_audit=oos_audit,
        calibration_generation=args.calibration_generation,
        ensemble_generation=args.ensemble_generation,
    )
    write_bounded_json(args.output.expanduser().resolve(), packet)
    authority = packet["authority_receipt"]
    print(json.dumps({
        "schema": packet["schema"],
        "output": str(args.output.expanduser().resolve()),
        "authority_scope": authority["authority_scope"],
        "artifact_count": authority["artifact_count"],
        "ensemble_count": authority["ensemble_count"],
        "activation_count": authority["activation_count"],
        "formal_promotion_created": False,
        "live_order_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
