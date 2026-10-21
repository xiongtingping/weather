"""Forward record of the board's composite ΔT card (user 2026-10-21, item 3 of
「继续3、2、6、4、5」): every fitted version is scored only on the station-days
after its training window, hour by hour, through the board's own
``station_day_board._composite_at`` -- each hour reads that hour's reports,
satellite and day-ahead forecast only, so the value is the one the card
showed (the satellite's ~1 h lag delays the card, not the value).

Beside it: the day-ahead forecast as it stands (ΔT = 0) and the board's sun
line (``sun_tmax_shift_v2``).  Writes the per-hour rows to ``forward_scores``
in the M1 store and ``data/runtime/tmax_composite/forward_summary.json`` for
the board.  Truth: the IEM local-day maximum (formal truth MAE 0.022 C).
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "docs" / "research" / "tmax_physics_m1_ingest_20261020"))
import evaluate as e  # noqa: E402
import store  # noqa: E402

from bayes_basket_target import station_day_board as board  # noqa: E402

RUNTIME = ROOT / "data" / "runtime" / "tmax_composite"
COMMITTED = ROOT / "config" / "ui" / "tmax_composite_shift_v1.json"
HOURS = range(0, 18)   # every hour a version fits (0..8 from M6, 6 h left off)


def versions() -> list[dict]:
    out = {}
    paths = [COMMITTED] + sorted((RUNTIME / "versions").glob("tmax_composite_shift_*.json"))
    for p in paths:
        try:
            fit = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        v = fit.get("version") or fit["fitted_at_utc"].replace(":", "").replace("-", "")
        out[v] = {**fit, "version": v}
    return [out[k] for k in sorted(out)]


def board_rows(r: dict) -> list[dict]:
    """The station-day as the board's hourly rows: 00..05 from the night
    arrays (PROTOCOL_M6), 06..17 from the day's."""

    n = r.get("night") or {}
    rows = []
    for h in range(24):
        if h < 6:
            src, i, sw = n, h, (0.0, 0.0)
        elif h < 18:
            src, i = r, h - 6
            sw = (r["sw_fc"][i], r["sw_obs"][i])
        else:
            rows.append({"hour": h, "forecast": {"sw_d1": 0.0}, "obs": None, "sat_sw_wm2": 0.0})
            continue
        get = lambda k: (src.get(k) or [None] * 12)[i]  # noqa: E731
        fc = {"sw_d1": sw[0], "temp_c_d1": get("t_fc"), "dew_c_d1": get("dew_fc"), "rh_pct_d1": get("rh_fc"),
              "wind_mps_d1": get("wind_fc")}
        obs = None
        if get("t_obs") is not None:
            obs = {"temp_c": get("t_obs"), "dew_c": get("dew_obs"), "rh_pct": get("rh_obs"),
                   "wind_mps": get("wind_obs"), "precip": bool(get("wet_obs"))}
        rows.append({"hour": h, "forecast": fc, "obs": obs, "sat_sw_wm2": sw[1]})
    return rows


def main() -> int:
    rows = json.loads(e.DAYS.read_text())
    today = datetime.now(timezone.utc).date().isoformat()
    scored = []
    for fit in versions():
        after = fit["window"][1]
        for r in rows:
            if not (after < r["date"] < today):
                continue
            br = {int(x["hour"]): x for x in board_rows(r)}
            y = r["tmax"] - r["tmax_fc"]
            for H in HOURS:
                if str(H) not in fit["by_hour"]:
                    continue
                p = board._composite_at(br, r["station"], fit, H)
                if p is None:
                    continue
                sun = e.sun_line(e.feats(r, H), H)
                scored.append((fit["version"], r["station"], r["date"], H, y, p["total_c"], p["resid_sd_c"], sun))
    conn = store.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS forward_scores (version TEXT, station TEXT, local_date TEXT, hour INTEGER,"
                 " y_c REAL, composite_c REAL, sd_c REAL, sun_line_c REAL, PRIMARY KEY (version, station, local_date, hour))")
    conn.executemany("INSERT OR REPLACE INTO forward_scores VALUES (?,?,?,?,?,?,?,?)", scored)
    conn.commit()
    conn.close()
    summary: dict = {"schema": "tmax_composite_forward_v1",
                     "written_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "truth": "IEM local-day maximum; y = truth - day-ahead forecast maximum", "versions": {}}
    for fit in versions():
        v = fit["version"]
        mine = [s for s in scored if s[0] == v]
        hours = {}
        for H in HOURS:
            at = [s for s in mine if s[3] == H]
            if not at:
                continue
            n = len(at)
            hours[str(H)] = {
                "n": n, "days": len({s[2] for s in at}),
                "mae_raw_c": round(sum(abs(s[4]) for s in at) / n, 3),
                "mae_sun_line_c": round(sum(abs(s[4] - s[7]) for s in at) / n, 3),
                "mae_composite_c": round(sum(abs(s[4] - s[5]) for s in at) / n, 3),
                "bias_composite_c": round(sum(s[4] - s[5] for s in at) / n, 3),
                "within_1sd": round(sum(1 for s in at if abs(s[4] - s[5]) <= s[6]) / n, 3),
                "logscore_composite": round(sum(0.5 * math.log(2 * math.pi * s[6] ** 2) + 0.5 * ((s[4] - s[5]) / s[6]) ** 2
                                                for s in at) / n, 4),
            }
        summary["versions"][v] = {"fitted_at_utc": fit.get("fitted_at_utc"), "window": fit.get("window"),
                                  "first_day": min((s[2] for s in mine), default=None),
                                  "last_day": max((s[2] for s in mine), default=None), "by_hour": hours}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME / "forward_summary.json.tmp"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    tmp.replace(RUNTIME / "forward_summary.json")
    print("forward:", len(scored), "rows;", {v: (x["first_day"], x["last_day"], (x["by_hour"].get("13") or {}).get("n"))
                                             for v, x in summary["versions"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
