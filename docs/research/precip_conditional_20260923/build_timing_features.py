"""When the rain fell, per station-day (user, 2026-09-23: 「还有雨落的时间，峰值后肯定没信息」).

Oracle features off the official reports: precipitation before / after a
fixed 15:00 local, and before / after the day's actual maximum (the AWC
lane's ``max_valid_at``).  Presence for every station, millimetres (hourly
P groups) for the ASOS stations.  Index-aligned with ``dataset.json``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from calibration_dashboard import PRECIP_ASOS_REMARK_SHARE, metar_precipitation_reading  # noqa: E402

BASE = HERE.parent / "conditional_belief_20260923"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "archive.db"
PEAK_CLOCK_HOUR = 15


def inst(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    started = time.time()
    data = json.loads((BASE / "dataset.json").read_text())
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    zones = {r[0]: ZoneInfo(r[1]) for r in conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
    conn.close()
    needed = {(r["station"], r["date"]) for r in data}
    stations = {s for s, _ in needed}
    floor = (datetime.fromisoformat(min(d for _, d in needed)) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    archive = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=30)
    max_at = {}
    for s, d, mv in archive.execute(
        "SELECT station_id, event_date, max_valid_at FROM reported_trajectory_station_day_bounds WHERE source_lane='awc'"):
        if mv:
            max_at[(s, d)] = inst(mv)
    for s, d, mv in archive.execute(
        "SELECT station_id, event_date, max_valid_at FROM reported_trajectory_canonical_station_day_bounds"):
        if mv and (s, d) not in max_at:
            max_at[(s, d)] = inst(mv)
    reports = defaultdict(list)
    metar_n, asos_n = Counter(), Counter()
    for s, rt, va, raw in archive.execute(
        "SELECT station_id, report_type, valid_at, raw_text FROM reported_trajectory_observations"
        " WHERE source_lane='awc' AND valid_at>=?", (floor,)):
        if s not in stations or s not in zones:
            continue
        at = inst(va)
        local = at.astimezone(zones[s])
        reading = metar_precipitation_reading(raw)
        is_metar = rt == "METAR"
        reports[(s, local.strftime("%Y-%m-%d"))].append((at, local, reading["present"], reading["hourly_mm"], is_metar))
        if is_metar:
            metar_n[s] += 1
            if reading["asos"]:
                asos_n[s] += 1
    archive.close()
    asos = {s for s in metar_n if asos_n[s] >= PRECIP_ASOS_REMARK_SHARE * metar_n[s]}
    out = []
    cache = {}
    for r in data:
        key = (r["station"], r["date"])
        if key not in cache:
            s = r["station"]
            rep = reports.get(key, [])
            hours = {loc.hour for (_a, loc, _p, _mm, m) in rep if m}
            covered = len(hours) >= 22
            peak = max_at.get(key)
            zone = zones[s]
            clock = datetime.fromisoformat(r["date"]).replace(tzinfo=zone) + timedelta(hours=PEAK_CLOCK_HOUR)

            def split(cut):
                before = [x for x in rep if x[0] <= cut]
                after = [x for x in rep if x[0] > cut]
                pres_b = any(p for (_a, _l, p, _mm, _m) in before)
                pres_a = any(p for (_a, _l, p, _mm, _m) in after)
                mm_b = mm_a = None
                if s in asos:
                    hb, ha = {}, {}
                    for a, loc, _p, mm, m in rep:
                        if not m:
                            continue
                        # The P group covers the hour ending at this report.
                        tgt = hb if a <= cut + timedelta(minutes=59) else ha
                        tgt[loc.hour] = max(tgt.get(loc.hour, 0.0), mm or 0.0)
                    mm_b, mm_a = sum(hb.values()), sum(ha.values())
                return pres_b, pres_a, mm_b, mm_a

            row = {"covered": float(covered), "asos": float(s in asos),
                   "max_local_hour": None if peak is None else peak.astimezone(zone).hour}
            if covered:
                pb, pa, mb, ma = split(clock)
                row.update({"pre15_present": float(pb), "post15_present": float(pa),
                            "only_post15_present": float(pa and not pb), "pre15_mm": mb, "post15_mm": ma})
                if peak is not None:
                    pb, pa, mb, ma = split(peak)
                    row.update({"premax_present": float(pb), "postmax_present": float(pa),
                                "only_postmax_present": float(pa and not pb), "premax_mm": mb, "postmax_mm": ma})
            cache[key] = row
        out.append(cache[key])
    keys = sorted({k for row in out for k in row})
    out = [{k: row.get(k) for k in keys} for row in out]
    (HERE / "timing_features.json").write_text(json.dumps(out))
    cov = {k: sum(1 for o in out if o.get(k) is not None) for k in keys}
    print("rows", len(out), "coverage", cov, "asos", sorted(asos), "seconds", round(time.time() - started))


if __name__ == "__main__":
    main()
