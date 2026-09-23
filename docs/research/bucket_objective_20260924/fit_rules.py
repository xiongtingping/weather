#!/usr/bin/env python3
"""Which scoring rule trains which personality -- charter 211's open question.

One posterior map family M(lambda, epsilon) on the served market-ladder
vectors, fitted forward-only under four rules (bucket Brier, multiclass
log loss, the 3-4 bucket window rule, and CRPS as the demoted control),
scored on the charter 211 panel.  See PROTOCOL.md; writes results.json.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from calibration_dashboard import CORE_RELIABILITY_EDGES, _core_murphy  # noqa: E402

DATA = ROOT / "docs" / "research" / "conditional_belief_20260923"
STAGES = ("P0", "P0-M", "P1")
RULES = ("BRIER", "LOG", "WINDOW", "CRPS")
# Ablations of the LOG rule: where its gain comes from (the exponent or the floor).
ABLATIONS = ("LOG_EPS0", "LOG_LAM1")
ALL_RULES = RULES + ABLATIONS
LAMBDA_GRID = np.round(np.arange(0.30, 2.0001, 0.05), 2)
EPS_GRID = np.array([0.0, 0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.20])
TRAIN_WINDOW_DAYS = 28
MIN_TRAIN_DAYS = 5
NLL_FLOOR = 1e-6       # the board's CORE_NLL_FLOOR
FIT_LOG_FLOOR = 1e-9
WINDOW_WIDTHS = (3, 4)
BOOT = 2000
BLOCK = 3
SEED = 20260924


def window_matrix(k: int) -> np.ndarray:
    rows = []
    for w in WINDOW_WIDTHS:
        for start in range(k - w + 1):
            row = np.zeros(k)
            row[start:start + w] = 1.0
            rows.append(row)
    return np.array(rows)


def apply_map(p: np.ndarray, lam: float, eps: float) -> np.ndarray:
    q = np.power(np.clip(p, 0.0, 1.0), lam)
    q = q / q.sum(axis=1, keepdims=True)
    return (1.0 - eps) * q + eps / p.shape[1]


def losses(r: np.ndarray, hit: np.ndarray, wm: np.ndarray) -> dict[str, np.ndarray]:
    n, k = r.shape
    onehot = np.zeros_like(r)
    onehot[np.arange(n), hit] = 1.0
    brier = ((r - onehot) ** 2).sum(axis=1)
    r_hit = r[np.arange(n), hit]
    log = -np.log(np.maximum(r_hit, FIT_LOG_FLOOR))
    win = ((r @ wm.T - onehot @ wm.T) ** 2).mean(axis=1)
    cdf = np.cumsum(r, axis=1)
    ocdf = np.cumsum(onehot, axis=1)
    crps = ((cdf - ocdf) ** 2).sum(axis=1)
    return {"BRIER": brier, "LOG": log, "WINDOW": win, "CRPS": crps}


def fit(p: np.ndarray, hit: np.ndarray, w: np.ndarray, wm: np.ndarray) -> dict[str, tuple[float, float]]:
    """Grid search of (lambda, eps) under every rule at once."""

    best: dict[str, tuple[float, float, float]] = {}
    total = w.sum()
    for lam in LAMBDA_GRID:
        for eps in EPS_GRID:
            r = apply_map(p, float(lam), float(eps))
            loss = losses(r, hit, wm)
            for rule in RULES:
                value = float((w * loss[rule]).sum() / total)
                if rule not in best or value < best[rule][0] - 1e-15:
                    best[rule] = (value, float(lam), float(eps))
            log_value = float((w * loss["LOG"]).sum() / total)
            if eps == 0.0 and ("LOG_EPS0" not in best or log_value < best["LOG_EPS0"][0] - 1e-15):
                best["LOG_EPS0"] = (log_value, float(lam), 0.0)
            if abs(lam - 1.0) < 1e-9 and ("LOG_LAM1" not in best or log_value < best["LOG_LAM1"][0] - 1e-15):
                best["LOG_LAM1"] = (log_value, 1.0, float(eps))
    return {rule: (best[rule][1], best[rule][2]) for rule in ALL_RULES}


def murphy_vectorised(r: np.ndarray, hit: np.ndarray, w: np.ndarray) -> dict[str, float]:
    """The board's _core_murphy, vectorised: same bins, same station-day mass."""

    n, k = r.shape
    edges = np.array(CORE_RELIABILITY_EDGES)
    bins = len(edges) - 1
    share = np.repeat(w / k, k)
    value = r.reshape(-1)
    happened = np.zeros_like(r)
    happened[np.arange(n), hit] = 1.0
    happened = happened.reshape(-1)
    slot = np.clip(np.searchsorted(edges, value, side="right") - 1, 0, bins - 1)
    W = np.bincount(slot, weights=share, minlength=bins)
    P = np.bincount(slot, weights=share * value, minlength=bins)
    Y = np.bincount(slot, weights=share * happened, minlength=bins)
    total = W.sum()
    base = Y.sum() / total
    live = W > 0
    rel = float(((P[live] - Y[live]) ** 2 / W[live]).sum() / total)
    res = float((W[live] * (Y[live] / W[live] - base) ** 2).sum() / total)
    return {"reliability": rel, "resolution": res, "base_rate": float(base)}


