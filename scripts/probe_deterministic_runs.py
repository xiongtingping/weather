#!/usr/bin/env python3
"""Is the family's next global run out?  The deterministic capture's doorbell.

Charter 201, the same doorbell charter 152 gave CMA: the capture writes the
SSOT and lives on the sweep queue at a six-hourly product's half cadence,
which the queue's drain rate pays for (``test_the_ssot_sweep_queue_fits_its_
drain_rate``); a run can land anywhere in that window.  So this pass asks the
cheap question per product -- is the newest run the lane still wants
published? -- with SSOT ``mode=ro`` and one request each (AIFS: one index
read on the GCS mirror; relays: one one-location one-hour call, charged to
the Open-Meteo budget), and writes one JSON file.  The daemon makes the
capture due when a product's answer turns yes (``honour_deterministic_probe``).
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

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATE = ROOT / "data" / "runtime" / "deterministic_run_probe.json"
# Charter 267 (二): the native deterministic products ring the same bell.
PRODUCTS = ("AIFS", "IFS9KM", "UKMO10", "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL", "ARPEGE", "UKMO_GLOBAL", "GFS_SFLUX")


def main() -> int:
    from bayes_basket_target import aifs_single_point_archive as aifs  # noqa: PLC0415
    from bayes_basket_target import native_deterministic_point_archive as native  # noqa: PLC0415
    from bayes_basket_target import open_meteo_quota  # noqa: PLC0415
    from bayes_basket_target import open_meteo_relay_archive as relay  # noqa: PLC0415
    from bayes_basket_target.deterministic_forecast_family import (  # noqa: PLC0415
        product_roster,
    )
    from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
        NATIVE_DETERMINISTIC_PRODUCTS, RELAY_PRODUCTS, plan_p1,
    )

    now = datetime.now(timezone.utc)
    state: dict = {"checked_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "products": {}}
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        try:
            for product in PRODUCTS:
                entry: dict = {"newest_wanted_run_utc": None, "published": False}
                state["products"][product] = entry
                try:
                    stations = product_roster(conn, product)
                    plan = plan_p1(conn, product=product, now=now, stations=stations, max_runs=2)
                    if not plan:
                        continue
                    newest = plan[0]["run_init"]
                    entry["newest_wanted_run_utc"] = newest.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if product == "AIFS":
                        entry["published"] = bool(aifs.run_published(newest))
                    elif product in NATIVE_DETERMINISTIC_PRODUCTS:
                        entry["published"] = bool(native.run_published(product, newest))
                    elif product in RELAY_PRODUCTS:
                        if open_meteo_quota.exhausted(now):
                            entry["error"] = "open_meteo_day_exhausted"
                            continue
                        _station, lat, lon, _zone = stations[0]
                        entry["published"] = bool(relay.run_published(product, newest, (lat, lon)))
                except Exception as exc:  # noqa: BLE001 -- a failed probe only means "ask again"
                    entry["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    finally:
        aifs.close_client()
        native.close_client()
        relay.close_client()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    tmp.replace(STATE)
    for product, entry in state["products"].items():
        print(f"{product} 探测：想要 {entry.get('newest_wanted_run_utc') or '—'}，"
              f"{'已发布' if entry.get('published') else '未发布'}"
              + (f"（{entry['error']}）" if entry.get("error") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
