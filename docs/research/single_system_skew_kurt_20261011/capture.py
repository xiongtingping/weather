#!/usr/bin/env python3
"""Single-system calibration with two more knobs: skewness and tail weight.

Four arms in one walk-forward (PROTOCOL.md): CUR (charter 216), FULL2 (the two
knobs, whole-ladder log loss, kappa 0), FULL4 and MODE4 (four knobs, kappa 0;
whole-ladder log loss / market mode +-1 binary log loss).

The two new knobs bend each forecast's member deviations with a sinh-arcsinh
map and put them back on the forecast's own mean and standard deviation:

    z = d / sd,  z' = sinh((asinh(z) + eps) / tau),  d' = sd * (z' - mean z') / sd(z')

so the corrected centre, the level-tercile bias and the dress kernel are the
production ones to the bit, and eps = 0, tau = 1 is the identity.  Training
and serving call production's own functions on the bent members
(``_truth_bucket_probabilities``, ``_apply_parameters_on_ladder``).

The walk-forward, arrivals, seats, serving chain and market windows are the
10-11 study's (``single_system_mean3_logloss_full_20261011/capture.py``),
imported.  Read-only; resumable per station.

    python3 capture.py --stations ZUUU,KLAX
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import minimize

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = _load("full_capture", ROOT / "docs/research/single_system_mean3_logloss_full_20261011/capture.py")
M, C, oos, replay = P.M, P.C, P.oos, P.replay
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from scripts.strict_replay_clock import training_fingerprint  # noqa: E402

OUT = HERE / "pass1"
ARMS = ("cur", "full2", "full4", "mode4")
WINDOWS = ("mode", "mean")
HALF_LIFE = oos.LOGLOSS_REFIT_HALF_LIFE_DAYS
P_FLOOR = oos.LOGLOSS_REFIT_P_FLOOR
EPS_BOUNDS, TAU_BOUNDS = (-1.5, 1.5), (0.5, 2.0)
MAXITER4 = 120
FIRST_SCORED, LAST_SCORED = M.FIRST_SCORED, M.LAST_SCORED


# ------------------------------------------------------------------ the bend

def bend_rows(deviations, mask, sd, eps, tau):
    """Bent deviations, row by row, on each row's own mean (0) and sd."""
    eps, tau = float(eps), float(tau)
    if eps == 0.0 and tau == 1.0:
        return deviations
    safe = np.where(sd > 0, sd, 1.0)[:, None]
    z = np.where(mask, deviations / safe, 0.0)
    zb = np.sinh((np.arcsinh(z) + eps) / tau)
    counts = mask.sum(axis=1)
    m = np.where(mask, zb, 0.0).sum(axis=1) / counts
    v = np.where(mask, (zb - m[:, None]) ** 2, 0.0).sum(axis=1) / counts
    s = np.sqrt(v)
    ok = (sd > 0) & (s > 0)
    bent = np.where(mask, (zb - m[:, None]) / np.where(s > 0, s, 1.0)[:, None] * safe, 0.0)
    return np.where(ok[:, None], bent, deviations)


def bend_values(values, eps, tau):
    """The same bend on one forecast's member values (mean and sd kept)."""
    if float(eps) == 0.0 and float(tau) == 1.0:
        return [float(v) for v in values]
    arr = np.array([float(v) for v in values])
    mean = float(arr.mean())
    sd = float(np.sqrt(((arr - mean) ** 2).mean()))
    if len(arr) < 2 or sd <= 0:
        return [float(v) for v in values]
    bent = bend_rows((arr - mean)[None, :], np.ones((1, len(arr)), bool), np.array([sd]), eps, tau)[0]
    # In the production order: the mean is summed the way _refit_case_arrays sums it.
    return [mean + float(d) for d in bent]


def bent_arrays(arrays, eps, tau):
    out = dict(arrays)
    out["deviations"] = bend_rows(arrays["deviations"], arrays["mask"], arrays["spreads"], eps, tau)
    return out


def _shifted4(moment, delta, spread, eps, tau, rule):
    out = P._shifted(moment, delta, spread, rule)
    out["skew_eps"], out["tail_tau"] = float(eps), float(tau)
    return out


