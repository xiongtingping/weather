"""Forecast low cloud for the cloud -> Tmax line (熊先生 2026-10-17 「保持口径
一致」): Open-Meteo ``cloud_cover_low`` -- the latest forecast, the card's own
basis.  The day-ahead series (``cloud_cover_low_previous_day1``) is not in the
archive at all (09-01..10-17 every hour null, checked 10-17), so the line
reads the latest forecast as the card does.  Hourly, per station over the
fit window, cached per station.  Charged to the research
lane of the production quota ledger (10-11 incident)."""
from __future__ import annotations

import gzip
import json
import math
import ssl
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import open_meteo_quota as quota  # noqa: E402

JX = HERE.parent / "jaxa_cloud_rad_p1_20261015"
CACHE = HERE / "cache"
FIRST, LAST = "2026-09-01", "2026-10-14"
FIELD = "cloud_cover_low"


def main() -> None:
    import certifi  # noqa: PLC0415

    CACHE.mkdir(exist_ok=True)
    where = {}
    for line in gzip.open(JX / "rows_gsmap.jsonl.gz", "rt"):
        r = json.loads(line)
        where.setdefault(r["station"], (r["lat"], r["lon"], r["tz"]))
    ctx = ssl.create_default_context(cafile=certifi.where())
    weight = max(1, math.ceil(((date.fromisoformat(LAST) - date.fromisoformat(FIRST)).days + 1) / 14))
    for st, (lat, lon, tz) in sorted(where.items()):
        path = CACHE / f"{st}.json.gz"
        if path.exists():
            continue
        quota.charge("research", weight)
        q = urlencode({"latitude": lat, "longitude": lon, "timezone": tz, "start_date": FIRST, "end_date": LAST,
                       "hourly": FIELD})
        for attempt in range(4):
            try:
                with urlopen(Request("https://previous-runs-api.open-meteo.com/v1/forecast?" + q,
                                     headers={"User-Agent": "weather-research"}), timeout=60, context=ctx) as r:
                    got = json.loads(r.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                print("  retry", st, exc, flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"open-meteo failed for {st}")
        json.dump({"timezone": got.get("timezone"), "hourly": got["hourly"]}, gzip.open(path, "wt"))
        print(st, len(got["hourly"]["time"]), flush=True)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
