#!/usr/bin/env python3
"""PROTOCOL_centres.md: the four centres and both sides' mass as conditioning features.

Same rows, folds, ridge and three-day-block reading as ``walkforward.py``; the
market enters the features here, so the arms are read against the market too.
Read-only; writes results_centres.json.

    python3 centres.py cache.pkl
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import walkforward as W  # noqa: E402

CENTRES = ("mode_p", "meanbucket_p", "mean_m", "meanbucket_m", "mode_m")
MASSES = ("p_at_mode_p", "m_at_mode_m", "p_at_mode_m", "m_at_mode_p", "skew_p", "skew_m", "near_tie")
ARMS = {  # arm -> (vector shifted, features, intercept only)
    "K1": ("p", CENTRES, False),
    "K2": ("p", CENTRES + MASSES, False),
    "K3": ("m", CENTRES + MASSES, False),
    "K3_0": ("m", (), True),
}


def centre_features(row):
    keys, centers = row["keys"], np.asarray(row["centers"])
    p, m = np.asarray(row["p"]), np.asarray(row["m"])
    mean_p, mean_m = float(np.dot(p, centers)), float(np.dot(m, centers))
    mode_p, mode_m = int(np.argmax(p)), int(np.argmax(m))
    mb_p, mb_m = int(np.argmin(np.abs(centers - mean_p))), int(np.argmin(np.abs(centers - mean_m)))
    positions = {"mode_p": centers[mode_p], "meanbucket_p": centers[mb_p], "mean_m": mean_m,
                 "meanbucket_m": centers[mb_m], "mode_m": centers[mode_m], "mean_p": mean_p}
    masses = {"p_at_mode_p": p[mode_p], "m_at_mode_m": m[mode_m], "p_at_mode_m": p[mode_m], "m_at_mode_p": m[mode_p],
              "skew_p": p[mb_p + 1:].sum() - p[:mb_p].sum(), "skew_m": m[mb_m + 1:].sum() - m[:mb_m].sum(),
              "near_tie": 1.0 if (m[mode_p] >= 0.40 and mode_p != mode_m) else 0.0}
    return positions, {k: float(v) for k, v in masses.items()}, mean_p, mean_m


def fit(train, features, anchor, intercept_only):
    """Station-day-weighted ridge of truth minus the anchor's mean, as walkforward.fit."""

    count = Counter((r["station"], r["date"]) for r in train)
    w = np.array([1.0 / count[(r["station"], r["date"])] for r in train])
    y = np.array([r["truth_c"] - r[f"mean_{anchor}"] for r in train])
    feats = () if intercept_only else features
    raw = np.array([[r["f"][anchor][f] for f in feats] for r in train]).reshape(len(train), len(feats))
    mu = (w[:, None] * raw).sum(0) / w.sum()
    sd = np.sqrt((w[:, None] * (raw - mu) ** 2).sum(0) / w.sum())
    sd = np.where(sd > 1e-9, sd, 1.0)
    X = np.column_stack([np.ones(len(train)), *((raw - mu) / sd).T])
    beta = np.linalg.solve(X.T @ (w[:, None] * X) + np.diag([0.0] + [W.RIDGE] * len(feats)), X.T @ (w * y))
    return feats, mu, sd, beta


def predict(model, row, anchor):
    feats, mu, sd, beta = model
    x = np.array([row["f"][anchor][f] for f in feats])
    return float(beta[0] + np.dot((x - mu) / sd, beta[1:]))


def shifted(row, vector, delta):
    base = np.asarray(row[vector])
    q = base if delta == 0.0 else W.D.as_vec(
        W.D.shift_tail(dict(zip(row["keys"], base)), delta_c=delta, keys=row["keys"],
                       centers=list(row["centers"]), open_edges=row["open_edges"]), row["keys"])
    return W.D.score(q, row["keys"], row["hit"], floor=(vector == "p"))


def main():
    rows, _ = W.build_rows(sys.argv[1])
    for r in rows:
        positions, masses, mean_p, mean_m = centre_features(r)
        r["mean_m"] = mean_m
        r["f"] = {  # positions relative to the vector being shifted
            "p": {**{k: v - mean_p for k, v in positions.items() if k != "mean_p"}, **masses},
            "m": {**{k: v - mean_m for k, v in positions.items() if k != "mean_m"}, **masses},
        }
        # the market arm's centre set names the model's mean where the model arm names the market's
        r["f"]["m"]["mean_m"] = positions["mean_p"] - mean_m
        r["s"] = {"A0": shifted(r, "p", 0.0), "M0": shifted(r, "m", 0.0)}
        r["d"] = {}
    days = sorted({r["date"] for r in rows})
    tested_days = []
    for day in days:
        test = [r for r in rows if r["date"] == day]
        cutoff = min(W.instant(r["at"]) for r in test)
        train = [r for r in rows if r["date"] < day and r["known_at"] <= cutoff]
        if len({(r["station"], r["date"]) for r in train}) < W.MIN_TRAIN_STATION_DAYS:
            continue
        tested_days.append(day)
        for arm, (vector, features, intercept_only) in ARMS.items():
            model = fit(train, features, vector, intercept_only)
            for r in test:
                r["d"][arm] = predict(model, r, vector)
    tested = [r for r in rows if r["date"] in tested_days]
    for r in tested:
        for arm, (vector, _f, _i) in ARMS.items():
            r["s"][arm] = shifted(r, vector, r["d"][arm])
    rng = np.random.default_rng(W.SEED)
    half = tested_days[len(tested_days) // 2]

    def diff(items, arm, ref):
        out = {}
        for metric, j in (("logscore", 0), ("brier", 1)):
            d, num, den = W.per_day(items, lambda r: r["s"][arm][j] - r["s"][ref][j])
            out[metric] = [float(num.sum() / den.sum()), *W.block_ci(d, num, den, rng)]
        return out

    result = {"protocol": "PROTOCOL_centres.md", "tested_days": [tested_days[0], tested_days[-1], len(tested_days)],
              "station_days": len({(r["station"], r["date"]) for r in tested}),
              "model_vs_market": diff(tested, "A0", "M0"), "arms": {}}
    for arm, (vector, _f, _i) in ARMS.items():
        refs = ("A0", "M0") if vector == "p" else ("M0",)
        result["arms"][arm] = {
            f"vs_{ref}": {"all": diff(tested, arm, ref),
                          "first_half": diff([r for r in tested if r["date"] < half], arm, ref),
                          "second_half": diff([r for r in tested if r["date"] >= half], arm, ref)}
            for ref in refs}
        result["arms"][arm]["abs_delta_c_median"] = float(np.median([abs(r["d"][arm]) for r in tested]))
    (HERE / "results_centres.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
