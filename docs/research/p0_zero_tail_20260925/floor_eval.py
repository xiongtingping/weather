#!/usr/bin/env python3
"""The existing 1e-3 model-zero floor (materialization.floor_probabilities) on P0.

Same test periods and scoring as tail_eval.py.  The floor acts per ladder
bucket, so the replay's integer vectors are folded onto an eleven-bucket
ladder centred on the served mean with open ends (production's shape) before
flooring and scoring; the live envelopes are scored on their own ladder.  The
charter-224 t-tail (eps 0.05, s 2) is re-scored on the same folds for
comparison.  Writes floor_eval.json.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.materialization import PROBABILITY_MODEL_ZERO_FLOOR, floor_probabilities  # noqa: E402

_spec = importlib.util.spec_from_file_location("tail_eval", HERE / "tail_eval.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)
HALF = 5                         # eleven buckets: centre +- 5, both ends open


def fold_replay(data):
    """Integer vectors -> ladder of 11 (open ends at centre-5 and centre+5)."""
    n, width = data["p"].shape
    centre_col = T.WINDOW
    p = np.zeros((n, 2 * HALF + 1)); y = np.zeros_like(p)
    lo = np.zeros_like(p); hi = np.zeros_like(p)
    keys_lo = data["lo"][:, centre_col]            # centre bucket lower edge (k - 0.5)
    for j in range(2 * HALF + 1):
        off = j - HALF
        col = centre_col + off
        if j == 0:
            p[:, j] = data["p"][:, :col + 1].sum(axis=1); y[:, j] = data["y"][:, :col + 1].sum(axis=1)
            lo[:, j] = -np.inf; hi[:, j] = keys_lo + off + 1
        elif j == 2 * HALF:
            p[:, j] = data["p"][:, col:].sum(axis=1); y[:, j] = data["y"][:, col:].sum(axis=1)
            lo[:, j] = keys_lo + off; hi[:, j] = np.inf
        else:
            p[:, j] = data["p"][:, col]; y[:, j] = data["y"][:, col]
            lo[:, j] = keys_lo + off; hi[:, j] = keys_lo + off + 1
    return {**data, "p": p, "y": y, "lo": lo, "hi": hi}


def floored(p_matrix, valid):
    out = np.zeros_like(p_matrix)
    for i in range(p_matrix.shape[0]):
        cols = np.where(valid[i])[0]
        vec, _ = floor_probabilities({str(c): float(p_matrix[i, c]) for c in cols})
        out[i, cols] = [vec[str(c)] for c in cols]
    return out


def evaluate(name, data, test_mask):
    test = T.subset(data, test_mask)
    w = T.weights(test["sd"])
    valid = ~np.isnan(test["lo"])
    b0, n0, p0 = T.scores(test["p"], test["y"])
    arms = {"floor_1e-3": floored(test["p"], valid)}
    q = T.tail_q(test, 2.0, "t")
    arms["t_tail_0.05_2"] = 0.95 * test["p"] + 0.05 * q
    out = {"data": name, "test_station_days": len(set(test["sd"])), "test_updates": int(test_mask.sum()), "arms": {}}
    mean = lambda v: float((w * v).sum() / w.sum())
    for arm, pm in arms.items():
        b1, n1, p1 = T.scores(pm, test["y"])
        changed = int((np.abs(pm - test["p"]).max(axis=1) > 1e-15).sum())
        out["arms"][arm] = {
            "brier": {"base": mean(b0), "arm": mean(b1), "delta": mean(b1 - b0), "ci95": T.boot_ci(b1 - b0, w, test["day"])},
            "logscore": {"base": mean(n0), "arm": mean(n1), "delta": mean(n1 - n0), "ci95": T.boot_ci(n1 - n0, w, test["day"])},
            "zero_truth": {"base": int((p0 <= 0).sum()), "arm": int((p1 <= 0).sum())},
            "updates_changed": changed,
            "min_bucket": float(pm[valid].min()),
        }
        a = out["arms"][arm]
        # Charter 224 (user ruling 2026-09-25): log score significantly lower and
        # Brier not significantly higher.
        a["non_inferior"] = bool(a["logscore"]["ci95"][1] < 0 and a["brier"]["ci95"][0] <= 0)
    return out


def main():
    replay = fold_replay(T.load_replay())
    live = T.load_live()
    results = [evaluate("replay_ll_on_ladder11", replay, replay["day"] > "2026-08-31"),
               evaluate("live_p0", live, live["day"] > "2026-09-08")]
    (HERE / "floor_eval.json").write_text(json.dumps(results, indent=1))
    print("floor", PROBABILITY_MODEL_ZERO_FLOOR)
    for r in results:
        for arm, a in r["arms"].items():
            print(f"{r['data']:22s} {arm:14s} {r['test_station_days']} 站日 | Brier {a['brier']['delta']:+.5f} "
                  f"[{a['brier']['ci95'][0]:+.5f},{a['brier']['ci95'][1]:+.5f}] | LogScore {a['logscore']['delta']:+.4f} "
                  f"[{a['logscore']['ci95'][0]:+.4f},{a['logscore']['ci95'][1]:+.4f}] | zeros {a['zero_truth']} | "
                  f"changed {a['updates_changed']}/{r['test_updates']} | min {a['min_bucket']:.2e} | 不劣={a['non_inferior']}", flush=True)


if __name__ == "__main__":
    main()
