"""M3 evaluator (PROTOCOL.md, committed c30cb32a before any reading).

    --smoke   pipeline check on whatever the store holds: row counts, finite
              predictions, no scores printed (the store is not whole yet)
    (no flag) the protocol's single judgement run: writes reading_<UTC>.json

Run from the repo root after ``features.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docs" / "research" / "tmax_physics_m2_core_20261021"))
import core  # noqa: E402

from bayes_basket_target.station_day_board import _satellite_product  # noqa: E402
from bayes_basket_target.station_registry import _COORDINATES  # noqa: E402

DAYS = ROOT / "data" / "tmax_physics" / "m3_station_days.json"
CHECK_HOURS = (9, 11, 13, 15, 17)
WARMUP_DAYS, REFIT_EVERY, K = 120, 7, 100.0
BLOCK, DRAWS, SEED = 3, 4000, 20261021
ARMS = ("mean", "sun_line", "rh_line", "B", "AB", "AB_station")
B_FEATS = ("sw", "rh", "dew", "wind", "tdev", "rain")


def region(st: str) -> str:
    p = _satellite_product(_COORDINATES[st][1])
    return "asia_pacific" if p == "jaxa_ptree_himawari" else "americas_goes" if p is None else "europe_africa"


def _newest(obs, fc, i):
    for j in range(i, -1, -1):
        if obs[j] is not None and fc[j] is not None:
            return obs[j] - fc[j]
    return None


def feats(r: dict, H: int) -> dict:
    i = H - 6
    sw = float(np.mean([o - f for o, f in zip(r["sw_obs"][:i + 1], r["sw_fc"][:i + 1])]))
    rh = _newest(r["rh_obs"], r["rh_fc"], i)
    return {
        "sw": max(-300.0, min(300.0, sw)),
        "rh": None if rh is None else max(-20.0, min(20.0, rh)),
        "dew": _newest(r["dew_obs"], r["dew_fc"], i),
        "wind": _newest(r["wind_obs"], r["wind_fc"], i),
        "tdev": _newest(r["t_obs"], r["t_fc"], i),
        "rain": float(sum(1 for w in r["wet_obs"][:i + 1] if w)),
    }


# --- the board's current lines, applied as the board does -----------------
SUN = json.loads((ROOT / "config" / "ui" / "sun_tmax_shift_v2.json").read_text())
RH = json.loads((ROOT / "config" / "ui" / "rh_tmax_shift_v1.json").read_text())


def sun_line(f: dict, H: int) -> float:
    line = SUN["by_last_hour"][str(min(H, max(int(h) for h in SUN["by_last_hour"])))]
    x = max(-float(SUN["x_clip_wm2"]), min(float(SUN["x_clip_wm2"]), f["sw"]))
    side = "dark" if x < 0 else "bright"
    return line[side + "_c_per_100wm2"] / 100.0 * x


def rh_line(f: dict, H: int) -> float:
    if f["rh"] is None:
        return 0.0
    line = RH["by_hour"][str(min(H, max(int(h) for h in RH["by_hour"])))]
    x = max(-float(RH["x_clip_pp"]), min(float(RH["x_clip_pp"]), f["rh"]))
    side = "drier" if x < 0 else "moister"
    ci = line[side + "_ci95"]
    return 0.0 if ci[0] <= 0.0 <= ci[1] else line[side + "_c_per_pp"] * x


# --- fitting on a training set ---------------------------------------------
def shrink(n: float, station, fleet):
    w = n / (n + K)
    return fleet + w * (station - fleet)


def fit_phys(train: list[dict]) -> dict[str, float]:
    """c per station from rise = tmax - t(06) on sqrt(Q_obs 06..15), within
    station x month (season out), shrunk to the fleet's within-c."""

    groups: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for r in train:
        groups.setdefault((r["station"], r["month"]), []).append(
            (math.sqrt(core.heat_mj(r["sw_obs"])), r["tmax"] - r["t_obs"][0]))
    dx: dict[str, list[float]] = {}
    dy: dict[str, list[float]] = {}
    for (st, _m), pts in groups.items():
        if len(pts) < 3:
            continue
        x = np.array([p[0] for p in pts]); y = np.array([p[1] for p in pts])
        dx.setdefault(st, []).extend(x - x.mean()); dy.setdefault(st, []).extend(y - y.mean())
    allx = np.concatenate([np.array(v) for v in dx.values()]) if dx else np.array([0.0])
    ally = np.concatenate([np.array(v) for v in dy.values()]) if dy else np.array([0.0])
    fleet = float((allx * ally).sum() / max((allx ** 2).sum(), 1e-9))
    out = {"_fleet": fleet}
    for st in dx:
        x, y = np.array(dx[st]), np.array(dy[st])
        c_st = float((x * y).sum() / max((x ** 2).sum(), 1e-9))
        out[st] = float(shrink(len(x), c_st, fleet))
    return out


