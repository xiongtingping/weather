#!/usr/bin/env python3
"""Why the charter 248 slice lane does not clear the gate (2026-10-03, read-only).

Rebuilds the lane's own slice cells with production functions (the evidence
floor, ``serving_cells_by_day``, ``_split_slice_cells``, ``_cell_tail_scores``)
and takes the per-cell change at a fixed weight apart:

1. where the gain comes from -- truth in the cold tail / body / warm tail of
   the cell's own (unmixed) vector;
2. what carries the interval's width -- each group's share of the
   station-day cluster variance the gate bootstraps;
3. whether the miss is one-sided (cold vs warm tail hit rates against the
   vector's own claim), and what a one-sided mix would have bought
   (exploratory, labelled so);
4. the lane's cells against the replay study's on the same station-days:
   cell weighting, ladder vs integer grid, served-history vs current chain.

Writes results_root_cause.json.
"""
from __future__ import annotations

import collections
import gzip
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import center_width_store as store  # noqa: E402
from bayes_basket_target.center_width_calibration import apply_center_width_calibration  # noqa: E402

F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
TAIL = 0.05
rng = np.random.default_rng(20261003)


def cluster_boot(values, clusters, draws=2000):
    ids = {}
    idx = np.array([ids.setdefault(c, len(ids)) for c in clusters])
    sums = np.bincount(idx, weights=values, minlength=len(ids))
    counts = np.bincount(idx, minlength=len(ids)).astype(float)
    pick = rng.integers(0, len(ids), size=(draws, len(ids)))
    stats = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    return float(values.mean()), float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def position(cell):
    """(PIT-below mass, PIT-above mass) of the settled bucket on the cell's own vector."""
    keys = cell["ordered_keys"]; p = cell["probabilities"]
    total = sum(float(p.get(k, 0.0)) for k in keys)
    hit = keys.index(cell["settled_bucket"])
    below = sum(float(p.get(k, 0.0)) for k in keys[:hit]) / total
    above = sum(float(p.get(k, 0.0)) for k in keys[hit + 1:]) / total
    return below, above


def claimed_tails(cell):
    """The vector's own mass on the buckets ``position`` would call cold / warm tail."""
    keys = cell["ordered_keys"]; p = [float(cell["probabilities"].get(k, 0.0)) for k in keys]
    total = sum(p); p = [v / total for v in p]
    cold = warm = 0.0; below = 0.0
    for i, v in enumerate(p):
        above = 1.0 - below - v
        if below < TAIL:
            cold += v
        if above < TAIL:
            warm += v
        below += v
    return cold, warm


def one_sided_mix(cell, weight, side, scale=1.5):
    """Exploratory: the tail mix applied to one side only -- the widened copy's
    mass beyond the mean on the other side is replaced by the input's own."""
    keys = cell["ordered_keys"]; centers = cell["key_centers_c"]
    p = {k: float(cell["probabilities"].get(k, 0.0)) for k in keys}
    total = sum(p.values()); p = {k: v / total for k, v in p.items()}
    widened, _ = apply_center_width_calibration(p, delta_c=0.0, width_lambda=scale, ordered_keys=keys, key_centers_c=centers,
                                                excluded_buckets=cell["excluded_buckets"], open_edge_bounds_c=cell.get("open_edge_bounds_c"))
    mean = sum(c * p[k] for c, k in zip(centers, keys))
    w = {}
    for c, k in zip(centers, keys):
        keep_input = (side == "cold" and c > mean) or (side == "warm" and c < mean)
        w[k] = p[k] if keep_input else float(widened.get(k, 0.0))
    s = sum(w.values()); w = {k: v / s for k, v in w.items()}
    mixed = {k: (1 - weight) * p[k] + weight * w[k] for k in keys}
    return store._served_scores(mixed, ordered_keys=keys, settled_bucket=cell["settled_bucket"],
                                excluded_buckets=cell["excluded_buckets"], key_centers_c=centers)


