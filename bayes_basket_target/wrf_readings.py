"""The WRF runs' daily-maximum forecast, read for the board (user 2026-10-22
「前端增加"wrf"板块内容」→「项目在这里：/Volumes/weather_data/wrf_apps。增加显式：
最高温的预报（输出）」).

The WRF project on the external volume (``PM_WRF_APPS_ROOT``, default
``/Volumes/weather_data/wrf_apps``) runs GFS-driven 9 km (and nested) WRF
for ZUUU: the baseline day runs under ``run/days/<YYYYMMDD>/wrf`` and one
directory per configuration experiment under ``run/exp/<config>/<YYYYMMDD>``.
For every run this reads T2 at the grid point nearest the station from each
``wrfout`` (the finest domain present), takes the maximum over the station's
local day, and sets it beside the formal truth (charter 225 lanes) and the
driving GFS's own 3-hourly 2 m temperature from the run's ``met_em`` files.

Writes ``data/board_readings/wrf_tmax.json`` (``board_readings`` schema).  A
reader: the SSOT read-only, the WRF files read-only, a per-file cache under
``data/runtime/wrf_readings`` keyed on size and mtime so an unchanged
``wrfout`` is never opened twice.  A file still being written is skipped
frame by frame; a missing volume writes ``state: volume_missing``.  Display
only; enters no layer.
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import board_readings
from .canonical_contract import FORMAL_TRUTH_LANES_SQL

ROOT = Path(__file__).resolve().parents[1]
WRF_ROOT = Path(os.environ.get("PM_WRF_APPS_ROOT") or "/Volumes/weather_data/wrf_apps")
CACHE_PATH = ROOT / "data" / "runtime" / "wrf_readings" / "frames_v2.json"  # v2: fill-value frames left out
READING_NAME = "wrf_tmax"
#: The station point the WRF project's own tools read (run/tools/zuuu_t2.py).
STATIONS = {"ZUUU": (30.578, 103.947)}
BASELINE = "baseline"
#: The live forecasts, ``run/fcst/<last day YYYYMMDD>_from_<init YYYYMMDDHH>``
#: (run/forecast3d.sh, user 2026-10-22 「把未来3日都计算」: one run from the day's
#: GFS 00Z to three days ahead 12Z): every local day from the init's date to the last
#: day is read from the run, and for each day the newest init that reached it is shown.
FORECAST = "fcst"
#: Two-driver forecast (user 2026-10-31 「驱动方案：上 IFS+GDPS 平均」): from then on each run holds
#: one WRF per driver under ``<run>/ifs`` and ``<run>/gdps`` (nudged, same physics).  Over 4 replayed
#: inits (14 station-days) IFS alone ran +0.98 (MAE 1.35), GDPS -1.04 (1.44), the mean of their
#: maxima -0.03 (0.64): wrf_apps run/exp_fcstphys/results.json.  Since 2026-11-02 (user 「为什么不是
#: IFS+GDPS」) the day's forecast is the peak of the members' hourly mean, the curve the board draws, as
#: the IFS+GDPS nowcast: on the 8 closed days then on the board the two differ by <=0.21 (MAE 0.74 vs
#: 0.75).  Earlier runs are single GFS runs read as before.
FCST_MEMBERS = ("ifs", "gdps")
#: Two-driver runs for the days before the switch (user 2026-10-31 「接回放+补 00/12Z」): the four
#: replayed inits linked in and the 00/12Z cycles 10-26..10-30 re-run, under ``run/fcst_backfill``
#: laid out as ``run/fcst``.  Their own series, never the live forecast's history or score: the
#: replayed days are the ones that chose the drivers.
FORECAST_BF = "fcst_bf"
FCST_CONFIGS = (FORECAST, FORECAST_BF)
#: The same-day nowcast forward test (run/nowcast.sh, user 2026-10-22 「做当天临近预报的前向测试」):
#: two arms per day under ``run/nowcast/<YYYYMMDD>/{sat,ctl}`` -- Himawari cloud in the
#: radiation up to the latest scan at issue time, and the same run without it -- and
#: ``run/nowcast/log.jsonl``, one record per issue with the market and the served belief
#: read at the moment the forecast became available.
#: Every forecast init's daily maxima, kept after its run directory is removed
#: (forecast3d.sh keeps seven days of runs): how each day's forecast moved run by run.
FCST_RUNS_PATH = board_readings.READINGS_DIR / "wrf_fcst_runs_v1.json"
NOWCAST_ARMS = {"sat": "nowcast_sat", "ctl": "nowcast_ctl"}
NOWCAST_LOG = "log.jsonl"
#: The sat arm re-run on every new scan (run/nowcast_update.sh, user 2026-10-25 「不应该只跑一次，
#: 有新数据就应该跑」): ``run/nowcast/<YYYYMMDD>/upd/<scan HH>``, one record per run in
#: ``run/nowcast/log_update.jsonl``.  The frozen issue above stays the forward test and its score.
NOWCAST_UPDATE_LOG = "log_update.jsonl"
#: The strict as-of replay of the frozen nowcast (run/replay_nowcast.sh, user 2026-10-25 「做严格回放：
#: 用冻结配置、每天只用 11:10 前已有的 GFS 前一日 18Z 和 02Z 前的云图，回放 10-01 至 10-24，单独成段，不计入
#: 前向天数」), with the aerosol arm (「测试打开气溶胶（雾霾）的效果」): ``run/replay/log.jsonl``, its own
#: segment and score, never the forward test's.
REPLAY_LOG = "replay/log.jsonl"
#: The same-day nowcast driven by IFS + GDPS (run/nowcast_ig.sh, user 2026-11-01 「另立一路」): the
#: newest live three-day forecast's two members carried on from their 00Z restart with the satellite
#: cloud, one record per scan in ``run/nowcast_ig/log.jsonl``.  Its own forward series: scored, with
#: the GFS route beside it, on what each said as of IG_SCORE_AT_UTC (12:00 local, before the peak).
NOWCAST_IG_LOG = "nowcast_ig/log.jsonl"
#: The observation archive (reported_trajectory_observations): the airport's reports for the today chart.
ARCHIVE_DB = ROOT / "data" / "archive.db"
IG_SCORE_AT_UTC = "04:00"
REPLAY_ARMS = ("sat", "ctl", "sat_aer")
#: The hindcast experiments' results frozen before their files were archived (user
#: 2026-10-22 「保留代码和结果，归档实验数据，冻结配置！」): a run absent on disk is filled
#: from here, so the board keeps every hindcast number.
FROZEN_HINDCAST = "hindcast_frozen_v1.json"
#: A run whose log moved within this many seconds and has not said SUCCESS is running.
RUNNING_WITHIN_SEC = 1800
#: The local hours a day's maximum must be covered through to count as complete.
PEAK_HOURS_LOCAL = (10, 18)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_dirs(root: Path = WRF_ROOT) -> list[tuple[str, str, Path]]:
    """(config, YYYYMMDD, directory) for every run directory holding output."""

    out = []
    for d in sorted(glob.glob(str(root / "run" / "days" / "*" / "wrf"))):
        out.append((BASELINE, Path(d).parent.name, Path(d)))
    for d in sorted(glob.glob(str(root / "run" / "exp" / "*" / "2*"))):
        out.append((Path(d).parent.name, Path(d).name, Path(d)))
    for sub, config in (("fcst", FORECAST), ("fcst_backfill", FORECAST_BF)):
        fcst = []
        for d in glob.glob(str(root / "run" / sub / "2*_from_*")):
            last, _, init = Path(d).name.partition("_from_")
            if len(init) == 10 and init.isdigit() and len(last) == 8 and last.isdigit():
                fcst.append((init, last, Path(d)))
        # Oldest init first: compute() lets a later init replace an earlier one only
        # for the days it has reached.
        for init, last, d in sorted(fcst):
            day, end = datetime.strptime(init[:8], "%Y%m%d"), datetime.strptime(last, "%Y%m%d")
            while day <= end:
                out.append((config, f"{day:%Y%m%d}", d))
                day += timedelta(days=1)
    for d in sorted(glob.glob(str(root / "run" / "nowcast" / "2*"))):
        for arm, config in NOWCAST_ARMS.items():
            out.append((config, Path(d).name, Path(d) / arm))
    return [(c, day, d) for c, day, d in out if len(day) == 8 and day.isdigit()
            and (glob.glob(str(d / "wrfout_d0*")) or any(glob.glob(str(d / m / "wrfout_d0*")) for m in FCST_MEMBERS))]


def _members(d: Path) -> list[Path]:
    """The per-driver runs of a two-driver forecast, or [] for a single run."""

    return [d / m for m in FCST_MEMBERS if (d / m).is_dir()]


def _status(d: Path, now: float) -> str:
    log = d / "wrf.out"
    if not log.exists():
        log = d / "rsl.out.0000"
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            tail = f.read().decode("utf-8", "ignore")
        mtime = log.stat().st_mtime
    except OSError:
        return "unknown"
    if "SUCCESS COMPLETE WRF" in tail:
        return "complete"
    return "running" if now - mtime < RUNNING_WITHIN_SEC else "stopped"


def _nearest(lat, lon, point: tuple[float, float]):
    import numpy as np  # noqa: PLC0415

    j, i = np.unravel_index(np.argmin((lat - point[0]) ** 2 + (lon - point[1]) ** 2), lat.shape)
    return int(j), int(i)


def _file_frames(path: str, var: str, point: tuple[float, float]) -> dict[str, float]:
    """{'YYYY-MM-DD_HH:MM:SS': value °C} at the nearest cell; frames that do
    not read (a file still being written) are left out."""

    import h5py  # noqa: PLC0415

    out: dict[str, float] = {}
    with h5py.File(path, "r") as f:
        lat = f["XLAT"][0] if "XLAT" in f else f["XLAT_M"][0]
        lon = f["XLONG"][0] if "XLONG" in f else f["XLONG_M"][0]
        j, i = _nearest(lat, lon, point)
        # Frame by frame: a file cut off mid-write (a run stopped at a chunk deadline)
        # keeps every frame written before the cut.
        times = []
        for k in range(f["Times"].shape[0]):
            try:
                t = f["Times"][k].tobytes().decode()
                # a run killed at its chunk alarm leaves the slot it was writing as NUL bytes
                _utc(t)
            except (OSError, ValueError):
                break
            times.append(t)
        for k, t in enumerate(times):
            try:
                v = f[var][k, 0, j, i] if var == "TT" else f[var][k, j, i]
            except (OSError, KeyError, ValueError):
                continue
            # ... or with its stamp written and the field still the netCDF fill (9.97e36); the
            # next chunk restarts at that time and writes it
            if not math.isfinite(float(v)) or float(v) > 1e30:
                continue
            out[t] = round(float(v) - 273.15, 2)
    return out


class _Cache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        try:
            self.rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.rows = {}
        self.used: set[str] = set()
        self.read = 0

    def frames(self, path: str, var: str, point: tuple[float, float]) -> dict[str, float]:
        try:
            st = os.stat(path)
        except OSError:
            return {}
        key = f"{path}|{var}|{point[0]},{point[1]}"
        stamp = [st.st_size, st.st_mtime_ns]
        self.used.add(key)
        hit = self.rows.get(key)
        if hit and hit.get("stamp") == stamp:
            return hit["frames"]
        try:
            frames = _file_frames(path, var, point)
        except (OSError, KeyError, ValueError):
            frames = {}
        self.read += 1
        self.rows[key] = {"stamp": stamp, "frames": frames}
        return frames

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: v for k, v in self.rows.items() if k in self.used}
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        tmp.replace(self.path)


def _utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)


def _day_max(frames: dict[str, float], day: str, zone: ZoneInfo) -> dict[str, Any] | None:
    """The local day's maximum, its local time and whether the peak hours are covered."""

    local = [(_utc(t).astimezone(zone), v) for t, v in frames.items()]
    local = sorted((t, v) for t, v in local if t.date().isoformat() == day)
    if not local:
        return None
    t_max, v_max = max(local, key=lambda x: x[1])
    hours = {t.hour for t, _ in local}
    complete = all(h in hours for h in range(PEAK_HOURS_LOCAL[0], PEAK_HOURS_LOCAL[1] + 1))
    return {"tmax_c": v_max, "at_local": t_max.strftime("%H:%M"), "peak_covered": complete,
            "last_local": local[-1][0].strftime("%H:%M"),
            "series": [[_iso(t), v] for t, v in local]}


