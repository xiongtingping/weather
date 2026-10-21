"""M3 evaluator (PROTOCOL.md, committed c30cb32a before any reading; addendum
1 at 40f14290).

    --smoke   pipeline check: row counts, finite predictions, no scores
    (no flag) the protocol's single judgement run: writes reading_<UTC>.json

Run from the repo root after ``features.py``.

Vectorised 2026-10-21 for the whole store (43,845 station-days, ~130 refits):
the per-row Python of 1097a03a would take about an hour.  Same fits, same
predictions, same bootstrap draws in the same order (checked against
1097a03a on a station subset); the addendum's Europe/Africa-by-source entries
come after every original entry so they draw from the generator last.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
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
REGIONS = ("all", "asia_pacific", "europe_africa", "americas_goes")
SPLITS = ("europe_africa|board_product", "europe_africa|with_sarah3")   # addendum 1, report only
PAIRS = {"AB-sun_line": ("AB", "sun_line"), "AB-rh_line": ("AB", "rh_line"), "AB-B": ("AB", "B"),
         "AB_station-AB": ("AB_station", "AB")}


def region(st: str) -> str:
    p = _satellite_product(_COORDINATES[st][1])
    return "asia_pacific" if p == "jaxa_ptree_himawari" else "americas_goes" if p is None else "europe_africa"


def _newest(obs, fc, i):
    for j in range(i, -1, -1):
        if obs[j] is not None and fc[j] is not None:
            return obs[j] - fc[j]
    return None


def feats(r: dict, H: int) -> dict:
    if H < 9:
        return night_feats(r, H)
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


def night_feats(r: dict, H: int) -> dict:
    """PROTOCOL_M6: at 0..8 h the report features look back to 00 h (the
    night's arrays before the day's 06..), rain counts from 00 h, sunshine
    only from 06 h (0 before)."""

    n = r.get("night") or {}
    full = lambda k: list(n.get(k) or [None] * 6) + list(r[k])  # noqa: E731 -- index = local hour
    rh = _newest(full("rh_obs"), full("rh_fc"), H)
    sw = 0.0 if H < 6 else float(np.mean([o - f for o, f in zip(r["sw_obs"][:H - 5], r["sw_fc"][:H - 5])]))
    return {
        "sw": max(-300.0, min(300.0, sw)),
        "rh": None if rh is None else max(-20.0, min(20.0, rh)),
        "dew": _newest(full("dew_obs"), full("dew_fc"), H),
        "wind": _newest(full("wind_obs"), full("wind_fc"), H),
        "tdev": _newest(full("t_obs"), full("t_fc"), H),
        "rain": float(sum(1 for w in full("wet_obs")[:H + 1] if w)),
    }


# --- the board's current lines, applied as the board does -----------------
SUN = json.loads((ROOT / "config" / "ui" / "sun_tmax_shift_v2.json").read_text())
RH = json.loads((ROOT / "config" / "ui" / "rh_tmax_shift_v1.json").read_text())


def sun_line(f: dict, H: int) -> float:
    line = SUN["by_last_hour"].get(str(min(H, max(int(h) for h in SUN["by_last_hour"]))))
    if line is None:   # the board's line starts at 9 h
        return 0.0
    x = max(-float(SUN["x_clip_wm2"]), min(float(SUN["x_clip_wm2"]), f["sw"]))
    side = "dark" if x < 0 else "bright"
    return line[side + "_c_per_100wm2"] / 100.0 * x


def rh_line(f: dict, H: int) -> float:
    if f["rh"] is None:
        return 0.0
    line = RH["by_hour"].get(str(min(H, max(int(h) for h in RH["by_hour"]))))
    if line is None:
        return 0.0
    x = max(-float(RH["x_clip_pp"]), min(float(RH["x_clip_pp"]), f["rh"]))
    side = "drier" if x < 0 else "moister"
    ci = line[side + "_ci95"]
    return 0.0 if ci[0] <= 0.0 <= ci[1] else line[side + "_c_per_pp"] * x


# --- precomputed arrays ------------------------------------------------------
class Data:
    def __init__(self, rows: list[dict]) -> None:
        self.stations = sorted({r["station"] for r in rows})
        sidx = {s: i for i, s in enumerate(self.stations)}
        self.dates = sorted({r["date"] for r in rows})
        didx = {d: i for i, d in enumerate(self.dates)}
        self.n = len(rows)
        self.st = np.array([sidx[r["station"]] for r in rows])
        self.day = np.array([didx[r["date"]] for r in rows])
        self.month = np.array([r["month"] for r in rows])
        self.y = np.array([r["tmax"] - r["tmax_fc"] for r in rows], dtype=float)
        self.rise = np.array([r["tmax"] - r["t_obs"][0] for r in rows], dtype=float)
        self.sqrtq = np.array([math.sqrt(core.heat_mj(r["sw_obs"])) for r in rows])
        self.region = np.array([region(r["station"]) for r in rows])
        self.split = np.array([("europe_africa|with_sarah3" if "eumetsat_sarah3" in r.get("sw_sources", [])
                                else "europe_africa|board_product") if region(r["station"]) == "europe_africa" else ""
                               for r in rows])
        self.F, self.G, self.SUN, self.RH = {}, {}, {}, {}
        for H in CHECK_HOURS:
            fs = [feats(r, H) for r in rows]
            self.F[H] = np.array([[0.0 if f[k] is None else f[k] for k in B_FEATS] for f in fs], dtype=float)
            self.G[H] = np.array([core.delta_t(1.0, r["sw_obs"], r["sw_fc"], H) for r in rows])
            self.SUN[H] = np.array([sun_line(f, H) for f in fs])
            self.RH[H] = np.array([rh_line(f, H) for f in fs])


def _sd_by_station(resid: np.ndarray, st: np.ndarray, n_st: int) -> np.ndarray:
    """sqrt of (n*mean r^2 + K*fleet mean r^2)/(n+K) per station; last = fleet."""

    s2f = float(np.mean(resid ** 2))
    cnt = np.bincount(st, minlength=n_st).astype(float)
    ss = np.bincount(st, weights=resid ** 2, minlength=n_st)
    out = np.full(n_st + 1, math.sqrt(s2f))
    have = cnt > 0
    out[:n_st][have] = np.sqrt((ss[have] + K * s2f) / (cnt[have] + K))
    return out


def fit_phys(D: Data, m: np.ndarray) -> np.ndarray:
    """c per station (index n_st = fleet): within station x month, groups of
    3+ only, shrunk with n = the station's rows in those groups."""

    n_st = len(D.stations)
    key = D.st[m] * 13 + D.month[m]
    x, y, st = D.sqrtq[m], D.rise[m], D.st[m]
    cnt = np.bincount(key, minlength=n_st * 13)
    keep = cnt[key] >= 3
    mx = np.bincount(key, weights=x, minlength=n_st * 13) / np.maximum(cnt, 1)
    my = np.bincount(key, weights=y, minlength=n_st * 13) / np.maximum(cnt, 1)
    dx, dy, s = (x - mx[key])[keep], (y - my[key])[keep], st[keep]
    fleet = float((dx * dy).sum() / max((dx ** 2).sum(), 1e-9)) if dx.size else 0.0
    sxy = np.bincount(s, weights=dx * dy, minlength=n_st)
    sxx = np.bincount(s, weights=dx * dx, minlength=n_st)
    n = np.bincount(s, minlength=n_st).astype(float)
    c = np.full(n_st + 1, fleet)
    have = n > 0
    c_st = sxy[have] / np.maximum(sxx[have], 1e-9)
    c[:n_st][have] = fleet + n[have] / (n[have] + K) * (c_st - fleet)
    return c