def main():
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    floor = store._evidence_floor(conn, stage="P0", caller_bound="2026-08-27T09:20:00Z")
    truth = store._truth(conn)
    by_day = store.serving_cells_by_day(conn, truth, stage="P0", min_decision_at_utc=floor)
    conn.close()
    _pooled, sliced = store._split_slice_cells(by_day)
    cells = [c for day in sorted(sliced) for c in sliced[day]]
    print("evidence floor", floor, "slice days", len(sliced), "cells", len(cells))
    rows = []
    for c in cells:
        base = store._cell_tail_scores(c, delta_c=0.0, width_lambda=1.0, tail_mix_weight=0.0)
        arms = {w: store._cell_tail_scores(c, delta_c=0.0, width_lambda=1.0, tail_mix_weight=w) for w in (0.1, 0.15, 0.2, 0.3)}
        below, above = position(c)
        claimed_cold, claimed_warm = claimed_tails(c)
        group = "cold_tail" if below < TAIL else ("warm_tail" if above < TAIL else "body")
        receipt = c.get("center_width_receipt") or {}
        rows.append({
            "station": c["station_id"], "day": c["event_date"], "decided": c["decision_at_utc"], "group": group,
            "below": below, "above": above, "claimed_cold": claimed_cold, "claimed_warm": claimed_warm, "unit": "F" if c["station_id"] in F_STATIONS else "C",
            "seated": c.get("seated_system_count"), "reserved": bool(c.get("reserved_under_current_width")),
            "legacy_slice": receipt.get("width_slice_lambda") is not None,
            "base": base, **{f"w{w}": s for w, s in arms.items()},
            "cold_02": one_sided_mix(c, 0.2, "cold"), "warm_02": one_sided_mix(c, 0.2, "warm"),
        })
    n = len(rows)
    clusters = [(r["station"], r["day"]) for r in rows]
    d = {w: np.array([r[f"w{w}"][0] - r["base"][0] for r in rows]) for w in (0.1, 0.15, 0.2, 0.3)}
    res = {"cells": n, "station_days": len(set(clusters)), "days": len({r["day"] for r in rows}), "evidence_floor": floor}
    res["fixed_weight_in_sample"] = {str(w): cluster_boot(d[w], clusters) for w in d}
    # 1. where the gain comes from (w = 0.2, the lane's recent choice)
    dd = d[0.2]
    groups = collections.defaultdict(list)
    for i, r in enumerate(rows):
        groups[r["group"]].append(i)
    decomposition = {}
    for g, idx in groups.items():
        idx = np.array(idx)
        decomposition[g] = {"cells": len(idx), "share_cells": len(idx) / n, "mean_dlog": float(dd[idx].mean()),
                            "contribution_to_mean": float(dd[idx].sum() / n), "mean_abs": float(np.abs(dd[idx]).mean())}
    res["decomposition_w0.2"] = decomposition
    # 2. what carries the interval: share of between-cluster variance by group
    ids = {}
    cidx = np.array([ids.setdefault(c, len(ids)) for c in clusters])
    mean = dd.mean()
    by_cluster = np.bincount(cidx, weights=dd - mean, minlength=len(ids))
    group_var = {}
    for g, idx in groups.items():
        mask = np.zeros(n, bool); mask[np.array(idx)] = True
        part = np.bincount(cidx, weights=np.where(mask, dd - mean, 0.0), minlength=len(ids))
        group_var[g] = float((part * by_cluster).sum() / (by_cluster ** 2).sum())
    res["variance_share_w0.2"] = group_var
    res["dlog_distribution_w0.2"] = {"p1": float(np.percentile(dd, 1)), "p5": float(np.percentile(dd, 5)), "p50": float(np.percentile(dd, 50)),
                                     "p95": float(np.percentile(dd, 95)), "p99": float(np.percentile(dd, 99)), "max": float(dd.max()), "min": float(dd.min()),
                                     "excess_kurtosis": float(((dd - mean) ** 4).mean() / ((dd - mean) ** 2).mean() ** 2 - 3)}
    # 3. one-sidedness
    below = np.array([r["below"] for r in rows]); above = np.array([r["above"] for r in rows])
    res["tail_hits"] = {"cold_realised": float((below < TAIL).mean()), "warm_realised": float((above < TAIL).mean()),
                        "cold_claimed": float(np.mean([r["claimed_cold"] for r in rows])), "warm_claimed": float(np.mean([r["claimed_warm"] for r in rows]))}
    # the walk-forward the lane runs, reproduced: each held-out day at the
    # weight the lane's fold chose for it (from the dry run), and at a fixed 0.2
    lane = json.loads(Path("/private/tmp/claude-501/-Users-xiong-weather/6685567a-65b6-4404-82ae-cb6b25b445fd/scratchpad/refit_dry_lane.json").read_text())
    chosen = {f["scored_day"]: f["slice_tail_mix_weight"] for f in lane["folds"]}
    held = [i for i, r in enumerate(rows) if r["day"] in chosen]
    wf = np.array([rows[i][f"w{chosen[rows[i]['day']]}"][0] - rows[i]["base"][0] if chosen[rows[i]['day']] in (0.1, 0.15, 0.2, 0.3) else np.nan for i in held])
    fixed = dd[np.array(held)]
    hc = [clusters[i] for i in held]
    res["walk_forward_reproduced"] = cluster_boot(wf[~np.isnan(wf)], [c for c, x in zip(hc, wf) if not np.isnan(x)])
    res["held_out_days_fixed_w0.2"] = cluster_boot(fixed, hc)
    res["held_out_days_fixed_w0.15"] = cluster_boot(d[0.15][np.array(held)], hc)
    res["fold_weight_counts"] = dict(collections.Counter(chosen.values()))
    # guardrails for the fixed weights on the held-out days (Brier index 1, CRPS index 2)
    hidx = np.array(held)
    for w in (0.15, 0.2):
        for name, k in (("brier", 1), ("crps", 2)):
            dv = np.array([rows[i][f"w{w}"][k] - rows[i]["base"][k] for i in hidx])
            res[f"held_out_days_fixed_w{w}_{name}"] = cluster_boot(dv, hc)
    # the deepest saves, named
    deep = np.argsort(dd)[:12]
    res["deepest_saves_w0.2"] = []
    for i in deep:
        r = rows[i]; c = cells[i]
        keys = c["ordered_keys"]; pv = c["probabilities"]; tot = sum(float(pv[k]) for k in keys)
        centers = c["key_centers_c"]
        mean_c = sum(cc * float(pv[k]) / tot for cc, k in zip(centers, keys))
        hit = keys.index(c["settled_bucket"])
        res["deepest_saves_w0.2"].append({
            "station": r["station"], "day": r["day"], "decided": r["decided"][:16], "seated": r["seated"],
            "truth_bucket": c["settled_bucket"], "vector_mean_c": round(mean_c, 2),
            "truth_minus_mean_c": round(float(centers[hit]) - mean_c, 2),
            "base_p_truth": round(math.exp(-r["base"][0]), 4), "mixed_p_truth": round(math.exp(-r["w0.2"][0]), 4),
            "dlog": round(float(dd[i]), 3),
        })
    # how concentrated the gain is: the deep saves
    order = np.sort(dd)
    res["concentration_w0.2"] = {
        "cells_dlog_lt_-0.5": int((dd < -0.5).sum()),
        "sum_of_those_over_n": float(dd[dd < -0.5].sum() / n),
        "mean_without_them": float(dd[dd >= -0.5].mean()),
        "top_1pct_cells_sum_over_n": float(order[: max(1, n // 100)].sum() / n),
        "mean_without_top_1pct": float(order[max(1, n // 100):].mean()),
        "station_days_behind_lt_-0.5": len({clusters[i] for i in np.where(dd < -0.5)[0]}),
        "station_days_behind_top_1pct": len({clusters[i] for i in np.argsort(dd)[: max(1, n // 100)]}),
    }
    # the gate's own clusters: drop the station-days behind the top 1% and re-read
    top_sd = {clusters[i] for i in np.argsort(dd)[: max(1, n // 100)]}
    keep = np.array([c not in top_sd for c in clusters])
    res["without_top_station_days_w0.2"] = cluster_boot(dd[keep], [c for c, k in zip(clusters, keep) if k])
    for side in ("cold", "warm"):
        ds = np.array([r[f"{side}_02"][0] - r["base"][0] for r in rows])
        res[f"one_sided_{side}_w0.2_exploratory"] = cluster_boot(ds, clusters)
    # 4. lane cells by era / unit / weighting
    def seg(mask, label):
        idx = np.where(mask)[0]
        if len(idx) < 30:
            return None
        return {"label": label, "cells": int(len(idx)), "station_days": len({clusters[i] for i in idx}),
                "dlog_w0.2": cluster_boot(dd[idx], [clusters[i] for i in idx]),
                "cold_tail_hits": float((below[idx] < TAIL).mean()), "warm_tail_hits": float((above[idx] < TAIL).mean())}
    day = np.array([r["day"] for r in rows])
    res["segments"] = [s for s in (
        seg(day <= "2026-09-25", "09-13..09-25 (pre-236 seating, more thin pools)"),
        seg(day >= "2026-09-26", "09-26..10-02"),
        seg(np.array([r["unit"] == "C" for r in rows]), "C stations"),
        seg(np.array([r["unit"] == "F" for r in rows]), "F stations (ladder rung 1.11C)"),
        seg(np.array([r["legacy_slice"] for r in rows]), "served under the retired 239 slice"),
        seg(np.array([not r["reserved"] for r in rows]), "not re-served (served vector as is)"),
    ) if s]
    # station-day equal weighting (the research's) vs cell weighting (the gate's)
    sd_means = collections.defaultdict(list)
    for i, c in enumerate(clusters):
        sd_means[c].append(dd[i])
    sd = np.array([np.mean(v) for v in sd_means.values()])
    res["station_day_weighted_w0.2"] = {"mean": float(sd.mean()), "station_days": int(len(sd))}
    # the replay study on the same station-days
    replay = {}
    for path in sorted((ROOT / "docs/research/downstream_after_247_20261002/pass1_down").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as h:
            for line in h:
                r = json.loads(line); b = r["base"]
                if (b.get("seated") or 9) <= 3 and (b.get("hours_to_settlement") or 0) > 36 and b.get("cw_active"):
                    replay.setdefault((r["station"], r["event_date"]), []).append(r)
    overlap = sorted(set(sd_means) & set(replay))
    res["overlap_with_replay_station_days"] = len(overlap)
    (HERE / "results_root_cause.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.4f [%+.4f,%+.4f]" % tuple(v)
    print(f"cells {n}  station-days {res['station_days']}  days {res['days']}")
    print("fixed weight, in-sample:", {k: f(v) for k, v in res["fixed_weight_in_sample"].items()})
    print("decomposition (w0.2):")
    for g, v in sorted(decomposition.items()):
        print(f"  {g:10s} cells {v['cells']:5d} ({v['share_cells']:.1%})  mean dLog {v['mean_dlog']:+.4f}  contributes {v['contribution_to_mean']:+.4f}  mean|d| {v['mean_abs']:.4f}  variance share {group_var[g]:.1%}")
    print("dLog per cell distribution:", {k: round(v, 4) for k, v in res["dlog_distribution_w0.2"].items()})
    th = res["tail_hits"]
    print("tail buckets: cold realised %.1f%% vs claimed %.1f%%; warm realised %.1f%% vs claimed %.1f%%" % (100 * th["cold_realised"], 100 * th["cold_claimed"], 100 * th["warm_realised"], 100 * th["warm_claimed"]))
    print("walk-forward reproduced (fold-chosen weights):", f(res["walk_forward_reproduced"]), " same held-out days at fixed 0.2:", f(res["held_out_days_fixed_w0.2"]), " fixed 0.15:", f(res["held_out_days_fixed_w0.15"]), " fold weights:", res["fold_weight_counts"])
    print("held-out fixed guardrails:", {k: f(v) for k, v in res.items() if k.startswith("held_out_days_fixed_w") and (k.endswith("_brier") or k.endswith("_crps"))})
    for s in res["deepest_saves_w0.2"]:
        print("  deep", s)
    print("without the station-days behind the top 1%:", f(res["without_top_station_days_w0.2"]))
    print("concentration:", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res["concentration_w0.2"].items()})
    print("one-sided exploratory: cold-only", f(res["one_sided_cold_w0.2_exploratory"]), " warm-only", f(res["one_sided_warm_w0.2_exploratory"]))
    for s in res["segments"]:
        print(f"  seg {s['label']:48s} cells {s['cells']:5d} sd {s['station_days']:4d}  dLog {f(s['dlog_w0.2'])}  tails cold/warm {s['cold_tail_hits']:.3f}/{s['warm_tail_hits']:.3f}")
    print("station-day weighted mean (w0.2):", round(res["station_day_weighted_w0.2"]["mean"], 4), "; cell-weighted:", round(float(dd.mean()), 4))
    print("overlap with replay station-days:", res["overlap_with_replay_station_days"])


if __name__ == "__main__":
    main()
