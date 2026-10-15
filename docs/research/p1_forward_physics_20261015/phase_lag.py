"""Lagged-kernel ceiling rerun (PROTOCOL_LAG.md).

Per-instant calibration of eta_tau,95 = station quantile of
r(t) / int_{t-tau}^{t_max} K.S0 on an hourly grid of training station-days,
walked forward by day; ceiling T_ceil = H + eta_tau,95 x int_{t-tau}^{t_end} S0
on the prefix-current P1 decisions of phase 0.  Main arm tau_phys(station) =
median(t_max - solar noon); diagnostic arm tau_grid chosen walk-forward on
training-day P1 dLogScore.  Writes readings_lag.json.
"""
from __future__ import annotations

import gzip
import json
import math
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

TAUS = [i * 0.5 for i in range(11)]          # 0 .. 5 h
GRID = [0.0, 1.0, 2.0, 3.0, 4.0]
MIN_E = 0.3
MIN_ROWS = 60
MIN_TRAIN_DAYS = 5
BOOT = 2000
Q = 0.95


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    db = sqlite3.connect(HERE / "inputs.db")
    elev = {r["icaoId"]: float(r["elev"]) for r in json.load(open(HERE / "awc_stationinfo.json"))}
    tz = {s: ZoneInfo(z) for s, z in db.execute("select station_id, settlement_timezone from tz")}
    geo = {s: (station_registry.station_coordinates(s)["latitude"], station_registry.station_coordinates(s)["longitude"], elev[s]) for s in tz}
    obs = defaultdict(list)
    for st, va, t in db.execute("select station_id, valid_at, temp_c from obs where temp_c is not null"):
        tu = utc(va)
        obs[(st, tu.astimezone(tz[st]).strftime("%Y-%m-%d"))].append((tu, float(t)))
    rad = {(st, h): float(v) for st, h, v in db.execute("select station_id, hour_utc, swr_wm2 from rad")}
    s0_cache = {}

    def s0_hour(st, hour_start):
        key = (st, hour_start)
        if key not in s0_cache:
            lat, lon, z = geo[st]
            s0_cache[key] = solar.hourly_s0_mean(lat, lon, z, hour_start)
        return s0_cache[key]

    def measured_energy(st, t0, t1):
        """int K.S0 from t0 to t1 (MJ/m2) with hourly measured K; None if an hour is missing."""
        if t1 <= t0:
            return 0.0
        e = 0.0
        t = t0
        while t < t1:
            hs = t.replace(minute=0, second=0, microsecond=0)
            seg_end = min(hs + timedelta(hours=1), t1)
            s0 = s0_hour(st, hs)
            if s0 > 50.0:
                swr = rad.get((st, hs.strftime("%Y-%m-%dT%H:00:00Z")))
                if swr is None:
                    return None
                e += min(max(swr / s0, 0.0), 1.2) * s0 * (seg_end - t).total_seconds() / 1e6
            t = seg_end
        return e

    def solar_noon(st, day_local):
        lat, lon, z = geo[st]
        best, best_t = -1.0, None
        t = day_local.replace(hour=9).astimezone(timezone.utc)
        end = day_local.replace(hour=16).astimezone(timezone.utc)
        while t <= end:
            h = solar.elevation_deg(lat, lon, t)
            if h > best:
                best, best_t = h, t
            t += timedelta(minutes=5)
        return best_t

    # ---- per-instant calibration rows -------------------------------------
    cal = []        # (station, local_date, tau_index, rho)
    lagdays = []    # (station, local_date, t_max - solar_noon hours)
    for (st, d), rows in obs.items():
        rows.sort()
        if d < "2026-08-31" or d > "2026-10-07":
            continue
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=tz[st])
        tmax = max(v for _, v in rows)
        t_max = next(tu for tu, v in rows if v >= tmax - 1e-9)
        if t_max.astimezone(tz[st]).hour < 10:
            continue
        noon = solar_noon(st, day)
        lagdays.append((st, d, (t_max - noon).total_seconds() / 3600.0))
        t = day.replace(hour=10).astimezone(timezone.utc)
        while t <= t_max:
            H = max((v for tu, v in rows if tu <= t), default=None)
            if H is not None:
                r = tmax - H
                for ti, tau in enumerate(TAUS):
                    e = measured_energy(st, t - timedelta(hours=tau), t_max)
                    if e is not None and e >= MIN_E:
                        cal.append((st, d, ti, r / e))
            t += timedelta(hours=1)
    print("calibration rows", len(cal), "lag days", len(lagdays))
    cal_by = defaultdict(list)
    for st, d, ti, rho in cal:
        cal_by[(st, ti)].append((d, rho))
    lag_by = defaultdict(list)
    for st, d, h in lagdays:
        lag_by[st].append((d, h))

    eta_cache = {}

    def eta95(st, ti, before):
        key = (st, ti, before)
        if key in eta_cache:
            return eta_cache[key]
        own = [rho for d, rho in cal_by[(st, ti)] if d < before]
        if len(own) < MIN_ROWS:
            own = [rho for (s2, t2), lst in cal_by.items() if t2 == ti for d, rho in lst if d < before]
            fb = True
        else:
            fb = False
        val = (float(np.quantile(own, Q)) if len(own) >= MIN_ROWS else None, fb)
        eta_cache[key] = val
        return val

    def tau_phys(st, before):
        own = [h for d, h in lag_by[st] if d < before]
        if len(own) < 8:
            own = [h for lst in lag_by.values() for d, h in lst if d < before]
        if len(own) < 8:
            return None
        return min(max(float(np.median(own)), 0.0), 5.0)

    # ---- decisions ----------------------------------------------------------
    recs = [json.loads(l) for l in gzip.open(HERE / "decisions.jsonl.gz", "rt") if '"ol_state": "APPLIED"' in l]
    n = len(recs)
    print("prefix-current rows", n)
    # clear-sky energy back from the decision instant in 0.5 h steps
    back = np.zeros((n, len(TAUS)))
    tphys = np.full(n, np.nan)
    eta_tab = np.full((n, len(TAUS)), np.nan)
    fb_flag = np.zeros(n, bool)
    for i, r in enumerate(recs):
        st = r["station_id"]
        lat, lon, z = geo[st]
        dec = utc(r["decision_at"])
        acc = 0.0
        for ti, tau in enumerate(TAUS):
            if ti > 0:
                acc += solar.integral_mj(lat, lon, z, dec - timedelta(hours=tau), dec - timedelta(hours=TAUS[ti - 1]), step_min=10.0)
            back[i, ti] = acc
        tp = tau_phys(st, r["event_date"])
        tphys[i] = tp if tp is not None else np.nan
        for ti in range(len(TAUS)):
            v, fb = eta95(st, ti, r["event_date"])
            eta_tab[i, ti] = np.nan if v is None else v
            fb_flag[i] |= fb
    s0rem = np.array([r["s0_rem_mj"] for r in recs])
    H = np.array([r["H"] for r in recs])
    ceil = H[:, None] + eta_tab * (back + s0rem[:, None])        # (n, taus)
    ti_phys = np.array([int(round(t * 2)) if not np.isnan(t) else -1 for t in tphys])
    ceil_phys = np.array([ceil[i, ti_phys[i]] if ti_phys[i] >= 0 else np.nan for i in range(n)])

    # ---- scoring per row for every tau ----------------------------------
    def floored(order, p):
        vec, _ = floor_probabilities(dict(zip(order, p)), ())
        return np.array([vec[b] for b in order])

    def scores(pv, rec, ti):
        edges = np.array(rec["upper"][:-1])
        widths = np.diff(np.concatenate([[edges[0] - (edges[1] - edges[0])], edges]))
        F = np.cumsum(pv)[:-1]
        return (-math.log(max(pv[ti], 1e-300)),
                float((((F - (rec["truth_c"] <= edges)) ** 2) * widths).sum()),
                float(((pv - (np.arange(len(pv)) == ti)) ** 2).sum()))

    def mask_scores(rec, p, c, base):
        lower = np.array(rec["lower"])
        raw = np.array(p)
        m = lower >= c
        above = float(raw[m].sum())
        kept = np.where(m, 0.0, raw)
        s = kept.sum()
        a = scores(floored(rec["order"], kept / s if s > 0 else raw), rec, rec["order"].index(rec["truth_bucket"]))
        return a[0] - base[0], a[1] - base[1], a[2] - base[2], above

    ncol = len(TAUS) + 1  # last column = tau_phys
    D = {k: np.full((n, ncol), np.nan) for k in ("dlog", "dcrps", "dbrier", "mass", "viol", "bind")}
    R = {k: np.full((n, ncol), np.nan) for k in ("dlog", "dcrps", "dbrier", "mass")}
    for i, r in enumerate(recs):
        ti = r["order"].index(r["truth_bucket"])
        base_p = scores(floored(r["order"], r["p1"]), r, ti)
        base_r = scores(floored(r["order"], r["root"]), r, ti) if r["root"] else None
        hb = next(j for j, (lo, up) in enumerate(zip(r["lower"], r["upper"])) if lo <= r["H"] < up)
        bind_edge = r["lower"][hb + 2] if hb + 2 < len(r["lower"]) else math.inf
        cs = list(ceil[i]) + [ceil_phys[i]]
        for j, c in enumerate(cs):
            if np.isnan(c):
                continue
            dl, dc, dbr, above = mask_scores(r, r["p1"], c, base_p)
            D["dlog"][i, j], D["dcrps"][i, j], D["dbrier"][i, j], D["mass"][i, j] = dl, dc, dbr, above
            D["viol"][i, j] = float(r["truth_c"] >= c)
            D["bind"][i, j] = float(c < bind_edge)
            if base_r:
                R["dlog"][i, j], R["dcrps"][i, j], R["dbrier"][i, j], R["mass"][i, j] = mask_scores(r, r["root"], c, base_r)

    # ---- tau_grid walk-forward selection --------------------------------
    dates = np.array([r["event_date"] for r in recs])
    sd_key = np.array([r["station_id"] + "|" + r["event_date"] for r in recs])
    sd_ids, sd_idx = np.unique(sd_key, return_inverse=True)
    day_ids, day_idx = np.unique(dates, return_inverse=True)
    sd_day = np.array([day_idx[np.where(sd_idx == j)[0][0]] for j in range(len(sd_ids))])
    grid_cols = [TAUS.index(t) for t in GRID]

    def sd_mean(col, sel):
        ok = sel & ~np.isnan(col)
        s = np.bincount(sd_idx[ok], weights=col[ok], minlength=len(sd_ids))
        c = np.bincount(sd_idx[ok], minlength=len(sd_ids))
        return np.where(c > 0, s / np.maximum(c, 1), 0.0), c > 0

    chosen = np.full(n, -1)
    sel_log = {}
    for k, d in enumerate(day_ids):
        train = dates < d
        if len(np.unique(dates[train])) < MIN_TRAIN_DAYS:
            continue
        best, best_j = None, None
        for j in grid_cols:
            m, has = sd_mean(D["dlog"][:, j], train)
            v = m[has].mean() if has.any() else None
            if v is not None and (best is None or v < best):
                best, best_j = v, j
        chosen[day_idx == k] = best_j if best_j is not None else -1
        sel_log[d] = None if best_j is None else TAUS[best_j]
    grid_col = {k: np.array([D[k][i, chosen[i]] if chosen[i] >= 0 else np.nan for i in range(n)]) for k in D}

    # ---- summaries -----------------------------------------------------------
    rng = np.random.default_rng(7)
    draws = rng.multinomial(len(day_ids), np.full(len(day_ids), 1.0 / len(day_ids)), size=BOOT)

    def boot(col, sel):
        m, has = sd_mean(col, sel)
        if not has.any():
            return None
        ds = np.bincount(sd_day, weights=m * has, minlength=len(day_ids))
        dc = np.bincount(sd_day, weights=has.astype(float), minlength=len(day_ids))
        point = ds.sum() / dc.sum()
        bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9)
        lo, hi = np.quantile(bs, [0.025, 0.975])
        return [float(point), float(lo), float(hi)], int(has.sum())

    hours = np.array([r["hours_to_end"] for r in recs])
    lhour = np.array([r["local_hour"] for r in recs])
    unit_f = np.array([r["unit"] == "F" for r in recs])
    official = np.array([r["rad_official"] == 1 for r in recs])
    slices = {
        "all": np.ones(n, bool), "local_12_18": (lhour >= 12) & (lhour < 18),
        "hours_lt_3": hours < 3, "hours_6_10": (hours >= 6) & (hours < 10),
        "goes_official": official, "relay_source": ~official, "f_station": unit_f,
    }
    arms = {"tau_phys": {k: D[k][:, -1] for k in D}, "tau0_per_instant": {k: D[k][:, 0] for k in D},
            "tau2": {k: D[k][:, TAUS.index(2.0)] for k in D}, "tau_grid_wf": grid_col,
            "root_tau_phys": {k: R[k][:, -1] for k in R}, "root_tau0": {k: R[k][:, 0] for k in R}}
    out = {"tau_phys_hours": {"median": float(np.nanmedian(tphys)), "p10": float(np.nanquantile(tphys, .1)), "p90": float(np.nanquantile(tphys, .9))},
           "tau_grid_selected_by_day": sel_log, "fleet_fallback_rows": float(fb_flag.mean()), "arms": {}, "by_hours": {}}
    for name, cols in arms.items():
        for sname, sel in slices.items():
            res = {}
            for metric in ("dlog", "dcrps", "dbrier"):
                b = boot(cols[metric], sel)
                if b:
                    res[metric], res["station_days"] = b
            if res:
                ok = sel & ~np.isnan(cols["mass"])
                res["mass_above_mean"] = float(cols["mass"][ok].mean())
                if "viol" in cols:
                    res["truth_above_rate"] = float(cols["viol"][ok].mean())
                    res["binding_rows"] = float(cols["bind"][ok].mean())
                out["arms"][f"{name}|{sname}"] = res
        if "bind" in cols:
            bsel = cols["bind"] == 1.0
            res = {}
            for metric in ("dlog", "dcrps", "dbrier"):
                b = boot(cols[metric], bsel)
                if b:
                    res[metric], res["station_days"] = b
            if res:
                res["rows"] = int(bsel.sum()); res["truth_above_rate"] = float(np.nanmean(cols["viol"][bsel]))
                out["arms"][f"{name}|binding"] = res
    for lo_, hi_ in ((0, 2), (2, 4), (4, 6), (6, 8), (8, 10)):
        sel = (hours >= lo_) & (hours < hi_)
        out["by_hours"][f"{lo_}-{hi_}h"] = {
            "rows": int(sel.sum()),
            **{f"{a}_truth_above": float(np.nanmean(arms[a]["viol"][sel])) for a in ("tau_phys", "tau0_per_instant", "tau2")},
            **{f"{a}_p1_mass_above": float(np.nanmean(arms[a]["mass"][sel])) for a in ("tau_phys", "tau0_per_instant", "tau2")},
            "tau_phys_median_headroom_c": float(np.nanmedian((ceil_phys - H)[sel])),
        }
    json.dump(out, open(HERE / "readings_lag.json", "w"), indent=1)
    print("tau_phys", out["tau_phys_hours"], "fleet fallback rows", out["fleet_fallback_rows"])
    print("tau_grid by day", sel_log)
    for k, v in out["arms"].items():
        print(k, "sd", v["station_days"], "mass", round(v.get("mass_above_mean", float("nan")), 4), "viol", round(v.get("truth_above_rate", float("nan")), 4),
              "dLog", [round(x, 4) for x in v["dlog"]], "dCRPS", [round(x, 4) for x in v["dcrps"]], "dBrier", [round(x, 4) for x in v["dbrier"]])
    print(json.dumps(out["by_hours"], indent=1))


if __name__ == "__main__":
    main()
