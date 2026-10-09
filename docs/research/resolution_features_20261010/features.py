#!/usr/bin/env python3
"""PROTOCOL.md sections 1-3: one row per (representative decision, bucket in the
market mean bucket +-1 window) with every group's features.  Read-only."""
from __future__ import annotations

import gzip
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402

OLD = ROOT / "docs/research/conditional_advantage_20261006/rows"
NEW = HERE / "rows"
FIRST, LAST, SPLIT_OLD = "2026-09-12", "2026-10-08", "2026-10-04"
FLOOR = 1e-3
AI = {"ECMWF_AIFS_ENS_GLOBAL", "NOAA_AIGEFS_025"}
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def floored(vec):
    v = [max(float(x or 0.0), FLOOR) for x in vec]
    s = sum(v)
    return [x / s for x in v]


def family(row):
    if row["stage"] == "P0":
        return "P0>36h" if (row["h"] or 0) > 36 else "P0≤36h"
    return row["stage"]


def argmax_low(v):
    return max(range(len(v)), key=lambda i: (v[i], -i))


def load_rows():
    out = defaultdict(list)
    for folder, keep in ((OLD, lambda d: d <= SPLIT_OLD), (NEW, lambda d: d > SPLIT_OLD)):
        for name in sorted(os.listdir(folder)):
            for line in gzip.open(folder / name, "rt"):
                r = json.loads(line)
                if FIRST <= r["d"] <= LAST and keep(r["d"]):
                    out[r["st"]].append(r)
    return out


