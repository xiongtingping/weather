#!/usr/bin/env python3
"""Addendum 1: IFS PF50 + GEFS 0.5 only, event days 2026-01-01..09-30, IEM truth
before 2026-07-01 injected at the data boundary, backfilled arrivals modelled
on the live-era lag.  Everything else is capture.py (production replay).

    python3 capture_long.py --stations EGLC,KLGA
"""
from __future__ import annotations
import dataclasses, json, os, sqlite3, statistics, sys, types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if os.environ.get("PM_SINGLE_SYSTEM_FIT_RULE"):
    sys.exit("unset PM_SINGLE_SYSTEM_FIT_RULE: the capture must use production's single-system rule")
ALL = bool(os.environ.get("SMW_ALL"))
KEEP = ({"ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05", "DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS", "ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"}
        if ALL else {"ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"})
PERIOD = "all" if ALL else "long"
IEM_BEFORE = "2026-07-01"
SOURCE = ROOT / "docs" / "research" / "single_system_logloss_reg_20260924" / "capture_reg.py"
text = SOURCE.read_text(encoding="utf-8")
def patch(old, new):
    global text
    assert text.count(old) == 1, old[:70]
    text = text.replace(old, new)
patch("forecasts = RegForecasts(cases_by_scope, first_commit, arrival_of)",
      "forecasts = replay.Forecasts(cases_by_scope, first_commit, arrival_of)")
patch('family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems}',
      'family = {s: str(systems[s]["independence_family_id"]) for s in natives if s in systems and s in KEEP}')
patch('        if MODEL_BACKFILL_ARRIVALS and init and scope[0] in ("ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"):',
      '''        if init and scope[0] in LONG_LAG:
            run = replay._utc(init)
            if arrived - run > HIST.BACKFILL_LAG:
                return run + LONG_LAG[scope[0]].get(scope[3], LONG_LAG[scope[0]]["*"])
        if MODEL_BACKFILL_ARRIVALS and init and scope[0] in ("ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"):''')
patch("        rows = capture_station(conn, station, zones[station])",
      "        rows = capture_station(TruthConn(conn, IEM, zones), station, zones[station])")
start = text.index("            scored = {}\n"); end = text.index("    return out\n", start)
text = text[:start] + '''            out.append({
                "station": station, "event_date": event_date, "tz": tz,
                "at": C._iso(at), "truth_known_at": C._iso(known), "lead": newest[2],
                "actual": actual, "lat": latitude, "cooling": cooling,
                "family": {s: family[s] for s in pool},
                "seat": {s: round(v, 6) for s, v in seat.items()},
                "sys": {s: {
                    "p": {k: round(float(v), 7) for k, v in day_rows[s]["probability"].items() if float(v) >= 1e-7},
                    "c": round(float(day_rows[s]["calibrated_center_c"]), 4),
                    "lead": packet[s][2][2], "run": packet[s][1], "n": day_rows[s]["train_n"],
                    "es": round(float(day_rows[s]["error_scale"]), 4),
                } for s in pool},
                "truth_source": "IEM_AGGREGATOR" if event_date < IEM_BEFORE else "FORMAL",
            })
''' + text[end:]

# ------------------------------------------------------------------ IEM truth at the data boundary
IEM = {st: {d: v["max_c"] for d, v in days.items() if v["complete"] and v["max_c"] is not None and d < IEM_BEFORE}
       for st, days in json.loads((HERE / "iem_daily_high.json").read_text()).items()}
def _known(station, day, zones):
    end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zones[station])) + timedelta(days=1, hours=6)
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
class _Rows(list):
    def fetchall(self): return list(self)
    def fetchone(self): return self[0] if self else None
class TruthConn:
    """Read-only connection wrapper: the two truth queries get IEM rows for
    days with no formal truth before IEM_BEFORE; every other query passes."""
    def __init__(self, conn, iem, zones): self._c, self._iem, self._zones = conn, iem, zones
    def __getattr__(self, name): return getattr(self._c, name)
    def execute(self, sql, params=()):
        cur = self._c.execute(sql, params)
        if "FROM calibration_station_daily_high_truth AS t" in sql and "t.official_tmax_c" in sql:
            station, as_of = str(params[0]), str(params[1]).replace("+00:00", "Z")
            rows = cur.fetchall(); have = {str(r[1]) for r in rows}
            extra = [(f"iem_{station}_{d}", d, float(v), k, 0, "IEM_AGGREGATOR")
                     for d, v in self._iem.get(station, {}).items()
                     if d not in have and (k := _known(station, d, self._zones)) <= as_of]
            return _Rows(sorted(list(rows) + extra, key=lambda r: str(r[1])))
        if "SELECT local_date, MIN(canonical_committed_at_utc)" in sql:
            station = str(params[0]); rows = cur.fetchall(); have = {str(r[0]) for r in rows}
            return _Rows(list(rows) + [(d, _known(station, d, self._zones)) for d in self._iem.get(station, {}) if d not in have])
        return cur

# ------------------------------------------------------------------ live-era arrival lag per system x cycle
def live_lag():
    conn = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
    rows = conn.execute(
        "SELECT ensemble_system_id, run_cycle, native_run_init_utc, MIN(archive_query_at_utc)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE ensemble_system_id IN ('ECMWF_IFS_ENS_OPEN_PF50','NOAA_GEFS_05') AND target_local_date BETWEEN '2026-08-15' AND '2026-09-30'"
        " AND qc_status='ACCEPTED' GROUP BY 1, 2, 3, station_id, target_local_date, archive_lead_bucket").fetchall()
    conn.close()
    def u(x): return datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    lags = {}
    for system, cycle, init, got in rows:
        if not init or not got: continue
        h = (u(got) - u(init)).total_seconds() / 3600
        if 0 < h < 24: lags.setdefault(system, {}).setdefault(cycle, []).append(h); lags[system].setdefault("*", []).append(h)
    return {s: {c: timedelta(hours=statistics.median(v)) for c, v in by.items()} for s, by in lags.items()}

R = types.ModuleType("capture_reg_station_model_long")
R.__file__ = str(SOURCE)
R.__dict__.update(dataclasses=dataclasses, KEEP=KEEP, TruthConn=TruthConn, IEM=IEM, IEM_BEFORE=IEM_BEFORE)
exec(compile(text, str(SOURCE), "exec"), R.__dict__)
assert R.oos.SINGLE_SYSTEM_FIT_RULE == R.oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS
R.LONG_LAG = live_lag()
R.TRAIN_FLOOR = "2025-12-01"
R.CUTOFF_UTC = "2026-10-01T09:00:00Z"
R.PERIODS[PERIOD] = lambda day: "2026-01-01" <= day <= "2026-09-30"
R.HERE = HERE

if __name__ == "__main__":
    print("modelled lag (h):", {s: {c: round(v.total_seconds() / 3600, 2) for c, v in by.items()} for s, by in R.LONG_LAG.items()}, flush=True)
    sys.argv += ["--period", PERIOD, "--model-backfill-arrivals", "--fast"]
    sys.exit(R.main())
