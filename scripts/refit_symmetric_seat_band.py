"""Refit the width band charter 79's symmetric seat is confined to.

Rides the daily calibration sweep beside the other served constants.  Reads
the pairs the runtime records on every symmetric day -- the centre+-1 window
the rule would force against the largest-edge window the enumeration would
choose, both priced on the same book at the same instant -- joins them to the
settled bucket, and picks the lower bound that would have been worth most per
seat, judged on a forward window it was not fitted on.

The incumbent is whatever is serving now: the fitted band if this lane has
ever cleared its gate, and charter 122's ruled default otherwise.  A candidate
must beat that incumbent forward by a per-seat margin before it replaces it,
so the first run after this lands cannot move anything -- there is nothing to
beat it with until the pairs accumulate.

    python3 scripts/refit_symmetric_seat_band.py --summary          # report
    python3 scripts/refit_symmetric_seat_band.py --commit --summary # serve it
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import symmetric_seat_band_store as store  # noqa: E402
from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    SYMMETRIC_SEAT_WIDTH_BAND,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _incumbents(conn: sqlite3.Connection) -> dict[str, float]:
    """What each stage is serving now, fitted first and the ruling behind it."""

    served = store.load_symmetric_seat_bands(conn)
    out: dict[str, float] = {}
    for stage in store.FIT_STAGES:
        found = served.get(stage)
        out[stage] = (
            float(found[0]) if found is not None
            else float(SYMMETRIC_SEAT_WIDTH_BAND[0])
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip().split("\n\n")[0]
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true",
                        help="activate an admitted band (default: report only)")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--since", default=None,
                        help="earliest event date to score, ISO-8601 date")
    args = parser.parse_args()

    conn = sqlite3.connect(str(args.db), timeout=180.0)
    failed = False
    try:
        store.ensure_schema(conn)
        stages = store.refit_all(
            conn,
            incumbent_lower_by_stage=_incumbents(conn),
            commit=args.commit,
            min_event_date=args.since,
        )
        if args.commit:
            conn.commit()
    finally:
        conn.close()

    print(json.dumps({
        "schema": "symmetric_seat_band_refit_report_v1",
        "stages": stages,
        "committed": bool(args.commit),
        "grid": list(store.LOWER_BOUND_GRID),
    }, ensure_ascii=False, sort_keys=True))

    if args.summary:
        for stage, entry in sorted(stages.items()):
            if entry.get("error"):
                failed = True
                print(f"SUMMARY {stage}: 本次未拟合 [FAILED] {entry['error']}")
                continue
            days = entry["day_count"]
            pairs = entry["pair_count"]
            if entry["accumulating"]:
                detail = (
                    f"攒证据中 {days}/{store.MIN_QUALIFYING_DAYS} 天、"
                    f"{pairs}/{store.MIN_QUALIFYING_PAIRS} 对，"
                    f"维持在任档 ≥{entry['incumbent_lower']:.2f} 格"
                )
            elif entry["admitted"]:
                detail = (
                    f"{days} 天 {pairs} 对，档 "
                    f"≥{entry['incumbent_lower']:.2f} → "
                    f"≥{entry['lower_bound']:.2f} 格，"
                    f"前向每席 {entry['forward_gain_per_seat']:+.4f}"
                )
            else:
                detail = (
                    f"{days} 天 {pairs} 对，最佳候选未过前向闸"
                    f"（每席 {entry['forward_gain_per_seat']:+.4f}，"
                    f"门槛 {store.MIN_FORWARD_GAIN_PER_SEAT:+.4f}），"
                    f"维持 ≥{entry['incumbent_lower']:.2f} 格"
                )
            print(f"SUMMARY {stage}: {entry['outcome']} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