def main():
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    rows_by_station = load_rows()
    out = []
    counts = defaultdict(int)
    for station, rows in sorted(rows_by_station.items()):
        # truth history: latest value, first commit instant
        truth = {}
        for day, value, committed in conn.execute(
                "SELECT local_date, official_tmax_c, canonical_committed_at_utc FROM calibration_station_daily_high_truth"
                f" WHERE station_id=? AND source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED'"
                " AND local_date>='2026-07-15' ORDER BY local_date, revision_no", (station,)):
            if value is None or not committed:
                continue
            known = utc(committed)
            prev = truth.get(day)
            truth[day] = (float(value), min(known, prev[1]) if prev else known)
        # physics versions
        phys = defaultdict(list)
        rad_all = []
        for day, rad, cloud, precip, wind, captured in conn.execute(
                "SELECT local_date, shortwave_radiation_sum, cloudcover_mean, precipitation_sum, windspeed_10m_max,"
                " captured_at_utc FROM calibration_station_forecast_physics_version WHERE station_id=?", (station,)):
            if captured:
                phys[day].append((utc(captured), rad, cloud, precip, wind))
                if rad is not None:
                    rad_all.append(float(rad))
        rad_mu = statistics.fmean(rad_all) if rad_all else None
        rad_sd = statistics.pstdev(rad_all) if len(rad_all) > 2 else None
        # first P0 mean per event day (for the model's recent error)
        first_p0 = {}
        for r in sorted(rows, key=lambda x: x["t"]):
            if r["stage"] == "P0" and r["d"] not in first_p0 and r.get("p") and None not in (r.get("temps") or [None]):
                p = floored(r["p"])
                first_p0[r["d"]] = sum(a * b for a, b in zip(p, r["temps"]))
        chosen = {}
        for r in sorted(rows, key=lambda x: x["t"]):
            key = (r["d"], family(r))
            if key in chosen or r.get("q") is None or not r.get("p"):
                continue
            if None in (r.get("temps") or [None]) or None in r["q"]:
                counts["unusable"] += 1
                continue
            chosen[key] = r
        for (day, fam), r in sorted(chosen.items()):
            names, temps, rung = r["names"], [float(x) for x in r["temps"]], float(r["rung"] or 1.0)
            if r["y"] not in names:
                counts["label_off_ladder"] += 1
                continue
            p, q = floored(r["p"]), floored(r["q"])
            mu_p = sum(a * b for a, b in zip(p, temps)); sd_p = math.sqrt(max(sum(a * (b - mu_p) ** 2 for a, b in zip(p, temps)), 1e-9))
            mu_q = sum(a * b for a, b in zip(q, temps))
            M = min(range(len(temps)), key=lambda k: (abs(temps[k] - mu_q), k))
            mode_q, mode_p = argmax_low(q), argmax_low(p)
            mean_p_bucket = min(range(len(temps)), key=lambda k: (abs(temps[k] - mu_p), k))
            t = utc(r["t"])
            # G3 history known at t
            known = sorted((d, v) for d, (v, k) in truth.items() if k <= t and d < day)
            last = known[-1] if known else None
            last3 = [v for _d, v in known[-3:]]
            last30 = [v for _d, v in known[-30:]]
            errs = [truth[d][0] - first_p0[d] for d, _v in known[-12:] if d in first_p0][-5:]
            lag = (datetime.fromisoformat(day) - datetime.fromisoformat(last[0])).days if last else None
            # G4 physics as of t
            vers = [v for v in phys.get(day, []) if v[0] <= t]
            ph = max(vers, key=lambda v: v[0]) if vers else None
            rad_z = ((float(ph[1]) - rad_mu) / rad_sd) if ph and ph[1] is not None and rad_sd else None
            # G5 seating
            w = {k: float(v) for k, v in (r.get("w") or {}).items() if v and float(v) > 0}
            cen = {k: float(v) for k, v in (r.get("centres") or {}).items() if v is not None}
            seated = [k for k in w if k in cen]
            if seated:
                tw = sum(w[k] for k in seated); ws = {k: w[k] / tw for k in seated}
                mbar = sum(ws[k] * cen[k] for k in seated)
                disp = sum(ws[k] * (cen[k] - mbar) ** 2 for k in seated) / sd_p ** 2
                neff = 1.0 / sum(v * v for v in ws.values())
                wmax = max(ws.values()); ai = sum(ws.get(k, 0.0) for k in AI)
                side = 1.0 if mu_p >= mu_q else -1.0
                agree = sum(ws[k] for k in seated if (cen[k] - mu_q) * side > 0)
            else:
                disp = neff = wmax = ai = agree = None
            # G6
            book = r.get("book") or []
            bid, ask = (book[M][0], book[M][1]) if M < len(book) else (None, None)
            spread = (ask - bid) if bid is not None and ask is not None else None
            age = (t - utc(r["snap_at"])).total_seconds() / 60.0 if r.get("snap_at") else None
            day_feats = {
                "dmu": (mu_p - mu_q) / rung, "adm": abs(mu_p - mu_q) / rung,
                "dmode": float(mode_p - mode_q), "meanbucket_eq_M": float(mean_p_bucket == M),
                "last_lag": lag, "model_err": (statistics.fmean(errs) / rung) if errs else None,
                "anom30": ((mu_q - statistics.fmean(last30)) / rung) if len(last30) >= 10 else None,
                "rad_z": rad_z, "cloud": (float(ph[2]) if ph and ph[2] is not None else None),
                "precip": (math.log1p(float(ph[3])) if ph and ph[3] is not None else None),
                "wind": (float(ph[4]) if ph and ph[4] is not None else None),
                "neff": neff, "nseat": float(len(seated)) if seated else None, "disp": disp, "wmax": wmax,
                "ai": ai, "agree_sgn": (agree * (1.0 if mu_p >= mu_q else -1.0)) if agree is not None else None,
                "hours": r["h"], "spread": spread, "snap_age": age,
                "midsum_dev": abs(float(r["ms"]) - 1.0) if r.get("ms") is not None else None,
            }
            mode_q_win = [mode_q + d for d in (-1, 0, 1) if 0 <= mode_q + d < len(names)]
            for b in (M - 1, M, M + 1):
                if not 0 <= b < len(names):
                    continue
                out.append({
                    "st": station, "d": day, "fam": fam, "dec": f"{station}|{day}|{fam}", "unit": "F" if station in F_STATIONS else "C",
                    "pos": b - M, "y": float(names[b] == r["y"]), "q": q[b], "p": p[b],
                    "in_mode_window": b in mode_q_win,
                    "bucket": {
                        "z": (temps[b] - mu_p) / sd_p, "is_model_mode": float(b == mode_p),
                        "d_last": ((last[1] - temps[b]) / rung) if last else None,
                        "d_last3": ((statistics.fmean(last3) - temps[b]) / rung) if last3 else None,
                    },
                    "day": day_feats,
                })
            counts[f"decisions:{fam}"] += 1
    conn.close()
    with gzip.open(HERE / "features.jsonl.gz", "wt") as fh:
        for row in out:
            fh.write(json.dumps(row) + "\n")
    print(dict(counts), "rows", len(out))


if __name__ == "__main__":
    main()