def design(rows: list[dict], H: int, with_phys: bool, cphys: dict[str, float]) -> np.ndarray:
    cols = []
    for r in rows:
        f = r["_f"][H]
        v = [0.0 if f[k] is None else f[k] for k in B_FEATS]
        if with_phys:
            v.append(core.delta_t(cphys.get(r["station"], cphys["_fleet"]), r["sw_obs"], r["sw_fc"], H))
        cols.append(v)
    return np.array(cols, dtype=float)


def fit_linear(train: list[dict], H: int, with_phys: bool, cphys, per_station: bool) -> dict:
    stations = sorted({r["station"] for r in train})
    y = np.array([r["tmax"] - r["tmax_fc"] for r in train])
    X = design(train, H, with_phys, cphys)
    S = np.array([[1.0 if r["station"] == s else 0.0 for s in stations] for r in train])
    beta, *_ = np.linalg.lstsq(np.hstack([S, X]), y, rcond=None)
    icpt = dict(zip(stations, beta[:len(stations)]))
    fleet = beta[len(stations):]
    coef = {s: fleet for s in stations}
    if per_station:
        for s in stations:
            idx = [i for i, r in enumerate(train) if r["station"] == s]
            if len(idx) < 30:
                continue
            Xs = np.hstack([np.ones((len(idx), 1)), X[idx]])
            bs, *_ = np.linalg.lstsq(Xs, y[idx], rcond=None)
            coef[s] = shrink(len(idx), bs[1:], fleet)
            icpt[s] = float(np.mean(y[idx] - X[idx] @ coef[s]))
    resid: dict[str, list[float]] = {}
    for i, r in enumerate(train):
        s = r["station"]
        resid.setdefault(s, []).append(float(y[i] - icpt[s] - X[i] @ coef[s]))
    return {"icpt": icpt, "coef": coef, "fleet": fleet, "sd": _sd(resid)}


def _sd(resid: dict[str, list[float]]) -> dict[str, float]:
    allr = np.concatenate([np.array(v) for v in resid.values()])
    s2f = float(np.mean(allr ** 2))
    out = {"_fleet": math.sqrt(s2f)}
    for s, v in resid.items():
        v = np.array(v)
        out[s] = math.sqrt((len(v) * float(np.mean(v ** 2)) + K * s2f) / (len(v) + K))
    return out


def fit_all(train: list[dict]) -> dict:
    cphys = fit_phys(train)
    y_by: dict[str, list[float]] = {}
    for r in train:
        y_by.setdefault(r["station"], []).append(r["tmax"] - r["tmax_fc"])
    mean = {s: float(np.mean(v)) for s, v in y_by.items()}
    allm = float(np.mean([r["tmax"] - r["tmax_fc"] for r in train]))
    out = {"cphys": cphys, "mean": mean, "mean_fleet": allm, "H": {}}
    for H in CHECK_HOURS:
        res: dict[str, list[float]] = {}
        res_sun: dict[str, list[float]] = {}
        res_rh: dict[str, list[float]] = {}
        for r in train:
            yv = r["tmax"] - r["tmax_fc"] - mean[r["station"]]
            res.setdefault(r["station"], []).append(yv)
            res_sun.setdefault(r["station"], []).append(yv - sun_line(r["_f"][H], H))
            res_rh.setdefault(r["station"], []).append(yv - rh_line(r["_f"][H], H))
        out["H"][H] = {"sd_mean": _sd(res), "sd_sun": _sd(res_sun), "sd_rh": _sd(res_rh),
                       "B": fit_linear(train, H, False, cphys, False),
                       "AB": fit_linear(train, H, True, cphys, False),
                       "AB_station": fit_linear(train, H, True, cphys, True)}
    return out


def predict(model: dict, r: dict, H: int) -> dict[str, tuple[float, float]]:
    s = r["station"]
    m = model["mean"].get(s, model["mean_fleet"])
    h = model["H"][H]
    pick = lambda d: d.get(s, d["_fleet"])  # noqa: E731
    out = {"mean": (m, pick(h["sd_mean"])),
           "sun_line": (m + sun_line(r["_f"][H], H), pick(h["sd_sun"])),
           "rh_line": (m + rh_line(r["_f"][H], H), pick(h["sd_rh"]))}
    for arm, phys in (("B", False), ("AB", True), ("AB_station", True)):
        fm = h[arm]
        x = design([r], H, phys, model["cphys"])[0]
        icpt = fm["icpt"].get(s, float(np.mean(list(fm["icpt"].values()))))
        out[arm] = (float(icpt + x @ fm["coef"].get(s, fm["fleet"])), pick(fm["sd"]))
    return out