def fit_linear(D: Data, m: np.ndarray, H: int, cphys: np.ndarray | None, per_station: bool) -> dict:
    n_st = len(D.stations)
    idx = np.flatnonzero(m)
    present = np.unique(D.st[idx])
    y = D.y[idx]
    X = D.F[H][idx]
    if cphys is not None:
        X = np.column_stack([X, cphys[D.st[idx]] * D.G[H][idx]])
    pos = {s: j for j, s in enumerate(present)}
    S = np.zeros((idx.size, present.size))
    S[np.arange(idx.size), [pos[s] for s in D.st[idx]]] = 1.0
    beta, *_ = np.linalg.lstsq(np.hstack([S, X]), y, rcond=None)
    icpt = np.full(n_st, np.nan)
    icpt[present] = beta[:present.size]
    fleet = beta[present.size:]
    coef = np.tile(fleet, (n_st, 1))
    if per_station:
        for s in present:
            rows = np.flatnonzero(D.st[idx] == s)
            if rows.size < 30:
                continue
            Xs = np.hstack([np.ones((rows.size, 1)), X[rows]])
            bs, *_ = np.linalg.lstsq(Xs, y[rows], rcond=None)
            coef[s] = fleet + rows.size / (rows.size + K) * (bs[1:] - fleet)
            icpt[s] = float(np.mean(y[rows] - X[rows] @ coef[s]))
    resid = y - icpt[D.st[idx]] - np.einsum("ij,ij->i", X, coef[D.st[idx]])
    return {"icpt": icpt, "icpt_fallback": float(np.mean(icpt[present])), "coef": coef,
            "sd": _sd_by_station(resid, D.st[idx], n_st)}