def _domain_files(d: Path) -> tuple[str, list[str]]:
    """The finest domain with output in the run (a nest covers the station)."""

    for dom in ("d03", "d02", "d01"):
        files = sorted(glob.glob(str(d / f"wrfout_{dom}_*")))
        if files:
            return dom, files
    return "d01", []


def _truth(conn: sqlite3.Connection, station: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for day, value in conn.execute(
        "SELECT local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE station_id=? AND source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED'"
        " AND local_date>='2026-09-01' ORDER BY revision_no", (station,),
    ):
        out[str(day)] = float(value)
    return out


def _zone(conn: sqlite3.Connection, station: str) -> ZoneInfo | None:
    row = conn.execute(
        "SELECT settlement_timezone FROM calibration_station_market_timezone_binding WHERE station_id=?"
        " ORDER BY bound_at_utc DESC LIMIT 1", (station,)).fetchone()
    return ZoneInfo(row[0]) if row else None


def _stats(errs: list[float]) -> dict[str, Any]:
    if not errs:
        return {"n": 0}
    return {"n": len(errs), "bias_c": round(sum(errs) / len(errs), 2),
            "mae_c": round(sum(abs(e) for e in errs) / len(errs), 2),
            "hit": sum(1 for e in errs if abs(e) < 0.5) / len(errs)}


def _paired(diffs: list[float]) -> dict[str, Any]:
    """|this − truth| − |sat − truth| over the same days: mean, sd and t (negative = this
    arm nearer the truth)."""

    import math  # noqa: PLC0415

    n = len(diffs)
    if n < 2:
        return {"n": n}
    mean = sum(diffs) / n
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1))
    return {"n": n, "mean_c": round(mean, 3), "sd_c": round(sd, 3),
            "t": None if sd == 0 else round(mean / (sd / math.sqrt(n)), 2)}


