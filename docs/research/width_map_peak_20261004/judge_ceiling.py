#!/usr/bin/env python3
"""PROTOCOL.md, the amendment: the ceiling arms on the current-chain replay
(rows/) and, as the independent check, on the served record
(final_vector_acceptance_20261004/frame.pkl).

    python3 judge_ceiling.py
"""
import json
import os
import pickle
import sys
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from bayes_basket_target.center_width_store import _proper_score_gate  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
import judge as J  # noqa: E402
import rows as R  # noqa: E402

C_GRID = tuple(round(0.40 + 0.01 * i, 2) for i in range(31))
MIN_CELLS = 100
MIN_PREFIX_DAYS = 8
SCOPE_FROM = 0.40
BOOT = 2000
SEED = 20261004


def ceiling(vec, c):
    """No more than ``c`` on the top bucket: the excess goes to its two
    neighbours in proportion to their mass, a neighbour stops at ``c``, and
    what is left goes one ring further out."""
    vec = np.array(vec, dtype=float)
    top = int(np.argmax(vec))
    if c is None or vec[top] <= c:
        return vec
    excess = vec[top] - c
    vec[top] = c
    ring = 1
    while excess > 1e-15 and ring < len(vec):
        sides = [i for i in (top - ring, top + ring) if 0 <= i < len(vec)]
        room = np.array([max(c - vec[i], 0.0) for i in sides])
        mass = np.array([vec[i] for i in sides])
        share = mass / mass.sum() if mass.sum() > 0 else np.full(len(sides), 1.0 / max(len(sides), 1))
        give = np.minimum(excess * share, room)
        left = excess - give.sum()
        # what one side could not take, the other may, up to its own room
        spare = room - give
        if left > 1e-15 and spare.sum() > 0:
            extra = np.minimum(spare, left * spare / spare.sum())
            give = give + extra
            left = excess - give.sum()
        for i, g in zip(sides, give):
            vec[i] += g
        excess = left
        ring += 1
    if excess > 1e-15:
        vec[top] += excess
    return vec


def fit_c(tops, hits):
    """The grid c whose claim equals the top bucket's hit rate at or above it."""
    best = None
    any_over = False
    for c in C_GRID:
        m = tops >= c
        if m.sum() < MIN_CELLS:
            continue
        h = hits[m].mean()
        if h <= c:
            any_over = True
        if best is None or abs(h - c) < best[0]:
            best = (abs(h - c), c)
    return best[1] if (best is not None and any_over) else None


def forward_c(days, d, known, tops, hits, group):
    out = {}
    for day in days:
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        m = (d <= cut) & (known <= day + "T00:00:00Z") & group
        if len(set(d[m])) < MIN_PREFIX_DAYS:
            out[day] = None
            continue
        out[day] = fit_c(tops[m], hits[m])
    return out


def boot_gap(num_a, num_b, codes, k):
    """mean(num_a), mean(num_b) and their difference with event-day intervals."""
    ones = np.ones(len(codes))
    sa, sb, n = np.bincount(codes, num_a, k), np.bincount(codes, num_b, k), np.bincount(codes, ones, k)
    idx = np.random.default_rng(SEED).integers(0, k, size=(BOOT, k))
    a, b = sa[idx].sum(1) / n[idx].sum(1), sb[idx].sum(1) / n[idx].sum(1)
    pct = lambda v: [float(x) for x in np.percentile(v, [2.5, 97.5])]  # noqa: E731
    return (float(num_a.mean()), pct(a)), (float(num_b.mean()), pct(b)), (float(num_a.mean() - num_b.mean()), pct(a - b))


def show(v, scale=100, digits=1):
    est, ci = v
    star = "*" if (ci[0] > 0 or ci[1] < 0) else " "
    return f"{est*scale:+.{digits}f} [{ci[0]*scale:+.{digits}f},{ci[1]*scale:+.{digits}f}]{star}"


def gate_line(base, cand, clusters):
    g = _proper_score_gate(base, cand, clusters)

    def f(v, ci):
        star = "*" if (ci[0] > 0 or ci[1] < 0) else " "
        return f"{v:+.5f} [{ci[0]:+.5f},{ci[1]:+.5f}]{star}"

    return g, f"ΔLogScore {f(g['d_logscore'], g['ci_logscore'])}  ΔCRPS {f(g['d_crps'], g['ci_crps'])}  ΔBrier {f(g['d_brier'], g['ci_brier'])}"


def not_worse(g):
    return all(g[k] is not None and g[k][0] <= 0.0 for k in ("ci_logscore", "ci_crps", "ci_brier"))


