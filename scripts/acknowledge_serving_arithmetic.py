"""Say that a stage's current serving fingerprint is inert.

The watchdog (``serving_arithmetic.watchdog``) lights when a stage's code
fingerprint was stamped after the version it serves started serving, and its
docstring has always said the only person who can tell an inert refactor from
a serving change is whoever wrote the commit.  Until 2026-09-08 there was no
place for that person to say it, so the lamp had been on for every stage on
every day since the mechanism landed.  This is that place.

    python3 scripts/acknowledge_serving_arithmetic.py --stage P0 \
        --reason "fingerprint scheme moved to call closure; served shape unchanged"

A real serving change is answered by bumping the stage's posterior_version,
never by acknowledging here: an acknowledgement asserts the version still
describes what is served, and a wrong one lets the calibration lanes keep
training on a shape nobody serves -- the exact failure the watchdog exists to
name.  The reason is required and is the audit's record.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import serving_arithmetic  # noqa: E402

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--stage", required=True, choices=("P0", "P0-M", "P1"))
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--fingerprint", default=None,
        help="a specific fingerprint sha256; default: the one served now",
    )
    parser.add_argument("--show", action="store_true",
                        help="print the watchdog reading and exit")
    args = parser.parse_args()
    conn = sqlite3.connect(str(args.db), timeout=120.0)
    try:
        before = serving_arithmetic.watchdog(conn, stage=args.stage)
        if args.show:
            print(json.dumps(before, ensure_ascii=False, indent=1))
            return 0
        receipt = serving_arithmetic.acknowledge(
            conn, stage=args.stage, reason=args.reason,
            fingerprint_sha256=args.fingerprint,
        )
        conn.commit()
        after = serving_arithmetic.watchdog(conn, stage=args.stage)
    finally:
        conn.close()
    print(json.dumps({
        "receipt": receipt,
        "discrepancy_before": before.get("discrepancy"),
        "discrepancy_after": after.get("discrepancy"),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
