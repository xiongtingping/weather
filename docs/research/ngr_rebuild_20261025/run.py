#!/usr/bin/env python3
"""NGR rebuild (PROTOCOL.md): arms P (current P0 with F), N1, N2; walk-forward on records.pkl.

    python3 docs/research/ngr_rebuild_20261025/run.py
"""

from __future__ import annotations

import json
import math
import pickle
import statistics
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import ndtr

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "det_ens_blend_20261025"))
import run as R  # noqa: E402  (Batch, det_features, fit_bias, arm B, compare, boot)

FAMILIES = ("ECMWF", "ECMWF_AIFS", "NOAA_GEFS", "NOAA_AIGEFS", "DWD_ICON_EPS", "ECCC_GEM")
NF = len(FAMILIES)
N_PARAMS = {"N1": NF + 7, "N2": NF + 9}


def load() -> list[dict]:
    recs = pickle.loads((HERE / "records.pkl").read_bytes())
    return [r for r in recs if len([k for k in r["systems"] if k in R.ROSTER]) >= 3]


class NBatch(R.Batch):
    def __init__(self, recs):
        for r in recs:
            r["systems"] = {k: v for k, v in r["systems"].items() if k in R.ROSTER}
        super().__init__(recs)
        n = len(recs)
        self.m = np.zeros((n, NF))
        self.s = np.ones((n, NF))
        self.has = np.zeros((n, NF), bool)
        width = self.shi - self.slo
        for i, r in enumerate(recs):
            k = len(r["p"])
            for j, fam in enumerate(FAMILIES):
                vec = r["family_vectors"].get(fam)
                if vec is None:
                    continue
                v = np.array(vec, float)
                v = v / v.sum()
                mid = self.mid[i, :k]
                mu = float((v * mid).sum())
                var = float((v * (mid - mu) ** 2).sum() + (v * width[i, :k] ** 2).sum() / 12.0)
                self.m[i, j], self.s[i, j], self.has[i, j] = mu, math.sqrt(max(var, 1e-4)), True
        self.rad = np.array([r["rad_z"] for r in recs])

    def ngr(self, arm: str, theta: np.ndarray, F: np.ndarray, Z: np.ndarray) -> np.ndarray:
        beta = theta[:NF]
        a, w0, w1, c0, c1, c2, c3 = theta[NF:NF + 7]
        pi = np.where(self.has, np.exp(np.clip(beta, -8, 8))[None, :], 0.0)
        tot = pi.sum(1)
        muE = (pi * self.m).sum(1) / tot
        within = (pi * self.s ** 2).sum(1) / tot
        between = (pi * (self.m - muE[:, None]) ** 2).sum(1) / tot
        SE = np.sqrt(within + between)
        mu = muE + a + (w0 + w1 * self.L) * (F - muE)
        if arm == "N2":
            rw, rc = theta[NF + 7:NF + 9]
            mu = mu + rw * np.maximum(self.rad, 0.0) + rc * np.minimum(self.rad, 0.0)
        logsig = c0 + c1 * self.L + c2 * np.log(SE) + c3 * Z
        sig = np.exp(np.clip(logsig, -3.0, 2.5))
        lo = np.where(np.isfinite(self.tlo), (self.tlo - mu[:, None]) / sig[:, None], -np.inf)
        hi = np.where(np.isfinite(self.thi), (self.thi - mu[:, None]) / sig[:, None], np.inf)
        q = np.where(self.mask, ndtr(hi) - ndtr(lo), 0.0)
        return self.floor(np.clip(q, 0.0, None))


def fit_ngr(arm, B, F, Z, warm):
    k = N_PARAMS[arm]
    x0 = np.zeros(k)
    x0[NF + 5] = 1.0
    if warm is not None:
        x0[: min(k, len(warm))] = warm[:k]

    def obj(theta):
        return float(B.scores(B.ngr(arm, theta, F, Z))["logscore"].mean())

    return minimize(obj, x0, method="L-BFGS-B", options={"maxiter": 300}).x


