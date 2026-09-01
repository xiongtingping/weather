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
    WINDOW_DAYS,
    fit_lambda_table,
    persist_and_activate,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


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
        fit = fit_lambda_table(conn, window_days=args.window_days)
        receipt = {
            "schema": "effective_edge_refit_v1",
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
        print(json.dumps(receipt, ensure_ascii=False, indent=1))
        # A blind band is the one condition a human has to see: the number
        # governing it is frozen and nothing on the book can refresh it.
        return 3 if fit["blind_bands"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
