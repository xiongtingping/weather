#!/usr/bin/env python3
"""Refit the off-centre claim correction on the band axis that serves today.

The correction has been a kernel constant since 2026-08-14 and was fitted by
hand twice: once by code that was never written down, and once (2026-09-08)
on claim-axis samples, days after P0 had moved to the distance axis.  A
constant that only a person can move does not follow the layer underneath it,
and the layer underneath it refits nightly.

This is the lane.  Without ``--commit`` it reports only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.off_centre_claim_store import (  # noqa: E402
    SERVED_STAGES,
    fit_off_centre_claim_table,
    persist_and_activate,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _line(fit: dict) -> str:
    if "error" in fit:
        return f"  {fit['stage']:5s} off-centre claim: {fit['error']}"
    cells = ", ".join(
        f"{cell['band'][1]:.2f}:{cell['served']:.3f}" for cell in fit["evidence"]
    )
    return (
        f"  {fit['stage']:5s} off-centre claim on {fit['band_axis']}:"
        f" {fit['station_day_count']} station-days, {fit['serving_day_count']}"
        f" days, {fit['fitted_cell_count']}/{len(fit['evidence'])} cells"
        f" fitted  |  {cells}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--as-of", default=None,
                        help="stamp for the activation; required to commit")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit and not args.as_of:
        print("--commit needs --as-of (scripts may not read the clock)")
        return 2
    conn = (
        sqlite3.connect(str(database), timeout=180.0)
        if args.commit
        else sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=180.0)
    )
    conn.execute("PRAGMA busy_timeout=180000")
    receipts = []
    try:
        for stage in SERVED_STAGES:
            fit = fit_off_centre_claim_table(conn, stage=stage)
            if args.commit and "error" not in fit:
                fit.update(persist_and_activate(conn, fit, now=str(args.as_of)))
                fit["committed"] = True
            print(_line(fit))
            receipts.append({
                key: value for key, value in fit.items() if key != "payload"
            })
        print(json.dumps(receipts, ensure_ascii=False, indent=1))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
