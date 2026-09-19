#!/usr/bin/env python3
"""Is the next CMA cycle out?  One request, no write -- the CMA capture's doorbell.

User, 2026-09-19 (「接 CMA 官方源」「做快车道」).  The capture writes the SSOT, so
it lives on the sweep queue at half its product's cadence, and the queue's
drain rate will not pay for more (``test_the_ssot_sweep_queue_fits_its_drain_rate``).
But a cycle can land any time in a three-hour window, and waiting out the
interval is up to three hours of P1 reading an older CMA run -- which, with the
aggregator holding a cycle ten to eleven hours after init and P1 admitting a
run for twelve, is most of the window a cycle can be read in at all.

So this pass asks the one question that is cheap: is the newest cycle the
lane still wants published yet?  It reads the SSOT ``mode=ro``, sends one
request, and writes a small JSON file.  When the answer turns yes the daemon
makes the capture due at once (``honour_cma_probe``); when it is no, nothing
else happens.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.cma_grapes_point_archive import (  # noqa: E402
    CAPTURE_STATIONS, close_client, run_published,
)
from bayes_basket_target.regional_point_lane import plan_p1  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATE = ROOT / "data" / "runtime" / "cma_named_run_probe.json"


def main() -> int:
    now = datetime.now(timezone.utc)
    state = {"checked_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "newest_wanted_run_utc": None, "published": False}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        try:
            plan = plan_p1(conn, product="CMA", now=now, stations=CAPTURE_STATIONS, max_runs=2)
        finally:
            conn.close()
        if plan:
            newest = plan[0]["run_init"]
            state["newest_wanted_run_utc"] = newest.strftime("%Y-%m-%dT%H:%M:%SZ")
            state["published"] = bool(run_published(newest))
    except Exception as exc:  # noqa: BLE001 -- a failed probe only means "ask again"
        state["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        close_client()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(STATE)
    print(f"CMA 探测：想要 {state['newest_wanted_run_utc'] or '—'}，"
          f"{'已发布' if state['published'] else '未发布'}"
          + (f"（{state['error']}）" if state.get("error") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
