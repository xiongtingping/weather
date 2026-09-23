"""Precipitation conditions for the conditional-belief rows, index-aligned.

Read-only.  For every row of ``../conditional_belief_20260923/dataset.json``:
the Open-Meteo daily precipitation as-of the decision (forward versions only),
the same value as of the local day's end (look-ahead ceiling), the day's
observed precipitation off the official METAR/SPECI archive (oracle ceiling;
millimetres where the report carries an amount, presence everywhere), what
had been reported up to the decision, and the settled high.  See PROTOCOL.md.
"""
from __future__ import annotations

import json
import math
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
from calibration_dashboard import (  # noqa: E402
    PRECIP_ASOS_REMARK_SHARE, PRECIP_COVERAGE_MIN_HOURS, PRECIP_WET_THRESHOLD_MM,
    metar_precipitation_reading,
)

BASE = HERE.parent / "conditional_belief_20260923"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = ROOT / "data" / "archive.db"
REPORT_LAG = timedelta(minutes=15)


def inst(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    started = time.time()
    data = json.loads((BASE / "dataset.json").read_text())
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    zones = {r[0]: ZoneInfo(r[1]) for r in conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding")}
    versions = defaultdict(list)
    for s, day, mm, mode, cap in conn.execute(
        "SELECT station_id, local_date, precipitation_sum, capture_mode, captured_at_utc"
        " FROM calibration_station_forecast_physics_version"
        " WHERE source_id='OPEN_METEO_MODEL_FORECAST' AND precipitation_sum IS NOT NULL"
        " AND capture_mode='forward'"):
        versions[(s, day)].append((inst(cap), float(mm)))
    settled = {}
    for s, day, rev, t in conn.execute(
        "SELECT station_id, event_date, truth_revision_no, settled_tmax_c FROM basket_station_day_settlement_label"):
        if (s, day) not in settled or rev > settled[(s, day)][0]:
            settled[(s, day)] = (rev, float(t))
    conn.close()
    needed = {(r["station"], r["date"]) for r in data}
    stations = {s for s, _ in needed}
    floor = (datetime.fromisoformat(min(d for _, d in needed)) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    archive = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True, timeout=30)
    reports = defaultdict(list)   # (station, local day) -> [(instant, present, hourly_mm or None, is_metar)]
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
        reports[(s, local.strftime("%Y-%m-%d"))].append((at, reading["present"], reading["hourly_mm"], is_metar, local.hour))
        if is_metar:
            metar_n[s] += 1
            if reading["asos"]:
                asos_n[s] += 1
    archive.close()
    asos = {s for s in metar_n if asos_n[s] >= PRECIP_ASOS_REMARK_SHARE * metar_n[s]}
    print("archive read", round(time.time() - started), "s; asos", sorted(asos), flush=True)
    out = []
    for r in data:
        s, day = r["station"], r["date"]
        zone = zones[s]
        at = inst(r["at"])
        start = datetime.fromisoformat(day).replace(tzinfo=zone)
        end = start + timedelta(days=1)
        vers = versions.get((s, day), [])
        known = [v for v in vers if v[0] <= at]
        sameday = [v for v in vers if v[0] <= end]
        rep = reports.get((s, day), [])
        hours = {h for (_a, _p, _mm, m, h) in rep if m}
        covered = len(hours) >= PRECIP_COVERAGE_MIN_HOURS
        hourly = {}
        for a, p, mm, m, h in rep:
            if m:
                hourly[h] = max(hourly.get(h, 0.0), mm or 0.0)
        truth_mm = sum(hourly.values()) if (covered and s in asos) else None
        truth_present = any(p for (_a, p, _mm, _m, _h) in rep) if covered else None
        cutoff = at - REPORT_LAG
        sofar_reports = [x for x in rep if x[0] <= cutoff]
        sofar_present = (any(p for (_a, p, _mm, _m, _h) in sofar_reports) if sofar_reports else None)
        sofar_mm = None
        if s in asos and sofar_reports:
            per_hour = {}
            for a, p, mm, m, h in sofar_reports:
                if m:
                    per_hour[h] = max(per_hour.get(h, 0.0), mm or 0.0)
            sofar_mm = sum(per_hour.values())
        best = max(known, default=None)
        best_same = max(sameday, default=None)
        out.append({
            "om_asof": None if best is None else best[1],
            "om_asof_age_h": None if best is None else (at - best[0]).total_seconds() / 3600.0,
            "om_sameday": None if best_same is None else best_same[1],
            "truth_mm": truth_mm,
            "truth_present": None if truth_present is None else float(truth_present),
            "truth_wet": None if truth_mm is None else float(truth_mm >= PRECIP_WET_THRESHOLD_MM),
            "sofar_present": None if sofar_present is None else float(sofar_present),
            "sofar_mm": sofar_mm,
            "reports_sofar": len(sofar_reports),
            "asos": float(s in asos),
            "settled_tmax_c": settled.get((s, day), (None, None))[1],
        })
    (HERE / "precip_features.json").write_text(json.dumps(out))
    cov = {k: sum(1 for o in out if o[k] is not None) for k in out[0]}
    print("rows", len(out), "coverage", cov, "seconds", round(time.time() - started))


if __name__ == "__main__":
    main()
