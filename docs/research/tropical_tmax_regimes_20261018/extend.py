"""The tropical rules at six more stations (PROTOCOL.md 扩站, user 2026-10-18
「把热带规律研究扩展到 RPLL、ZGGG、ZGSZ、OEJN、MPMG、MMMX……规律成立的站才显示卡片」).

Fetches what is not on disk (fetch.py's request, one per station-year), builds
each station's days in its own settlement zone, prints the card's tables and
the four-condition verdict, and writes extend_rates.json -- the card's numbers.
WSSS/WMKK are read from days.json, unchanged, as the criterion's control.

    uv run python docs/research/tropical_tmax_regimes_20261018/extend.py
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import fetch  # noqa: E402
from analyze import line, rate, rng  # noqa: E402,F401
from build import RAIN, num  # noqa: E402

ZONES = {"RPLL": "Asia/Manila", "ZGGG": "Asia/Shanghai", "ZGSZ": "Asia/Shanghai", "OEJN": "Asia/Riyadh",
         "MPMG": "America/Panama", "MMMX": "America/Mexico_City"}
DAY_FROM = 6.0
MIN_N = 30


def ensure(station: str) -> None:
    for year in fetch.YEARS:
        out = HERE / f"iem_{station}_{year}.csv.gz"
        if out.exists():
            continue
        body = fetch.fetch(station, year)
        out.write_bytes(gzip.compress(body))
        print(f"{station} {year}: {body.count(b'\n') - 1} rows, {len(body) // 1024} KB", file=sys.stderr)
        time.sleep(3)


def load(station: str, zone: ZoneInfo) -> list[dict]:
    obs = []
    for f in sorted(HERE.glob(f"iem_{station}_*.csv.gz")):
        for r in csv.DictReader(io.StringIO(gzip.decompress(f.read_bytes()).decode("utf-8", "replace"))):
            t = num(r.get("tmpf"))
            at = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(zone)
            wx = r.get("wxcodes") or ""
            wx = "" if wx == "M" else wx
            obs.append({"at": at, "t": None if t is None else round((t - 32) / 1.8),
                        "rain": any(k in wx for k in RAIN) and not wx.startswith("VC"), "ts": "TS" in wx})
    return obs


def build(station: str) -> list[dict]:
    zone = ZoneInfo(ZONES[station])
    by = defaultdict(list)
    for o in load(station, zone):
        by[o["at"].date()].append(o)
    tmax = {d: max((o["t"] for o in v if o["t"] is not None), default=None) for d, v in by.items()}
    hour_t = {d: {h: statistics.mean(x["t"] for x in v if x["t"] is not None and x["at"].hour == h)
                  for h in {x["at"].hour for x in v if x["t"] is not None}} for d, v in by.items()}
    out = []
    for d in sorted(by):
        if tmax[d] is None or len(hour_t[d]) < 20:
            continue          # a day with big gaps reads a wrong maximum
        prev = [tmax[d - timedelta(days=k)] for k in range(1, 11) if tmax.get(d - timedelta(days=k)) is not None
                and len(hour_t.get(d - timedelta(days=k), {})) >= 20]
        if len(prev) < 8:
            continue
        base = statistics.median(prev)
        y = tmax[d]
        mt = [hour_t[d].get(h) for h in (8, 9)]
        past = [statistics.mean(hour_t[d - timedelta(days=k)][h] for h in (8, 9))
                for k in range(1, 11) if all(h in hour_t.get(d - timedelta(days=k), {}) for h in (8, 9))]
        y1 = tmax.get(d - timedelta(days=1))
        out.append({
            "station": station, "date": d.isoformat(), "month": d.month, "year": d.year, "tmax": y, "base": base,
            "cls": "cool" if y <= base - 1 else "warm" if y >= base + 1 else "usual",
            "prev_anom": None if y1 is None else y1 - base,
            "morning_t_anom": (statistics.mean(mt) - statistics.median(past)) if None not in mt and len(past) >= 6 else None,
            "day_rain_h": min((x["at"].hour + x["at"].minute / 60 for x in by[d]
                               if (x["rain"] or x["ts"]) and x["at"].hour + x["at"].minute / 60 >= DAY_FROM), default=None),
        })
    return out


GROUPS = {
    "rain_lt10": lambda r: r["day_rain_h"] is not None and r["day_rain_h"] < 10,
    "rain_10_13": lambda r: r["day_rain_h"] is not None and 10 <= r["day_rain_h"] < 13,
    "rain_13_16": lambda r: r["day_rain_h"] is not None and 13 <= r["day_rain_h"] < 16,
    "rain_ge16": lambda r: r["day_rain_h"] is not None and r["day_rain_h"] >= 16,
    "rain_none": lambda r: r["day_rain_h"] is None,
    "morning_cold": lambda r: r["morning_t_anom"] is not None and r["morning_t_anom"] <= -1,
    "morning_usual": lambda r: r["morning_t_anom"] is not None and -1 < r["morning_t_anom"] < 1,
    "morning_warm": lambda r: r["morning_t_anom"] is not None and r["morning_t_anom"] >= 1,
    "prev_low": lambda r: r["prev_anom"] is not None and r["prev_anom"] <= -1,
    "prev_usual": lambda r: r["prev_anom"] is not None and -1 < r["prev_anom"] < 1,
    "prev_high": lambda r: r["prev_anom"] is not None and r["prev_anom"] >= 1,
}
#: PROTOCOL.md 扩站: (group, class whose rate must clear the station's own).
CONDITIONS = (("rain_lt10", "cool"), ("rain_ge16", "warm"), ("morning_cold", "cool"), ("morning_warm", "warm"))


def boot_lo(rows: list[dict], cls: str) -> float:
    by = defaultdict(list)
    for r in rows:
        by[r["year"]].append(r)
    ys = list(by)
    xs = sorted(rate([r for _ in ys for r in by[rng.choice(ys)]], cls) for _ in range(500))
    return xs[12]


def judge(station: str, R: list[dict]) -> dict:
    base = (rate(R, "cool"), rate(R, "warm"))
    print(f"\n# {station}  {len(R)} 天  {R[0]['date']}..{R[-1]['date']}  降温日 {100 * base[0]:.0f}%  突破日 {100 * base[1]:.0f}%")
    for key, f in GROUPS.items():
        print(line(key, [r for r in R if f(r)], base))
    checks = []
    for key, cls in CONDITIONS:
        rows = [r for r in R if GROUPS[key](r)]
        lo = boot_lo(rows, cls) if len(rows) >= MIN_N else None
        ok = lo is not None and lo > base[0 if cls == "cool" else 1]
        checks.append({"group": key, "class": cls, "n": len(rows), "lo": lo, "ok": ok})
        print(f"  判据 {key} {cls}: n={len(rows)} 下沿 {'—' if lo is None else f'{100 * lo:.0f}%'}"
              f" 对全体 {100 * base[0 if cls == 'cool' else 1]:.0f}% → {'成立' if ok else '不成立'}")
    holds = all(c["ok"] for c in checks)
    print(f"  ⇒ {station} 规律{'成立' if holds else '不成立'}")
    pct = lambda rows: (round(100 * rate(rows, "cool")), round(100 * rate(rows, "warm"))) if rows else None  # noqa: E731
    return {
        "holds": holds, "checks": checks, "days": len(R),
        "rates": {"base": pct(R), **{k: pct([r for r in R if f(r)]) for k, f in GROUPS.items() if k != "rain_none"}},
        "month": [pct([r for r in R if r["month"] == m]) for m in range(1, 13)],
    }


def main() -> None:
    out = {}
    old = json.loads((HERE / "days.json").read_text())
    for st in ("WSSS", "WMKK"):            # the criterion's control, current numbers untouched
        R = [r for r in old if r["station"] == st]
        for r in R:
            r["day_rain_h"] = None
        hours = defaultdict(list)
        import build as B  # noqa: PLC0415
        for o in B.load(st):
            if o["rain"] or o["ts"]:
                hours[o["at"].date().isoformat()].append(o["at"].hour + o["at"].minute / 60)
        for r in R:
            r["day_rain_h"] = min((h for h in hours.get(r["date"], []) if h >= DAY_FROM), default=None)
        out[st] = {**judge(st, R), "control": True}
    for st in ZONES:
        ensure(st)
        out[st] = judge(st, build(st))
    (HERE / "extend_rates.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("\n# 结论  " + "  ".join(f"{k} {'成立' if v['holds'] else '不成立'}" for k, v in out.items()))


if __name__ == "__main__":
    main()
