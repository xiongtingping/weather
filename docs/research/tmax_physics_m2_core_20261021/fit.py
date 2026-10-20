"""M2 per-station fit of the physics core and a pipeline self-check.

Pipeline validation only (user 2026-10-21 「先用已有数据进行下一步」): the store
holds the 14 GOES stations x 2024-12..2025-07 so far; everything is refitted
on the whole store and judged out of sample in M3.  Nothing printed here is a
reading.

Fit, per station, on measured heat: rise = tmax - t(06:00) = a + c*sqrt(Q_obs)
(OLS; the fleet line for reference).  Self-check: at each last-measured hour
H, the core's dT(H) beside y = tmax - day-ahead forecast tmax (station
demeaned) -- the correlation and the slope of y on dT, in sample.
Writes data/tmax_physics/m2_fit.json.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import core  # noqa: E402

ROOT = HERE.parents[2]
DAYS = ROOT / "data" / "tmax_physics" / "m2_station_days.json"
OUT = ROOT / "data" / "tmax_physics" / "m2_fit.json"


def ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    r2 = 1.0 - resid.var() / y.var() if y.var() > 0 else float("nan")
    return float(beta[0]), float(beta[1]), float(r2)


def main() -> int:
    rows = json.loads(DAYS.read_text())
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["station"], []).append(r)
    fits = {}
    xs_all, ys_all = [], []
    for st, rs in sorted(by.items()):
        x = np.array([math.sqrt(core.heat_mj(r["sw_obs"])) for r in rs])
        y = np.array([r["tmax"] - r["t_obs"][0] for r in rs])
        a, c, r2 = ols(x, y)
        fits[st] = {"n": len(rs), "a": round(a, 3), "c": round(c, 4), "r2": round(r2, 3),
                    "implied_g_f_K_per_km": round(core.implied_g_f(c) * 1000.0, 3),
                    "q_obs_mean_mj": round(float((x ** 2).mean()), 2)}
        xs_all.append(x)
        ys_all.append(y)
    a0, c0, r20 = ols(np.concatenate(xs_all), np.concatenate(ys_all))
    check = {}
    for H in (9, 11, 13, 15):
        dts, ys = [], []
        for st, rs in by.items():
            yy = np.array([r["tmax"] - r["tmax_fc"] for r in rs])
            yy = yy - yy.mean()
            for r, y in zip(rs, yy):
                dts.append(core.delta_t(fits[st]["c"], r["sw_obs"], r["sw_fc"], H))
                ys.append(y)
        dts, ys = np.array(dts), np.array(ys)
        _, slope, r2 = ols(dts, ys)
        check[str(H)] = {"corr": round(float(np.corrcoef(dts, ys)[0, 1]), 3), "slope_y_on_dT": round(slope, 3),
                         "dT_sd": round(float(dts.std()), 3), "n": int(len(ys))}
    out = {"schema": "tmax_physics_m2_fit_v0", "status": "PIPELINE_VALIDATION_NOT_A_READING",
           "data": {"station_days": len(rows), "stations": len(by),
                    "dates": [min(r["date"] for r in rows), max(r["date"] for r in rows)]},
           "fleet": {"a": round(a0, 3), "c": round(c0, 4), "r2": round(r20, 3)},
           "stations": fits, "self_check_in_sample": check}
    OUT.write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("data", "fleet", "self_check_in_sample")}, indent=1))
    for st, f in fits.items():
        print(st, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
