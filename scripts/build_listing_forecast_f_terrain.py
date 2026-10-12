#!/usr/bin/env python3
"""Build (or rebuild) the terrain table the station-value F_v3 reads (charter 267 补记六).

Reads each member's own terrain field for one run (default: the newest 00Z/12Z
of yesterday, which every publisher has) and AWC's station elevations; writes
``data/runtime/listing_forecast_f_terrain_v1.json``.  Read-only on the SSOT.
Run once after install and again whenever a member's cell rule changes.

    python3 scripts/build_listing_forecast_f_terrain.py [--run 2026-10-11T12:00Z] [--products AIFS,GFS,...]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import listing_forecast_f_terrain as terrain  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--run", default=None, help="run init, e.g. 2026-10-11T12:00Z (default: yesterday 12Z)")
    parser.add_argument("--products", default=",".join(terrain.PRODUCTS))
    parser.add_argument("--out", type=Path, default=terrain.TERRAIN_PATH)
    args = parser.parse_args()
    if args.run:
        run_init = datetime.fromisoformat(args.run.replace("Z", "+00:00")).astimezone(timezone.utc)
    else:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        run_init = yesterday.replace(hour=12, minute=0, second=0, microsecond=0)
    products = [p.strip() for p in args.products.split(",") if p.strip()]
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=60)
    try:
        conn.execute("PRAGMA query_only=1")
        payload = terrain.build(conn, run_init=run_init, products=products)
    finally:
        conn.close()
    terrain.write(payload, args.out)
    for system, rows in payload["systems"].items():
        offsets = [r["offset_c"] for r in rows.values()]
        print(f"{system}: {len(rows)} stations, offset mean {sum(offsets) / max(1, len(offsets)):+.2f} C,"
              f" range {min(offsets):+.2f}..{max(offsets):+.2f}")
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
