#!/usr/bin/env python3
"""Refit charter 132's valuation pool per served stage and activate it.

Thin CLI over ``bayes_basket_target.valuation_store``: the population (the
committed envelope, the market reference built from the official WS book the
decision priced against, the ledger's knowable settlement), the fit of beta in
[0, 1] on the truth bucket's log score, the by-event-day draws, the
persistence and the journal.  Without ``--commit`` it reports only.

Activation is not promotion.  A row serves the shadow reading only: charter
230 retired the lane's ``on`` gate (the L4 posterior is the basket's one
belief) together with ``scripts/valuation_four_arm.py``, the verdict writer
that gate read.

    python3 scripts/refit_valuation.py                        # report
    python3 scripts/refit_valuation.py --commit --as-of ...   # fit + activate

One stage per invocation keeps a slot under the 300-second budget; the sweep
runs the three in sequence.
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

from bayes_basket_target.valuation_store import (  # noqa: E402
    STAGE_SCOPES,
    fit_valuation,
    journal_fit,
    load_valuation_as_of,
    persist_and_activate,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _line(fit: dict) -> str:
    ev = fit["evidence"]
    sc = ev.get("scores") or {}
    head = (
        f"  {fit['stage_scope']:5s} {fit['station_day_count']} station-days /"
        f" {fit['day_count']} days"
    )
    if fit["accumulating"]:
        return (
            head + f" -- ACCUMULATING (needs {ev['min_station_days']} /"
            f" {ev['min_days']}); serves nothing"
        )
    geo = ev.get("geometric_pool") or {}
    return (
        head
        + f" -- beta {ev['beta']:.2f} [{ev['beta_lcb']:.2f}, {ev['beta_ucb']:.2f}]"
        + f"; log score market {sc.get('log_market'):.4f}"
        + f" weather {sc.get('log_weather'):.4f} pool {sc.get('log_pool'):.4f}"
        + (
            f"; geometric g {geo.get('g'):.2f}"
            f" (+{(geo.get('log_score') or 0) - (geo.get('log_score_market') or 0):.4f} nat)"
            if geo else ""
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--as-of", default=None,
        help="activation stamp and label cutoff; required to commit",
    )
    parser.add_argument(
        "--stage", choices=sorted(STAGE_SCOPES) + ["all"], default="all",
    )
    parser.add_argument(
        "--unanchored", action="store_true",
        help="research reading only: include decisions from before the stage's"
             " serving-arithmetic anchor; refused together with --commit",
    )
    args = parser.parse_args()
    if args.commit and args.unanchored:
        print("--unanchored is a research reading and may not be committed")
        return 2
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
    scopes = sorted(STAGE_SCOPES) if args.stage == "all" else [args.stage]
    receipts = []
    try:
        for scope in scopes:
            fit = fit_valuation(
                conn, stage_scope=scope, cutoff_utc=args.as_of,
                anchored=not args.unanchored,
            )
            if args.commit:
                before = load_valuation_as_of(conn, stage_scope=scope)
                fit.update(persist_and_activate(conn, fit, now=str(args.as_of)))
                outcome = (
                    "UNCHANGED"
                    if before is not None and before["sha256"] == fit["content_sha256"]
                    else "ACTIVATED"
                )
                fit["fit_id"] = journal_fit(
                    conn, fit=fit, outcome=outcome, fitted_at_utc=str(args.as_of),
                )
                fit["outcome"] = outcome
            print(_line(fit))
            receipt = {k: v for k, v in fit.items() if k != "table"}
            receipt["evidence"] = {
                k: v for k, v in fit["evidence"].items() if k != "beta_draws"
            }
            receipts.append(receipt)
        print(json.dumps(receipts, ensure_ascii=False, indent=1, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
