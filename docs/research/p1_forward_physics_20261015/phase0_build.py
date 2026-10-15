"""Phase 0 build: eta calibration and the K* / clear-sky ceiling per P1 decision (writes eta_table.json, decisions.jsonl.gz).

Read-only on inputs.db (built from SSOT/archive/research caches); writes
eta_table.json, decisions.jsonl.gz and readings.json beside it.  PROTOCOL.md
fixes every definition used here.
"""
from __future__ import annotations

import gzip
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
import solar  # noqa: E402
from bayes_basket_target import station_registry  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

Q_ARMS = (0.95, 0.90, 0.50)
MAIN_Q = 0.95
K_CAP = 1.0
MIN_STATION_DAYS = 8
MIN_ENERGY_MJ = 0.3
TEST_FROM, TEST_TO = "2026-09-01", "2026-10-14"
BOOT = 2000


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main() -> None:
    db = sqlite3.connect(HERE / "inputs.db")
    elev = {r["icaoId"]: float(r["elev"]) for r in json.load(open(HERE / "awc_stationinfo.json"))}
    tz = {s: ZoneInfo(z) for s, z in db.execute("select station_id, settlement_timezone from tz")}
    coords = {s: station_registry.station_coordinates(s) for s in tz}
    geo = {s: (coords[s]["latitude"], coords[s]["longitude"], elev[s]) for s in tz}

    # truth: newest revision per station-day
    truth: dict[tuple[str, str], float] = {}
    for st, d, t, rev in db.execute(
        "select station_id, local_date, official_tmax_c, revision_no from truth order by revision_no"
    ):
        truth[(st, d)] = float(t)

    # observations by station, local day
    obs = defaultdict(list)
    for st, va, t in db.execute("select station_id, valid_at, temp_c from obs where temp_c is not null"):
        tu = utc(va)
        tl = tu.astimezone(tz[st])
        obs[(st, tl.strftime("%Y-%m-%d"))].append((tu, tl, float(t)))
    for k in obs:
        obs[k].sort()

    rad = {}
    rad_official = {}
    for st, h, v, off in db.execute("select station_id, hour_utc, swr_wm2, official from rad"):
        rad[(st, h)] = float(v)
        rad_official[st] = int(off)

    # ---- daily efficiency eta_d -------------------------------------------------
    eta_rows = []
    for (st, d), rows in obs.items():
        if (st, d) not in truth:
            continue
        lat, lon, z = geo[st]
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=tz[st])
        t09 = day.replace(hour=9)
        near = [r for r in rows if abs((r[1] - t09).total_seconds()) <= 1800]
        if not near:
            continue
        t09_obs = min(near, key=lambda r: abs((r[1] - t09).total_seconds()))
        tmax_obs = max(r[2] for r in rows)
        t_max = next(r for r in rows if r[2] >= tmax_obs - 1e-9)  # first time the day's high appears
        if t_max[1] <= t09:
            continue
        # energy 09:00 -> t_max from hourly K x S0
        energy = 0.0
        missing = False
        t = t09.astimezone(timezone.utc)
        end = t_max[0]
        while t < end:
            hour_start = t.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            seg_end = min(hour_end, end)
            s0 = solar.hourly_s0_mean(lat, lon, z, hour_start)
            if s0 > 50.0:
                swr = rad.get((st, hour_start.strftime("%Y-%m-%dT%H:00:00Z")))
                if swr is None:
                    missing = True
                    break
                k = min(max(swr / s0, 0.0), 1.2)
                energy += k * s0 * (seg_end - t).total_seconds() / 1e6
            t = seg_end
        if missing or energy < MIN_ENERGY_MJ:
            continue
        rise = tmax_obs - t09_obs[2]
        eta_rows.append({
            "station_id": st, "local_date": d, "t09_c": t09_obs[2], "tmax_obs_c": tmax_obs,
            "truth_c": truth[(st, d)], "energy_mj": energy, "eta": rise / energy,
            "tmax_local_minutes": t_max[1].hour * 60 + t_max[1].minute,
            "rad_official": rad_official.get(st, 0),
        })
    print("eta station-days", len(eta_rows), "stations", len({r["station_id"] for r in eta_rows}))
    by_station = defaultdict(list)
    for r in eta_rows:
        by_station[r["station_id"]].append(r)

    def eta_table(station: str, before: str) -> dict:
        own = [r for r in by_station[station] if r["local_date"] < before]
        fleet = [r for r in eta_rows if r["local_date"] < before]
        src = own if len(own) >= MIN_STATION_DAYS else fleet
        if len(src) < MIN_STATION_DAYS:
            return {}
        etas = np.array([r["eta"] for r in src])
        tm = np.array([r["tmax_local_minutes"] for r in src])
        return {
            "fallback": len(own) < MIN_STATION_DAYS, "n": len(src),
            **{f"eta_q{int(q*100)}": float(np.quantile(etas, q)) for q in Q_ARMS},
            "t_end_minutes": float(np.quantile(tm, 0.95)),
        }

    full_table = {st: eta_table(st, "9999") for st in tz}
    json.dump({"schema": "p1_forward_phase0_eta_v1", "rows": eta_rows, "per_station_full_window": full_table},
              open(HERE / "eta_table.json", "w"), indent=1)

    # ---- decisions: ceiling per P1 row ------------------------------------------
    roots = {}
    for cid, probs, prop in db.execute("select checkpoint_id, probabilities_json, proportional_json from root"):
        p = json.loads(probs)
        if prop:
            try:
                p = json.loads(prop)["proportional_probabilities"]
            except Exception:
                pass
        roots[cid] = p
    out = gzip.open(HERE / "decisions.jsonl.gz", "wt")
    n_rows = n_kept = 0
    rows_out = []
    for row in db.execute(
        "select station_id, event_date, decision_at, probabilities_json, ordinals_json, intervals_json, "
        "reported_high_c, root_checkpoint_id, root_stage, ol_state from p1 where event_date between ? and ?",
        (TEST_FROM, TEST_TO),
    ):
        st, d, dec, pj, oj, ij, hc, rcid, rstage, ol = row
        n_rows += 1
        if not ij or hc is None or (st, d) not in truth:
            continue
        tab = eta_table(st, d)
        if not tab:
            continue
        lat, lon, z = geo[st]
        dec_u = utc(dec)
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=tz[st])
        t09 = day.replace(hour=9)
        t_end = day + timedelta(minutes=tab["t_end_minutes"])
        if dec_u < t09.astimezone(timezone.utc) or dec_u > t_end.astimezone(timezone.utc):
            continue
        s0_rem = solar.integral_mj(lat, lon, z, dec_u, t_end.astimezone(timezone.utc))
        H = float(hc)
        probs = json.loads(pj)
        ordinals = json.loads(oj)
        intervals = json.loads(ij)
        order = sorted(probs, key=lambda b: ordinals[b])
        lower = {b: (-math.inf if intervals[b][0] is None else float(intervals[b][0])) for b in order}
        upper = {b: (math.inf if intervals[b][1] is None else float(intervals[b][1])) for b in order}
        T = truth[(st, d)]
        tb = next((b for b in order if lower[b] <= T < upper[b]), None)
        if tb is None:
            continue
        root = roots.get(rcid)
        rec = {
            "station_id": st, "event_date": d, "decision_at": dec, "H": H, "s0_rem_mj": s0_rem,
            "hours_to_end": (t_end.astimezone(timezone.utc) - dec_u).total_seconds() / 3600.0,
            "local_hour": dec_u.astimezone(tz[st]).hour + dec_u.astimezone(tz[st]).minute / 60.0,
            "unit": "F" if "°F" in order[0] else "C", "truth_c": T, "truth_bucket": tb,
            "rad_official": rad_official.get(st, 0), "fallback": tab["fallback"], "ol_state": ol,
            "order": order, "lower": [lower[b] for b in order], "upper": [upper[b] for b in order],
            "p1": [float(probs[b]) for b in order],
            "root": [float(root[b]) for b in order] if root and set(root) == set(probs) else None,
            "ceil": {f"q{int(q*100)}": H + tab[f"eta_q{int(q*100)}"] * K_CAP * s0_rem for q in Q_ARMS},
        }
        out.write(json.dumps(rec) + "\n")
        rows_out.append(rec)
        n_kept += 1
    out.close()
    print("p1 rows", n_rows, "kept", n_kept, "station-days", len({(r["station_id"], r["event_date"]) for r in rows_out}))



if __name__ == "__main__":
    main()
