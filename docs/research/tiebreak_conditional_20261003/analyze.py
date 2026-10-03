#!/usr/bin/env python3
"""PROTOCOL.md's six readings, from ``data.json`` only.  Writes
``results.json`` and prints the tables."""

from __future__ import annotations

import json
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
import scan_tiebreak_loss_cells as scan  # noqa: E402

DISCOVERY_CEILING = "2026-09-20"
R4_FORWARD_FLOOR = "2026-09-24"
DRAWS = 4000
SHUFFLES = 2000
SEED = 164
V23 = "tiebreak_model_mode_market_midband_spread_maker_paid_cap_v2_3"


def roi(rows):
    return shadow._roi(rows)


def boot_days(rows, stat, *, quantiles=(0.05, 0.95), draws=DRAWS, seed=SEED):
    """Quantiles of ``stat(pool)`` resampling event days."""

    by_day = defaultdict(list)
    for row in rows:
        by_day[str(row["event_date"])].append(row)
    days = sorted(by_day)
    if len(days) < 3:
        return None
    rng = random.Random(seed)
    got = []
    for _ in range(draws):
        pool = [r for d in (rng.choice(days) for _ in days) for r in by_day[d]]
        value = stat(pool)
        if value is not None:
            got.append(value)
    got.sort()
    return [got[min(len(got) - 1, int(q * len(got)))] for q in quantiles]


def boot_contrast(a_rows, b_rows, stat, *, draws=DRAWS, seed=SEED):
    by_a, by_b = defaultdict(list), defaultdict(list)
    for row in a_rows:
        by_a[str(row["event_date"])].append(row)
    for row in b_rows:
        by_b[str(row["event_date"])].append(row)
    days = sorted(set(by_a) | set(by_b))
    if len(days) < 3:
        return None
    rng = random.Random(seed)
    got = []
    for _ in range(draws):
        picked = [rng.choice(days) for _ in days]
        a = stat([r for d in picked for r in by_a.get(d, ())])
        b = stat([r for d in picked for r in by_b.get(d, ())])
        if a is not None and b is not None:
            got.append(a - b)
    if len(got) < draws // 10:
        return None
    got.sort()
    return [got[int(0.05 * len(got))], got[int(0.95 * len(got))]]


def pct(x, digits=1):
    return "   n/a" if x is None else f"{100 * x:+.{digits}f}%"


def pts(x):
    return "  n/a" if x is None else f"{100 * x:+.1f}"


# -- R1 ---------------------------------------------------------------------

def unit_edge(t):
    return (t["payout_usdc"] - t["filled_notional_usdc"]) / t["filled_size"]


def mean_edge(rows):
    return st.mean(unit_edge(t) for t in rows) if rows else None