def fit_all(D: Data, m: np.ndarray) -> dict:
    n_st = len(D.stations)
    cphys = fit_phys(D, m)
    cnt = np.bincount(D.st[m], minlength=n_st)
    mean = np.where(cnt > 0, np.bincount(D.st[m], weights=D.y[m], minlength=n_st) / np.maximum(cnt, 1), np.nan)
    mean_fleet = float(np.mean(D.y[m]))
    mean_full = np.where(np.isnan(mean), mean_fleet, mean)
    out = {"cphys": cphys, "mean": mean_full, "H": {}}
    yv = D.y[m] - mean_full[D.st[m]]
    for H in CHECK_HOURS:
        out["H"][H] = {"sd_mean": _sd_by_station(yv, D.st[m], n_st),
                       "sd_sun": _sd_by_station(yv - D.SUN[H][m], D.st[m], n_st),
                       "sd_rh": _sd_by_station(yv - D.RH[H][m], D.st[m], n_st),
                       "B": fit_linear(D, m, H, None, False),
                       "AB": fit_linear(D, m, H, cphys, False),
                       "AB_station": fit_linear(D, m, H, cphys, True)}
    return out


def _sd_pick(sd: np.ndarray, st: np.ndarray, seen: np.ndarray) -> np.ndarray:
    return np.where(seen[st], sd[st], sd[-1])


