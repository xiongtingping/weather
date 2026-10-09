#!/usr/bin/env python3
"""PROTOCOL.md addendum 1: the persistence lead, coefficients frozen on 09-12..10-08
(I2), replayed on the unseen event days 08-15..09-11.  Read-only."""
from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.linear_model import LogisticRegression

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
MODES = ROOT / "docs/research/single_system_mode3_logloss_20261009/market_modes"
X = "S4 持续性(昨日METAR最高−众数)"
FIRST, LAST = "2026-08-15", "2026-09-11"
METAR_TD = re.compile(r"\s(M?\d{2})/(M?\d{2})?\s")


def logit(x):
    x = min(max(x, 1e-6), 1 - 1e-6)
    return math.log(x / (1 - x))


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def centre_c(label):
    """Bucket centre in C and rung in C from the market's own label."""
    s = label.replace("°", "°")
    if "°F" in s:
        nums = [int(n) for n in re.findall(r"\d+", s)]
        if "below" in s:
            f = nums[0] + 0.5 - 1.0
        elif "higher" in s or "above" in s:
            f = nums[0] - 0.5 + 1.0
        else:
            f = (nums[0] + nums[1]) / 2.0
        return (f - 32) * 5 / 9, 2 * 5 / 9
    n = int(re.findall(r"-?\d+", s)[0])
    return float(n), 1.0


# ---- frozen fit on the discovery sample
disc = [json.loads(l) for l in gzip.open(HERE / "signals.jsonl.gz", "rt")]
disc = [r for r in disc if r["tag"] == "I2" and r["y_side"] != 0 and r["x"].get(X) is not None]
Xb = np.array([[logit(r["Lq"])] for r in disc]); Xc = np.array([[logit(r["Lq"]), r["x"][X]] for r in disc])
yd = np.array([1.0 if r["y_side"] > 0 else 0.0 for r in disc])
base = LogisticRegression(C=1e6, max_iter=5000).fit(Xb, yd)
cand = LogisticRegression(C=1e6, max_iter=5000).fit(Xc, yd)
frozen = {"base": [float(base.intercept_[0]), float(base.coef_[0][0])],
          "cand": [float(cand.intercept_[0])] + [float(v) for v in cand.coef_[0]], "n_discovery": len(disc)}

# ---- backtest rows
ssot = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True)
arch = sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro", uri=True)
tz = dict(ssot.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall())
labels = {}
for st, day, bucket, rev in ssot.execute(
        "SELECT station_id, event_date, settled_bucket, truth_revision_no FROM basket_station_day_settlement_label"
        " WHERE event_date BETWEEN ? AND ? ORDER BY truth_revision_no", (FIRST, LAST)):
    labels[(st, day)] = bucket
rows, counts = [], defaultdict(int)
for path in sorted(MODES.glob("*.json.gz")):
    st = path.name[:4]
    modes = json.loads(gzip.open(path, "rt").read())
    zone = ZoneInfo(tz[st])
    obs = [(utc(v), raw) for v, raw in arch.execute(
        "SELECT valid_at, raw_text FROM reported_trajectory_observations WHERE station_id=? AND valid_at>=? AND valid_at<?"
        " ORDER BY valid_at", (st, "2026-08-10", "2026-09-13"))]
    for day, series in modes.items():
        if not FIRST <= day <= LAST:
            continue
        truth = labels.get((st, day))
        if truth is None:
            counts["no_label"] += 1; continue
        instant = datetime.fromisoformat(day).replace(hour=3, tzinfo=zone).astimezone(timezone.utc)
        found = None
        for h, mode, q, _seen in series:
            if utc(h) <= instant:
                found = (mode, q)
            else:
                break
        if not found or not found[0] or not found[1]:
            counts["no_book"] += 1; continue
        mode, q = found
        order = list(q)
        if truth not in order or mode not in order:
            counts["label_off_ladder"] += 1; continue
        M, k = order.index(mode), order.index(truth)
        if k == M:
            counts["at_mode"] += 1; continue
        Qp, Qm = sum(q[b] for b in order[M + 1:]), sum(q[b] for b in order[:M])
        tm, rung = centre_c(mode)
        yday = (datetime.fromisoformat(day) - timedelta(days=1)).date()
        lo = datetime(yday.year, yday.month, yday.day, tzinfo=zone).astimezone(timezone.utc); hi = lo + timedelta(days=1)
        temps = []
        for t, raw in obs:
            if lo <= t < hi and raw:
                m = METAR_TD.search(" " + raw + " ")
                if m:
                    s = m.group(1); temps.append(-float(s[1:]) if s.startswith("M") else float(s))
        if len(temps) < 12:
            counts["no_metar"] += 1; continue
        rows.append({"st": st, "d": day, "Lq": Qp / (Qp + Qm) if Qp + Qm > 0 else 0.5, "x": (max(temps) - tm) / rung,
                     "y": 1.0 if k > M else 0.0})
        counts["rows"] += 1


def prob(coef, feats):
    z = coef[0] + sum(c * f for c, f in zip(coef[1:], feats))
    return 1 / (1 + math.exp(-z))


def bll(p, y):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


diff = [bll(prob(frozen["cand"], [logit(r["Lq"]), r["x"]]), r["y"]) - bll(prob(frozen["base"], [logit(r["Lq"])]), r["y"]) for r in rows]
days = sorted({r["d"] for r in rows}); half = days[len(days) // 2]
by = defaultdict(list)
for r, d in zip(rows, diff):
    by[r["d"]].append(d)
sums = np.array([sum(by[d]) for d in days]); cnt = np.array([len(by[d]) for d in days], float)
rng = np.random.default_rng(20261010); draws = []
for _ in range(2000):
    starts = rng.integers(0, len(days), size=math.ceil(len(days) / 3))
    idx = np.array([(s + j) % len(days) for s in starts for j in range(3)][:len(days)])
    draws.append(sums[idx].sum() / cnt[idx].sum())
lo, hi = float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))
h1 = float(np.mean([d for r, d in zip(rows, diff) if r["d"] < half])); h2 = float(np.mean([d for r, d in zip(rows, diff) if r["d"] >= half]))
shape = []
for a, b in ((-99, -1.5), (-1.5, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 99)):
    g = [r for r in rows if a <= r["x"] < b]
    if g:
        shape.append({"bin": [a, b], "n": len(g), "market_up": float(np.mean([r["Lq"] for r in g])), "actual_up": float(np.mean([r["y"] for r in g]))})
out = {"frozen": frozen, "counts": dict(counts), "days": [days[0], days[-1], len(days)], "n": len(rows), "delta": float(np.mean(diff)),
       "ci": [lo, hi], "halves": [h1, h2], "verdict": "复现" if hi < 0 and h1 < 0 and h2 < 0 else "不复现", "shape": shape}
(HERE / "results_backtest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print(json.dumps({k: v for k, v in out.items() if k != "shape"}, ensure_ascii=False))
for s in shape:
    print(f"  昨日最高−众数 {s['bin']} n={s['n']:3d} 市场说向上 {s['market_up']:.3f} 实际向上 {s['actual_up']:.3f}")
