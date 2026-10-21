"""M11 (PROTOCOL_M11.md): which way of carrying today's report-vs-forecast
trend best predicts the later hours' trend, per variable.  Read-only over
``data/tmax_physics/m3_station_days.json``; writes ``reading_m11_<stamp>.json``."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
DAYS = json.loads((ROOT / "data" / "tmax_physics" / "m3_station_days.json").read_text())
CANDS = ["F", "W1", "W2", "W3", "W4", "W6", "ALL", "W3d3", "W3d6"]


def full(r, key):
    n, e = r["night"], r["evening"]
    return [math.nan if v is None else float(v) for v in n[key] + r[key] + e[key]]


def arr(key):
    return np.array([full(r, key) for r in DAYS])


def sw_arr(key):
    a = np.full((len(DAYS), 24), np.nan)
    a[:, 6:18] = np.array([[math.nan if v is None else float(v) for v in r[key]] for r in DAYS])
    return a


def carried(anom, H, cand, lo):
    """Per day, the anomaly carried from anchor H (NaN where none)."""
    n = anom.shape[0]
    if cand == "F":
        return np.zeros(n)
    if cand == "ALL":
        k = 99
    else:
        k = int(cand[1])
    s = np.zeros(n); c = np.zeros(n)
    for h in range(H, lo - 1, -1):
        v = anom[:, h]
        take = ~np.isnan(v) & (c < k)
        s[take] += v[take]; c[take] += 1
    out = np.where(c > 0, s / np.maximum(c, 1), 0.0)
    return out


def decay(cand, dh):
    if cand.startswith("W3d"):
        return math.exp(-dh / float(cand[3:]))
    return 1.0


def score(var, obs, fc, anchors, last, mode, mask_rows):
    """Mean error over (day, anchor, later hour); mode diff|ratio|rain."""
    res = {}
    for cand in CANDS:
        err_sum = 0.0; n_sum = 0
        for H in anchors:
            lo = 6 if H >= 9 else 0
            if mode == "ratio":
                ok = (fc >= 20.0) & ~np.isnan(obs)
                o = np.where(ok, obs, np.nan); f = np.where(ok, fc, np.nan)
                kk = 99 if cand == "ALL" else (0 if cand == "F" else int(cand[1]))
                so = np.zeros(len(obs)); sf = np.zeros(len(obs)); c = np.zeros(len(obs))
                if kk:
                    for h in range(H, lo - 1, -1):
                        t = ~np.isnan(o[:, h]) & (c < kk)
                        so[t] += o[t, h]; sf[t] += f[t, h]; c[t] += 1
                ratio = np.where(sf > 0, np.clip(so / np.maximum(sf, 1e-9), 0, 3), 1.0)
            elif mode == "rain":
                wet = obs
                if cand == "F":
                    share = np.zeros(len(obs))
                else:
                    share = carried(wet, H, cand if not cand.startswith("W3d") else "W3", lo)
            else:
                base = carried(obs - fc, H, cand if not cand.startswith("W3d") else "W3", lo)
            for h in range(H + 1, last + 1):
                rows = mask_rows
                if mode == "ratio":
                    w = 1.0 if cand == "F" else (1.0 + (ratio - 1.0) * decay(cand, h - H))
                    pred = fc[:, h] * w
                    e = np.abs(pred - obs[:, h])
                elif mode == "rain":
                    p = (share * decay(cand, h - H)) >= 0.5
                    e = (p.astype(float) - obs[:, h]) ** 2
                else:
                    pred = fc[:, h] + base * decay(cand, h - H)
                    if var == "wind":
                        pred = np.maximum(pred, 0)
                    if var == "rh":
                        pred = np.clip(pred, 0, 100)
                    if var == "sw":
                        pred = np.maximum(pred, 0)
                    e = np.abs(pred - obs[:, h])
                good = rows & ~np.isnan(e)
                err_sum += float(e[good].sum()); n_sum += int(good.sum())
        res[cand] = err_sum / n_sum if n_sum else None
    return res


def main():
    dates = sorted({r["date"] for r in DAYS})
    cut = dates[int(len(dates) * 2 / 3)]
    early = np.array([r["date"] < cut for r in DAYS]); late = ~early
    allr = np.ones(len(DAYS), bool)
    wet = np.array([[math.nan if v is None else float(v) for v in r["night"]["wet_obs"] + r["wet_obs"] + r["evening"]["wet_obs"]]
                    for r in DAYS])
    specs = {
        "sw_ratio": ("sw", sw_arr("sw_obs"), sw_arr("sw_fc"), range(6, 17), 17, "ratio"),
        "sw_diff": ("sw", sw_arr("sw_obs"), sw_arr("sw_fc"), range(6, 17), 17, "diff"),
        "temp": ("temp", arr("t_obs"), arr("t_fc"), range(6, 23), 23, "diff"),
        "dew": ("dew", arr("dew_obs"), arr("dew_fc"), range(6, 23), 23, "diff"),
        "rh": ("rh", arr("rh_obs"), arr("rh_fc"), range(6, 23), 23, "diff"),
        "wind": ("wind", arr("wind_obs"), arr("wind_fc"), range(6, 23), 23, "diff"),
        "rain": ("rain", wet, None, range(6, 23), 23, "rain"),
    }
    out = {"n_days": len(DAYS), "cut_date": cut, "vars": {}}
    for name, (var, obs, fc, anchors, last, mode) in specs.items():
        got = {seg: score(var, obs, fc, anchors, last, mode, m) for seg, m in (("early", early), ("late", late), ("all", allr))}
        best_e = min(got["early"], key=lambda c: got["early"][c])
        best_l = min(got["late"], key=lambda c: got["late"][c])
        holds = got["late"][best_e] <= got["late"][best_l] * 1.02
        chosen = best_e if holds else min(got["all"], key=lambda c: got["all"][c])
        out["vars"][name] = {"scores": got, "best_early": best_e, "best_late": best_l, "early_choice_holds": holds,
                             "chosen": chosen}
        print(name, "early", best_e, "late", best_l, "holds", holds, "->", chosen,
              {c: round(v, 3) for c, v in got["late"].items()})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (Path(__file__).parent / f"reading_m11_{stamp}.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
