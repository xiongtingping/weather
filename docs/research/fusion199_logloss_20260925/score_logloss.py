#!/usr/bin/env python3
"""PROTOCOL.md: charter 199's overlay trained by bucket log loss vs today's centre-MSE + CRPS.

Every settled, served day-ahead root in production's training table is
re-weighted twice by production's ``overlay_system_weights``, as of its own
decision instant: once as served (``blend_weights``) and once with
``blend_weights`` replaced, in this process only, by ``logloss_weights`` below
(written as it would sit in ``fusion_blend``).  Both are pooled on the
market ladder, floored at charter 224's 1e-3 and scored.  Writes results.json.

    python3 score_logloss.py <scratch-dir-with-snapshot.db>
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import fusion_blend as FB  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

ASOS = frozenset({"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"})
LOGLOSS_P_FLOOR = 1e-9
BLOCK, BOOT = 3, 2000


# ------------------------------------------------ the candidate, production-shaped

class TrainingRowLL(FB.TrainingRow):
    """A training row that also carries each system's truth-rung probability."""

    __slots__ = ("truth_probability",)

    def __init__(self, record):
        super().__init__(record)
        truth = {}
        names = set(record.keys())
        if "probabilities_json" in names and record["probabilities_json"] and record["truth_bucket_index"] is not None:
            index = int(record["truth_bucket_index"])
            truth = {str(s): float(v[index]) for s, v in json.loads(record["probabilities_json"]).items()}
        self.truth_probability = truth


def fit_logloss_stack(rows, target, *, prior, ridge):
    """``fit_stack`` with the pool's bucket log loss as the objective."""

    from scipy.optimize import minimize  # noqa: PLC0415

    keys = target.keys
    start = np.array([prior[system] for system in keys])
    p = np.array([[row.truth_probability[system] for system in keys] for row in rows])

    def fun(w):
        q = np.maximum(p @ w, LOGLOSS_P_FLOOR)
        return float(-np.mean(np.log(q)) + ridge * np.sum((w - start) ** 2))

    def jac(w):
        q = p @ w
        live = q > LOGLOSS_P_FLOOR
        grad = np.zeros(len(keys))
        if live.any():
            grad = -(p[live] / q[live, None]).sum(axis=0) / len(p)
        return grad + 2 * ridge * (w - start)

    fit = minimize(fun, start, jac=jac, bounds=[(0, 1)] * len(keys),
                   constraints=FB._constraints(target, keys), method="SLSQP",
                   options={"maxiter": 100, "ftol": 1e-10})
    if not fit.success:
        raise ValueError("fusion_blend_optimisation_failed:" + str(fit.message))
    weights = dict(zip(keys, map(float, fit.x)))
    FB._check(weights, target)
    return weights


def logloss_weights(rows, target):
    """The hierarchical log-loss fit, in ``blend_weights``' contract."""

    base = dict(target.weights)
    detail = {"center_rows": 0, "center_state": "NOT_USED_LOGLOSS"}
    local = FB.eligible(rows, target, FB.FUSION_BLEND_WINDOW_DAYS, probability=True)
    fleet = FB.eligible(rows, target, FB.FUSION_BLEND_WINDOW_DAYS, fleet=True, lead=True, probability=True)
    detail["fleet_scope"] = "same_lead"
    if not fleet:
        fleet = FB.eligible(rows, target, FB.FUSION_BLEND_WINDOW_DAYS, fleet=True, probability=True)
        detail["fleet_scope"] = "all_leads"
    detail["local_rows"] = len(local)
    detail["fleet_rows"] = len(fleet)
    weights = base
    detail["distribution_state"] = "SEAT_PRIOR_NO_FLEET"
    used = []
    if fleet:
        try:
            prior = fit_logloss_stack(fleet, target, prior=base, ridge=FB.FUSION_BLEND_FLEET_RIDGE)
            weights = (fit_logloss_stack(local, target, prior=prior,
                                         ridge=FB.FUSION_BLEND_LOCAL_RIDGE_SCALE / len(local))
                       if local else prior)
            detail["distribution_state"] = "FITTED_LOCAL" if local else "FITTED_FLEET_ONLY"
            used = list(fleet) + list(local)
        except ValueError as error:
            weights = base
            detail["distribution_state"] = "SEAT_PRIOR_" + str(error)
    detail["center_weights"] = dict(sorted(base.items()))
    detail["distribution_weights"] = dict(sorted(weights.items()))
    detail["training_rows_sha256"] = hashlib.sha256(
        "\n".join(sorted({row.identity for row in used})).encode()).hexdigest()
    return dict(weights), detail


# ------------------------------------------------------------------ replay

def scored_rows(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(
        "SELECT * FROM fusion_blend_training_fact WHERE probability_source IN ('ENVELOPE','PACKET_RECONSTRUCTION')")]
    conn.close()
    latest = {}
    for r in out:
        key = (r["station_id"], r["event_date"])
        if key not in latest or r["truth_revision"] > latest[key]["truth_revision"]:
            latest[key] = r
    return sorted(latest.values(), key=lambda r: (r["station_id"], r["event_date"]))


