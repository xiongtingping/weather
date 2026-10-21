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
        times = [t.tobytes().decode() for t in f["Times"][:]]
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


def compute(conn: sqlite3.Connection, *, root: Path = WRF_ROOT, now: datetime | None = None,
            cache: _Cache | None = None) -> dict[str, Any]:
    now = now or _now()
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
            rec = {"status": status, "domain": dom, "init_utc": _iso(_utc(init)) if init else None,
                   **{k: m[k] for k in ("tmax_c", "at_local", "peak_covered", "last_local", "series")}}
            entry = days.setdefault(day, {"truth_c": truth.get(day), "configs": {}})
            if config == BASELINE:
                gfs: dict[str, float] = {}
                for fn in sorted(glob.glob(str(d / "met_em.d01.*"))):
                    gfs.update(cache.frames(fn, "TT", point))
                g = _day_max(gfs, day, zone)
                if g:
                    entry["gfs"] = {"tmax_c": g["tmax_c"], "at_local": g["at_local"]}
                    if entry["truth_c"] is not None:
                        gfs_errs.append(g["tmax_c"] - entry["truth_c"])
            entry["configs"][config] = rec
            if entry["truth_c"] is not None and status == "complete" and m["peak_covered"]:
                rec["error_c"] = round(m["tmax_c"] - entry["truth_c"], 2)
                errs.setdefault(config, []).append(rec["error_c"])
        configs = sorted({c for e in days.values() for c in e["configs"]}, key=lambda c: (c != BASELINE, c))
        latest = max(days) if days else None
        stations[station] = {
            "state": "ok" if days else "no_runs", "timezone": str(zone), "point": list(point),
            "configs": configs, "days": dict(sorted(days.items())), "latest_day": latest,
            "scores": {c: _stats(errs.get(c, [])) for c in configs},
            "gfs_score": _stats(gfs_errs),
        }
    return {"state": "ok", "root": str(root), "as_of_utc": _iso(now), "files_read": cache.read,
            "running": [f"{c}/{ymd}" for c, ymd, d in runs if _status(d, now.timestamp()) == "running"],
            "stations": stations}


def run(*, db: Path = board_readings.DEFAULT_DB) -> dict[str, Any]:
    t0 = time.monotonic()
    cache = _Cache()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        value = compute(conn, cache=cache)
    finally:
        conn.close()
    if value.get("state") == "ok":
        cache.save()
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    board_readings.write_reading(READING_NAME, value, code=code, build_sec=time.monotonic() - t0)
    return value


def main() -> None:
    value = run()
    print(json.dumps({"state": value.get("state"), "files_read": value.get("files_read"),
                      "running": value.get("running")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
