#!/usr/bin/env python3
"""Mint charter 134 pool seats for stations the resident rotation has not reached.

The resident rebuilds one station's OOS per production cycle, so after the
2026-09-14 switch the fleet would stay mixed for about ten hours.  This does
the same work sooner, with the same function calls the resident makes
(``build_native_ensemble_oos`` at the forward-accumulation window, then
``persist_native_ensemble_oos`` with ``activated_at_utc`` equal to the build's
as-of), and without starving it:

* each build runs on a read-only connection, outside any lock;
* each station persists in its own short transaction, taken the way the
  calibration sweep takes the lock (``_begin_immediate``: busy_timeout plus a
  bounded retry), and the lock is released before the next build starts;
* no whole-database integrity check and no multi-station transaction -- the
  two things that made ``build_native_ensemble_forward_oos.py --commit`` unfit
  to run beside the resident.

Stations that already carry a pool seat are skipped, so a run cut at its time
budget resumes where it stopped.  Receipts go to stdout, one line per station.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.band_reliability_store import _begin_immediate  # noqa: E402
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
    FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from bayes_basket_target.stable_pool import (  # noqa: E402
    P0_MEMBERSHIP_MODE,
    P0_MEMBERSHIP_MODE_STABLE_POOL,
    STABLE_POOL_WEIGHT_RULE,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-seconds", type=float, default=520.0)
    parser.add_argument("--pause-seconds", type=float, default=1.0)
    parser.add_argument("--station", action="append", default=[])
    args = parser.parse_args()
    if P0_MEMBERSHIP_MODE != P0_MEMBERSHIP_MODE_STABLE_POOL:
        raise SystemExit("P0_MEMBERSHIP_MODE is not stable_pool; nothing to mint")
    reader = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    reader.execute("PRAGMA query_only=1")
    writer = sqlite3.connect(str(DB), timeout=60.0, isolation_level=None)
    writer.execute("PRAGMA busy_timeout=60000")
    stations = args.station or sorted({
        str(row[0]) for row in reader.execute(
            "SELECT DISTINCT station_id FROM basket_probability_envelope"
            " WHERE stage='P0' AND state='COMMITTED'"
            " AND decision_at_utc>=datetime('now','-2 days')"
        )
    })
    minted = {
        str(row[0]) for row in reader.execute(
            "SELECT DISTINCT station_id FROM calibration_cross_native_ensemble_artifact"
            " WHERE combination_method LIKE ?",
            (STABLE_POOL_WEIGHT_RULE + "%",),
        )
    }
    todo = [station for station in stations if station not in minted]
    print(json.dumps({"stations": len(stations), "already_minted": len(minted & set(stations)), "todo": len(todo)}), flush=True)
    started = time.monotonic()
    for station in todo:
        if time.monotonic() - started > args.budget_seconds:
            break
        as_of = _now()
        t0 = time.monotonic()
        report = build_native_ensemble_oos(
            reader, station_id=station, as_of_utc=as_of,
            min_train_days=FORWARD_ACCUMULATION_MIN_TRAIN_DAYS,
            min_holdout_days=FORWARD_ACCUMULATION_MIN_HOLDOUT_DAYS,
        )
        built = time.monotonic() - t0
        attempts = _begin_immediate(writer)
        t1 = time.monotonic()
        try:
            receipt = persist_native_ensemble_oos(writer, report, activated_at_utc=as_of)
            writer.execute("COMMIT")
        except Exception:
            writer.execute("ROLLBACK")
            raise
        held = time.monotonic() - t1
        print(json.dumps({
            "station": station, "as_of_utc": as_of, "state": receipt.get("state"),
            "candidates": len(report["cross_candidates"]),
            "passed": sum(c.get("state") == "PRODUCTION_NO_LIVE_OOS_PASS" for c in report["cross_candidates"]),
            "counts": receipt.get("counts"), "build_seconds": round(built, 1),
            "lock_attempts": attempts, "lock_held_seconds": round(held, 2),
        }), flush=True)
        time.sleep(args.pause_seconds)
    remaining = [
        station for station in todo
        if not reader.execute(
            "SELECT 1 FROM calibration_cross_native_ensemble_artifact"
            " WHERE station_id=? AND combination_method LIKE ? LIMIT 1",
            (station, STABLE_POOL_WEIGHT_RULE + "%"),
        ).fetchone()
    ]
    print(json.dumps({"remaining": len(remaining), "remaining_stations": remaining}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