def predict(D: Data, model: dict, rows: np.ndarray, H: int, seen: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    st = D.st[rows]
    m = model["mean"][st]
    h = model["H"][H]
    out = {"mean": (m, _sd_pick(h["sd_mean"], st, seen)),
           "sun_line": (m + D.SUN[H][rows], _sd_pick(h["sd_sun"], st, seen)),
           "rh_line": (m + D.RH[H][rows], _sd_pick(h["sd_rh"], st, seen))}
    for arm, phys in (("B", False), ("AB", True), ("AB_station", True)):
        fm = h[arm]
        X = D.F[H][rows]
        if phys:
            c = np.where(seen[st], model["cphys"][st], model["cphys"][-1])
            X = np.column_stack([X, c * D.G[H][rows]])
        icpt = np.where(np.isnan(fm["icpt"][st]), fm["icpt_fallback"], fm["icpt"][st])
        coef = fm["coef"][st]
        out[arm] = (icpt + np.einsum("ij,ij->i", X, coef), _sd_pick(fm["sd"], st, seen))
    return out


def scores(y: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    z = (y - mu) / sd
    ls = 0.5 * np.log(2 * np.pi * sd * sd) + 0.5 * z * z
    pdf = np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    cdf = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
    crps = sd * (z * (2 * cdf - 1) + 2 * pdf - 1 / math.sqrt(math.pi))
    return np.column_stack([ls, crps, np.abs(y - mu)])


def walk_forward(D: Data) -> dict:
    """Per H: test row indices and each arm's (mu, sd)."""

    n_days = len(D.dates)
    res = {H: {"rows": [], **{a: ([], []) for a in ARMS}} for H in CHECK_HOURS}
    if n_days <= WARMUP_DAYS:
        return res
    model, fitted_at, seen = None, -REFIT_EVERY, None
    for k in range(WARMUP_DAYS, n_days):
        if model is None or k - fitted_at >= REFIT_EVERY:
            m = D.day < k
            model, fitted_at = fit_all(D, m), k
            seen = np.bincount(D.st[m], minlength=len(D.stations)) > 0
        rows = np.flatnonzero(D.day == k)
        if not rows.size:
            continue
        for H in CHECK_HOURS:
            pr = predict(D, model, rows, H, seen)
            res[H]["rows"].append(rows)
            for a in ARMS:
                res[H][a][0].append(pr[a][0]); res[H][a][1].append(pr[a][1])
    for H in CHECK_HOURS:
        res[H]["rows"] = np.concatenate(res[H]["rows"])
        for a in ARMS:
            res[H][a] = (np.concatenate(res[H][a][0]), np.concatenate(res[H][a][1]))
    return res


def _entry(D: Data, res: dict, H: int, sel: np.ndarray, rng) -> dict:
    rows = res[H]["rows"][sel]
    y = D.y[rows]
    sc = {a: scores(y, res[H][a][0][sel], res[H][a][1][sel]) for a in ARMS}
    day_of = D.day[rows]
    days, inv = np.unique(day_of, return_inverse=True)
    nd = days.size
    cnt = np.bincount(inv, minlength=nd).astype(float)
    entry = {"n": int(rows.size), "days": int(nd),
             "mean": {a: [round(float(v), 4) for v in sc[a].mean(axis=0)] for a in ARMS}, "pairs": {}}
    for name, (a, b) in PAIRS.items():
        d = sc[a] - sc[b]
        sums = np.column_stack([np.bincount(inv, weights=d[:, j], minlength=nd) for j in range(3)])
        draws = np.empty((DRAWS, 3))
        for t in range(DRAWS):
            starts = rng.integers(0, nd, size=math.ceil(nd / BLOCK))
            pick = ((starts[:, None] + np.arange(BLOCK)[None, :]) % nd).ravel()
            draws[t] = sums[pick].sum(axis=0) / cnt[pick].sum()
        entry["pairs"][name] = {"mean": [round(float(v), 4) for v in d.mean(axis=0)],
                                "ci95": [[round(float(np.quantile(draws[:, j], q)), 4) for q in (0.025, 0.975)]
                                         for j in range(3)]}
    return entry


def summarise(D: Data, res: dict) -> dict:
    rng = np.random.default_rng(SEED)
    out: dict = {}
    for H in CHECK_HOURS:
        reg = D.region[res[H]["rows"]]
        for g in REGIONS:
            sel = np.ones(reg.size, bool) if g == "all" else reg == g
            if sel.any():
                out[f"{H}|{g}"] = _entry(D, res, H, sel, rng)
    for H in CHECK_HOURS:   # addendum 1: report only, drawn after every original entry
        sp = D.split[res[H]["rows"]]
        for g in SPLITS:
            sel = sp == g
            if sel.any():
                out[f"{H}|{g}"] = _entry(D, res, H, sel, rng)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    rows = json.loads(DAYS.read_text())
    D = Data(rows)
    res = walk_forward(D)
    if args.smoke:
        finite = all(np.isfinite(res[H][a][i]).all() for H in CHECK_HOURS for a in ARMS for i in (0, 1))
        print(json.dumps({"station_days": D.n, "stations": len(D.stations), "dates": len(D.dates),
                          "test_predictions": int(sum(res[H]["rows"].size for H in CHECK_HOURS)),
                          "test_days": int(np.unique(D.day[res[CHECK_HOURS[0]]["rows"]]).size),
                          "all_finite": bool(finite), "note": "smoke only -- no scores printed"}, indent=1))
        return 0
    reading = {"schema": "tmax_physics_m3_reading_v1", "protocol": "c30cb32a+40f14290",
               "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "metrics": ["LogScore", "CRPS", "MAE"],
               "data": {"station_days": D.n, "stations": D.stations, "dates": [D.dates[0], D.dates[-1]]},
               "summary": summarise(D, res)}
    stamp = reading["read_at_utc"].replace(":", "").replace("-", "")
    (HERE / f"reading_{stamp}.json").write_text(json.dumps(reading, indent=1))
    print("written", f"reading_{stamp}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
