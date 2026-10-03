#!/usr/bin/env python3
"""PROTOCOL_4.md: why the model's side won the disagreements before 09-21
and the market's side after.  Reads ``data5.json`` and ``data4.json``;
writes ``results5.json``."""

from __future__ import annotations

import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

CUT = "2026-09-20"
FLOOR_P = 1e-3
DRAWS = 3000
SEED = 164
STAGES = ("P0", "P0-M", "P1")
TAU_BINS = ((None, 12), (12, 24), (24, 36), (36, 48), (48, None))


def width(row):
    return 1.0 if row["unit"] == "C" else 10.0 / 9.0


def mean(values):
    values = list(values)
    return st.mean(values) if values else None


STATS = {
    "model_bias": lambda g: mean((r["model_mu"] - r["truth"]) * width(r) for r in g),
    "market_bias": lambda g: mean((r["market_mu"] - r["truth"]) * width(r) for r in g),
    "model_mae": lambda g: mean(abs(r["model_mu"] - r["truth"]) * width(r) for r in g),
    "market_mae": lambda g: mean(abs(r["market_mu"] - r["truth"]) * width(r) for r in g),
    "mae_gap": lambda g: mean((abs(r["model_mu"] - r["truth"]) - abs(r["market_mu"] - r["truth"])) * width(r) for r in g),
    "model_mode_hit": lambda g: mean(float(r["model_mode"] == r["truth"]) for r in g),
    "market_mode_hit": lambda g: mean(float(r["market_mode"] == r["truth"]) for r in g),
    "model_logp": lambda g: mean(math.log(max(r["p_model_truth"], FLOOR_P)) for r in g),
    "market_logp": lambda g: mean(math.log(max(r["p_market_truth"], FLOOR_P)) for r in g),
    "logp_gap": lambda g: mean(math.log(max(r["p_model_truth"], FLOOR_P)) - math.log(max(r["p_market_truth"], FLOOR_P)) for r in g),
    "centre_gap": lambda g: mean((r["model_mu"] - r["market_mu"]) * width(r) for r in g),
    "centre_abs_gap": lambda g: mean(abs(r["model_mu"] - r["market_mu"]) * width(r) for r in g),
    "disagree_share": lambda g: mean(float(r["model_mode"] != r["market_mode"]) for r in g),
}


def boot(rows, stat, *, seed=SEED, draws=DRAWS):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row["event_date"]].append(row)
    days = sorted(by_day)
    if len(days) < 3:
        return []
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        value = stat([r for d in (rng.choice(days) for _ in days) for r in by_day[d]])
        if value is not None:
            out.append(value)
    return out


def interval(draws):
    if len(draws) < 100:
        return None
    draws = sorted(draws)
    return [draws[int(0.05 * len(draws))], draws[int(0.95 * len(draws))]]


def period_change(early, late, stat):
    """late minus early, the two periods resampled independently by day."""

    a, b = boot(early, stat, seed=SEED), boot(late, stat, seed=SEED + 1)
    n = min(len(a), len(b))
    point_a, point_b = stat(early), stat(late)
    return {
        "early": point_a, "late": point_b,
        "change": None if (point_a is None or point_b is None) else point_b - point_a,
        "ci90": interval([b[i] - a[i] for i in range(n)]),
    }


def f(x, digits=3, sign=True):
    if x is None:
        return "  n/a"
    return f"{x:+.{digits}f}" if sign else f"{x:.{digits}f}"


def ci(x, digits=3):
    return "[n/a]" if not x else f"[{x[0]:+.{digits}f},{x[1]:+.{digits}f}]"


def who_won(rows, *, a="model_mode", b="market_mode"):
    n = len(rows)
    if not n:
        return {"n": 0}
    model = sum(1 for r in rows if r[a] == r["truth"])
    market = sum(1 for r in rows if r[b] == r["truth"])
    return {"n": n, "model": model / n, "market": market / n, "neither": 1 - (model + market) / n,
            "model_minus_market": (model - market) / n}


