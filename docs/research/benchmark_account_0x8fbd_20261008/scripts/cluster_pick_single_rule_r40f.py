"""Round 40f (熊先生 10-12 「我说的是单桶，什么时候选择f、什么时候选择f-1或者f+1」):
one bucket only.  A three-way model (F-1 / F / F+1) on F's position in its
bucket and the directional signals (members' corrected mass above minus below,
our P0 mass above minus below, ECMWF momentum, the cluster's own station
history, ensemble mass), picking the likeliest of the three; and a two-step
rule (move to the neighbour only when F sits within t of that edge AND the
direction model agrees, t chosen inside each training fold).  Judged
leave-one-event-day-out against always-F, and by halves of the event days.
"""
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
src = here.with_name("cluster_pick_direction_r40e.py").read_text()
ns = {"__name__": "r40e", "__file__": str(here.with_name("cluster_pick_direction_r40e.py"))}
exec(compile(src.split("off = [ev for ev")[0], "r40e_head", "exec"), ns)
events, signals = ns["events"], ns["signals"]
NAMES = ["position", "member mass", "P0 mass", "momentum", "history", "ensemble mass"]


def feats(ev):
    s = signals(ev)
    return np.array([s[k] for k in NAMES] + [abs(s["position"])])


def label(ev):
    d = ev["choice"] - ev["fb"]
    return {-1: 0, 0: 1, 1: 2}.get(d)


def fit_multinomial(X, Y, l2=1.0, iters=3000, lr=0.2):
    k = 3
    W = np.zeros((X.shape[1] + 1, k))
    xb = np.hstack([X, np.ones((len(X), 1))])
    T = np.eye(k)[Y]
    for _ in range(iters):
        U = xb @ W
        P = np.exp(U - U.max(1, keepdims=True)); P /= P.sum(1, keepdims=True)
        G = xb.T @ (P - T) / len(X)
        G[:-1] += l2 * W[:-1] / len(X)
        W -= lr * G
    return W


def predict(W, x):
    return int(np.argmax(np.r_[x, 1] @ W)) - 1  # -1, 0, +1 relative to F


days = sorted({ev["ed"] for ev in events})
picks = {}
rule_picks = {}
for d in days:
    tr = [ev for ev in events if ev["ed"] != d and label(ev) is not None]
    te = [ev for ev in events if ev["ed"] == d]
    X = np.array([feats(ev) for ev in tr])
    Y = np.array([label(ev) for ev in tr])
    W = fit_multinomial(X, Y)
    # side model: up vs down on the training one-off picks
    off_tr = [ev for ev in tr if label(ev) in (0, 2)]
    Xs = np.array([feats(ev) for ev in off_tr])
    Ys = np.array([label(ev) == 2 for ev in off_tr], dtype=float)
    ws = np.zeros(Xs.shape[1] + 1)
    xb = np.hstack([Xs, np.ones((len(Xs), 1))])
    for _ in range(2000):
        p = 1 / (1 + np.exp(-xb @ ws))
        ws -= 0.1 * (xb.T @ (p - Ys) / len(Ys) + np.r_[ws[:-1], 0] / len(Ys))
    # choose t on the training fold
    best_t, best_hit = None, -1
    for t in (0.0, 0.05, 0.1, 0.15, 0.2, 0.25):
        hit = 0
        for ev in tr:
            u = signals(ev)["position"]
            side = 1 if np.r_[feats(ev), 1] @ ws > 0 else -1
            move = (0.5 - abs(u)) <= t and ((u > 0) == (side > 0)) and t > 0
            hit += ev["choice"] == ev["fb"] + (side if move else 0)
        if hit > best_hit:
            best_t, best_hit = t, hit
    for ev in te:
        picks[id(ev)] = predict(W, feats(ev))
        u = signals(ev)["position"]
        side = 1 if np.r_[feats(ev), 1] @ ws > 0 else -1
        move = best_t > 0 and (0.5 - abs(u)) <= best_t and ((u > 0) == (side > 0))
        rule_picks[id(ev)] = side if move else 0


def score(sub, chooser):
    n = max(1, len(sub))
    return sum(ev["choice"] == ev["fb"] + chooser(ev) for ev in sub) / n


mid = days[len(days) // 2]
for label_, sub in (("all", events), (f"days < {mid}", [e for e in events if e["ed"] < mid]),
                    (f"days >= {mid}", [e for e in events if e["ed"] >= mid])):
    print(f"{label_:16s} n={len(sub):3d}  always F {score(sub, lambda e: 0):.1%}"
          f"   three-way model {score(sub, lambda e: picks[id(e)]):.1%}"
          f"   edge+direction rule {score(sub, lambda e: rule_picks[id(e)]):.1%}")
moved = [e for e in events if picks[id(e)] != 0]
print(f"three-way model moves off F on {len(moved)} picks; right on {sum(e['choice'] == e['fb'] + picks[id(e)] for e in moved)},"
      f" F would have been right on {sum(e['choice'] == e['fb'] for e in moved)}")
moved = [e for e in events if rule_picks[id(e)] != 0]
print(f"edge+direction rule moves off F on {len(moved)} picks; right on {sum(e['choice'] == e['fb'] + rule_picks[id(e)] for e in moved)},"
      f" F would have been right on {sum(e['choice'] == e['fb'] for e in moved)}")
W = fit_multinomial(np.array([feats(e) for e in events if label(e) is not None]),
                    np.array([label(e) for e in events if label(e) is not None]))
print("three-way weights (columns F-1, F, F+1):")
for name, row in zip(NAMES + ["|position|", "bias"], W):
    print(f"   {name:14s} " + "  ".join(f"{v:+.2f}" for v in row))