def nowcast_records(root: Path = WRF_ROOT) -> dict[str, dict[str, Any]]:
    """day (YYYY-MM-DD) -> the day's latest nowcast record (market and served belief as of
    the moment the forecast became available)."""

    out: dict[str, dict[str, Any]] = {}
    try:
        lines = (root / "run" / "nowcast" / NOWCAST_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("day"):
            out[str(r["day"])] = r
    return out


def nowcast_updates(root: Path = WRF_ROOT) -> dict[str, list[dict[str, Any]]]:
    """day (YYYY-MM-DD) -> the day's update records, oldest first."""

    out: dict[str, list[dict[str, Any]]] = {}
    try:
        lines = (root / "run" / "nowcast" / NOWCAST_UPDATE_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("day") and r.get("ready_utc"):
            out.setdefault(str(r["day"]), []).append(r)
    for recs in out.values():
        recs.sort(key=lambda r: str(r["ready_utc"]))
    return out


def nowcast_ig_updates(root: Path = WRF_ROOT) -> dict[str, list[dict[str, Any]]]:
    """day (YYYY-MM-DD) -> the day's IFS+GDPS nowcast records, oldest first."""

    out: dict[str, list[dict[str, Any]]] = {}
    try:
        lines = (root / "run" / NOWCAST_IG_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("day") and r.get("ready_utc"):
            out.setdefault(str(r["day"]), []).append(r)
    for recs in out.values():
        recs.sort(key=lambda r: str(r["ready_utc"]))
    return out


def _ig_series(rec: dict[str, Any]) -> list[list[Any]]:
    """The mean of the members' satellite-run T2 by UTC hour, where every member has the hour."""

    per = [((v or {}).get("hourly") or {}) for v in (rec.get("wrf_sat_members") or {}).values()]
    if not per:
        return []
    hours = sorted(set.intersection(*(set(h) for h in per)))
    return [[f"{rec['day']}T{hh}:00:00Z", round(sum(float(h[hh]) for h in per) / len(per), 2)] for hh in hours]


def _ig_view(rec: dict[str, Any]) -> dict[str, Any]:
    """An IFS+GDPS record's view, its maximum the highest hour of the members' hourly mean -- the peak of
    the curve the board draws (user 2026-11-02 「曲线是上20.3,数据不一致」; the record's own field is the
    mean of the members' maxima, which peak at different hours), when every member covers 01..12Z."""

    v = _nowcast_view(rec)
    for arm in ("sat", "ctl"):
        per = [((m or {}).get("hourly") or {}) for m in (rec.get(f"wrf_{arm}_members") or {}).values()]
        full = bool(per) and all(all(f"{h:02d}" in m for h in range(1, 13)) for m in per)
        hours = sorted(set.intersection(*(set(m) for m in per))) if full else []
        top = max(sum(float(m[hh]) for m in per) / len(per) for hh in hours) if hours else None
        v[f"{arm}_c"] = None if top is None else round(top, 2)
        obs = rec.get("metar_max_so_far_c")
        v[f"{arm}_obs_c"] = None if top is None else round(max(top, -99 if obs is None else float(obs)), 2)
    return v


def _obs_series(station: str, day: str, zone: ZoneInfo, path: Path = ARCHIVE_DB) -> list[list[Any]]:
    """The station's reported temperatures over its local day, from the observation archive
    (read only; empty when it cannot be read)."""

    start = datetime.fromisoformat(day).replace(tzinfo=zone).astimezone(timezone.utc)
    lo, hi = start.strftime("%Y-%m-%dT%H:%M"), (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT valid_at, reported_temperature_c FROM reported_trajectory_observations"
                " WHERE station_id=? AND valid_at>=? AND valid_at<? AND reported_temperature_c IS NOT NULL"
                " ORDER BY valid_at", (station, lo, hi)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out: dict[str, float] = {}
    for t, v in rows:
        out[str(t)[:16]] = float(v)   # a re-sent report: the last one
    return [[f"{t}:00Z", v] for t, v in sorted(out.items())]


def _as_of(recs: list[dict[str, Any]], at: str) -> dict[str, Any] | None:
    """The newest record out by ``at`` (ISO UTC), records sorted by ready time."""

    out = None
    for r in recs:
        if str(r.get("ready_utc") or "") <= at:
            out = r
    return out


def replay_records(root: Path = WRF_ROOT) -> dict[str, dict[str, Any]]:
    """day -> the day's newest replay record."""

    out: dict[str, dict[str, Any]] = {}
    try:
        lines = (root / "run" / REPLAY_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("day"):
            out[str(r["day"])] = r
    return out


def nowcast_issues(root: Path = WRF_ROOT) -> dict[str, list[dict[str, Any]]]:
    """day -> every nowcast record, the frozen issue and its updates, by ready time: what
    the newest WRF said as of any moment."""

    out = {day: [rec] for day, rec in nowcast_records(root).items()}
    for day, recs in nowcast_updates(root).items():
        out.setdefault(day, []).extend(recs)
    for recs in out.values():
        recs.sort(key=lambda r: str(r.get("ready_utc") or ""))
    return out


def _nowcast_view(rec: dict[str, Any]) -> dict[str, Any]:
    m, b = rec.get("market") or {}, rec.get("belief") or {}
    return {
        "ready_utc": rec.get("ready_utc"), "last_sat_hour_utc": rec.get("last_sat_hour_utc"),
        "last_scan_utc": rec.get("last_scan_utc") or (None if rec.get("last_sat_hour_utc") is None
                                                      else f"{int(rec['last_sat_hour_utc']):02d}:00"),
        "metar_max_so_far_c": rec.get("metar_max_so_far_c"),
        "sat_c": rec.get("wrf_sat_tmax_c"), "ctl_c": rec.get("wrf_ctl_tmax_c"),
        "sat_obs_c": rec.get("wrf_sat_tmax_with_obs_c"), "ctl_obs_c": rec.get("wrf_ctl_tmax_with_obs_c"),
        "market_mean_c": m.get("mean_c"), "market_mode_c": m.get("mode_c"), "market_mode_p": m.get("mode_p"),
        "belief_mean_c": b.get("mean_c"), "belief_mode_c": b.get("mode_c"), "belief_mode_p": b.get("mode_p"),
        "belief_stage": b.get("stage"), "kind": rec.get("kind") or "frozen",
    }


def load_fcst_runs(path: Path = FCST_RUNS_PATH) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_fcst_runs(runs: dict[str, Any], path: Path = FCST_RUNS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(runs, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def compute(conn: sqlite3.Connection, *, root: Path = WRF_ROOT, now: datetime | None = None,
            cache: _Cache | None = None, fcst_runs: dict[str, Any] | None = None) -> dict[str, Any]:
    """``fcst_runs`` (station -> init -> day -> maximum) is updated in place with
    every forecast run read now; the caller persists it."""

    now = now or _now()
    fcst_runs = {} if fcst_runs is None else fcst_runs
    if not (root / "run").is_dir():
        return {"state": "volume_missing", "root": str(root)}
    cache = cache or _Cache()
    runs = run_dirs(root)
    stations: dict[str, Any] = {}
    for station, point in STATIONS.items():
        zone = _zone(conn, station)
        if zone is None:
            stations[station] = {"state": "no_timezone"}
            continue
        truth = _truth(conn, station)
        days: dict[str, dict[str, Any]] = {}
        errs: dict[str, list[float]] = {}
        gfs_errs: list[float] = []
        bf_runs: dict[str, dict[str, Any]] = {}  # the backfill is re-read every time, never persisted
        for config, ymd, d in runs:
            day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            members = _members(d) if config in FCST_CONFIGS else []
            member_max: dict[str, float] = {}
            if members:
                # Every member must reach the day; the day's maximum is the peak of the members'
                # hourly mean, each member's own maximum kept beside it.
                per = []
                for md in members:
                    dom, files = _domain_files(md)
                    mf: dict[str, float] = {}
                    for fn in files:
                        for t, v in cache.frames(fn, "T2", point).items():
                            mf.setdefault(t, v)
                    per.append((md, mf, _day_max(mf, day, zone)))
                if len(per) != len(FCST_MEMBERS) or any(mm is None for _, _, mm in per):
                    continue
                common = set.intersection(*(set(mf) for _, mf, _ in per))
                frames = {t: sum(mf[t] for _, mf, _ in per) / len(per) for t in common}
                m = _day_max(frames, day, zone)
                if m is None:
                    continue
                member_max = {md.name: round(mm["tmax_c"], 2) for md, _, mm in per}
                m["tmax_c"] = round(m["tmax_c"], 2)
                m["peak_covered"] = all(mm["peak_covered"] for _, _, mm in per)
                st = [_status(md, now.timestamp()) for md, _, _ in per]
                status = "complete" if all(s == "complete" for s in st) else "running" if "running" in st else "stopped"
            else:
                dom, files = _domain_files(d)
                frames = {}
                for fn in files:
                    for t, v in cache.frames(fn, "T2", point).items():
                        frames.setdefault(t, v)
                m = _day_max(frames, day, zone)
                if m is None:
                    continue
                status = _status(d, now.timestamp())
            init = min(frames) if frames else None
            if config in FCST_CONFIGS:
                ic = d.name.partition("_from_")[2]
                init = f"{ic[:4]}-{ic[4:6]}-{ic[6:8]}_{ic[8:10]}:00:00"
            if config in NOWCAST_ARMS.values():
                # Initial state = the previous day's 18Z GFS at its 6 h step.
                init = f"{(datetime.strptime(ymd, '%Y%m%d') - timedelta(days=1)):%Y-%m-%d}_18:00:00"
            rec = {"status": status, "domain": dom, "init_utc": _iso(_utc(init)) if init else None,
                   **{k: m[k] for k in ("tmax_c", "at_local", "peak_covered", "last_local", "series")}}
            if member_max:
                rec["members"] = member_max
                # each member's own hourly curve, for the today card's chart
                rec["member_series"] = {md.name: [[t, round(v, 2)] for t, v in mm["series"]] for md, _, mm in per}
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            # The driving GFS: a newer forecast init replaces an older one's for the day.  A day
            # with no baseline run (10-21, run only for the hindcast table, user 2026-10-22
            # 「缺10.14-10.21」) reads it from the no-satellite run's inputs, the same GFS 00Z.
            if (config in (BASELINE, "indep_rhc85") and "gfs" not in entry) or config == FORECAST:
                gfs: dict[str, float] = {}
                for fn in sorted(glob.glob(str(d / "met_em.d01.*"))):
                    gfs.update(cache.frames(fn, "TT", point))
                g = _day_max(gfs, day, zone)
                if g:
                    entry["gfs"] = {"tmax_c": g["tmax_c"], "at_local": g["at_local"]}
            if config in FCST_CONFIGS:
                (fcst_runs.setdefault(station, {}) if config == FORECAST else bf_runs).setdefault(rec["init_utc"], {})[day] = {
                    k: rec[k] for k in ("tmax_c", "at_local", "peak_covered", "status", "members") if k in rec}
                # A newer init stands for the day only once it covers the day's peak hours, or
                # when the older one did not either: the 10-24 12Z run starts at 20:00 local and
                # must not replace 10-22 00Z's whole-day 10-24 maximum with an evening reading.
                held = entry["configs"].get(config)
                if not held or rec["peak_covered"] or not held.get("peak_covered"):
                    entry["configs"][config] = rec
                continue  # scored below, once per day on the init that stands
            entry["configs"][config] = rec
            if entry["truth_c"] is not None and status == "complete" and m["peak_covered"]:
                rec["error_c"] = round(m["tmax_c"] - entry["truth_c"], 2)
                errs.setdefault(config, []).append(rec["error_c"])
        # Run by run: each day's forecast from every init that covered its peak hours,
        # and the standing run's change against the one before it.
        for config, src, key in ((FORECAST, fcst_runs.get(station) or {}, "fcst_history"), (FORECAST_BF, bf_runs, "fcst_bf_history")):
            by_day: dict[str, list[list[Any]]] = {}
            for init, per_day in sorted(src.items()):
                for day, r in per_day.items():
                    if r.get("peak_covered"):
                        by_day.setdefault(day, []).append([init, r["tmax_c"]])
            for day, hist in by_day.items():
                entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
                entry[key] = hist
                rec = entry["configs"].get(config)
                if rec and rec.get("peak_covered"):
                    before = [h for h in hist if h[0] < rec["init_utc"]]
                    if before:
                        rec["prev_init_utc"], rec["prev_tmax_c"] = before[-1]
                        rec["change_c"] = round(rec["tmax_c"] - before[-1][1], 2)
        for entry in days.values():
            if entry.get("gfs") and entry["truth_c"] is not None:
                gfs_errs.append(entry["gfs"]["tmax_c"] - entry["truth_c"])
            for config in FCST_CONFIGS:
                rec = entry["configs"].get(config)
                if rec and entry["truth_c"] is not None and rec["status"] == "complete" and rec["peak_covered"]:
                    rec["error_c"] = round(rec["tmax_c"] - entry["truth_c"], 2)
                    errs.setdefault(config, []).append(rec["error_c"])
        try:
            frozen = json.loads((root / "run" / FROZEN_HINDCAST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            frozen = {}
        for day, F in (((frozen.get("stations") or {}).get(station) or {}).get("days") or {}).items():
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            if F.get("gfs") and "gfs" not in entry:
                entry["gfs"] = F["gfs"]
                if entry["truth_c"] is not None:
                    gfs_errs.append(F["gfs"]["tmax_c"] - entry["truth_c"])
            for config, rec in (F.get("configs") or {}).items():
                if config in entry["configs"]:
                    continue  # still on disk: the live read wins
                rec = {**rec, "frozen": True}
                rec.pop("error_c", None)
                if entry["truth_c"] is not None and rec.get("status") == "complete" and rec.get("peak_covered"):
                    rec["error_c"] = round(rec["tmax_c"] - entry["truth_c"], 2)
                    errs.setdefault(config, []).append(rec["error_c"])
                entry["configs"][config] = rec
        # Same-moment comparison (forward test): WRF arms (raw and with the observed
        # maximum so far), market and served belief, each as of the record's ready_utc.
        nowcast_errs: dict[str, list[float]] = {}
        for day, rec in nowcast_records(root).items():
            if rec.get("station", station) != station:
                continue
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            entry["nowcast"] = _nowcast_view(rec)
            t = entry["truth_c"]
            if t is None:
                continue
            for k in ("sat_obs_c", "ctl_obs_c", "market_mean_c", "belief_mean_c"):
                v = entry["nowcast"][k]
                if v is not None:
                    nowcast_errs.setdefault(k, []).append(float(v) - t)
        # The re-runs on newer scans: every one's numbers, and the newest run's hourly T2.
        # Not scored -- each issue is a different moment; the frozen issue is the test.
        for day, recs in nowcast_updates(root).items():
            recs = [r for r in recs if r.get("station", station) == station]
            if not recs:
                continue
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            entry["nowcast_updates"] = [_nowcast_view(r) for r in recs]
            last = recs[-1]
            d = root / "run" / "nowcast" / day.replace("-", "") / str(last.get("run") or "")
            frames: dict[str, float] = {}
            for fn in _domain_files(d)[1]:
                frames.update(cache.frames(fn, "T2", point))
            m = _day_max(frames, day, zone)
            if m:
                entry["nowcast_update_run"] = {
                    "status": _status(d, now.timestamp()), "domain": "d01", "run": last.get("run"),
                    "init_utc": _iso(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(hours=6)),
                    **{k: m[k] for k in ("tmax_c", "at_local", "peak_covered", "last_local", "series")}}
        # The IFS+GDPS route: every version, each member's maximum, and its source cycle; scored
        # with the GFS route's newest version (frozen issue or re-run) as of the same moment.
        ig_errs: dict[str, list[float]] = {}
        ig_pairs: list[float] = []
        gfs_issues = nowcast_issues(root)
        for day, recs in nowcast_ig_updates(root).items():
            recs = [r for r in recs if r.get("station", station) == station]
            if not recs:
                continue
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            entry["nowcast_ig_updates"] = [
                {**_ig_view(r), "source_cycle_utc": r.get("source_cycle_utc"),
                 "members_sat_c": {m: (v or {}).get("tmax_c") for m, v in (r.get("wrf_sat_members") or {}).items()},
                 "members_ctl_c": {m: (v or {}).get("tmax_c") for m, v in (r.get("wrf_ctl_members") or {}).items()},
                 "series": _ig_series(r)}
                for r in recs]
            t = entry["truth_c"]
            at = f"{day}T{IG_SCORE_AT_UTC}:00Z"
            ig = _as_of(recs, at)
            if t is None or ig is None:
                continue
            v = _ig_view(ig)
            for k in ("sat_obs_c", "ctl_obs_c", "market_mean_c", "belief_mean_c"):
                if v[k] is not None:
                    ig_errs.setdefault(k, []).append(float(v[k]) - t)
            g = _as_of([r for r in gfs_issues.get(day, []) if r.get("station", station) == station], at)
            gv = (g or {}).get("wrf_sat_tmax_with_obs_c")
            if gv is not None:
                ig_errs.setdefault("gfs_sat_obs_c", []).append(float(gv) - t)
                if v["sat_obs_c"] is not None:
                    ig_pairs.append(abs(float(v["sat_obs_c"]) - t) - abs(float(gv) - t))
        # The airport's reports through today (the today card's hourly chart): yesterday on.
        if zone is not None:
            since = (now.astimezone(zone) - timedelta(days=1)).date().isoformat()
            for day in [d for d in days if d >= since]:
                obs = _obs_series(station, day, zone)
                if obs:
                    days[day]["obs_series"] = obs
        # The replay segment: each arm with the observed maximum so far, the market and the
        # served belief as of the nominal issue, scored on their own; and each arm paired
        # against the sat arm on the same days.
        replay_errs: dict[str, list[float]] = {}
        replay_pairs: dict[str, list[float]] = {}
        for day, rec in replay_records(root).items():
            if rec.get("station", station) != station:
                continue
            m, b = rec.get("market") or {}, rec.get("belief") or {}
            view = {f"{arm}_obs_c": rec.get(f"wrf_{arm}_tmax_with_obs_c") for arm in REPLAY_ARMS}
            view.update({f"{arm}_c": rec.get(f"wrf_{arm}_tmax_c") for arm in REPLAY_ARMS})
            view.update({"ready_utc": rec.get("ready_utc"), "last_sat_hour_utc": rec.get("last_sat_hour_utc"),
                         "metar_max_so_far_c": rec.get("metar_max_so_far_c"),
                         "market_mean_c": m.get("mean_c"), "belief_mean_c": b.get("mean_c"), "belief_stage": b.get("stage")})
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            entry["replay"] = view
            t = entry["truth_c"]
            if t is None:
                continue
            for k in [f"{arm}_obs_c" for arm in REPLAY_ARMS] + ["market_mean_c", "belief_mean_c"]:
                if view.get(k) is not None:
                    replay_errs.setdefault(k, []).append(float(view[k]) - t)
            if view.get("sat_obs_c") is not None:
                for k in [f"{arm}_obs_c" for arm in REPLAY_ARMS if arm != "sat"] + ["market_mean_c", "belief_mean_c"]:
                    if view.get(k) is not None:
                        replay_pairs.setdefault(k, []).append(abs(float(view[k]) - t) - abs(float(view["sat_obs_c"]) - t))
        configs = sorted({c for e in days.values() for c in e["configs"]},
                         key=lambda c: (c != "nowcast_sat", c != "nowcast_ctl", c != FORECAST, c != FORECAST_BF, c != BASELINE, c))
        latest = max(days) if days else None
        stations[station] = {
            "state": "ok" if days else "no_runs", "timezone": str(zone), "point": list(point),
            "configs": configs, "days": dict(sorted(days.items())), "latest_day": latest,
            "scores": {c: _stats(errs.get(c, [])) for c in configs},
            "gfs_score": _stats(gfs_errs),
            "nowcast_scores": {k: _stats(v) for k, v in nowcast_errs.items()},
            "nowcast_ig_scores": {"at_utc": IG_SCORE_AT_UTC, **{k: _stats(v) for k, v in ig_errs.items()},
                                  "ig_vs_gfs": _paired(ig_pairs)},
            "replay_scores": {k: _stats(v) for k, v in replay_errs.items()},
            "replay_vs_sat": {k: _paired(v) for k, v in replay_pairs.items()},
        }
    return {"state": "ok", "root": str(root), "as_of_utc": _iso(now), "files_read": cache.read,
            "running": [f"{c}/{ymd}" for c, ymd, d in runs if _status(d, now.timestamp()) == "running"],
            "stations": stations}


def stored(station: str, day: str) -> dict[str, Any] | None:
    """The station board's WRF card (user 2026-10-22 「在上方看板增加一个卡片」): the
    stored reading's view of one station-day, read only -- None for a station WRF
    does not run, ``state`` for a missing reading or day."""

    if station not in STATIONS:
        return None
    doc = board_readings.load_reading(READING_NAME)
    value = (doc or {}).get("value") if doc and "value" in doc else doc
    if not value or value.get("state") != "ok":
        return {"state": (value or {}).get("state") or "not_computed"}
    S = (value.get("stations") or {}).get(station) or {}
    E = (S.get("days") or {}).get(day)
    if not E:
        return {"state": "no_run"}
    configs = E.get("configs") or {}
    keep = {c: {k: configs[c].get(k) for k in ("tmax_c", "at_local", "status", "series", "init_utc", "last_local",
                                                "peak_covered", "prev_init_utc", "prev_tmax_c", "change_c",
                                                "members", "member_series")}
            for c in ("nowcast_sat", "nowcast_ctl", FORECAST) if c in configs}
    scores = S.get("scores") or {}
    # The card leads with the newest issue by ready time: a re-run on a newer scan (user
    # 2026-10-25 「有新数据就应该跑」) or an early issue before 11:10 (user 2026-10-26 「在 11:10 之前
    # 加两轮带卫星云的早版」) when it came out after the frozen 11:10 issue or there is none yet.
    nowcast, updates = E.get("nowcast"), E.get("nowcast_updates") or []
    if updates and E.get("nowcast_update_run") and (
            not nowcast or str(updates[-1].get("ready_utc") or "") > str(nowcast.get("ready_utc") or "")):
        nowcast = updates[-1]
        keep["nowcast_sat"] = {k: E["nowcast_update_run"].get(k) for k in keep.get("nowcast_sat") or E["nowcast_update_run"]}
    return {"state": "ok", "as_of_utc": value.get("as_of_utc"), "timezone": S.get("timezone"),
            "truth_c": E.get("truth_c"), "gfs": E.get("gfs"), "nowcast": nowcast, "runs": keep,
            "nowcast_first": E.get("nowcast"), "nowcast_updates": updates,
            "nowcast_ig_updates": E.get("nowcast_ig_updates") or [],
            "fcst_history": E.get("fcst_history") or [],
            "nowcast_scores": S.get("nowcast_scores") or {},
            "oracle_score": scores.get("satcld_rhc85"), "selfcloud_score": scores.get("indep_rhc85"),
            # the station board's three-day block: the reports on the local day, both routes' and the
            # three-day forecast's forward scores
            "obs_series": E.get("obs_series") or [], "nowcast_ig_scores": S.get("nowcast_ig_scores") or {},
            "fcst_score": scores.get(FORECAST)}


def run(*, db: Path = board_readings.DEFAULT_DB) -> dict[str, Any]:
    t0 = time.monotonic()
    cache = _Cache()
    fcst_runs = load_fcst_runs()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        value = compute(conn, cache=cache, fcst_runs=fcst_runs)
    finally:
        conn.close()
    if value.get("state") == "ok":
        cache.save()
        save_fcst_runs(fcst_runs)
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    board_readings.write_reading(READING_NAME, value, code=code, build_sec=time.monotonic() - t0)
    return value


def main() -> None:
    value = run()
    print(json.dumps({"state": value.get("state"), "files_read": value.get("files_read"),
                      "running": value.get("running")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
