#!/usr/bin/env python3
"""Refit the shadow band table (station x bucket-position) per stage.

Thin CLI over ``bayes_basket_target.band_shadow_store``, which holds the
geometry, the re-set admission rule and the pooled-fallback hierarchy.

This layer is SHADOW: no artifact can pin it, no serving path imports it,
and ``--commit`` writes cells and a journal row without activating
anything.  What accumulates is the answer to one question -- does a
station-by-bucket-position factor ever beat the pooled one on evidence
its own gates accept -- recorded day over day so the answer is auditable
before any of it reaches a served distribution.
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

from bayes_basket_target.band_shadow_store import (  # noqa: E402
    FDR_Q,
    MIN_CELL_CLAIMED_MASS,
    refit_shadow_all_stages,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="write cells and journal (still activates nothing)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="also print one plain-language SUMMARY line per stage",
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
    stages = refit_shadow_all_stages(conn, commit=args.commit)
    failed = sorted(
        stage for stage, entry in stages.items() if entry.get("error")
    )
    print(json.dumps({
        "schema": "band_shadow_refit_report_v1",
        "stages": stages,
        "failed_stages": failed,
        "committed": bool(args.commit),
        "gate_a_min_claimed_mass": MIN_CELL_CLAIMED_MASS,
        "gate_b_fdr_q": FDR_Q,
    }, ensure_ascii=False, sort_keys=True))
    if args.summary:
        for stage, entry in sorted(stages.items()):
            if entry.get("error"):
                print(f"SUMMARY {stage}: 本次未拟合 [FAILED] {entry['error']}")
                continue
            cells = entry["cell_count"]
            inherited = entry["promoted_cell_count"]
            gain = entry["forward_nll_gain"]
            days = entry["serving_day_count"]
            keys = entry["promoted_keys_by_level"]
            if not cells:
                detail = "无已结算样本，影子表为空"
            elif not inherited:
                detail = (
                    f"{days} 天证据，{cells} 个细格无一够格，全部回退池权重"
                )
            else:
                # Count what was *fitted* per level, not what inherited it:
                # the two differ by two orders of magnitude and only the
                # first answers "does the station dimension earn anything".
                earned = "，".join(
                    f"{level} 层 {keys.get(level, 0)} 格"
                    for level in ("station_position", "station", "position")
                )
                detail = (
                    f"{days} 天证据，自身够格的：{earned}"
                    f"；{inherited}/{cells} 个细格因此拿到非池因子"
                    f"（多数是继承粗层，不是本站自己测出来的）"
                    f"；前向窗 {gain:+.2f} NLL"
                )
            state = (entry.get("commit") or {}).get("state", "REPORT_ONLY")
            print(f"SUMMARY {stage}: {detail} [SHADOW/{state}]")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