def panel(r: np.ndarray, hit: np.ndarray, w: np.ndarray, wm: np.ndarray) -> dict[str, float]:
    n, k = r.shape
    loss = losses(r, hit, wm)
    r_hit = r[np.arange(n), hit]
    total = w.sum()
    mean = lambda x: float((w * x).sum() / total)  # noqa: E731
    mode = r.argmax(axis=1)
    idx = np.arange(k)[None, :]
    tail = np.where(np.abs(idx - mode[:, None]) > 2, r, 0.0).sum(axis=1)
    entropy = -(np.where(r > 0, r * np.log(np.maximum(r, 1e-300)), 0.0)).sum(axis=1)
    wmass = r @ wm.T
    n3 = k - WINDOW_WIDTHS[0] + 1
    best3 = wmass[:, :n3].max(axis=1)
    best4 = wmass[:, n3:].max(axis=1)
    out = {
        "bucket_brier": mean(loss["BRIER"]),
        "logscore": mean(-np.log(np.maximum(r_hit, NLL_FLOOR))),
        "window_score": mean(loss["WINDOW"]),
        "crps_rungs": mean(loss["CRPS"]),
        "zero_hits": int((r_hit < 1e-6).sum()),
        "peak_mass": mean(r.max(axis=1)),
        "best3_mass": mean(best3),
        "best4_mass": mean(best4),
        "tail_beyond_2": mean(tail),
        "entropy": mean(entropy),
        "p_hit_mean": mean(r_hit),
    }
    out.update(murphy_vectorised(r, hit, w))
    return out


def basket_readings(r: np.ndarray, r0: np.ndarray, hit: np.ndarray, w: np.ndarray, wm: np.ndarray) -> dict[str, float]:
    """The trading layer's main basket: the heaviest 4-window (and 3-window)."""

    n, k = r.shape
    n3 = k - WINDOW_WIDTHS[0] + 1
    total = w.sum()
    out = {}
    for label, lo, hi in (("win3", 0, n3), ("win4", n3, wm.shape[0])):
        sub = wm[lo:hi]
        mass = r @ sub.T
        mass0 = r0 @ sub.T
        pick = mass.argmax(axis=1)
        pick0 = mass0.argmax(axis=1)
        inside = sub[pick][np.arange(n), hit] > 0
        claimed = mass[np.arange(n), pick]
        out[f"{label}_changed_share"] = float((w * (pick != pick0)).sum() / total)
        out[f"{label}_claimed"] = float((w * claimed).sum() / total)
        out[f"{label}_realised"] = float((w * inside).sum() / total)
    return out


