#!/usr/bin/env python3
"""Refit every stage's centre shift and width factor and (with --commit) activate.

Thin CLI over ``bayes_basket_target.center_width_store``, which holds the
search, the held-out forward admission rule, the content-addressed
persistence and the fit journal.  The store had all of that and no caller at
all: the scalars in production were a single hand fit from 2026-08-16 that
could never refresh itself, so a serving change could not be answered by the
layer whose whole job is to answer serving changes.

``--since`` is not tuning.  These scalars measure how wrong the *served*
distribution is, so decisions taken before a change to the serving arithmetic
describe a distribution nobody serves any more.  Left unbounded the fit pools
across such changes and re-derives whatever it had been compensating for --
measured: after the 2026-08-18 width fix an unbounded fit reproduced the old
0.825 exactly, because weeks of pre-fix decisions outvoted the post-fix ones.

The bound is on the decision timestamp, not the event date.  A P0 decision
for an event two days out is taken two days early, so an event-date bound
still admits decisions from before it -- event date 2026-08-19 carries
decisions reaching back to 2026-08-17.

Without ``--commit`` this reports only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import (  # noqa: E402
    MIN_QUALIFYING_DAYS,
    refit_all_center_width,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="activate each admitted stage (default: report only)",
    )
    parser.add_argument(
        "--since",
        help=(
            "Earliest decision timestamp to score, ISO-8601.  A bare date is "
            "read as its midnight UTC.  Use the moment a serving change "
            "landed, so the fit cannot be outvoted by decisions taken under "
            "the arithmetic it replaced."
        ),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=0,
        help=(
            "Rolling alternative to --since: score only decisions from the "
            "last N days.  0 means every decision on record."
        ),
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="also print one plain-language SUMMARY line per stage",
    )
    args = parser.parse_args()
    if args.since and args.lookback_days:
        parser.error("--since and --lookback-days are alternatives")
    min_decision = args.since
    if min_decision and len(min_decision) == 10:
        min_decision = f"{min_decision}T00:00:00Z"
    if args.lookback_days:
        min_decision = (
            date.today() - timedelta(days=int(args.lookback_days))
        ).isoformat() + "T00:00:00Z"

    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0
        )
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        stages = refit_all_center_width(
            conn, commit=args.commit, min_decision_at_utc=min_decision
        )
        if args.commit:
            conn.commit()
    finally:
        conn.close()

    activated = [
        stage
        for stage, entry in stages.items()
        if str(entry.get("outcome") or "") == "ACTIVATED"
    ]
    print(json.dumps({
        "schema": "center_width_refit_report_v1",
        "stages": stages,
        "activated_stages": activated,
        "min_decision_at_utc": min_decision,
        "committed": bool(args.commit),
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    if args.summary:
        for stage, entry in sorted(stages.items()):
            days = entry["serving_day_count"]
            if not entry["served_stage"]:
                detail = (
                    f"未接入服务，仅存证据（δ={entry['delta_c']:+.3f} "
                    f"λ={entry['width_lambda']:.3f}）"
                )
            elif entry["accumulating"]:
                detail = (
                    f"攒证据中 {days}/{MIN_QUALIFYING_DAYS} 天，维持恒等"
                )
            elif not entry["admitted"]:
                detail = (
                    f"{days} 天证据，前向增益 "
                    f"{entry['forward_nll_gain']:+.4f} 未达门槛，不换代"
                )
            else:
                detail = (
                    f"{days} 天证据，δ={entry['delta_c']:+.3f} "
                    f"λ={entry['width_lambda']:.3f}，"
                    f"前向增益 {entry['forward_nll_gain']:+.4f}"
                )
            state = entry.get("outcome", "REPORT_ONLY")
            print(f"SUMMARY {stage}: {state} — {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