def serve(values, params, intervals):
    """Ladder vector of one forecast under ``params`` (bend first, then production).

    The level bin is resolved at the forecast's own mean, as production does
    and as the training arrays do: the bent members' mean differs from it in
    the last bit, which flips the bin when a training case's mean is itself a
    tercile cut (CYYZ GEFS 12Z, 2026-10-11 parity check)."""
    eps, tau = float(params.get("skew_eps", 0.0)), float(params.get("tail_tau", 1.0))
    if eps == 0.0 and tau == 1.0:
        return oos._apply_parameters_on_ladder(values, params, intervals)
    current = [float(v) for v in values]
    pinned = dict(params, correction_form="constant",
                  bias_c=oos._resolved_bias_c(sum(current) / len(current), params))
    return oos._apply_parameters_on_ladder(bend_values(current, eps, tau), pinned, intervals)


# ------------------------------------------------------------------ forecasts

class Forecasts(P.Forecasts):

    def _fit_full4(self, train, moment):
        days = [date.fromisoformat(str(c["event_date"])[:10]) for c in train]
        anchor = max(days)
        weights = np.array([0.5 ** ((anchor - d).days / HALF_LIFE) for d in days])
        arrays = oos._refit_case_arrays(train, moment)

        def objective(x):
            p = oos._truth_bucket_probabilities(bent_arrays(arrays, x[2], x[3]), delta_c=float(x[0]), spread=float(x[1]))
            return float(-(weights * np.log(np.maximum(p, P_FLOOR))).sum() / weights.sum())

        fit = minimize(objective, x0=np.array([0.0, float(moment["spread_factor"]), 0.0, 1.0]), method="L-BFGS-B",
                       bounds=[oos.LOGLOSS_REFIT_DELTA_BOUNDS_C, oos.LOGLOSS_REFIT_SPREAD_BOUNDS, EPS_BOUNDS, TAU_BOUNDS],
                       options={"maxiter": MAXITER4})
        d, s, e, t = (float(v) for v in fit.x)
        return _shifted4(moment, d, s, e, t, "full4_research")

    def _fit_mode4(self, scope, train, moment, full4):
        rows = []
        for case in train:
            w = self._window("mode", scope, case)
            if w is not None:
                rows.append((case, w))
        self.stats["mode4_train_cases"] += len(train)
        self.stats["mode4_train_cases_in_window"] += len(rows)
        if len(rows) < replay.MIN_TRAIN:
            self.stats["mode4_fallback_full4"] += 1
            return dict(full4, fit_rule="mode4_fallback_full4")
        cases = [c for c, _w in rows]
        arrays = oos._refit_case_arrays(cases, moment)
        n = len(rows)
        lo = np.zeros((n, 3)); hi = np.zeros((n, 3)); y = np.zeros((n, 3)); valid = np.zeros((n, 3), bool)
        for r, (_c, (window, bounds, flags, _ladder)) in enumerate(rows):
            for k, ((a, b), f) in enumerate(zip(bounds, flags)):
                lo[r, k], hi[r, k], y[r, k], valid[r, k] = a, b, f, True
        anchor = max(date.fromisoformat(str(c["event_date"])[:10]) for c in train)
        weights = np.array([0.5 ** ((anchor - date.fromisoformat(str(c["event_date"])[:10])).days / HALF_LIFE)
                            for c in cases])

        def objective(x):
            p = np.clip(M._interval_probs(bent_arrays(arrays, x[2], x[3]), lo, hi, x[0], x[1]), P_FLOOR, 1.0 - P_FLOOR)
            loss = -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))
            return float((weights * np.where(valid, loss, 0.0).sum(axis=1)).sum() / weights.sum())

        fit = minimize(objective, x0=np.array([0.0, float(moment["spread_factor"]), 0.0, 1.0]), method="L-BFGS-B",
                       bounds=[oos.LOGLOSS_REFIT_DELTA_BOUNDS_C, oos.LOGLOSS_REFIT_SPREAD_BOUNDS, EPS_BOUNDS, TAU_BOUNDS],
                       options={"maxiter": MAXITER4})
        d, s, e, t = (float(v) for v in fit.x)
        params = _shifted4(moment, d, s, e, t, "mode4_research")
        if self.parity_checked < 25:
            # the bent window mass equals production's serving on the bent members
            probe = M._interval_probs(bent_arrays(arrays, e, t), lo, hi, d, s)
            for r in range(min(3, n)):
                case, (window, _b, _f, ladder) = rows[r]
                served = serve(case["values"], params, ladder["intervals"])
                for k, b in enumerate(window):
                    assert abs(served[b] - probe[r, k]) <= 1e-9, (served[b], probe[r, k])
            self.parity_checked += 1
        return params

    def forecast(self, scope, case, knowable):
        train = self.train(scope, case["event_date"], knowable)
        key = (scope, case["event_date"], case["member_set_sha256"], training_fingerprint(train))
        if key in self.memo:
            return self.memo[key]
        ladder = self.ladder_of(case["event_date"])
        if len(train) < replay.MIN_TRAIN or ladder is None:
            self.memo[key] = None
            return None
        cur = oos._fit_parameters(train)
        info = {"rule": cur.get("fit_rule")}
        if cur.get("fit_rule") == oos.SINGLE_SYSTEM_FIT_RULE_LOGLOSS:
            moment = M._moment_parameters(train)
            delta, spread = oos._logloss_refit(train, moment)
            full2 = P._shifted(moment, float(delta), float(spread), "full2_research")
            full4 = self._fit_full4(train, moment)
            mode4 = self._fit_mode4(scope, train, moment, full4)
            # identity check: the bend at eps 0, tau 1 serves production to the bit
            if self.stats["identity_checked"] < 25:
                values = [float(v) for v in case["values"]]
                a = oos._apply_parameters_on_ladder(values, full2, ladder["intervals"])
                b = serve(values, dict(full2, skew_eps=0.0, tail_tau=1.0), ladder["intervals"])
                assert all(abs(a[k] - b[k]) <= 1e-12 for k in a)
                self.stats["identity_checked"] += 1
            info.update(full2_shift=float(delta), full2_s=float(spread),
                        full4_shift=full4["logloss_shift_c"], full4_s=full4["spread_factor"],
                        full4_eps=full4["skew_eps"], full4_tau=full4["tail_tau"],
                        mode4_shift=mode4["logloss_shift_c"], mode4_s=mode4["spread_factor"],
                        mode4_eps=mode4.get("skew_eps"), mode4_tau=mode4.get("tail_tau"),
                        mode4_fallback=mode4.get("fit_rule") == "mode4_fallback_full4")
            self.stats["refits"] += 1
        else:
            full2 = full4 = mode4 = cur
            self.stats["not_refitted"] += 1
        values = [float(v) for v in case["values"]]
        mean = sum(values) / len(values)
        row = {
            "event_date": case["event_date"],
            "actual_tmax_c": float(case["actual_tmax_c"]),
            "error_scale": float(cur.get("train_residual_sigma_c") or cur["train_rmse_c"]),
            "train_n": len(train),
            "residuals": {c["event_date"]: float(c["actual_tmax_c"]) - sum(float(v) for v in c["values"]) / len(c["values"])
                          for c in train},
            # The bend keeps each forecast's mean, so the corrected centre is production's.
            "arms": {arm: {"probability": serve(values, p, ladder["intervals"]),
                           "calibrated_center_c": oos._corrected_center(mean, p)}
                     for arm, p in (("cur", cur), ("full2", full2), ("full4", full4), ("mode4", mode4))},
            "fit": info,
        }
        self.memo[key] = row
        return row


# The capture loop is the 10-11 study's, with these arms and this forecaster.
P.ARMS = ARMS
P.Forecasts = Forecasts
P.OUT = OUT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations", required=True)
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    conn = sqlite3.connect(f"file:{C.SSOT}?mode=ro", uri=True, timeout=30)
    zones = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
    long_lag = M.live_lag(conn)
    for station in [s.strip().upper() for s in args.stations.split(",") if s.strip()]:
        path = OUT / f"{station}.jsonl.gz"
        if path.exists():
            continue
        t0 = time.time()
        rows, stats = P.capture_station(conn, station, zones[station], long_lag)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        tmp.rename(path)
        (OUT / f"{station}.stats.json").write_text(json.dumps(stats))
        print(f"{station}: {len(rows)} decisions, {time.time() - t0:.0f}s, {stats}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
