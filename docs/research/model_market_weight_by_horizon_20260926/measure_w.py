#!/usr/bin/env python3
"""PROTOCOL.md: the model-vs-market weight w by hours to settlement.

Bucket unit: production's own L4 population (``valuation_store.population``,
one row per decision with the served weather vector p, the market's
normalised mid vector m and the settled bucket), all three stages pooled on
the hours-to-settlement axis.  Window unit: production's candidate window
evidence joined with the settlement labels.  Read-only; writes results.json.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import valuation_store  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

BINS = ((0, 12), (12, 24), (24, 36), (36, 48), (48, 60), (60, math.inf))
LABELS = ["0-12h", "12-24h", "24-36h", "36-48h", "48-60h", "60h+"]
W_LIN = np.round(np.arange(0.0, 1.0001, 0.01), 2)
W_LOG = np.round(np.arange(-0.5, 1.5001, 0.01), 2)
EPS = 1e-6
DRAWS = 1000
SEED = 20260926
SUBPERIOD_FROM = "2026-09-14"


def bin_of(hours):
    for index, (low, high) in enumerate(BINS):
        if low <= hours < high:
            return index
    return None


def logit(x):
    x = np.clip(x, EPS, 1.0 - EPS)
    return np.log(x / (1.0 - x))


def bucket_population(conn):
    decisions, counts = [], Counter()
    for stage in ("P0", "P0-M", "P1"):
        rows, meta = valuation_store.population(conn, stage_scope=stage, anchored=False, window_days=70)
        counts.update({f"{stage}:{k}": v for k, v in meta.items() if isinstance(v, int)})
        for row in rows:
            hours = hours_to_settlement_if_known(row["date"], as_of_utc=row["at"],
                                                 settlement_timezone=row.get("settlement_timezone"))
            index = None if hours is None else bin_of(hours)
            if index is None:
                counts[f"{stage}:no_clock_or_after_settlement"] += 1
                continue
            keys = list(row["m"])
            m = np.array([float(row["m"][k]) for k in keys])
            p = np.array([float(row["p"].get(k, 0.0) or 0.0) for k in keys])
            if p.sum() <= 0:
                counts[f"{stage}:p_empty_on_contract"] += 1
                continue
            p = p / p.sum()
            floored, _ = floor_probabilities({k: float(v) for k, v in zip(keys, p)})
            p = np.array([floored[k] for k in keys])
            decisions.append({"stage": stage, "station": row["station"], "date": row["date"], "bin": index,
                              "hours": hours, "m": m / m.sum(), "p": p, "hit": keys.index(row["label"])})
    return decisions, dict(counts)


def window_population(conn):
    labels = knowable_labels(conn)
    rows = conn.execute(
        "SELECT station_id, event_date, stage, buckets_json, raw_win_probability, market_mid, hours_to_cutoff"
        " FROM basket_candidate_window_evidence WHERE market_mid IS NOT NULL AND hours_to_cutoff IS NOT NULL"
    ).fetchall()
    out, counts = [], Counter()
    for station, day, stage, buckets, claim, mid, hours in rows:
        label = labels.get((str(station), str(day)))
        if label is None:
            counts["no_label"] += 1
            continue
        index = bin_of(float(hours))
        if index is None or claim is None:
            counts["no_bin_or_claim"] += 1
            continue
        out.append({"stage": stage, "station": station, "date": day, "bin": index, "hours": float(hours),
                    "P": float(claim), "M": float(mid), "hit": 1.0 if label["bucket"] in json.loads(buckets) else 0.0})
    return out, dict(counts)


def station_day_weights(items):
    count = Counter((d["station"], d["date"]) for d in items)
    return np.array([1.0 / count[(d["station"], d["date"])] for d in items])


def bucket_scores(items):
    """Per decision, LogScore and Brier at every grid w, both forms."""

    n = len(items)
    log_lin = np.empty((n, len(W_LIN)))
    bri_lin = np.empty((n, len(W_LIN)))
    for i, d in enumerate(items):
        m, p, h = d["m"], d["p"], d["hit"]
        q_truth = (1.0 - W_LIN) * m[h] + W_LIN * p[h]
        log_lin[i] = -np.log(np.maximum(q_truth, 1e-12))
        y = np.zeros_like(m)
        y[h] = 1.0
        a, b, c = np.sum((m - y) ** 2), np.sum((m - y) * (p - m)), np.sum((p - m) ** 2)
        bri_lin[i] = a + 2.0 * W_LIN * b + W_LIN ** 2 * c
    lengths = np.array([len(d["m"]) for d in items])
    starts = np.concatenate([[0], np.cumsum(lengths)[:-1]])
    lm = logit(np.concatenate([d["m"] for d in items]))
    lp = logit(np.concatenate([d["p"] for d in items]))
    y = np.zeros(lengths.sum())
    y[starts + np.array([d["hit"] for d in items])] = 1.0
    log_log = np.empty((n, len(W_LOG)))
    bri_log = np.empty((n, len(W_LOG)))
    for j, w in enumerate(W_LOG):
        raw = 1.0 / (1.0 + np.exp(-(lm + w * (lp - lm))))
        total = np.add.reduceat(raw, starts)
        b = raw / np.repeat(total, lengths)
        log_log[:, j] = -np.log(np.maximum(np.add.reduceat(b * y, starts), 1e-12))
        bri_log[:, j] = np.add.reduceat((b - y) ** 2, starts)
    return {"lin": (log_lin, bri_lin), "logit": (log_log, bri_log)}


def window_scores(items):
    P = np.array([d["P"] for d in items])[:, None]
    M = np.array([d["M"] for d in items])[:, None]
    h = np.array([d["hit"] for d in items])[:, None]
    q = np.clip((1.0 - W_LIN) * M + W_LIN * P, EPS, 1.0 - EPS)
    return {"lin": (-(h * np.log(q) + (1.0 - h) * np.log(1.0 - q)), (q - h) ** 2)}


def analyse(items, scores, grid, rng_seed):
    """w point + day-bootstrap CI; leave-one-day-out comparisons."""

    w = station_day_weights(items)
    days = sorted({d["date"] for d in items})
    day_index = {day: i for i, day in enumerate(days)}
    di = np.array([day_index[d["date"]] for d in items])
    nd = len(days)
    sum_w = np.bincount(di, weights=w, minlength=nd)
    log_s, bri_s = scores
    S = np.zeros((nd, len(grid)))
    B = np.zeros((nd, len(grid)))
    np.add.at(S, di, w[:, None] * log_s)
    np.add.at(B, di, w[:, None] * bri_s)
    point = int(np.argmin(S.sum(axis=0) / sum_w.sum()))
    rng = np.random.default_rng(rng_seed)
    boots, draws_idx = [], []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, nd, nd), minlength=nd).astype(float)
        draws_idx.append(pick)
        boots.append(grid[int(np.argmin(pick @ S / (pick @ sum_w)))])
    zero = int(np.argmin(np.abs(grid - 0.0)))
    one = int(np.argmin(np.abs(grid - 1.0)))
    loo = np.array([int(np.argmin(S.sum(axis=0) - S[d])) for d in range(nd)])
    fused_log = S[np.arange(nd), loo]
    fused_bri = B[np.arange(nd), loo]
    per_day = {
        "fused_vs_market": (fused_log - S[:, zero], fused_bri - B[:, zero]),
        "fused_vs_model": (fused_log - S[:, one], fused_bri - B[:, one]),
        "model_vs_market": (S[:, one] - S[:, zero], B[:, one] - B[:, zero]),
    }
    comparisons = {}
    for name, (dl, db) in per_day.items():
        stats = {}
        for label, diff in (("logscore", dl), ("brier", db)):
            mean = float(diff.sum() / sum_w.sum())
            boot = np.array([float(pick @ diff / (pick @ sum_w)) for pick in draws_idx])
            stats[label] = [mean, float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]
        comparisons[name] = stats
    return {
        "decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}), "days": nd,
        "stages": dict(Counter(d["stage"] for d in items)),
        "w": float(grid[point]), "w_ci": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))],
        "logscore_market": float(S[:, zero].sum() / sum_w.sum()),
        "logscore_model": float(S[:, one].sum() / sum_w.sum()),
        "logscore_fused_in_sample": float(S[:, point].sum() / sum_w.sum()),
        "loo_w_range": [float(grid[loo].min()), float(grid[loo].max())],
        "comparisons": comparisons,
    }


def run_bucket(decisions, label):
    out = {}
    for index, name in enumerate(LABELS):
        items = [d for d in decisions if d["bin"] == index]
        if len({d["date"] for d in items}) < 3:
            out[name] = {"decisions": len(items), "note": "fewer than 3 event days"}
            continue
        scores = bucket_scores(items)
        out[name] = {form: analyse(items, scores[key], grid, SEED + index)
                     for form, key, grid in (("linear", "lin", W_LIN), ("logit", "logit", W_LOG))}
        print(label, name, "linear w", out[name]["linear"]["w"], out[name]["linear"]["w_ci"],
              "logit w", out[name]["logit"]["w"], out[name]["logit"]["w_ci"], flush=True)
    return out


def run_window(windows):
    out = {}
    for index, name in enumerate(LABELS):
        items = [d for d in windows if d["bin"] == index]
        if len({d["date"] for d in items}) < 3:
            out[name] = {"windows": len(items), "note": "fewer than 3 event days"}
            continue
        out[name] = {"linear": analyse(items, window_scores(items)["lin"], W_LIN, SEED + 100 + index)}
        print("window", name, "w", out[name]["linear"]["w"], out[name]["linear"]["w_ci"], flush=True)
    return out


def main():
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    import pickle  # noqa: PLC0415
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache is not None and cache.exists():
        decisions, bucket_counts, windows, window_counts = pickle.loads(cache.read_bytes())
    else:
        decisions, bucket_counts = bucket_population(conn)
        windows, window_counts = window_population(conn)
        if cache is not None:
            cache.write_bytes(pickle.dumps((decisions, bucket_counts, windows, window_counts)))
    conn.close()
    result = {
        "protocol": "PROTOCOL.md",
        "bucket_population": {"decisions": len(decisions), "counts": bucket_counts,
                              "dates": [min(d["date"] for d in decisions), max(d["date"] for d in decisions)]},
        "window_population": {"windows": len(windows), "counts": window_counts,
                              "dates": [min(d["date"] for d in windows), max(d["date"] for d in windows)] if windows else None},
        "bucket": run_bucket(decisions, "bucket"),
        "bucket_since_0914": run_bucket([d for d in decisions if d["date"] >= SUBPERIOD_FROM], "bucket>=09-14"),
        "window": run_window(windows),
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