def overlay(r, db):
    return FB.overlay_system_weights(
        station_id=r["station_id"], event_date=r["event_date"],
        decision_at_utc=r["root_decision_at_utc"], timezone_name=r["settlement_timezone"],
        system_weights=json.loads(r["seat_weights_json"]),
        family_by_system=json.loads(r["family_by_system_json"]),
        family_caps=json.loads(r["family_caps_json"]),
        lead_by_system=json.loads(r["lead_by_system_json"]),
        training_db=db, mode=FB.FUSION_BLEND_MODE_BLEND,
    )


def score(r, w):
    keys = json.loads(r["bucket_keys_json"])
    probs = json.loads(r["probabilities_json"])
    pooled = {k: sum(float(w.get(s, 0.0)) * probs[s][i] for s in probs) for i, k in enumerate(keys)}
    total = sum(pooled.values())
    floored, _ = floor_probabilities({k: v / total for k, v in pooled.items()})
    hit = keys[int(r["truth_bucket_index"])]
    return (sum((floored[k] - (1.0 if k == hit else 0.0)) ** 2 for k in keys),
            -math.log(max(floored[hit], 1e-12)))


def compare(items, label):
    days = sorted({it["day"] for it in items})
    by_day = defaultdict(list)
    for i, it in enumerate(items):
        by_day[it["day"]].append(i)
    d_b = np.array([it["ll"][0] - it["cur"][0] for it in items])
    d_n = np.array([it["ll"][1] - it["cur"][1] for it in items])
    rng = np.random.default_rng(20260925)
    n_blocks = math.ceil(len(days) / BLOCK)
    boots_b, boots_n = [], []
    for _ in range(BOOT):
        idx = np.concatenate([by_day[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(BLOCK)])
        boots_b.append(float(d_b[idx].mean())); boots_n.append(float(d_n[idx].mean()))
    mean = lambda arm, j: float(np.mean([it[arm][j] for it in items]))  # noqa: E731
    out = {
        "label": label, "station_days": len(items), "days": len(days),
        "current": {"brier": mean("cur", 0), "nll": mean("cur", 1)},
        "logloss": {"brier": mean("ll", 0), "nll": mean("ll", 1)},
        "delta": {"brier": float(d_b.mean()), "nll": float(d_n.mean())},
        "ci95": {"brier": [float(np.percentile(boots_b, 2.5)), float(np.percentile(boots_b, 97.5))],
                 "nll": [float(np.percentile(boots_n, 2.5)), float(np.percentile(boots_n, 97.5))]},
    }
    out["point_estimates_both_lower"] = out["delta"]["brier"] < 0 and out["delta"]["nll"] < 0
    out["holds"] = out["ci95"]["brier"][1] < 0 and out["ci95"]["nll"][1] < 0
    return out


def main():
    work = Path(sys.argv[1])
    db = work / "snapshot.db"
    FB.TrainingRow = TrainingRowLL           # before the first load fills the cache
    original = FB.blend_weights
    items, states = [], defaultdict(int)
    for r in scored_rows(db):
        FB.blend_weights = original
        cur_w, cur_receipt = overlay(r, db)
        FB.blend_weights = logloss_weights
        ll_w, ll_receipt = overlay(r, db)
        FB.blend_weights = original
        states[f"{cur_receipt['state']} -> {ll_receipt['state']}"] += 1
        moved = 0.5 * sum(abs(float(ll_w.get(s, 0)) - float(cur_w.get(s, 0))) for s in set(cur_w) | set(ll_w))
        items.append({"station": r["station_id"], "day": r["event_date"], "asos": r["station_id"] in ASOS,
                      "cur": score(r, cur_w), "ll": score(r, ll_w), "moved": moved})
    result = {
        "protocol": "PROTOCOL.md",
        "pooled": compare(items, "全部（判定）"),
        "after_134": compare([it for it in items if it["day"] >= "2026-09-14"], "宪章 134 之后"),
        "asos": compare([it for it in items if it["asos"]], "ASOS 11 站"),
        "non_asos": compare([it for it in items if not it["asos"]], "非 ASOS"),
        "weights_differ": compare([it for it in items if it["moved"] > 1e-9], "两臂权重不同的站日"),
        "states": dict(states),
        "moved_half_l1_median_when_different": float(np.median([it["moved"] for it in items if it["moved"] > 1e-9] or [0])),
    }
    result["deploy"] = result["pooled"]["holds"]
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key in ("pooled", "after_134", "asos", "non_asos", "weights_differ"):
        c = result[key]
        print(f"{c['label']}: {c['station_days']} 站日 / {c['days']} 天 | Brier {c['current']['brier']:.5f}->{c['logloss']['brier']:.5f} "
              f"({c['delta']['brier']:+.5f} [{c['ci95']['brier'][0]:+.5f},{c['ci95']['brier'][1]:+.5f}]) | LogScore {c['current']['nll']:.4f}->{c['logloss']['nll']:.4f} "
              f"({c['delta']['nll']:+.4f} [{c['ci95']['nll'][0]:+.4f},{c['ci95']['nll'][1]:+.4f}]) | holds={c['holds']}", flush=True)
    print("states:", json.dumps(result["states"], ensure_ascii=False))
    print("moved median:", round(result["moved_half_l1_median_when_different"], 3), "| DEPLOY =", result["deploy"])


if __name__ == "__main__":
    main()