def replay(result):
    rows = J.load()
    days = sorted({r["d"] for r in rows})
    code = {d: i for i, d in enumerate(days)}
    d = np.array([r["d"] for r in rows])
    known = np.array([r["known"] for r in rows])
    tops = np.array([max(r["p75"]) for r in rows])
    hits = np.array([1.0 if r["keys"][int(np.argmax(r["p75"]))] == r["truth"] else 0.0 for r in rows])
    far = np.array([r["bin"] <= 1 for r in rows])
    c_far = forward_c(days, d, known, tops, hits, far)
    c_near = forward_c(days, d, known, tops, hits, ~far)
    late = [x for x in days if x >= "2026-09-01"]
    print(f"现行链回放  决策 {len(rows)}  >36h {int(far.sum())}")
    print("  前推 c（后段）  >36h:", sorted({c_far[x] for x in late}, key=lambda v: (v is None, v)),
          "  ≤36h:", sorted({c_near[x] for x in late}, key=lambda v: (v is None, v)))
    result["replay"] = {"c_far": c_far, "c_near": c_near}

    def cell(r, c):
        keys = r["keys"]
        return R.read(ceiling(r["p75"], c), keys, [float(int(k)) for k in keys], r["actual"], r["truth"])

    base = [r["P"][J.G75] for r in rows]
    arms = {"K_far": [cell(r, c_far[r["d"]]) if r["bin"] <= 1 else b for r, b in zip(rows, base)],
            "K_all": [cell(r, c_far[r["d"]] if r["bin"] <= 1 else c_near[r["d"]]) for r in rows]}
    after = {"K_far": np.array([min(t, c_far[r["d"]]) if (r["bin"] <= 1 and c_far[r["d"]] is not None) else t
                                for r, t in zip(rows, tops)]),
             "K_all": np.array([min(t, c) if c is not None else t for t, c in
                                ((t, c_far[r["d"]] if r["bin"] <= 1 else c_near[r["d"]]) for r, t in zip(rows, tops))])}
    codes = np.array([code[x] for x in d])
    verdict = {}
    for arm, scope, sname in (("K_far", far, ">36h"), ("K_all", far, ">36h"), ("K_all", ~far, "≤36h")):
        m = scope & (tops >= SCOPE_FROM)
        gap_p, gap_k, diff = boot_gap(hits[m] - tops[m], hits[m] - after[arm][m], codes[m], len(days))
        print(f"  [{arm} {sname}] 压顶前 p_top≥0.40 的决策 {int(m.sum())}：平均 p_top {tops[m].mean():.3f} → {after[arm][m].mean():.3f}，"
              f"最高桶命中 {hits[m].mean():.3f}")
        print(f"      命中−p_top  P@0.75 {show(gap_p)}   {arm} {show(gap_k)}   两臂之差 {show(diff)}")
        peak_ok = bool(gap_k[1][0] <= 0 <= gap_k[1][1] and abs(gap_k[0]) < abs(gap_p[0]) and (diff[1][0] > 0 or diff[1][1] < 0))
        verdict[f"{arm} {sname}"] = {"gap_base": gap_p, "gap_arm": gap_k, "diff": diff, "peak_fixed": peak_ok}
    for arm in ("K_far", "K_all"):
        for sname, scope in ((">36h", far), ("全部决策", np.ones(len(rows), bool))):
            ok = True
            for seg, start, end in J.SEGMENTS:
                m = scope & (d >= start) & (d <= end)
                idx = np.where(m)[0]
                g, line = gate_line([tuple(base[i][0]) for i in idx], [tuple(arms[arm][i][0]) for i in idx],
                                    [(rows[i]["st"], rows[i]["d"]) for i in idx])
                print(f"  [{arm} {sname} {seg}] {line}")
                if seg == "全期":
                    ok &= not_worse(g)
                else:
                    ok &= g["d_logscore"] <= 0.001
                verdict.setdefault(f"{arm} scores", {})[f"{sname} {seg}"] = g
            verdict[f"{arm} scores"][f"{sname} not_worse"] = bool(ok)
            print(f"      分数不变差（{sname}）：{'是' if ok else '否'}")
    # reliability of every bucket after the ceiling, for the record
    for arm in ("K_far",):
        sub = {"P@0.75": [c for c, ok in zip(base, far) if ok], arm: [c for c, ok in zip(arms[arm], far) if ok]}
        for lo, hi, name in ((0.4, 0.5, "0.4–0.5"), (0.5, 1.01, "≥0.5")):
            t = J.boot_gap(sub, list(codes[far]), len(days), lo, hi, "P@0.75")
            for a, v in t.items():
                if v is None:
                    print(f"  [>36h 全部桶 p {name}] {a}: 无")
                else:
                    print(f"  [>36h 全部桶 p {name}] {a}: 桶 {v['n']} 预测 {v['pred']:.3f} 命中 {v['hit']:.3f} 差 {v['gap']*100:+.1f} [{v['ci'][0]*100:+.1f},{v['ci'][1]*100:+.1f}]")
    result["replay"]["verdict"] = verdict


