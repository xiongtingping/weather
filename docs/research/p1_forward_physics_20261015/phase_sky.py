"""Satellite clearness (measured over clear sky) as a P1 direct-update feature (PROTOCOL_SKY.md).

Rows: the production fitter's own samples (p1_observation_samples.db) for the
16 Himawari stations, joined to the root vector (inputs.db root) and the
station-day ladder (inputs.db p1 intervals).  Arms A (production features,
refit here), B (A + K2 + K_gap), C (A + K2); walk-forward by event date.
Writes readings_sky.json.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
import solar  # noqa: E402
from bayes_basket_target import p1_direct_update as DU  # noqa: E402
from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
from bayes_basket_target import station_registry  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

SAMPLES = HERE.parents[2] / "data" / "runtime" / "bayes_basket_target_nwp" / "p1_observation_samples.db"
PUBLISH_AFTER = timedelta(minutes=75)
LAM = 1.0
MIN_TRAIN_DAYS = 15
TEST_FROM, TEST_TO = "2026-09-21", "2026-10-07"
BOOT = 2000
EXTRA = {"A": [], "B": ["k2", "k_gap"], "C": ["k2"]}


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    db = sqlite3.connect(HERE / "inputs.db")
    elev = {r["icaoId"]: float(r["elev"]) for r in json.load(open(HERE / "awc_stationinfo.json"))}
    stations = sorted(r[0] for r in db.execute("select distinct station_id from rad where source='OPENMETEO_jma_jaxa_himawari'"))
    geo = {s: (station_registry.station_coordinates(s)["latitude"], station_registry.station_coordinates(s)["longitude"], elev[s]) for s in stations}
    rad = {(st, h): v for st, h, v in db.execute("select station_id, hour_utc, swr_wm2 from rad where source='OPENMETEO_jma_jaxa_himawari'")}
    roots = {}
    for cid, probs, prop in db.execute("select checkpoint_id, probabilities_json, proportional_json from root"):
        p = json.loads(probs)
        if prop:
            try:
                p = json.loads(prop)["proportional_probabilities"]
            except Exception:
                pass
        roots[cid] = p
    ladders = {}
    for st, d, oj, ij in db.execute("select station_id, event_date, ordinals_json, intervals_json from p1 where intervals_json is not null"):
        ladders.setdefault((st, d), (json.loads(oj), json.loads(ij)))
    artifact = OL.load_artifact()
    s0_cache = {}

    def s0_hour(st, hs):
        if (st, hs) not in s0_cache:
            lat, lon, z = geo[st]
            s0_cache[(st, hs)] = solar.hourly_s0_mean(lat, lon, z, hs)
        return s0_cache[(st, hs)]

    def k2_at(st, as_of):
        """Mean K over the last two complete daylight hours published by as_of."""
        last_end = (as_of - PUBLISH_AFTER).replace(minute=0, second=0, microsecond=0)
        ks = []
        hs = last_end - timedelta(hours=1)
        for _ in range(8):
            s0 = s0_hour(st, hs)
            if s0 > 50.0:
                v = rad.get((st, hs.strftime("%Y-%m-%dT%H:00:00Z")))
                if v is None:
                    break
                ks.append(min(max(v / s0, 0.0), 1.2))
                if len(ks) == 2:
                    break
            elif ks:
                break
            hs -= timedelta(hours=1)
            if (as_of - hs) > timedelta(hours=6):
                break
        return float(np.mean(ks)) if ks else None

    sdb = sqlite3.connect(f"file:{SAMPLES}?mode=ro", uri=True)
    cols = [r[1] for r in sdb.execute("pragma table_info(metar_phase_hazard_sample)")]
    rows = []
    miss = defaultdict(int)
    for rec in sdb.execute(
        "select * from metar_phase_hazard_sample where event_date between '2026-09-01' and '2026-10-07' and station_id in (%s)" % ",".join("?" * len(stations)), stations):
        s = dict(zip(cols, rec))
        key = (s["station_id"], s["event_date"])
        if key not in ladders:
            miss["ladder"] += 1; continue
        root = roots.get(s["root_checkpoint_id"])
        if root is None:
            miss["root"] += 1; continue
        ordinals, intervals = ladders[key]
        if set(ordinals) != set(root):
            miss["support"] += 1; continue
        order = sorted(ordinals, key=lambda b: ordinals[b])
        lo = [intervals[b][0] for b in order]; up = [intervals[b][1] for b in order]
        w = next(u - l for l, u in zip(lo, up) if l is not None and u is not None)
        centers = {b: ((l + u) / 2 if l is not None and u is not None else (l + w / 2 if l is not None else u - w / 2)) for b, l, u in zip(order, lo, up)}
        uppers = {b: u for b, u in zip(order, up)}
        H = float(s["running_high_c"]); T = float(s["truth_tmax_c"])
        hb = next((b for b, l, u in zip(order, lo, up) if (l is None or l <= H) and (u is None or H < u)), None)
        tb = next((b for b, l, u in zip(order, lo, up) if (l is None or l <= T) and (u is None or T < u)), None)
        if hb is None or tb is None:
            miss["bucket"] += 1; continue
        excluded = [b for b in order if ordinals[b] < ordinals[hb]]
        bases = DU.bucket_bases(bucket_ordinals=ordinals, bucket_centers_c=centers, bucket_upper_bounds_c=uppers,
                                running_high_c=H, running_high_bucket=hb, hard_excluded_buckets=excluded)
        if ordinals[tb] < ordinals[hb]:
            miss["truth_below_high"] += 1; continue
        try:
            rf = DU.root_features(root, bases, bucket_centers_c=centers, running_high_c=H)
        except Exception:
            miss["root_zero"] += 1; continue
        as_of = utc(s["as_of_utc"])
        k2 = k2_at(s["station_id"], as_of)
        feats = {n: (None if s.get(n) in (None, "None") else float(s[n])) for n in DU.FEATURES}
        rows.append({
            "station": s["station_id"], "day": s["event_date"], "as_of": s["as_of_utc"],
            "order": order, "lo": lo, "up": up, "truth_c": T, "ti": order.index(tb),
            "prior": [float(root[b]) for b in order], "bases": [bases[b] for b in order],
            "feats": feats, "rf": rf, "k2": k2,
            "hours": OL.clock_hours(float(s["remaining_hours"]), artifact, s["station_id"]),
            "weight": float(s["station_day_weight"]),
            "local_hour": float(s["local_hour"]), "remaining_hours": float(s["remaining_hours"]),
        })
    print("rows", len(rows), "station-days", len({(r["station"], r["day"]) for r in rows}), "miss", dict(miss),
          "k2 available", round(np.mean([r["k2"] is not None for r in rows]), 3))

    # ---- design -------------------------------------------------------------
    n = len(rows); width = max(len(r["order"]) for r in rows)
    prior = np.zeros((n, width)); live = np.zeros((n, width), bool); rise = np.zeros((n, width)); slope = np.zeros((n, width)); top = np.zeros((n, width))
    truth = np.zeros(n, int); hours = np.zeros(n); weight = np.zeros(n); days = np.empty(n, object)
    base_names = list(DU.FEATURES) + list(DU.ROOT_FEATURES)
    raw_base = np.full((n, len(base_names)), np.nan)
    k2 = np.full(n, np.nan); cloud = np.full(n, np.nan)
    for i, r in enumerate(rows):
        m = len(r["order"])
        prior[i, :m] = r["prior"]
        for j, (rung, rr, ss, oo) in enumerate(r["bases"]):
            if rung >= 0:
                live[i, j] = True; rise[i, j] = rr; slope[i, j] = ss; top[i, j] = oo
        truth[i] = r["ti"]; hours[i] = r["hours"]; weight[i] = r["weight"]; days[i] = r["day"]
        raw_base[i] = [np.nan if r["feats"][f] is None else r["feats"][f] for f in DU.FEATURES] + [r["rf"][f] for f in DU.ROOT_FEATURES]
        k2[i] = np.nan if r["k2"] is None else r["k2"]
        cloud[i] = np.nan if r["feats"]["cloud_cover_fraction"] is None else r["feats"]["cloud_cover_fraction"]
    usable = (prior * live).sum(1) > 1e-12
    knots = np.array(DU.KNOTS_HOURS)
    hats = np.stack([np.interp(hours, knots, np.eye(len(knots))[k]) for k in range(len(knots))], 1)
    late = (hours < DU.LATE_BELOW_HOURS).astype(float)
    mid = ((hours >= DU.LATE_BELOW_HOURS) & (hours < DU.MID_BELOW_HOURS)).astype(float)

    def build_raw(arm, train_idx):
        extra = EXTRA[arm]
        if not extra:
            return raw_base
        ok = train_idx[~np.isnan(k2[train_idx]) & ~np.isnan(cloud[train_idx])]
        A = np.vstack([np.ones(len(ok)), cloud[ok]]).T
        a, b = np.linalg.lstsq(A, k2[ok], rcond=None)[0]
        k_gap = k2 - (a + b * cloud)
        add = {"k2": k2, "k_gap": k_gap}
        return np.hstack([raw_base] + [add[e][:, None] for e in extra])

    def x_of(raw, idx, scaling):
        filled = raw[idx].copy()
        for k in range(filled.shape[1]):
            filled[np.isnan(filled[:, k]), k] = scaling["fallback"][k]
        standard = (filled - scaling["mean"]) / scaling["scale"]
        return np.hstack([np.ones((len(idx), 1)), hats[idx], standard, late[idx, None] * standard, mid[idx, None] * standard])

    def scaling_of(raw, idx):
        fb = np.array([np.nanmean(raw[idx, k]) if np.isfinite(raw[idx, k]).any() else 0.0 for k in range(raw.shape[1])])
        filled = raw[idx].copy()
        for k in range(filled.shape[1]):
            filled[np.isnan(filled[:, k]), k] = fb[k]
        return {"fallback": fb, "mean": filled.mean(0), "scale": filled.std(0) + 1e-9}

    def f_of(idx, x, theta):
        w = x.shape[1]
        tr, ts, tsq, tt = theta[:w], theta[w:2 * w], theta[-2], theta[-1]
        return rise[idx] * (x @ tr)[:, None] + slope[idx] * (x @ ts)[:, None] + slope[idx] ** 2 * tsq + top[idx] * tt

    def posterior(idx, x, theta):
        lv = live[idx]
        z = np.where(lv, np.log(np.where(lv, np.maximum(prior[idx], 1e-300), 1.0)) + f_of(idx, x, theta), -np.inf)
        z = z - z.max(1, keepdims=True)
        p = np.where(lv, np.exp(z), 0.0)
        return p / p.sum(1, keepdims=True)

    def loss(theta, idx, x, lam):
        p = posterior(idx, x, theta)
        rr = np.arange(len(idx)); t = truth[idx]; w = weight[idx]
        val = -(w * np.log(np.maximum(p[rr, t], 1e-300))).sum() + 0.5 * lam * (theta ** 2).sum()
        g = p.copy(); g[rr, t] -= 1.0; g *= w[:, None]
        sl = slope[idx]
        grad = np.concatenate([x.T @ (g * rise[idx]).sum(1), x.T @ (g * sl).sum(1), [(g * sl * sl).sum(), (g * top[idx]).sum()]])
        return val, grad + lam * theta

    def fit(idx, x):
        theta0 = np.zeros(2 * x.shape[1] + 2)
        return minimize(loss, theta0, args=(idx, x, LAM), jac=True, method="L-BFGS-B", options={"maxiter": 400}).x

    def served_scores(p, i):
        r = rows[i]; m = len(r["order"])
        vec, _ = floor_probabilities(dict(zip(r["order"], p[:m])), ())
        pv = np.array([vec[b] for b in r["order"]])
        ls = -math.log(max(pv[r["ti"]], 1e-300))
        brier = float(((pv - (np.arange(m) == r["ti"])) ** 2).sum())
        edges = np.array(r["up"][:-1], float)
        widths = np.diff(np.concatenate([[edges[0] - (edges[1] - edges[0])], edges]))
        F = np.cumsum(pv)[:-1]
        crps = float((((F - (r["truth_c"] <= edges)) ** 2) * widths).sum())
        return ls, crps, brier

    # ---- walk-forward -------------------------------------------------------
    test_days = sorted({d for d in days if TEST_FROM <= d <= TEST_TO})
    S = {arm: np.full((n, 3), np.nan) for arm in EXTRA}
    S["root"] = np.full((n, 3), np.nan)
    kgap_col = np.full(n, np.nan)
    for d in test_days:
        train = np.where((days < d) & usable)[0]
        test = np.where((days == d) & usable)[0]
        if len({days[i] for i in train}) < MIN_TRAIN_DAYS or len(test) == 0:
            continue
        for arm in EXTRA:
            raw = build_raw(arm, train)
            sc = scaling_of(raw, train)
            theta = fit(train, x_of(raw, train, sc))
            p = posterior(test, x_of(raw, test, sc), theta)
            for k, i in enumerate(test):
                S[arm][i] = served_scores(p[k], i)
            if arm == "B":
                kgap_col[test] = raw[test, -1]
        for i in test:
            lv = live[i]; pr = np.where(lv, prior[i], 0.0); pr = pr / pr.sum()
            S["root"][i] = served_scores(pr, i)
        print("day", d, "train days", len({days[i] for i in train}), "test rows", len(test),
              "A", round(np.nanmean(S["A"][test, 0]), 4), "B", round(np.nanmean(S["B"][test, 0]), 4), flush=True)

    # ---- verdict ------------------------------------------------------------
    sd_key = np.array([r["station"] + "|" + r["day"] for r in rows])
    sd_ids, sd_idx = np.unique(sd_key, return_inverse=True)
    day_ids, day_idx = np.unique(days.astype(str), return_inverse=True)
    sd_day = np.array([day_idx[np.where(sd_idx == j)[0][0]] for j in range(len(sd_ids))])
    rng = np.random.default_rng(7)
    draws = rng.multinomial(len(day_ids), np.full(len(day_ids), 1.0 / len(day_ids)), size=BOOT)

    def boot(col, sel):
        ok = sel & ~np.isnan(col)
        if not ok.any():
            return None
        s = np.bincount(sd_idx[ok], weights=col[ok], minlength=len(sd_ids)); c = np.bincount(sd_idx[ok], minlength=len(sd_ids))
        has = c > 0; m = np.where(has, s / np.maximum(c, 1), 0.0)
        ds = np.bincount(sd_day, weights=m * has, minlength=len(day_ids)); dc = np.bincount(sd_day, weights=has.astype(float), minlength=len(day_ids))
        point = ds.sum() / dc.sum(); bs = (draws @ ds) / np.maximum(draws @ dc, 1e-9)
        lo, hi = np.quantile(bs, [0.025, 0.975])
        return [float(point), float(lo), float(hi)], int(has.sum())

    local_hour = np.array([r["local_hour"] for r in rows]); rem = np.array([r["remaining_hours"] for r in rows])
    station = np.array([r["station"] for r in rows])
    slices = {"all": np.ones(n, bool), "local_12_18": (local_hour >= 12) & (local_hour < 18), "remaining_6_10": (rem >= 6) & (rem < 10),
              "kgap_abs_gt_0p2": np.abs(kgap_col) > 0.2, "zuuu_zbaa_wsss": np.isin(station, ["ZUUU", "ZBAA", "WSSS"]), "k2_known": ~np.isnan(k2)}
    out = {"rows": n, "test_days": test_days, "comparisons": {}}
    for name, (a, b) in {"B_minus_A": ("B", "A"), "C_minus_A": ("C", "A"), "A_minus_root": ("A", "root"), "B_minus_root": ("B", "root")}.items():
        for sname, sel in slices.items():
            res = {}
            for k, metric in enumerate(("dlog", "dcrps", "dbrier")):
                r = boot(S[a][:, k] - S[b][:, k], sel)
                if r:
                    res[metric], res["station_days"] = r
            if res:
                out["comparisons"][f"{name}|{sname}"] = res
    json.dump(out, open(HERE / "readings_sky.json", "w"), indent=1)
    for k, v in out["comparisons"].items():
        print(k, "sd", v["station_days"], "dLog", [round(x, 4) for x in v["dlog"]], "dCRPS", [round(x, 4) for x in v["dcrps"]], "dBrier", [round(x, 4) for x in v["dbrier"]])


if __name__ == "__main__":
    main()
