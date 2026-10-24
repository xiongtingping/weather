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
CACHE_PATH = ROOT / "data" / "runtime" / "wrf_readings" / "frames_v1.json"
READING_NAME = "wrf_tmax"
#: The station point the WRF project's own tools read (run/tools/zuuu_t2.py).
STATIONS = {"ZUUU": (30.578, 103.947)}
BASELINE = "baseline"
#: The live forecasts, ``run/fcst/<last day YYYYMMDD>_from_<init YYYYMMDDHH>``
#: (run/forecast3d.sh, user 2026-10-22 「把未来3日都计算」: one run from the day's
#: GFS 00Z to three days ahead 12Z): every local day from the init's date to the last
#: day is read from the run, and for each day the newest init that reached it is shown.
FORECAST = "fcst"
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
    fcst = []
    for d in glob.glob(str(root / "run" / "fcst" / "2*_from_*")):
        last, _, init = Path(d).name.partition("_from_")
        if len(init) == 10 and init.isdigit() and len(last) == 8 and last.isdigit():
            fcst.append((init, last, Path(d)))
    # Oldest init first: compute() lets a later init replace an earlier one only
    # for the days it has reached.
    for init, last, d in sorted(fcst):
        day, end = datetime.strptime(init[:8], "%Y%m%d"), datetime.strptime(last, "%Y%m%d")
        while day <= end:
            out.append((FORECAST, f"{day:%Y%m%d}", d))
            day += timedelta(days=1)
    for d in sorted(glob.glob(str(root / "run" / "nowcast" / "2*"))):
        for arm, config in NOWCAST_ARMS.items():
            out.append((config, Path(d).name, Path(d) / arm))
    return [(c, day, d) for c, day, d in out if len(day) == 8 and day.isdigit() and glob.glob(str(d / "wrfout_d0*"))]


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
                times.append(f["Times"][k].tobytes().decode())
            except (OSError, ValueError):
                break
        for k, t in enumerate(times):
            try:
                v = f[var][k, 0, j, i] if var == "TT" else f[var][k, j, i]
            except (OSError, KeyError, ValueError):
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
        for config, ymd, d in runs:
            day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            dom, files = _domain_files(d)
            frames: dict[str, float] = {}
            for fn in files:
                for t, v in cache.frames(fn, "T2", point).items():
                    frames.setdefault(t, v)
            m = _day_max(frames, day, zone)
            if m is None:
                continue
            status = _status(d, now.timestamp())
            init = min(frames) if frames else None
            if config == FORECAST:
                ic = d.name.partition("_from_")[2]
                init = f"{ic[:4]}-{ic[4:6]}-{ic[6:8]}_{ic[8:10]}:00:00"
            if config in NOWCAST_ARMS.values():
                # Initial state = the previous day's 18Z GFS at its 6 h step.
                init = f"{(datetime.strptime(ymd, '%Y%m%d') - timedelta(days=1)):%Y-%m-%d}_18:00:00"
            rec = {"status": status, "domain": dom, "init_utc": _iso(_utc(init)) if init else None,
                   **{k: m[k] for k in ("tmax_c", "at_local", "peak_covered", "last_local", "series")}}
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
            if config == FORECAST:
                fcst_runs.setdefault(station, {}).setdefault(rec["init_utc"], {})[day] = {
                    k: rec[k] for k in ("tmax_c", "at_local", "peak_covered", "status")}
                # A newer init stands for the day only once it covers the day's peak hours, or
                # when the older one did not either: the 10-24 12Z run starts at 20:00 local and
                # must not replace 10-22 00Z's whole-day 10-24 maximum with an evening reading.
                held = entry["configs"].get(FORECAST)
                if not held or rec["peak_covered"] or not held.get("peak_covered"):
                    entry["configs"][config] = rec
                continue  # scored below, once per day on the init that stands
            entry["configs"][config] = rec
            if entry["truth_c"] is not None and status == "complete" and m["peak_covered"]:
                rec["error_c"] = round(m["tmax_c"] - entry["truth_c"], 2)
                errs.setdefault(config, []).append(rec["error_c"])
        # Run by run: each day's forecast from every init that covered its peak hours,
        # and the standing run's change against the one before it.
        by_day: dict[str, list[list[Any]]] = {}
        for init, per_day in sorted((fcst_runs.get(station) or {}).items()):
            for day, r in per_day.items():
                if r.get("peak_covered"):
                    by_day.setdefault(day, []).append([init, r["tmax_c"]])
        for day, hist in by_day.items():
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            entry["fcst_history"] = hist
            rec = entry["configs"].get(FORECAST)
            if rec and rec.get("peak_covered"):
                before = [h for h in hist if h[0] < rec["init_utc"]]
                if before:
                    rec["prev_init_utc"], rec["prev_tmax_c"] = before[-1]
                    rec["change_c"] = round(rec["tmax_c"] - before[-1][1], 2)
        for entry in days.values():
            if entry.get("gfs") and entry["truth_c"] is not None:
                gfs_errs.append(entry["gfs"]["tmax_c"] - entry["truth_c"])
            rec = entry["configs"].get(FORECAST)
            if rec and entry["truth_c"] is not None and rec["status"] == "complete" and rec["peak_covered"]:
                rec["error_c"] = round(rec["tmax_c"] - entry["truth_c"], 2)
                errs.setdefault(FORECAST, []).append(rec["error_c"])
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
            m, b = rec.get("market") or {}, rec.get("belief") or {}
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            entry["nowcast"] = {
                "ready_utc": rec.get("ready_utc"), "last_sat_hour_utc": rec.get("last_sat_hour_utc"),
                "metar_max_so_far_c": rec.get("metar_max_so_far_c"),
                "sat_c": rec.get("wrf_sat_tmax_c"), "ctl_c": rec.get("wrf_ctl_tmax_c"),
                "sat_obs_c": rec.get("wrf_sat_tmax_with_obs_c"), "ctl_obs_c": rec.get("wrf_ctl_tmax_with_obs_c"),
                "market_mean_c": m.get("mean_c"), "market_mode_c": m.get("mode_c"), "market_mode_p": m.get("mode_p"),
                "belief_mean_c": b.get("mean_c"), "belief_mode_c": b.get("mode_c"), "belief_mode_p": b.get("mode_p"),
                "belief_stage": b.get("stage"),
            }
            t = entry["truth_c"]
            if t is None:
                continue
            for k in ("sat_obs_c", "ctl_obs_c", "market_mean_c", "belief_mean_c"):
                v = entry["nowcast"][k]
                if v is not None:
                    nowcast_errs.setdefault(k, []).append(float(v) - t)
        configs = sorted({c for e in days.values() for c in e["configs"]},
                         key=lambda c: (c != "nowcast_sat", c != "nowcast_ctl", c != FORECAST, c != BASELINE, c))
        latest = max(days) if days else None
        stations[station] = {
            "state": "ok" if days else "no_runs", "timezone": str(zone), "point": list(point),
            "configs": configs, "days": dict(sorted(days.items())), "latest_day": latest,
            "scores": {c: _stats(errs.get(c, [])) for c in configs},
            "gfs_score": _stats(gfs_errs),
            "nowcast_scores": {k: _stats(v) for k, v in nowcast_errs.items()},
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
                                                "peak_covered", "prev_init_utc", "prev_tmax_c", "change_c")}
            for c in ("nowcast_sat", "nowcast_ctl", FORECAST) if c in configs}
    scores = S.get("scores") or {}
    return {"state": "ok", "as_of_utc": value.get("as_of_utc"), "timezone": S.get("timezone"),
            "truth_c": E.get("truth_c"), "gfs": E.get("gfs"), "nowcast": E.get("nowcast"), "runs": keep,
            "fcst_history": E.get("fcst_history") or [],
            "nowcast_scores": S.get("nowcast_scores") or {},
            "oracle_score": scores.get("satcld_rhc85"), "selfcloud_score": scores.get("indep_rhc85")}


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
