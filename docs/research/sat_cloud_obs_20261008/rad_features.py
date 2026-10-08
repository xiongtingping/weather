"""Observed surface sunshine against the day-ahead forecast, per anchor.

熊先生 2026-10-08「加辐射数据」.  Observed shortwave (W/m2) at the 48 stations:

* GOES-19/18 ``ABI-L2-DSRF`` (16 Americas stations): 2 km, the first scan of
  each hour, 5x5 box mean; admitted after the object's S3 LastModified.
* Open-Meteo satellite archive (the other 32): JMA/JAXA Himawari, DWD SIS,
  EUMETSAT LSA SAF hourly means of the preceding hour, captured by
  ``obs_increment_recheck_20261007``.  A republisher without historical
  arrival receipts: each hour is admitted one hour after it ends (the
  previous study's middle delay hypothesis) -- research evidence only.

Forecast: Open-Meteo ``shortwave_radiation_previous_day1`` (hourly mean of
the preceding hour, every value issued >= 24 h before its hour).

At an anchor the deficit is taken over the newest two observations admitted
by then (both begun within 4 h):

    deficit = (forecast - observed) / max(forecast, 200 W/m2), clipped to [-1, 1]

positive = darker than forecast.  Night and dawn (forecast < 50 W/m2 in both
hours) give no evidence.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
RECHECK = HERE.parent / "obs_increment_recheck_20261007"
FORECAST = HERE.parent / "obs_increment_p1_20261007" / "cache"
GOES_STATIONS = {"CYYZ", "KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO",
                 "MMMX", "MPMG", "SAEZ", "SBGR"}
OPEN_METEO_LAG = timedelta(hours=float(__import__("os").environ.get("RAD_OPEN_METEO_LAG_HOURS", "1")))
MAX_SPAN = timedelta(hours=4)  # two hour-means admitted 1 h late reach back 3.5 h


def utc(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _local_hourly(payload: dict, key: str) -> dict[datetime, float]:
    zone = ZoneInfo(payload["timezone"])
    h = payload["hourly"]
    return {datetime.fromisoformat(t).replace(tzinfo=zone).astimezone(timezone.utc): float(v)
            for t, v in zip(h["time"], h[key]) if v is not None}


class Radiation:
    def __init__(self) -> None:
        self.obs: dict[str, list[dict]] = {}
        self.fc: dict[str, dict[datetime, float]] = {}
        self.source: dict[str, str] = {}
        for st in GOES_STATIONS:
            rows = []
            for path in sorted((HERE / "goes").glob("*/ABI-L2-DSRF/*.jsonl.gz")):
                for line in gzip.open(path, "rt"):
                    r = json.loads(line)
                    if r.get("station") == st and r.get("state") == "OK" and r.get("DSR_box_mean") is not None:
                        rows.append(dict(at=utc(r["scan_start"]), available=utc(r["s3_last_modified"]), sw=float(r["DSR_box_mean"]), kind="instant"))
            if rows:
                self.obs[st] = sorted(rows, key=lambda x: x["at"])
                self.source[st] = "goes_abi_l2_dsr"
        chosen = {s["station"]: s["model"] for s in json.loads((RECHECK / "station_sources.json").read_text())["stations"] if s.get("model")}
        for st, model in chosen.items():
            if st in self.obs:
                continue
            path = next(iter(sorted(RECHECK.glob(f"source_cache/{st}_{model}_2026-09-01_*.json.gz"))), None)
            if path is None:
                continue
            body = json.load(gzip.open(path, "rt"))
            payload = body["payload"] if isinstance(body["payload"], dict) else json.loads(body["payload"])
            hourly = _local_hourly(payload, "shortwave_radiation")
            # Open-Meteo hourly radiation is the mean of the hour ending at the stamp.
            self.obs[st] = [dict(at=t - timedelta(minutes=30), available=t + OPEN_METEO_LAG, sw=v, kind="hour_mean")
                            for t, v in sorted(hourly.items())]
            self.source[st] = "open_meteo_" + model
        for path in sorted(FORECAST.glob("*.json.gz")):
            payload = json.load(gzip.open(path, "rt"))
            self.fc[path.name.split(".")[0]] = _local_hourly(payload, "shortwave_radiation_previous_day1")

    def forecast_at(self, st: str, at: datetime) -> float | None:
        """Forecast hour means sit at their hour's middle; linear between.
        ``at`` is the instant asked for (a scan time, or an hour's middle)."""
        table = self.fc.get(st, {})
        mid = at - timedelta(minutes=30)
        lo = (mid.replace(minute=0, second=0, microsecond=0))
        a, b = table.get(lo + timedelta(hours=1)), table.get(lo + timedelta(hours=2))
        if a is None or b is None:
            return None
        w = (mid - lo).total_seconds() / 3600.0
        return a * (1 - w) + b * w

    def at(self, st: str, as_of: str) -> dict | None:
        t = utc(as_of)
        seen = [x for x in self.obs.get(st, ()) if x["available"] <= t and t - x["at"] <= MAX_SPAN]
        if len(seen) < 2:
            return None
        last = seen[-2:]
        # An hour-mean observation sits at its hour's middle, where the
        # forecast_at interpolation returns exactly that hour's forecast mean.
        fcs = [self.forecast_at(st, x["at"]) for x in last]
        if any(f is None for f in fcs) or max(fcs) < 50:
            return None
        obs, fc = sum(x["sw"] for x in last), sum(fcs)
        deficit = max(-1.0, min(1.0, (fc - obs) / max(fc, 200.0 * len(last))))
        return dict(source=self.source[st], valid_at=last[-1]["at"].isoformat().replace("+00:00", "Z"),
                    available_at=last[-1]["available"].isoformat().replace("+00:00", "Z"),
                    observed_sw=round(obs / 2, 1), forecast_sw=round(fc / 2, 1), deficit=round(deficit, 4))