def scores(y: float, mu: float, sd: float) -> tuple[float, float, float]:
    z = (y - mu) / sd
    ls = 0.5 * math.log(2 * math.pi * sd * sd) + 0.5 * z * z
    pdf, cdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi), 0.5 * (1 + math.erf(z / math.sqrt(2)))
    crps = sd * (z * (2 * cdf - 1) + 2 * pdf - 1 / math.sqrt(math.pi))
    return ls, crps, abs(y - mu)


def walk_forward(rows: list[dict]) -> list[dict]:
    dates = sorted({r["date"] for r in rows})
    if len(dates) <= WARMUP_DAYS:
        return []
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    out, model, fitted_at = [], None, -REFIT_EVERY
    for k, d in enumerate(dates[WARMUP_DAYS:], start=WARMUP_DAYS):
        if model is None or k - fitted_at >= REFIT_EVERY:
            model = fit_all([r for dd in dates[:k] for r in by_date[dd]])
            fitted_at = k
        for r in by_date[d]:
            y = r["tmax"] - r["tmax_fc"]
            for H in CHECK_HOURS:
                pr = predict(model, r, H)
                out.append({"date": d, "station": r["station"], "region": region(r["station"]), "H": H,
                            "y": y, "pred": pr})
    return out


def summarise(res: list[dict]) -> dict:
    rng = np.random.default_rng(SEED)
    out: dict = {}
    for H in CHECK_HOURS:
        for reg in ("all", "asia_pacific", "europe_africa", "americas_goes"):
            rr = [x for x in res if x["H"] == H and (reg == "all" or x["region"] == reg)]
            if not rr:
                continue
            sc = {a: np.array([scores(x["y"], *x["pred"][a]) for x in rr]) for a in ARMS}
            days = sorted({x["date"] for x in rr})
            idx_by_day: dict[str, list[int]] = {}
            for i, x in enumerate(rr):
                idx_by_day.setdefault(x["date"], []).append(i)
            pairs = {"AB-sun_line": ("AB", "sun_line"), "AB-rh_line": ("AB", "rh_line"), "AB-B": ("AB", "B"),
                     "AB_station-AB": ("AB_station", "AB")}
            entry = {"n": len(rr), "days": len(days),
                     "mean": {a: [round(float(v), 4) for v in sc[a].mean(axis=0)] for a in ARMS}, "pairs": {}}
            for name, (a, b) in pairs.items():
                d = sc[a] - sc[b]
                draws = []
                for _ in range(DRAWS):
                    starts = rng.integers(0, len(days), size=math.ceil(len(days) / BLOCK))
                    pick = [i for s0 in starts for j in range(BLOCK) for i in idx_by_day[days[(s0 + j) % len(days)]]]
                    draws.append(d[pick].mean(axis=0))
                draws = np.array(draws)
                entry["pairs"][name] = {"mean": [round(float(v), 4) for v in d.mean(axis=0)],
                                        "ci95": [[round(float(np.quantile(draws[:, m], q)), 4) for q in (0.025, 0.975)]
                                                 for m in range(3)]}
            out[f"{H}|{reg}"] = entry
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    rows = json.loads(DAYS.read_text())
    for r in rows:
        r["_f"] = {H: feats(r, H) for H in CHECK_HOURS}
    res = walk_forward(rows)
    if args.smoke:
        finite = all(math.isfinite(v) for x in res for a in ARMS for v in x["pred"][a]) if res else None
        print(json.dumps({"station_days": len(rows), "stations": len({r["station"] for r in rows}),
                          "dates": len({r["date"] for r in rows}), "test_predictions": len(res),
                          "test_days": len({x["date"] for x in res}), "all_finite": finite,
                          "missing_rh_at_13": sum(1 for r in rows if r["_f"][13]["rh"] is None),
                          "note": "smoke only -- no scores printed"}, indent=1))
        return 0
    reading = {"schema": "tmax_physics_m3_reading_v1", "protocol": "c30cb32a",
               "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "metrics": ["LogScore", "CRPS", "MAE"], "summary": summarise(res)}
    stamp = reading["read_at_utc"].replace(":", "").replace("-", "")
    (HERE / f"reading_{stamp}.json").write_text(json.dumps(reading, indent=1))
    print("written", f"reading_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