def served(result):
    """The served record: final vectors on the market ladder, v<=20, past 36 h."""
    sys.path.insert(0, os.path.join(ROOT, "docs", "research", "final_vector_acceptance_20261004"))
    with open(os.path.join(ROOT, "docs", "research", "final_vector_acceptance_20261004", "frame.pkl"), "rb") as fh:
        rec = pickle.load(fh)
    days = sorted({x["d"] for x in rec})
    code = {d: i for i, d in enumerate(days)}
    d = np.array([x["d"] for x in rec])
    tops = np.array([float(x["p"].max()) for x in rec])
    hits = np.array([1.0 if int(np.argmax(x["p"])) == x["y"] else 0.0 for x in rec])
    known = np.array(["" for _ in rec])                      # the label of d-2 is settled by d 00:00Z
    c_by_day = forward_c(days, d, known, tops, hits, np.ones(len(rec), bool))
    print(f"\n实服记录（市场梯子，v≤20，>36h）  决策 {len(rec)}  事件日 {len(days)}  {days[0]}..{days[-1]}")
    print("  前推 c：", sorted({v for v in c_by_day.values()}, key=lambda v: (v is None, v)))

    def scores(vec, y):
        vec = np.asarray(vec, dtype=float)
        onehot = (np.arange(len(vec)) == y).astype(float)
        return (-float(np.log(vec[y])), float(((vec - onehot) ** 2).sum()),
                float(((np.cumsum(vec) - np.cumsum(onehot)) ** 2).sum()))

    def floored(vec):
        out, _ = floor_probabilities({str(i): float(v) for i, v in enumerate(vec)})
        return np.array([out[str(i)] for i in range(len(vec))])

    base = [scores(x["p"], x["y"]) for x in rec]
    capped = [floored(ceiling(x["p"], c_by_day[x["d"]])) for x in rec]
    cand = [scores(v, x["y"]) for v, x in zip(capped, rec)]
    after = np.array([float(v[int(np.argmax(x["p"]))]) for v, x in zip(capped, rec)])
    codes = np.array([code[x] for x in d])
    m = tops >= SCOPE_FROM
    gap_p, gap_k, diff = boot_gap(hits[m] - tops[m], hits[m] - after[m], codes[m], len(days))
    print(f"  压顶前 p_top≥0.40 的决策 {int(m.sum())}：平均 p_top {tops[m].mean():.3f} → {after[m].mean():.3f}，最高桶命中 {hits[m].mean():.3f}")
    print(f"      命中−p_top  服务向量 {show(gap_p)}   压顶后 {show(gap_k)}   两者之差 {show(diff)}")
    scored = [i for i, x in enumerate(rec) if c_by_day[x["d"]] is not None]
    verdict = {"gap_base": gap_p, "gap_arm": gap_k, "diff": diff,
               "peak_fixed": bool(gap_k[1][0] <= 0 <= gap_k[1][1] and abs(gap_k[0]) < abs(gap_p[0])
                                  and (diff[1][0] > 0 or diff[1][1] < 0))}
    for seg, keep in (("有 c 的全部日子", lambda x: True), ("09-26 起", lambda x: x["d"] >= "2026-09-26")):
        idx = [i for i in scored if keep(rec[i])]
        g, line = gate_line([base[i] for i in idx], [cand[i] for i in idx], [(rec[i]["st"], rec[i]["d"]) for i in idx])
        print(f"  [{seg}] 决策 {len(idx)}  {line}")
        verdict[seg] = g
    verdict["scores_not_worse"] = bool(not_worse(verdict["有 c 的全部日子"]))
    result["served"] = {"c": c_by_day, "verdict": verdict}
    print(f"      峰修好 {'是' if verdict['peak_fixed'] else '否'}   分数不变差 {'是' if verdict['scores_not_worse'] else '否'}")


if __name__ == "__main__":
    out = {}
    replay(out)
    served(out)
    with open(os.path.join(HERE, "results_ceiling.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, default=float)