def ledger(rows):
    stake = sum(t["filled_notional_usdc"] for t in rows)
    shares = sum(t["filled_size"] for t in rows)
    pnl = sum(t["pnl_usdc"] for t in rows)
    days = sorted({t["event_date"] for t in rows})
    halves = None
    if len(days) >= 2:
        cut = days[len(days) // 2]
        halves = {"cut": cut,
                  "first": mean_edge([t for t in rows if t["event_date"] < cut]),
                  "second": mean_edge([t for t in rows if t["event_date"] >= cut])}
    return {
        "n": len(rows), "hits": sum(1 for t in rows if t["hit"]),
        "event_days": len(days), "stake_usdc": stake, "pnl_usdc": pnl,
        "roi": (pnl / stake) if stake else None,
        "avg_paid": (stake / shares) if shares else None,
        "hit_rate": (sum(1 for t in rows if t["hit"]) / len(rows)) if rows else None,
        "edge_per_share": mean_edge(rows),
        "edge_ci90": boot_days(rows, mean_edge) if len(rows) >= 5 else None,
        "halves": halves,
    }


def r1(data):
    filled = data["live"]["trades"]["rows"]
    settled = [t for t in filled if t["hit"] is not None]
    out = {"filled": len(filled), "open": len(filled) - len(settled),
           "all": ledger(settled), "by_version": {}, "v23_by_stage": {}, "by_day": {}}
    for version in sorted({str(t["rule_version"]) for t in settled}):
        out["by_version"][version] = ledger([t for t in settled if t["rule_version"] == version])
    v23 = [t for t in settled if t["rule_version"] == V23]
    for stage in sorted({str(t["stage"]) for t in v23}):
        out["v23_by_stage"][stage] = ledger([t for t in v23 if t["stage"] == stage])
    for day in sorted({t["event_date"] for t in settled}):
        rows = [t for t in settled if t["event_date"] == day]
        out["by_day"][day] = {"n": len(rows), "hits": sum(1 for t in rows if t["hit"]),
                              "pnl_usdc": sum(t["pnl_usdc"] for t in rows)}
    return out, settled


# -- R3 ---------------------------------------------------------------------

def r3(replay):
    cells = scan.candidate_cells()
    disc = [r for r in replay if r["event_date"] <= DISCOVERY_CEILING]
    val = [r for r in replay if r["event_date"] > DISCOVERY_CEILING]
    days = sorted({r["event_date"] for r in disc})
    cut = days[len(days) // 2]
    first = [r for r in disc if r["event_date"] < cut]
    second = [r for r in disc if r["event_date"] >= cut]
    table, selected = [], []
    for name, pred in cells:
        a, b = [r for r in first if pred(r)], [r for r in second if pred(r)]
        v = [r for r in val if pred(r)]
        rest = [r for r in val if not pred(r)]
        entry = {
            "cell": name, "n_first": len(a), "n_second": len(b),
            "roi_first": roi(a), "roi_second": roi(b), "roi_discovery": roi(a + b),
            "n_validation": len(v), "roi_validation": roi(v),
            "roi_validation_rest": roi(rest),
        }
        entry["selected"] = bool(
            len(a) >= scan.MIN_ROWS_PER_HALF and len(b) >= scan.MIN_ROWS_PER_HALF
            and entry["roi_first"] > 0 and entry["roi_second"] > 0
            and entry["roi_discovery"] >= 0.10
        )
        table.append(entry)
        if entry["selected"]:
            selected.append((name, pred))
    k = len(selected)
    readings = []
    for name, pred in selected:
        v = [r for r in val if pred(r)]
        rest = [r for r in val if not pred(r)]
        ci = boot_days(v, roi)
        lower = boot_days(v, roi, quantiles=(0.05 / k,)) if v else None
        readings.append({
            "cell": name, "n": len(v), "event_days": len({r["event_date"] for r in v}),
            "roi": roi(v), "ci90": ci,
            "lower_bound_at_0.05_over_k": lower[0] if lower else None,
            "contrast_vs_rest": (
                None if (roi(v) is None or roi(rest) is None) else roi(v) - roi(rest)
            ),
            "contrast_p_permutation": shadow.permutation_p(v, rest, draws=SHUFFLES),
        })
    # (b) the best selected cell against labels shuffled inside their day.
    observed = max((r["roi"] for r in readings if r["roi"] is not None), default=None)
    by_day = defaultdict(list)
    for row in val:
        by_day[row["event_date"]].append(row)
    truth = {id(r): r["hit"] for r in val}
    rng = random.Random(SEED)
    reached = 0
    best_draws = []
    for _ in range(SHUFFLES if selected else 0):
        for rows in by_day.values():
            hits = [r["hit"] for r in rows]
            rng.shuffle(hits)
            for row, hit in zip(rows, hits):
                row["hit"] = hit
        best = max(
            (x for x in (roi([r for r in val if pred(r)]) for _n, pred in selected)
             if x is not None), default=None,
        )
        if best is not None:
            best_draws.append(best)
            if observed is not None and best >= observed:
                reached += 1
    for row in val:
        row["hit"] = truth[id(row)]
    best_draws.sort()
    both = [e for e in table if e["roi_discovery"] is not None and e["roi_validation"] is not None
            and e["n_validation"] >= 8 and (e["n_first"] + e["n_second"]) >= 16]
    rank = None
    if len(both) >= 5:
        def ranks(values):
            order = sorted(range(len(values)), key=lambda i: values[i])
            out = [0.0] * len(values)
            for position, index in enumerate(order):
                out[index] = float(position)
            return out
        rank = st.correlation(ranks([e["roi_discovery"] for e in both]),
                              ranks([e["roi_validation"] for e in both]))
    return {
        "cells_declared": len(cells), "discovery_rows": len(disc), "validation_rows": len(val),
        "discovery_days": len(days), "discovery_split": cut,
        "validation_days": len({r["event_date"] for r in val}),
        "roi_discovery_all": roi(disc), "roi_validation_all": roi(val),
        "roi_validation_all_ci90": boot_days(val, roi),
        "selected": readings, "k": k,
        "selected_mean_validation_roi": (
            st.mean(r["roi"] for r in readings if r["roi"] is not None)
            if any(r["roi"] is not None for r in readings) else None
        ),
        "best_selected_validation_roi": observed,
        "p_best_reached_by_shuffle": (reached / len(best_draws)) if best_draws else None,
        "shuffle_best_median": best_draws[len(best_draws) // 2] if best_draws else None,
        "shuffle_best_p95": best_draws[int(0.95 * len(best_draws))] if best_draws else None,
        "rank_correlation_discovery_vs_validation": rank,
        "rank_correlation_cells": len(both),
        "table": table,
    }


# -- R4 / R5 ----------------------------------------------------------------

def join_trades(settled, evaluations):
    by_full, by_key = {}, {}
    for row in evaluations:
        by_full[(row["station_id"], row["event_date"], row["stage"], row["rule_version"])] = row
        by_key.setdefault((row["station_id"], row["event_date"], row["stage"]), row)
    out = []
    for t in settled:
        row = by_full.get((t["station_id"], t["event_date"], t["stage"], t["rule_version"]))
        row = row or by_key.get((t["station_id"], t["event_date"], t["stage"]))
        out.append(dict(t, evaluation=row))
    return out


def r4(joined):
    forward = [t for t in joined if t["event_date"] >= R4_FORWARD_FLOOR and t["evaluation"]]
    spec = [
        ("点差 ≤0.02 对 0.03",
         lambda t: (t["evaluation"]["spread_on_model_mode"] or 0) <= 0.025,
         lambda t: (t["evaluation"]["spread_on_model_mode"] or 0) > 0.025),
        ("实付 ≥0.45 对 <0.45",
         lambda t: t["ask_paid"] >= 0.45, lambda t: t["ask_paid"] < 0.45),
        ("市场第二名在热侧 对 在冷侧",
         lambda t: t["evaluation"].get("runner_up_side") == "warm",
         lambda t: t["evaluation"].get("runner_up_side") == "cold"),
        ("两众数重合 对 相邻",
         lambda t: t["evaluation"]["mode_distance"] == 0,
         lambda t: t["evaluation"]["mode_distance"] == 1),
    ]
    out = {"forward_floor": R4_FORWARD_FLOOR, "forward_trades": len(forward),
           "forward_days": len({t["event_date"] for t in forward}),
           "unjoined": sum(1 for t in joined
                           if t["event_date"] >= R4_FORWARD_FLOOR and not t["evaluation"]),
           "paid_at_or_above_0.60": sum(1 for t in forward if t["ask_paid"] >= 0.60),
           "splits": []}
    for name, good, bad in spec:
        a, b = [t for t in forward if good(t)], [t for t in forward if bad(t)]
        ea, eb = mean_edge(a), mean_edge(b)
        out["splits"].append({
            "split": name, "n_good": len(a), "n_bad": len(b),
            "hits_good": sum(1 for t in a if t["hit"]), "hits_bad": sum(1 for t in b if t["hit"]),
            "edge_good": ea, "edge_bad": eb,
            "contrast": None if (ea is None or eb is None) else ea - eb,
            "contrast_ci90": boot_contrast(a, b, mean_edge) if (a and b) else None,
        })
    return out


def r5(data, settled):
    """v2.3's orders by what the venue did with them (order events), grouped
    to the station-day bucket the lane was trying to buy."""

    rows = [r for r in data["evaluations_fired"] if r["rule_version"] == V23]
    refusals = defaultdict(int)
    for row in rows:
        refusals[str(row["refusal"] or "sent").split(":")[0]] += 1
    legs = [leg for leg in data["legs"] if leg["rule_version"] == V23]
    rested_legs = [leg for leg in legs if "POST_ACK" in leg["events"]]
    groups = defaultdict(list)
    for leg in legs:
        groups[(leg["station_id"], leg["event_date"], leg["bucket"])].append(leg)
    filled, unfilled, never_rested = [], [], 0
    for key, members in groups.items():
        rested = sorted((m for m in members if "POST_ACK" in m["events"]),
                        key=lambda m: str(m["created_at"]))
        if not rested:
            never_rested += 1
            continue
        settled_bucket = members[0]["settled_bucket"]
        if settled_bucket is None:
            continue
        shares = sum(m["filled_size"] for m in members)
        entry = {
            "event_date": key[1], "hit": int(settled_bucket == key[2]),
            "first_limit": float(rested[0]["limit_price"]), "sends": len(rested),
            "paid": (sum(m["filled_notional_usdc"] for m in members) / shares) if shares else None,
        }
        entry["best_ask"] = entry["paid"] if shares else entry["first_limit"]
        (filled if shares > 0 else unfilled).append(entry)

    def side(group):
        return {
            "n": len(group), "event_days": len({g["event_date"] for g in group}),
            "hit_rate": (sum(g["hit"] for g in group) / len(group)) if group else None,
            "avg_first_limit": st.mean(g["first_limit"] for g in group) if group else None,
            "avg_price": st.mean(g["best_ask"] for g in group) if group else None,
            "hit_minus_price": (
                st.mean(g["hit"] - g["best_ask"] for g in group) if group else None
            ),
            "avg_sends": st.mean(g["sends"] for g in group) if group else None,
        }

    return {
        "v23_fired": len(rows), "refusals": dict(sorted(refusals.items(), key=lambda kv: -kv[1])),
        "legs": len(legs), "rested_legs": len(rested_legs),
        "rested_legs_with_fill": sum(1 for leg in rested_legs if leg["filled_size"] > 0),
        "never_rested_station_day_buckets": never_rested,
        "filled": side(filled), "unfilled": side(unfilled),
        "fill_rate": (len(filled) / (len(filled) + len(unfilled))) if (filled or unfilled) else None,
        "hit_rate_contrast_ci90": boot_contrast(
            filled, unfilled, lambda g: (sum(x["hit"] for x in g) / len(g)) if g else None,
        ) if (filled and unfilled) else None,
    }


# -- R6 ---------------------------------------------------------------------

def r6(replay):
    from datetime import date, timedelta

    by_day = defaultdict(list)
    for row in replay:
        by_day[row["event_date"]].append(row)
    edge = {d: st.mean(r["hit"] - r["best_ask"] for r in rows) for d, rows in by_day.items()}
    lag = lambda d: (date.fromisoformat(d) - timedelta(days=2)).isoformat()  # noqa: E731
    pairs = [(edge[lag(d)], edge[d]) for d in sorted(edge) if lag(d) in edge]
    corr = st.correlation([a for a, _b in pairs], [b for _a, b in pairs]) if len(pairs) >= 5 else None
    val_days = [d for d in sorted(edge) if d > DISCOVERY_CEILING]
    gated_days = [d for d in val_days if lag(d) in edge and edge[lag(d)] > 0]
    closed_days = [d for d in val_days if lag(d) in edge and edge[lag(d)] <= 0]
    gated = [r for d in gated_days for r in by_day[d]]
    closed = [r for d in closed_days for r in by_day[d]]
    everything = [r for d in val_days for r in by_day[d]]
    def lag_corr(k, keep=lambda d: True):
        back = lambda d: (date.fromisoformat(d) - timedelta(days=k)).isoformat()  # noqa: E731
        got = [(edge[back(d)], edge[d]) for d in sorted(edge) if back(d) in edge and keep(d)]
        if len(got) < 5:
            return None, len(got)
        return st.correlation([a for a, _b in got], [b for _a, b in got]), len(got)

    return {
        "post_hoc_lag_correlations": {str(k): lag_corr(k) for k in (1, 2, 3, 4, 5)},
        "post_hoc_lag2_discovery": lag_corr(2, lambda d: d <= DISCOVERY_CEILING),
        "post_hoc_lag2_validation": lag_corr(2, lambda d: d > DISCOVERY_CEILING),
        "days": len(edge), "pairs": len(pairs), "lag2_correlation": corr,
        "validation_days": len(val_days),
        "gated_days": len(gated_days), "gated_rows": len(gated), "gated_roi": roi(gated),
        "gated_ci90": boot_days(gated, roi),
        "closed_days": len(closed_days), "closed_rows": len(closed), "closed_roi": roi(closed),
        "ungated_roi": roi(everything), "ungated_ci90": boot_days(everything, roi),
        "daily_edge": {d: edge[d] for d in sorted(edge)},
    }


def main() -> int:
    data = json.loads((HERE / "data.json").read_text())
    replay = data["replay"]
    result = {"schema": "tiebreak_conditional_results_v1",
              "captured_at_utc": data["captured_at_utc"], "overlap": data["overlap"],
              "relabelled_rows": data["relabelled_rows"]}

    one, settled = r1(data)
    result["R1"] = one
    print("== R1 实盘结账")
    print(f"成交 {one['filled']} 笔，未结算 {one['open']} 笔")

    def line(label, x):
        ci = x["edge_ci90"]
        halves = x["halves"] or {}
        print(f"  {label:<26} n={x['n']:3d} 日={x['event_days']:2d} 命中={pct(x['hit_rate'])}"
              f" 均价={x['avg_paid']:.3f} 盈亏={x['pnl_usdc']:+7.2f}U ROI={pct(x['roi'])}"
              f" 每份={pts(x['edge_per_share'])}点"
              f" [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]"
              f" 前后半={pts(halves.get('first'))}/{pts(halves.get('second'))}")
    line("全版本", one["all"])
    for version, x in one["by_version"].items():
        line(version.replace("tiebreak_model_mode_market_midband_", ""), x)
    for stage, x in one["v23_by_stage"].items():
        line(f"v2.3 {stage}", x)
    print("  按日:", "  ".join(f"{d[5:]}:{v['hits']}/{v['n']}({v['pnl_usdc']:+.1f})"
                              for d, v in one["by_day"].items()))

    print("\n== R2 已登记假设（前向＝事件日 > 2026-09-20）")
    readings = shadow.hypothesis_readings(replay)
    result["R2"] = readings
    for r in readings:
        if r["reading_window"] != "forward":
            continue
        print(f"  {r['hypothesis_id']:<34} n={r['n_treatment']:3d}/{r['n_control']:3d}"
              f" 日={r['event_days']:2d} 处理={pct(r['roi_treatment'])} 对照={pct(r['roi_control'])}"
              f" 读数={pts(r['contrast'])} [{pts(r['ci_lo'])},{pts(r['ci_hi'])}]"
              f" P(≤0)={r['p_at_or_below_zero']} 置换p={r['p_permutation']}")

    three = r3(replay)
    result["R3"] = three
    print(f"\n== R3 机械选格：{three['cells_declared']} 格，发现期 {three['discovery_rows']} 行/"
          f"{three['discovery_days']} 日，验证期 {three['validation_rows']} 行/"
          f"{three['validation_days']} 日")
    ci = three["roi_validation_all_ci90"]
    print(f"  全体触发行 发现期 {pct(three['roi_discovery_all'])} → 验证期"
          f" {pct(three['roi_validation_all'])} [{pct(ci[0])},{pct(ci[1])}]")
    print(f"  入选 {three['k']} 格：")
    for r in three["selected"]:
        ci = r["ci90"]
        disc = next(e for e in three["table"] if e["cell"] == r["cell"])
        print(f"   {r['cell']:<38} 发现 {pct(disc['roi_discovery'])}"
              f"({disc['n_first'] + disc['n_second']})"
              f" → 验证 {pct(r['roi'])}(n={r['n']},日={r['event_days']})"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
              f" 0.05/k下界={pct(r['lower_bound_at_0.05_over_k'])}"
              f" 对补集={pts(r['contrast_vs_rest'])} p={r['contrast_p_permutation']}")
    print(f"  入选格验证期平均 {pct(three['selected_mean_validation_roi'])}；最好一格"
          f" {pct(three['best_selected_validation_roi'])}，日内洗牌达到它的比例"
          f" {three['p_best_reached_by_shuffle']}（洗牌最好格中位"
          f" {pct(three['shuffle_best_median'])}，95 分位 {pct(three['shuffle_best_p95'])}）")
    print(f"  各格 发现期ROI 与 验证期ROI 的秩相关 {three['rank_correlation_discovery_vs_validation']}"
          f"（{three['rank_correlation_cells']} 格）")

    joined = join_trades(settled, data["evaluations_fired"])
    four = r4(joined)
    result["R4"] = four
    print(f"\n== R4 09-23 实盘分组的前向（事件日 ≥ {R4_FORWARD_FLOOR}，"
          f"{four['forward_trades']} 笔/{four['forward_days']} 日，未对上评估行 {four['unjoined']}）")
    for s in four["splits"]:
        ci = s["contrast_ci90"]
        print(f"  {s['split']:<22} 好侧 {s['hits_good']}/{s['n_good']} {pts(s['edge_good'])}点"
              f" | 坏侧 {s['hits_bad']}/{s['n_bad']} {pts(s['edge_bad'])}点"
              f" | 差 {pts(s['contrast'])} [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]")

    five = r5(data, settled)
    result["R5"] = five
    print(f"\n== R5 v2.3 执行：触发行 {five['v23_fired']}，去向 {five['refusals']}")
    print(f"  订单 {five['legs']} 张，真正挂上 {five['rested_legs']} 张，其中有成交"
          f" {five['rested_legs_with_fill']} 张；从未挂上的站日桶 {five['never_rested_station_day_buckets']}")
    for label, name in (("filled", "成交的站日桶"), ("unfilled", "挂过没成交的")):
        x = five[label]
        print(f"  {name} n={x['n']:3d} 日={x['event_days']:2d} 命中={pct(x['hit_rate'])}"
              f" 首挂价={x['avg_first_limit']:.3f} 计价={x['avg_price']:.3f}"
              f" 命中−计价={pts(x['hit_minus_price'])}点 平均挂 {x['avg_sends']:.1f} 次")
    ci = five["hit_rate_contrast_ci90"]
    print(f"  站日桶成交率 {pct(five['fill_rate'])}；命中率差（成交−未成交）区间"
          f" [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]")

    six = r6(replay)
    result["R6"] = six
    print(f"\n== R6 日级延续：{six['days']} 日，滞后 2 天相关 {six['lag2_correlation']}"
          f"（{six['pairs']} 对）")
    gci, uci = six["gated_ci90"], six["ungated_ci90"]
    print(f"  验证期 {six['validation_days']} 日：过闸 {six['gated_days']} 日/{six['gated_rows']} 行"
          f" {pct(six['gated_roi'])} [{pct(gci[0]) if gci else 'n/a'},{pct(gci[1]) if gci else 'n/a'}]"
          f" | 被挡 {six['closed_days']} 日/{six['closed_rows']} 行 {pct(six['closed_roi'])}"
          f" | 不设闸 {pct(six['ungated_roi'])} [{pct(uci[0])},{pct(uci[1])}]")

    print(f"  （协议外，仅说明反号）各滞后相关 {six['post_hoc_lag_correlations']}；"
          f"滞后 2 天 发现期 {six['post_hoc_lag2_discovery']} / 验证期 {six['post_hoc_lag2_validation']}")
    for r in readings:
        if r["reading_window"] == "all" and r["hypothesis_id"] == "cell_itself_v1":
            print(f"\n== 格子本身全期（站日粒度）n={r['n_treatment']} 日={r['event_days']}"
                  f" {pct(r['roi_treatment'])} [{pct(r['ci_lo'])},{pct(r['ci_hi'])}]")

    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
