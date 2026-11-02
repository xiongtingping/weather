"""The direction model's physics series for the backtest window (PROTOCOL.md).

One Open-Meteo previous-runs request per station, previous_day1 only (on d0
the lag is always previous_day1), the same variables and models production
reads; each request is charged to the shared quota ledger before it is made.
Cached in ``physics.json.gz``; a station already cached is not fetched again.

    uv run python docs/research/d0_skew_direction_backtest_20261017/fetch_physics.py
"""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
import sys
import urllib.parse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import open_meteo_quota as quota  # noqa: E402
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402

OUT = Path(__file__).with_name("physics.json.gz")
START, END = "2026-08-04", "2026-10-18"
LANE = "research_d0_direction_backtest"


def url(lat: float, lon: float, tz: str) -> str:
    hourly = ",".join(f"{v}_previous_day1" for v in ds.PHYSICS_VARIABLES)
    q = urllib.parse.urlencode({"latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "start_date": START,
                                "end_date": END, "hourly": hourly, "models": ",".join(ds.PHYSICS_MODELS),
                                "timezone": tz})
    return f"{ds.PHYSICS_API_URL}?{q}"


def main() -> int:
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro", uri=True)
    zones = ds.fleet_timezones(conn)
    conn.close()
    held = json.loads(gzip.open(OUT, "rt").read()) if OUT.exists() else {}
    days = (date.fromisoformat(END) - date.fromisoformat(START)).days + 1
    series = len(ds.PHYSICS_VARIABLES) * len(ds.PHYSICS_MODELS)
    weight = math.ceil(max(1.0, series / 10) * max(1.0, days / 14))
    failed = {}
    for station, tz in sorted(zones.items()):
        if station in held:
            continue
        quota.charge(LANE, weight)
        where = station_coordinates(station)
        try:
            payload = ds._http_fetch(url(float(where["latitude"]), float(where["longitude"]), tz))
            held[station] = {"timezone": tz, "daily": ds.daily_means(payload)}
        except Exception as exc:  # noqa: BLE001 -- recorded, the rest go on
            failed[station] = f"{type(exc).__name__}:{exc}"[:160]
        with gzip.open(OUT, "wt", encoding="utf-8") as f:
            f.write(json.dumps(held))
    print(json.dumps({"stations": len(zones), "cached": len(held), "weight_each": weight, "failed": failed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
