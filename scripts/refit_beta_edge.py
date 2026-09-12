#!/usr/bin/env python3
"""Refit charter 128's beta per served stage and activate it (shadow lane).

Thin CLI over ``bayes_basket_target.beta_edge_store``: the fit, the
persistence, the journal.  Without ``--commit`` it reports only.  There is
no promotion guard here on purpose -- the lane is ``shadow`` until a ruling
turns ``PM_BETA_EDGE`` to ``on``, and the shadow's own record (admitted
minus refused, out of sample) is what that ruling is argued on.

    python3 scripts/refit_beta_edge.py                      # report
    python3 scripts/refit_beta_edge.py --commit --as-of ... # fit + activate
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

from bayes_basket_target.beta_edge_store import (  # noqa: E402
    STAGE_SCOPES,
    fit_beta,
    journal_fit,
    load_beta_as_of,
    persist_and_activate,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _line(fit: dict) -> str:
    ev = fit["evidence"]
    if fit["accumulating"]:
        return (
            f"  {fit['stage_scope']:5s} beta: ACCUMULATING"
            f" ({fit['row_count']} rows / {fit['day_count']} days;"
            f" needs {ev['min_rows']} / {ev['min_days']}) -- serves beta_lcb 0"
        )
    return (
        f"  {fit['stage_scope']:5s} beta {ev['beta']:+.3f}"
        f" [lcb {ev['beta_lcb']:+.3f}, ucb {ev['beta_ucb']:+.3f}]"
        f" intercept {ev['intercept']:+.3f} on {fit['row_count']} rows /"
        f" {fit['day_count']} days; days beta>0 {ev['days_beta_positive']}"
        f"/{fit['day_count']}; serves beta_lcb {ev['served_beta_lcb']:+.3f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--as-of", default=None,
                        help="stamp for the activation; required to commit")
    parser.add_argument("--as-of-event-date", default=None,
                        help="fit as of this event date (default: newest settled)")
    parser.add_argument(
        "--unanchored", action="store_true",
        help="research reading only: ignore the serving-arithmetic anchor"
             " (rows from before the served distribution last changed"
             " shape); refused together with --commit",
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
    receipts = []
    try:
        for scope in STAGE_SCOPES:
            fit = fit_beta(
                conn, stage_scope=scope, as_of=args.as_of_event_date,
                anchored=not args.unanchored,
            )
            outcome = None
            if args.commit:
                before = load_beta_as_of(conn, stage_scope=scope)
                fit.update(persist_and_activate(conn, fit, now=str(args.as_of)))
                outcome = (
                    "UNCHANGED"
                    if before is not None and before["sha256"] == fit["content_sha256"]
                    else "ACTIVATED"
                )
                fit["fit_id"] = journal_fit(
                    conn, fit=fit, outcome=outcome, fitted_at_utc=str(args.as_of)
                )
                fit["outcome"] = outcome
            print(_line(fit))
            receipts.append({k: v for k, v in fit.items() if k != "table"})
        print(json.dumps(receipts, ensure_ascii=False, indent=1, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
