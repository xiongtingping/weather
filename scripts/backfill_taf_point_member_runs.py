"""One-shot/manual projection of TAF TX points into the cross-OOS archive.

The daemon runs the same projection after every TAF capture ingest
(``project_taf_point_member_runs``); this CLI exists for backfill and audit.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bayes_basket_target.native_ensemble_training_facts import (  # noqa: E402
    project_taf_point_member_runs,
)

DB = Path(__file__).resolve().parent.parent / "data/bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(str(DB), timeout=60.0)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        receipt = project_taf_point_member_runs(conn)
        if args.apply:
            conn.execute("COMMIT")
            print("committed:", receipt)
        else:
            conn.execute("ROLLBACK")
            print("dry-run (rolled back):", receipt)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