def main() -> int:
    rows = json.loads((HERE / "data5.json").read_text())["rows"]
    fired = {
        (x["station_id"], x["event_date"], x["stage"])
        for x in json.loads((HERE / "data4.json").read_text())["first_decisions"]
        if x["market_mean"]["fired"] and x["market_mean"]["best_ask"] and not x["market_mean"]["is_model_mode"]
    }
    for r in rows:
        r["phenomenon"] = (r["station_id"], r["event_date"], r["stage"]) in fired
    early = [r for r in rows if r["event_date"] <= CUT]
    late = [r for r in rows if r["event_date"] > CUT]
    result: dict = {"schema": "tiebreak_conditional_results5_v1", "rows": len(rows),
                    "early": len(early), "late": len(late)}
    print(f"全部已结算首决策 {len(rows)}：前段 {len(early)} 行/{len({r['event_date'] for r in early})} 日，"
          f"近段 {len(late)} 行/{len({r['event_date'] for r in late})} 日")

    # -- 1: skill on everything ----------------------------------------------
    print("\n== 1 全体技能（°C；对数概率下限 1e-3）  前段 → 近段，变化与 90% 区间")
    result["skill"] = {}
    for stage in ("全部",) + STAGES:
        a = [r for r in early if stage == "全部" or r["stage"] == stage]
        b = [r for r in late if stage == "全部" or r["stage"] == stage]
        block = {name: period_change(a, b, STATS[name]) for name in STATS}
        block["n_early"], block["n_late"] = len(a), len(b)
        result["skill"][stage] = block
        print(f"  [{stage}] 前段 {len(a)} 行 / 近段 {len(b)} 行")
        for name, label in (
            ("model_bias", "模型均值偏差"), ("market_bias", "市场均值偏差"),
            ("model_mae", "模型均值 MAE"), ("market_mae", "市场均值 MAE"),
            ("mae_gap", "MAE 模型−市场"),
            ("model_mode_hit", "模型众数命中"), ("market_mode_hit", "市场众数命中"),
            ("model_logp", "模型 对数概率"), ("market_logp", "市场 对数概率"),
            ("logp_gap", "对数概率 模型−市场"),
            ("centre_gap", "模型中心−市场中心"), ("centre_abs_gap", "两中心相距"),
            ("disagree_share", "两众数不同的比例"),
        ):
            x = block[name]
            print(f"     {label:<12} {f(x['early'])} → {f(x['late'])}  变化 {f(x['change'])} {ci(x['ci90'])}")

    # -- 2: the disagreements -------------------------------------------------
    print("\n== 2 两众数不同的行：真值落在谁的众数桶")
    result["disagreements"] = {}
    for label, keep in (("全部", lambda r: True), ("模型比市场暖", lambda r: r["model_mode"] > r["market_mode"]),
                        ("模型比市场冷", lambda r: r["model_mode"] < r["market_mode"])):
        a = [r for r in early if r["model_mode"] != r["market_mode"] and keep(r)]
        b = [r for r in late if r["model_mode"] != r["market_mode"] and keep(r)]
        wa, wb = who_won(a), who_won(b)
        change = period_change(a, b, lambda g: who_won(g).get("model_minus_market"))
        result["disagreements"][label] = {"early": wa, "late": wb, "change": change}
        print(f"  {label:<8} 前段 n={wa['n']:4d} 模型 {f(wa.get('model'), 3, False)} 市场 {f(wa.get('market'), 3, False)}"
              f" 都不是 {f(wa.get('neither'), 3, False)} | 近段 n={wb['n']:4d} 模型 {f(wb.get('model'), 3, False)}"
              f" 市场 {f(wb.get('market'), 3, False)} 都不是 {f(wb.get('neither'), 3, False)}"
              f" | 「模型对−市场对」变化 {f(change['change'])} {ci(change['ci90'])}")

    # -- 3: the rows the phenomenon lives on ----------------------------------
    print("\n== 3 现象所在的行（市场均值桶在价带内、且不是模型众数桶）")
    result["phenomenon"] = {}
    for label, pool in (("前段", early), ("近段", late)):
        g = [r for r in pool if r["phenomenon"]]
        n = len(g)
        on_market = sum(1 for r in g if r["truth"] == r["market_mean"])
        on_model = sum(1 for r in g if r["truth"] == r["model_mode"])
        model_side = sum(
            1 for r in g if r["truth"] != r["market_mean"] and r["truth"] != r["model_mode"]
            and (r["truth"] > r["market_mean"]) == (r["model_mode"] > r["market_mean"])
        )
        other = n - on_market - on_model - model_side
        entry = {"n": n, "market_mean": on_market / n, "model_mode": on_model / n,
                 "beyond_on_model_side": model_side / n, "other_side": other / n,
                 "avg_market_share": mean(r["p_market_on_market_mean"] for r in g),
                 "model_p_on_its_mode": mean(r["p_model_on_model_mode"] for r in g),
                 "model_mode_distance": mean(abs(r["model_mode"] - r["market_mean"]) for r in g),
                 "stage_mix": {s: sum(1 for r in g if r["stage"] == s) / n for s in STAGES},
                 "model_warmer": sum(1 for r in g if r["model_mode"] > r["market_mean"]) / n}
        result["phenomenon"][label] = entry
        print(f"  {label} n={n}: 落市场均值桶 {entry['market_mean']:.3f} | 落模型众数桶 {entry['model_mode']:.3f}"
              f" | 更远的模型那侧 {entry['beyond_on_model_side']:.3f} | 市场另一侧 {entry['other_side']:.3f}"
              f" | 阶段 {({k: round(v, 2) for k, v in entry['stage_mix'].items()})}"
              f" | 模型在暖侧 {entry['model_warmer']:.2f} | 相隔 {entry['model_mode_distance']:.2f} 档")

    # -- 4: by week -----------------------------------------------------------
    print("\n== 4 按周（ISO 周）")
    result["weekly"] = []
    by_week = defaultdict(list)
    for r in rows:
        iso = date.fromisoformat(r["event_date"]).isocalendar()
        by_week[f"{iso[0]}-W{iso[1]:02d}"].append(r)
    for week in sorted(by_week):
        g = by_week[week]
        dis = [r for r in g if r["model_mode"] != r["market_mode"]]
        ph = [r for r in g if r["phenomenon"]]
        entry = {
            "week": week, "from": min(r["event_date"] for r in g), "to": max(r["event_date"] for r in g),
            "n": len(g), "logp_gap": STATS["logp_gap"](g), "mae_gap": STATS["mae_gap"](g),
            "model_mae": STATS["model_mae"](g), "market_mae": STATS["market_mae"](g),
            "model_bias": STATS["model_bias"](g), "market_bias": STATS["market_bias"](g),
            "disagreements": len(dis), "model_minus_market": who_won(dis).get("model_minus_market"),
            "phenomenon_n": len(ph),
            "phenomenon_market_hit": mean(float(r["truth"] == r["market_mean"]) for r in ph),
        }
        result["weekly"].append(entry)
        print(f"  {week} {entry['from'][5:]}..{entry['to'][5:]} n={entry['n']:4d}"
              f" 对数差 {f(entry['logp_gap'])} MAE 模型 {f(entry['model_mae'], 2, False)} 市场"
              f" {f(entry['market_mae'], 2, False)} 偏差 模型 {f(entry['model_bias'], 2)} 市场 {f(entry['market_bias'], 2)}"
              f" | 不一致 {entry['disagreements']:3d} 模型对−市场对 {f(entry['model_minus_market'], 2)}"
              f" | 现象行 {entry['phenomenon_n']:3d} 市场均值桶命中 {f(entry['phenomenon_market_hit'], 2, False)}")

    # -- 5: by posterior version ---------------------------------------------
    print("\n== 5 按 posterior_version")
    result["versions"] = []
    by_version = defaultdict(list)
    for r in rows:
        by_version[(r["stage"], r["posterior_version"])].append(r)
    for (stage, version), g in sorted(by_version.items(), key=lambda kv: (kv[0][0], min(r["event_date"] for r in kv[1]))):
        dis = [r for r in g if r["model_mode"] != r["market_mode"]]
        entry = {
            "stage": stage, "posterior_version": version, "n": len(g),
            "from": min(r["event_date"] for r in g), "to": max(r["event_date"] for r in g),
            "model_mae": STATS["model_mae"](g), "market_mae": STATS["market_mae"](g),
            "model_bias": STATS["model_bias"](g), "logp_gap": STATS["logp_gap"](g),
            "disagreements": len(dis), "model_minus_market": who_won(dis).get("model_minus_market"),
        }
        result["versions"].append(entry)
        if len(g) >= 20:
            print(f"  {stage:<5} {version[-28:]:<28} {entry['from'][5:]}..{entry['to'][5:]} n={entry['n']:4d}"
                  f" MAE 模型 {f(entry['model_mae'], 2, False)} 市场 {f(entry['market_mae'], 2, False)}"
                  f" 模型偏差 {f(entry['model_bias'], 2)} 对数差 {f(entry['logp_gap'])}"
                  f" 不一致 {entry['disagreements']:3d} 模型对−市场对 {f(entry['model_minus_market'], 2)}")

    # -- 6: timing, and the aligned contrast ----------------------------------
    print("\n== 6 首决策距结算小时（中位）与对齐后的不一致行对比")
    result["timing"] = {}
    for stage in STAGES:
        a = sorted(r["tau"] for r in early if r["stage"] == stage)
        b = sorted(r["tau"] for r in late if r["stage"] == stage)
        result["timing"][stage] = {"early_median": a[len(a) // 2], "late_median": b[len(b) // 2]}
        print(f"  {stage:<5} 前段 {a[len(a) // 2]:5.1f}h → 近段 {b[len(b) // 2]:5.1f}h")

    def stratum(r):
        for low, high in TAU_BINS:
            if (low is None or r["tau"] >= low) and (high is None or r["tau"] < high):
                return (r["stage"], low)
        return (r["stage"], None)

    dis_early = [r for r in early if r["model_mode"] != r["market_mode"]]
    dis_late = [r for r in late if r["model_mode"] != r["market_mode"]]
    cells = defaultdict(lambda: [[], []])
    for r in dis_early:
        cells[stratum(r)][0].append(r)
    for r in dis_late:
        cells[stratum(r)][1].append(r)
    weight_total, aligned_early, aligned_late = 0, 0.0, 0.0
    for key, (a, b) in cells.items():
        if len(a) < 5 or len(b) < 5:
            continue
        w = len(b)
        weight_total += w
        aligned_early += w * who_won(a)["model_minus_market"]
        aligned_late += w * who_won(b)["model_minus_market"]
    raw = (who_won(dis_early)["model_minus_market"], who_won(dis_late)["model_minus_market"])
    result["aligned"] = {
        "raw_early": raw[0], "raw_late": raw[1],
        "aligned_early": aligned_early / weight_total, "aligned_late": aligned_late / weight_total,
        "rows_covered_late": weight_total, "rows_late": len(dis_late),
    }
    print(f"  不一致行「模型对−市场对」：原样 前段 {f(raw[0])} → 近段 {f(raw[1])}；"
          f"按阶段×小时档对齐（近段权重，覆盖近段 {weight_total}/{len(dis_late)} 行）"
          f" 前段 {f(aligned_early / weight_total)} → 近段 {f(aligned_late / weight_total)}")
    (HERE / "results5.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
