"""Daily table (PROTOCOL.md 定义) from the IEM reports.

    uv run python docs/research/tropical_tmax_regimes_20261018/build.py
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
TZ = timezone(timedelta(hours=8))  # both stations, no DST
STATIONS = ("WSSS", "WMKK")
RAIN = ("RA", "SH", "TS", "DZ")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(station):
    obs = []
    for f in sorted(HERE.glob(f"iem_{station}_*.csv.gz")):
        for r in csv.DictReader(io.StringIO(gzip.decompress(f.read_bytes()).decode("utf-8", "replace"))):
            t = num(r.get("tmpf"))
            at = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(TZ)
            wx = r.get("wxcodes") or ""
            wx = "" if wx == "M" else wx
            obs.append({
                "at": at, "t": None if t is None else round((t - 32) / 1.8),
                "td": None if num(r.get("dwpf")) is None else (num(r["dwpf"]) - 32) / 1.8,
                "dir": num(r.get("drct")), "kt": num(r.get("sknt")),
                "c1": r.get("skyc1") or "", "h1": num(r.get("skyl1")),
                "c2": r.get("skyc2") or "", "h2": num(r.get("skyl2")),
                "rain": any(k in wx for k in RAIN) and not wx.startswith("VC"),
                "ts": "TS" in wx, "vcts": "VCTS" in wx, "wx": wx,
            })
    return obs


def low_cloud(o):
    return any(c in ("BKN", "OVC") and h is not None and h <= 3000 for c, h in ((o["c1"], o["h1"]), (o["c2"], o["h2"])))


def main() -> None:
    out = []
    for st in STATIONS:
        by = defaultdict(list)
        for o in load(st):
            by[o["at"].date()].append(o)
        days = sorted(by)
        tmax = {d: max((o["t"] for o in by[d] if o["t"] is not None), default=None) for d in days}
        hour_t = defaultdict(dict)   # date -> hour -> mean temp
        for d in days:
            hh = defaultdict(list)
            for o in by[d]:
                if o["t"] is not None:
                    hh[o["at"].hour].append(o["t"])
            hour_t[d] = {h: statistics.mean(v) for h, v in hh.items()}
        for d in days:
            if tmax[d] is None or len([o for o in by[d] if o["t"] is not None]) < 30:
                continue          # a day with big gaps reads a wrong maximum
            prev = [tmax[d - timedelta(days=k)] for k in range(1, 11) if tmax.get(d - timedelta(days=k)) is not None]
            if len(prev) < 8:
                continue
            base = statistics.median(prev)
            y = tmax[d]
            cls = "cool" if y <= base - 1 else "warm" if y >= base + 1 else "usual"
            o = by[d]
            pd = by.get(d - timedelta(days=1), [])
            morning = [x for x in o if 7 <= x["at"].hour < 10]
            early = [x for x in o if x["at"].hour < 10]
            def m(xs, k):
                v = [x[k] for x in xs if x[k] is not None]
                return statistics.mean(v) if v else None
            # morning temperature against the same hours of the ten days before
            mt = [hour_t[d].get(h) for h in (8, 9)]
            past_mt = [statistics.mean([hour_t[d - timedelta(days=k)][h] for h in (8, 9)])
                       for k in range(1, 11) if all(h in hour_t.get(d - timedelta(days=k), {}) for h in (8, 9))]
            mt_anom = (statistics.mean(mt) - statistics.median(past_mt)) if None not in mt and len(past_mt) >= 6 else None
            past_td = [m([x for x in by.get(d - timedelta(days=k), []) if 7 <= x["at"].hour < 10], "td") for k in range(1, 11)]
            past_td = [v for v in past_td if v is not None]
            td = m(morning, "td")
            u = [x["kt"] * math.sin(math.radians(x["dir"])) for x in morning if x["kt"] is not None and x["dir"] is not None]
            v = [x["kt"] * math.cos(math.radians(x["dir"])) for x in morning if x["kt"] is not None and x["dir"] is not None]
            first_rain = min((x["at"].hour + x["at"].minute / 60 for x in o if x["rain"] or x["ts"]), default=None)
            first_ts = min((x["at"].hour + x["at"].minute / 60 for x in o if x["ts"] or x["vcts"]), default=None)
            # hour the day's max was first reached
            peak_h = min(x["at"].hour + x["at"].minute / 60 for x in o if x["t"] == y)
            out.append({
                "station": st, "date": d.isoformat(), "month": d.month, "year": d.year,
                "tmax": y, "base": base, "anom": y - base, "cls": cls,
                # known by 10:00 local
                "prev_anom": None if tmax.get(d - timedelta(days=1)) is None else tmax[d - timedelta(days=1)] - base,
                "prev_eve_rain": any((x["rain"] or x["ts"]) and 14 <= x["at"].hour < 21 for x in pd),
                "morning_rain": any(x["rain"] or x["ts"] for x in early),
                "morning_t_anom": mt_anom,
                "morning_td": td,
                "morning_td_anom": (td - statistics.median(past_td)) if td is not None and len(past_td) >= 6 else None,
                "morning_u": statistics.mean(u) if u else None, "morning_v": statistics.mean(v) if v else None,
                "morning_kt": m(morning, "kt"),
                "morning_low_cloud": (sum(low_cloud(x) for x in morning) / len(morning)) if morning else None,
                # the day itself
                "first_rain_h": first_rain, "first_ts_h": first_ts, "peak_h": peak_h,
                "noon_low_cloud": (lambda xs: sum(low_cloud(x) for x in xs) / len(xs) if xs else None)(
                    [x for x in o if 12 <= x["at"].hour < 15]),
            })
    (HERE / "days.json").write_text(json.dumps(out))
    for st in STATIONS:
        rows = [r for r in out if r["station"] == st]
        print(st, len(rows), {c: sum(r["cls"] == c for r in rows) for c in ("cool", "usual", "warm")},
              rows[0]["date"], rows[-1]["date"])


if __name__ == "__main__":
    main()