def load_stage(stage: str):
    z = np.load(DATA / f"rows_{stage}.npz", allow_pickle=True)
    scored = z["scored"]
    p = z["p"][scored].astype(float)
    hit = z["hit"][scored].astype(int)
    day = np.array([str(d) for d in z["day"][scored]])
    sd = np.array([str(s) for s in z["sd"][scored]])
    copies = Counter(sd)
    w = np.array([1.0 / copies[s] for s in sd])
    assert np.allclose(p.sum(axis=1), 1.0, atol=1e-6)
    return p, hit, day, sd, w


def block_bootstrap(days: np.ndarray, per_row: dict[str, np.ndarray], row_days: np.ndarray, w: np.ndarray,
                    r_by_rule: dict[str, np.ndarray], hit: np.ndarray, rng: np.random.Generator):
    """3-day circular block bootstrap over event days of the deltas vs identity."""

    uniq = sorted(set(days))
    n_days = len(uniq)
    index_by_day = {d: np.flatnonzero(row_days == d) for d in uniq}
    n_blocks = math.ceil(n_days / BLOCK)
    out = {rule: defaultdict(list) for rule in per_row}
    for _ in range(BOOT):
        starts = rng.integers(0, n_days, size=n_blocks)
        picked = []
        for s in starts:
            picked.extend(uniq[(s + j) % n_days] for j in range(BLOCK))
        idx = np.concatenate([index_by_day[d] for d in picked[:n_days]])
        ww = w[idx]
        for rule, deltas in per_row.items():
            for key, arr in deltas.items():
                out[rule][key].append(float((ww * arr[idx]).sum() / ww.sum()))
            m1 = murphy_vectorised(r_by_rule[rule][idx], hit[idx], ww)
            m0 = murphy_vectorised(r_by_rule["IDENTITY"][idx], hit[idx], ww)
            out[rule]["reliability"].append(m1["reliability"] - m0["reliability"])
            out[rule]["resolution"].append(m1["resolution"] - m0["resolution"])
    return {rule: {key: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))]
                   for key, v in d.items()} for rule, d in out.items()}