def main() -> int:
    recs = load()
    print("records", len(recs), "days", len({r["day"] for r in recs}), flush=True)
    days = sorted({r["day"] for r in recs})
    rows: dict[str, list[dict]] = {}
    path = {"P": [], "N1": [], "N2": []}
    warm = {"P": None, "N1": None, "N2": None}
    for day in [d for d in days if d >= R.SCORE_FROM]:
        train = [r for r in recs if r["day"] < day]
        if len({r["day"] for r in train}) < R.MIN_TRAIN_DAYS:
            continue
        test = [r for r in recs if r["day"] == day and r["decision_at_utc"] >= R.SCORE_FROM]
        if not test:
            continue
        bias = R.fit_bias(train)
        Btr, Bte = NBatch(train), NBatch(test)
        Ftr, Ztr = R.det_features(train, bias)
        Fte, Zte = R.det_features(test, bias)
        thP = R.fit_arm("B", Btr, Ftr, Ztr, warm["P"])
        warm["P"] = thP
        qs = {"A": R.arm_vector("A", None, Bte, Fte, Zte), "P": R.arm_vector("B", thP, Bte, Fte, Zte)}
        path["P"].append({"day": day, "theta": [round(float(t), 4) for t in thP]})
        for arm in ("N1", "N2"):
            th = fit_ngr(arm, Btr, Ftr, Ztr, warm[arm])
            warm[arm] = th
            path[arm].append({"day": day, "theta": [round(float(t), 4) for t in th]})
            qs[arm] = Bte.ngr(arm, th, Fte, Zte)
        sc = {a: Bte.scores(q) for a, q in qs.items()}
        for i, r in enumerate(test):
            row = {"day": day, "station": r["station"], "family": r["family"]}
            for a in qs:
                for k in ("logscore", "brier", "crps"):
                    row[f"{a}_{k}"] = float(sc[a][k][i])
                q, mid = qs[a][i], Bte.mid[i]
                mu = float((q * mid).sum())
                sd = math.sqrt(max(float((q * (mid - mu) ** 2).sum()) + float((Bte.shi[i] - Bte.slo[i]).mean()) ** 2 / 12, 1e-6))
                row[f"{a}_z2"] = ((r["y_c"] - mu) / sd) ** 2
            rows.setdefault(day, []).append(row)
        print(day, len(test), {a: round(float(sc[a]["logscore"].mean()), 4) for a in qs}, flush=True)
    half = sorted(rows)[len(rows) // 2]
    slices = {"all": lambda r: True, "far": lambda r: r["family"] == "far", "near": lambda r: r["family"] == "near",
              "lt12": lambda r: r["family"] == "lt12", "first_half": lambda r: r["day"] < half,
              "second_half": lambda r: r["day"] >= half}
    pairs = [("N1", "P"), ("N2", "P"), ("N2", "N1"), ("P", "A"), ("N1", "A"), ("N2", "A")]
    out = {"split_day": half, "pairs": {f"{h}-{l}": {k: R.compare(rows, h, l, f) for k, f in slices.items()}
                                        for h, l in pairs}}
    flat = [r for rs in rows.values() for r in rs]
    out["mean"] = {a: {k: round(statistics.mean(r[f"{a}_{k}"] for r in flat), 5)
                       for k in ("logscore", "brier", "crps", "z2")} for a in ("A", "P", "N1", "N2")}
    by_st: dict[str, list] = {}
    for r in flat:
        by_st.setdefault(r["station"], []).append(r)
    out["stations_better"] = {f"{h}-{l}": sum(1 for v in by_st.values()
                                               if statistics.mean(x[f"{h}_logscore"] - x[f"{l}_logscore"] for x in v) < 0)
                              for h, l in pairs}
    out["n_stations"] = len(by_st)
    out["families"] = FAMILIES
    out["params_path"] = path
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(out["mean"]))
    for p, sl in out["pairs"].items():
        print(p, " | ".join(f"{k}: {v['logscore']['delta']:+.4f}[{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}]"
                            f" rps{v['crps']['delta']:+.4f} br{v['brier']['delta']:+.4f} {v['n_days']}d/{v['days_better']} {v['verdict'][:4]}"
                            for k, v in sl.items()))
    print("stations", out["stations_better"], out["n_stations"])
    print("last", {a: v[-1] for a, v in path.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
