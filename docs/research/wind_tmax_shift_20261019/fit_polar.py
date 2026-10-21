"""The wind's polar component against its speed (PROTOCOL.md 追加; 熊先生
2026-10-19 「按风的南北分量重新拟合……和现在的风速差比较样本外，哪个好卡片用哪个」).

x_polar at local hour H = the report's wind component from the pole (m/s;
northerly in the north, southerly in the south positive) minus the latest
forecast's.  The same y, line, folds and bootstrap as fit.py; then, on the
station-days both x exist for, the paired out-of-sample difference
(polar - speed) per hour, bootstrapped by event day.  Stations without a
cached forecast direction are left out (the run says how many).

    uv run python docs/research/wind_tmax_shift_20261019/fit_polar.py
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("wind_fit", HERE / "fit.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
C = W.C

from bayes_basket_target.station_day_board import polar_wind_component  # noqa: E402

OUT = ROOT / "config" / "ui" / "wind_polar_tmax_shift_v1.json"


def forecast(cache: str, field: str, scale: float = 1.0) -> dict[str, dict[str, float | None]]:
    out = {}
    for path in (HERE / cache).glob("*.json.gz"):
        body = json.load(gzip.open(path, "rt"))
        out[path.name.split(".")[0]] = dict(zip(body["hourly"]["time"],
                                                [None if v is None else v * scale for v in body["hourly"][field]]))
    return out


def station_days(frames):
    """(station, day) -> {polar: report[h], fc_polar[h], speed: report[h], fc_speed[h], fc_tmax}."""
    spd, dirn = forecast("cache", "wind_speed_10m", 1 / 3.6), forecast("cache_dir", "wind_direction_10m")
    arc = sqlite3.connect(f"file:{C.ARCHIVE}?mode=ro", uri=True, timeout=30)
    out = {}
    for (st, day), fr in frames.items():
        fc = fr["fc"]
        if len(fc) < 24 or any(x.get("t") is None for x in fc) or st not in dirn or st not in spd:
            continue
        zone = ZoneInfo(fr["tz"])
        lat = float(fr["lat"])
        d0 = datetime.fromisoformat(day).replace(tzinfo=zone)
        lo, hi = d0.astimezone(timezone.utc), (d0 + timedelta(days=1)).astimezone(timezone.utc)
        polar, speed = [None] * 24, [None] * 24
        for va, raw in arc.execute(
                "SELECT valid_at, raw_text FROM reported_trajectory_observations"
                " WHERE station_id=? AND valid_at>=? AND valid_at<? AND raw_text<>''",
                (st, lo.strftime("%Y-%m-%dT%H:%M:%S"), hi.strftime("%Y-%m-%dT%H:%M:%S"))):
            t = C.utc(va).astimezone(zone)
            if t.minute > 10:
                continue
            tokens = str(raw).split()
            body = tokens[:tokens.index("RMK")] if "RMK" in tokens else tokens
            wind = next((m for m in (re.fullmatch(r"(VRB|\d{3})(\d{2,3})(?:G\d{2,3})?(KT|MPS)", x) for x in body) if m), None)
            if wind is None:
                continue
            mps = float(wind.group(2)) * (0.514444 if wind.group(3) == "KT" else 1.0)
            speed[t.hour] = mps
            polar[t.hour] = polar_wind_component(None if wind.group(1) == "VRB" else int(wind.group(1)), mps, lat)
        key = lambda h: f"{day}T{h:02d}:00"  # noqa: E731
        out[(st, day)] = {"polar": polar, "speed": speed,
                          "fc_polar": [polar_wind_component(dirn[st].get(key(h)), spd[st].get(key(h)), lat) for h in range(24)],
                          "fc_speed": [spd[st].get(key(h)) for h in range(24)],
                          "fc_tmax": max(x["t"] for x in fc)}
    arc.close()
    return out


def rows_for(hour, days, truth, xs):
    """Station-demeaned rows carrying every x in ``xs`` (name -> (obs key, forecast key)); only where all exist."""
    rows = []
    for (st, day), d in days.items():
        if (st, day) not in truth:
            continue
        x = {}
        for name, (ok, fk) in xs.items():
            if d[ok][hour] is None or d[fk][hour] is None:
                break
            x[name] = d[ok][hour] - d[fk][hour]
        else:
            rows.append({"st": st, "day": day, "x": x, "y": truth[(st, day)] - d["fc_tmax"]})
    by = defaultdict(list)
    for r in rows:
        by[r["st"]].append(r)
    out = []
    for rs in by.values():
        if len(rs) < C.MIN_STATION_DAYS:
            continue
        my = sum(r["y"] for r in rs) / len(rs)
        for r in rs:
            r["y"] -= my
            out.append(r)
    return out


def paired(rows):
    """Out-of-sample (polar - speed) squared error, same folds, bootstrapped by day."""
    se = {}
    for name in ("polar", "speed"):
        e, _m = C.cv(rows, lambda r, n=name: r["x"][n], W.X_CLIP)
        se[name] = e
    keys = list(se["polar"])
    return C.boot(keys, se["polar"], se["speed"])


def main() -> None:
    frames, _clp, truth = C.load()
    days = station_days(frames)
    both = {"polar": ("polar", "fc_polar"), "speed": ("speed", "fc_speed")}
    table, compare, diffs = {}, {}, []
    for hour in W.HOURS:
        rows = rows_for(hour, days, truth, both)
        polar_rows = [{**r, "x": r["x"]["polar"]} for r in rows]
        line = W.line(polar_rows, hour)
        line = {k.replace("calmer", "equatorward").replace("windier", "poleward"): v for k, v in line.items()}
        table[str(hour)] = line
        compare[str(hour)] = {"rows": len(rows), "stations": len({r["st"] for r in rows}), "polar_minus_speed_mse": paired(rows)}
        diffs.append(compare[str(hour)]["polar_minus_speed_mse"][0])
        print(hour, json.dumps(line, ensure_ascii=False), "| polar−speed:", compare[str(hour)], flush=True)
    stations = len({st for st, _ in days})
    verdict = "polar" if sum(diffs) / len(diffs) < 0 else "speed"
    if stations < 40:
        print(f"only {stations} stations have a forecast direction: preliminary, config not written")
        return
    print("stations with a forecast direction:", stations, "| mean polar−speed over 9..17:", round(sum(diffs) / len(diffs), 4), "→", verdict)
    OUT.write_text(json.dumps({
        "schema": "wind_polar_tmax_shift_v1",
        "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [min(d for _, d in days), max(d for _, d in days)], "stations_with_direction": stations,
        "x_clip_mps": W.X_CLIP,
        "model": "shift_c = intercept_c + equatorward*min(x,0) + poleward*max(x,0), x in m/s clipped",
        "x": "at local hour H, report wind component from the pole (north in NH, south in SH) - latest forecast's, m/s",
        "versus_speed": compare, "card_uses": verdict, "by_hour": table,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
