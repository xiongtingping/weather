#!/usr/bin/env python3
"""Refit charter 68's lambda table from the rolling settled window.

Thin CLI over ``bayes_basket_target.effective_edge_store``, which holds the
admission rule, the rolling fit, the content-addressed persistence and the
journal.  Without ``--commit`` it reports only.

Charter 68 shipped a table fitted once by hand.  This is what keeps it from
going stale -- and, more importantly, what says out loud when a band has
stopped producing observations, which is the failure the endogeneity
warning in ``SELECTION_DISCOUNT`` predicts for any rule that refuses the
seats it distrusts.
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

from bayes_basket_target.effective_edge_store import (  # noqa: E402
    DQ_SHADOW_MODE,
    WINDOW_DAYS,
    fit_lambda_table,
    fit_lambda_table_dq_shadow,
    persist_and_activate,
    persist_dq_shadow,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _serving_reading(conn, receipt: dict) -> str:
    """One line per scope: what the table production serves buys today.

    The activation verdict already says whether a challenger won.  This says
    something the verdict cannot: how the INCUMBENT -- the table actually
    serving -- is doing on today's population, and whether that has moved
    since the last fit.  Charter 68's lambda widens and narrows what the book
    may buy, so the number to watch after a table changes is the admitted
    COUNT, not its ROI: a gate that doubles its admissions and holds its
    return earned the widening; one that doubles them while the return falls
    simply bought more of the same losing windows.  P0 went from 160 to 316
    on 2026-09-09, which is what this line exists to follow.

    Read out of the journal, which has carried the guard since it existed, so
    this adds a line of output and no new state.
    """

    scope = str(receipt.get("stage_scope") or "")
    guard = receipt.get("activation_guard") or {}
    now = guard.get("incumbent") or {}
    if not guard:
        # The guard is part of the commit path, so a dry run has no verdict
        # and no reading.  Said plainly rather than shown as an empty one.
        return f"  {scope:5s} serving reading: dry run, guard not evaluated"
    if not now.get("available"):
        return f"  {scope:5s} serving reading: no scored population"
    prev = None
    try:
        row = conn.execute(
            "SELECT evidence_json FROM calibration_effective_edge_fit_journal"
            " WHERE stage_scope=? ORDER BY fit_id DESC LIMIT 1 OFFSET 1",
            (scope,),
        ).fetchone()
        if row:
            prev = ((json.loads(row[0]) or {}).get("activation_guard")
                    or {}).get("incumbent")
    except Exception:  # noqa: BLE001 -- a missing history is not an error
        prev = None
    windows = int(now.get("windows") or 0)
    admitted = int(now.get("admitted") or 0)
    share = (admitted / windows * 100.0) if windows else 0.0
    line = (f"  {scope:5s} serving reading: admitted {admitted}/{windows}"
            f" ({share:.1f}%)  roi {float(now.get('roi') or 0.0) * 100:+.2f}%")
    if prev and prev.get("available"):
        pa = int(prev.get("admitted") or 0)
        pr = float(prev.get("roi") or 0.0) * 100
        line += f"  |  last fit {pa} at {pr:+.2f}%"
        if pa and admitted >= 2 * pa:
            line += "  <-- admissions doubled, watch the return"
    if (
        receipt.get("committed")
        and not guard.get("blocked")
        and not guard.get("bands_unchanged")
    ):
        # An unchanged table activates too -- that is a refresh of the
        # evidence, not a promotion, and calling it one would train the eye
        # to skip the word on the day it means something.
        line += "  |  NEW TABLE ACTIVATED"
    return line


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=120.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=120.0
        )
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        # Two scopes, two fits, never pooled: P0/P0-M under charter 68's
        # table, P1 under its own (user ruling, 2026-09-02).  Each journals
        # and activates on its own row, so refreshing one cannot retire the
        # other.
        receipts = []
        blind = False
        # Three window scopes and one bucket scope (2026-09-09).  The bucket
        # lane is a single table across P0 and P0-M -- what the kernel's
        # per-rung dispatch reads -- and it is the ONLY lane whose numbers
        # were never fitted: until today it served four constants hand-set on
        # 2026-09-03 while these three refit nightly.  It journals and
        # activates on its own row, so it can neither retire nor be retired
        # by a window table.
        for scope, claim_scope in (
            ("P0", "window"), ("P0-M", "window"), ("P1", "window"),
            ("P0", "bucket"),
        ):
            fit = fit_lambda_table(
                conn, window_days=args.window_days, stage_scope=scope,
                claim_scope=claim_scope,
            )
            receipt = {
                "schema": "effective_edge_refit_v2",
                "stage_scope": scope,
                "claim_scope": claim_scope,
                "content_sha256": fit["content_sha256"],
                "seat_count": fit["seat_count"],
                "evidence": fit["evidence"],
                "accumulating_bands": fit["accumulating_bands"],
                "blind_bands": fit["blind_bands"],
                "committed": False,
            }
            if args.commit:
                receipt.update(persist_and_activate(conn, fit))
                receipt["committed"] = True
            # The market-move axis rides the same fit, in shadow: journaled
            # next to the served table, read by nobody who sizes a seat.
            if DQ_SHADOW_MODE != "off" and claim_scope == "window":
                shadow = fit_lambda_table_dq_shadow(
                    conn, window_days=args.window_days, stage_scope=scope
                )
                receipt["dq_shadow"] = {
                    "content_sha256": shadow["content_sha256"],
                    "seat_count": shadow["seat_count"],
                    "admitted_cells": shadow["admitted_cells"],
                    "evidence": shadow["evidence"],
                    "committed": False,
                }
                if args.commit:
                    receipt["dq_shadow"].update(persist_dq_shadow(conn, shadow))
                    receipt["dq_shadow"]["committed"] = True
            receipts.append(receipt)
            blind = blind or bool(fit["blind_bands"])
        # The daily line a person actually reads, ahead of the JSON.
        for receipt in receipts:
            print(_serving_reading(conn, receipt))
        print(json.dumps(receipts, ensure_ascii=False, indent=1))
        # A blind band is the one condition a human has to see: the number
        # governing it is frozen and nothing on the book can refresh it.
        return 3 if blind else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