def run_stage(stage: str, rng: np.random.Generator) -> dict:
    p, hit, day, sd, w = load_stage(stage)
    k = p.shape[1]
    wm = window_matrix(k)
    # Parity with the board's own decomposition on the raw served vectors.
    ours = murphy_vectorised(p, hit, w)
    theirs = _core_murphy([(float(wi), {"probabilities": list(map(float, row)), "hit": int(h)})
                           for wi, row, h in zip(w, p, hit)])
    assert abs(ours["reliability"] - theirs["reliability"]) < 1e-12, (ours, theirs)
    assert abs(ours["resolution"] - theirs["resolution"]) < 1e-12
    uniq = sorted(set(day))
    fitted_path = []
    served_idx = []
    r_by_rule = {rule: np.zeros_like(p) for rule in ALL_RULES}
    for d in uniq:
        d_date = date.fromisoformat(d)
        lo = (d_date - timedelta(days=TRAIN_WINDOW_DAYS)).isoformat()
        train = np.flatnonzero((day >= lo) & (day < d))
        if len(set(day[train])) < MIN_TRAIN_DAYS:
            continue
        params = fit(p[train], hit[train], w[train], wm)
        today = np.flatnonzero(day == d)
        served_idx.extend(today.tolist())
        for rule in ALL_RULES:
            lam, eps = params[rule]
            r_by_rule[rule][today] = apply_map(p[today], lam, eps)
        fitted_path.append({"day": d, "train_days": len(set(day[train])), "train_rows": int(len(train)),
                            **{rule: {"lambda": params[rule][0], "eps": params[rule][1]} for rule in ALL_RULES}})
    idx = np.array(sorted(served_idx))
    r_by_rule = {rule: arr[idx] for rule, arr in r_by_rule.items()}
    r_by_rule["IDENTITY"] = p[idx]
    hit_f, w_f, day_f, sd_f = hit[idx], w[idx], day[idx], sd[idx]
    panels = {rule: panel(r, hit_f, w_f, wm) for rule, r in r_by_rule.items()}
    baskets = {rule: basket_readings(r, r_by_rule["IDENTITY"], hit_f, w_f, wm) for rule, r in r_by_rule.items()}
    cross = {rule: {score: panels[rule][{"BRIER": "bucket_brier", "LOG": "logscore", "WINDOW": "window_score",
                                           "CRPS": "crps_rungs"}[score]] for score in RULES}
             for rule in r_by_rule}
    per_row = {}
    base_loss = losses(r_by_rule["IDENTITY"], hit_f, wm)
    base_log = -np.log(np.maximum(r_by_rule["IDENTITY"][np.arange(len(idx)), hit_f], NLL_FLOOR))
    for rule in ALL_RULES:
        loss = losses(r_by_rule[rule], hit_f, wm)
        log = -np.log(np.maximum(r_by_rule[rule][np.arange(len(idx)), hit_f], NLL_FLOOR))
        per_row[rule] = {"bucket_brier": loss["BRIER"] - base_loss["BRIER"], "logscore": log - base_log,
                         "window_score": loss["WINDOW"] - base_loss["WINDOW"], "crps_rungs": loss["CRPS"] - base_loss["CRPS"]}
    ci = block_bootstrap(day_f, per_row, day_f, w_f, r_by_rule, hit_f, rng)
    deltas = {rule: {key: panels[rule][key] - panels["IDENTITY"][key]
                     for key in ("bucket_brier", "logscore", "window_score", "crps_rungs", "reliability", "resolution")}
              for rule in ALL_RULES}
    params_summary = {rule: {"lambda_median": float(np.median([f[rule]["lambda"] for f in fitted_path])),
                             "lambda_min": float(min(f[rule]["lambda"] for f in fitted_path)),
                             "lambda_max": float(max(f[rule]["lambda"] for f in fitted_path)),
                             "eps_median": float(np.median([f[rule]["eps"] for f in fitted_path])),
                             "eps_positive_share": float(np.mean([f[rule]["eps"] > 0 for f in fitted_path]))}
                      for rule in ALL_RULES}
    # One real station-day, the same served vector under the four fitted maps
    # on its own day (the plain-language example the report needs).
    example = None
    last = fitted_path[-1]
    cand = np.flatnonzero(day_f == last["day"])
    if len(cand):
        i = int(cand[np.argmax(w_f[cand])])
        example = {"station_day": sd_f[i], "hit": int(hit_f[i]),
                   "identity": [round(float(v), 4) for v in r_by_rule["IDENTITY"][i]],
                   **{rule: [round(float(v), 4) for v in r_by_rule[rule][i]] for rule in ALL_RULES},
                   "params": {rule: last[rule] for rule in ALL_RULES}}
    return {
        "stage": stage, "rows_scored": int(len(p)), "station_days_scored": len(set(sd)),
        "rows_forward": int(len(idx)), "station_days_forward": len(set(sd_f)),
        "days_forward": len(set(day_f)), "first_forward_day": str(min(day_f)), "last_forward_day": str(max(day_f)),
        "stations": len({s.split("|")[0] for s in sd_f}),
        "panels": panels, "deltas_vs_identity": deltas, "ci95_vs_identity": ci, "cross_matrix": cross,
        "baskets": baskets, "params": params_summary, "fitted_path": fitted_path, "example": example,
    }


def main() -> None:
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    out = {"protocol": "PROTOCOL.md", "lambda_grid": LAMBDA_GRID.tolist(), "eps_grid": EPS_GRID.tolist(),
           "train_window_days": TRAIN_WINDOW_DAYS, "min_train_days": MIN_TRAIN_DAYS, "stages": {}}
    for stage in STAGES:
        t = time.time()
        out["stages"][stage] = run_stage(stage, rng)
        print(f"{stage}: {out['stages'][stage]['station_days_forward']} station-days forward, {time.time() - t:.0f}s", flush=True)
    out["seconds"] = round(time.time() - t0, 1)
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"done {out['seconds']}s")


if __name__ == "__main__":
    main()
