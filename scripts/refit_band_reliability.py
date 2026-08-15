#!/usr/bin/env python3
"""Refit the band reliability table and (with --commit) activate it.

Thin CLI over ``bayes_basket_target.band_reliability_store``, which holds
the admission rule, both evidence-lane loaders (including exact inversion
of post-band serving rows from their receipts), and the content-addressed
persistence.  Without ``--commit`` this only reports: the fitted table,
the active table, and any drift between them.  With ``--commit`` the
fitted table becomes the active one; artifacts already gated against an
older version keep serving that exact version through their pinned sha,
so a refit never rewrites served math.
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

from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_TABLE_CELLS,
    band_table_sha256,
    fit_band_reliability_table,
    load_band_table,
    persist_and_activate_band_table,
)
from bayes_basket_target.cross_consensus import (  # noqa: E402
    BAND_RELIABILITY_FACTORS,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="activate the fitted table (default: report only)",
    )
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0
        )
    conn.execute("PRAGMA busy_timeout=60000")

    fitted = fit_band_reliability_table(
        conn, bootstrap_samples=args.bootstrap_samples
    )
    active = load_band_table(conn)
    active_factors = dict(
        active[0] if active is not None else BAND_RELIABILITY_FACTORS
    )
    fitted_factors = dict(fitted["factors"])
    drift = [
        {
            "band": list(band),
            "active_factor": active_factors.get(band),
            "fitted_factor": fitted_factors[band],
        }
        for band in BAND_TABLE_CELLS
        if abs(
            fitted_factors[band] - float(active_factors.get(band, 1.0))
        ) > 0.02
    ]
    result = {
        "schema": "band_reliability_refit_report_v2",
        "fitted_factors": [
            [list(band), factor] for band, factor in fitted["factors"]
        ],
        "fitted_table_sha256": band_table_sha256(fitted["factors"]),
        "active_table_sha256": (
            None if active is None else active[1]
        ),
        "active_is_bootstrap_constants": active is None,
        "serving_day_count": fitted["serving_day_count"],
        "basis": fitted["evidence"]["basis"],
        "drift": drift,
        "table_change_suggested": bool(drift) or active is None,
    }
    if args.commit:
        result["commit"] = persist_and_activate_band_table(
            conn,
            factors=fitted["factors"],
            evidence=fitted["evidence"],
            serving_day_count=fitted["serving_day_count"],
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
