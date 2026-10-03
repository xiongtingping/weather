#!/usr/bin/env python3
"""PROTOCOL_2.md: per-trade attribution, the station dimension, and the
maker fills' timing.  Reads ``data.json`` and ``data2.json`` only; writes
``results2.json`` and ``trades.csv``."""

from __future__ import annotations

import csv
import json
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
from analyze import boot_contrast, boot_days, pct, pts  # noqa: E402

DISCOVERY_CEILING = "2026-09-20"
CALM = 0.15
WALK_FLOOR = "2026-09-01"
WALK_LAG_DAYS = 3
WALK_MIN_ROWS = 8
SHUFFLES = 2000
SEED = 164


def roi(rows):
    return shadow._roi(rows)


def region_of(zone: str | None) -> str:
    head = str(zone or "").split("/")[0]
    return head if head in ("America", "Europe", "Asia") else "其他"


def eta_squared(rows, group, value):
    values = [value(r) for r in rows]
    grand = st.mean(values)
    total = sum((v - grand) ** 2 for v in values)
    by = defaultdict(list)
    for row, v in zip(rows, values):
        by[group(row)].append(v)
    between = sum(len(vs) * (st.mean(vs) - grand) ** 2 for vs in by.values())
    return between / total if total else None


def eta_test(rows, group, value, *, within_day: bool, draws=SHUFFLES, seed=SEED):
    """Observed eta squared and where it sits among shuffles of the values --
    inside the event day (which holds each day's own difficulty fixed) or
    across the whole sample (the test for the day itself)."""

    observed = eta_squared(rows, group, value)
    rng = random.Random(seed)
    values = [value(r) for r in rows]
    groups = [group(r) for r in rows]
    days = [r["event_date"] for r in rows]
    index_by_day = defaultdict(list)
    for i, d in enumerate(days):
        index_by_day[d].append(i)
    grand = st.mean(values)
    total = sum((v - grand) ** 2 for v in values)
    null = []
    for _ in range(draws):
        shuffled = values[:]
        if within_day:
            for idx in index_by_day.values():
                picked = [values[i] for i in idx]
                rng.shuffle(picked)
                for i, v in zip(idx, picked):
                    shuffled[i] = v
        else:
            rng.shuffle(shuffled)
        by = defaultdict(list)
        for g, v in zip(groups, shuffled):
            by[g].append(v)
        null.append(sum(len(vs) * (st.mean(vs) - grand) ** 2 for vs in by.values()) / total)
    null.sort()
    return {
        "eta2": observed, "groups": len(set(groups)),
        "null_mean": st.mean(null), "null_p95": null[int(0.95 * len(null))],
        "p": sum(1 for x in null if x >= observed) / len(null),
    }


def spearman(xs, ys):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for position, index in enumerate(order):
            out[index] = float(position)
        return out
    return st.correlation(ranks(xs), ranks(ys)) if len(xs) >= 5 else None


def unit_edge(t):
    return (t["payout_usdc"] - t["filled_notional_usdc"]) / t["filled_size"]


def main() -> int:
    data = json.loads((HERE / "data.json").read_text())
    extra = json.loads((HERE / "data2.json").read_text())
    zones = extra["zones"]
    replay = [dict(r, resid=r["hit"] - r["best_ask"], region=region_of(zones.get(r["station_id"])))
              for r in data["replay"]]
    trades = data["live"]["trades"]["rows"]
    settled = [t for t in trades if t["hit"] is not None]
    for t in trades:
        t.update(extra["trade_extra"].get(str(t["order_intent_id"]), {}))
        t["region"] = region_of(zones.get(t["station_id"]))
    result: dict = {"schema": "tiebreak_conditional_results2_v1"}

    # -- A: the fleet's daily shift ------------------------------------------
    by_day_off = defaultdict(list)
    for row in extra["first_decision_offsets"]:
        by_day_off[row["event_date"]].append(row["offset"])
    shift = {d: st.mean(v) for d, v in by_day_off.items()}

    def day_class(d):
        b = shift.get(d)
        if b is None:
            return "未知"
        return "平日" if abs(b) < CALM else ("全队偏冷日" if b < 0 else "全队偏热日")

    classes = ("平日", "全队偏冷日", "全队偏热日")
    live_by_class, replay_by_class = {}, {}
    for name in classes:
        rows = [t for t in settled if day_class(t["event_date"]) == name]
        live_by_class[name] = {
            "days": len({t["event_date"] for t in rows}), "n": len(rows),
            "hits": sum(1 for t in rows if t["hit"]),
            "pnl_usdc": sum(t["pnl_usdc"] for t in rows),
            "stake_usdc": sum(t["filled_notional_usdc"] for t in rows),
        }
        rows = [r for r in replay if day_class(r["event_date"]) == name]
        replay_by_class[name] = {
            "days": len({r["event_date"] for r in rows}), "n": len(rows),
            "hit_rate": (sum(r["hit"] for r in rows) / len(rows)) if rows else None,
            "avg_ask": st.mean(r["best_ask"] for r in rows) if rows else None,
            "roi": roi(rows), "ci90": boot_days(rows, roi),
        }
    tags = defaultdict(lambda: {"n": 0, "pnl_usdc": 0.0})
    for t in settled:
        if t["hit"]:
            t["miss_tag"] = ""
            continue
        b = shift.get(t["event_date"])
        same = (
            b is not None and abs(b) >= CALM and t.get("offset") is not None
            and (t["offset"] < 0) == (b < 0)
        )
        t["miss_tag"] = "天气日" if same else "单站"
        tags[t["miss_tag"]]["n"] += 1
        tags[t["miss_tag"]]["pnl_usdc"] += t["pnl_usdc"]
    offsets = defaultdict(int)
    for t in settled:
        if not t["hit"] and t.get("offset") is not None:
            offsets[t["offset"]] += 1
    variance = {
        "replay": {
            "事件日": eta_test(replay, lambda r: r["event_date"], lambda r: r["resid"], within_day=False),
            "站点": eta_test(replay, lambda r: r["station_id"], lambda r: r["resid"], within_day=True),
            "阶段": eta_test(replay, lambda r: r["stage"], lambda r: r["resid"], within_day=True),
        },
        "live": {
            "事件日": eta_test(settled, lambda t: t["event_date"], unit_edge, within_day=False),
            "站点": eta_test(settled, lambda t: t["station_id"], unit_edge, within_day=True),
            "阶段": eta_test(settled, lambda t: t["stage"], unit_edge, within_day=True),
        },
    }
    result["A"] = {
        "daily_shift": {d: shift[d] for d in sorted(shift)},
        "live_by_day_class": live_by_class, "replay_by_day_class": replay_by_class,
        "miss_tags": dict(tags), "miss_offsets": dict(sorted(offsets.items())),
        "variance": variance,
    }
    print("== A 逐笔归因")
    for name in classes:
        x, y = live_by_class[name], replay_by_class[name]
        ci = y["ci90"]
        print(f"  {name:<6} 实盘 {x['days']:2d} 日 {x['hits']:2d}/{x['n']:3d} 盈亏 {x['pnl_usdc']:+7.2f}U"
              f" | 回放 {y['days']:2d} 日 {y['n']:3d} 行 命中 {pct(y['hit_rate'])}@"
              f"{(y['avg_ask'] or 0):.3f} ROI {pct(y['roi'])}"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]")
    print(f"  落空差几档 {dict(sorted(offsets.items()))}；去向 "
          + "，".join(f"{k} {v['n']} 笔 {v['pnl_usdc']:+.2f}U" for k, v in tags.items()))
    for pool in ("replay", "live"):
        print(f"  方差分摊（{pool}）: " + "；".join(
            f"{k} η²={v['eta2']:.3f}（零分布均值 {v['null_mean']:.3f}，p={v['p']:.3f}，{v['groups']} 组）"
            for k, v in variance[pool].items()))

    # -- S: the station dimension --------------------------------------------
    disc = [r for r in replay if r["event_date"] <= DISCOVERY_CEILING]
    val = [r for r in replay if r["event_date"] > DISCOVERY_CEILING]
    s1 = variance["replay"]["站点"]
    table = []
    for station in sorted({r["station_id"] for r in replay}):
        a = [r for r in disc if r["station_id"] == station]
        b = [r for r in val if r["station_id"] == station]
        live = [t for t in settled if t["station_id"] == station]
        table.append({
            "station_id": station, "region": region_of(zones.get(station)),
            "n": len(a) + len(b), "edge": st.mean(r["resid"] for r in a + b),
            "roi": roi(a + b),
            "n_discovery": len(a), "edge_discovery": st.mean(r["resid"] for r in a) if a else None,
            "n_validation": len(b), "edge_validation": st.mean(r["resid"] for r in b) if b else None,
            "live_n": len(live), "live_hits": sum(1 for t in live if t["hit"]),
            "live_pnl_usdc": sum(t["pnl_usdc"] for t in live),
        })
    both = [e for e in table if e["n_discovery"] >= 5 and e["n_validation"] >= 5]
    s2 = spearman([e["edge_discovery"] for e in both], [e["edge_validation"] for e in both])

    by_station_day = defaultdict(list)
    for r in replay:
        by_station_day[r["station_id"]].append(r)
    white, black, unknown = [], [], []
    for r in replay:
        if r["event_date"] < WALK_FLOOR:
            continue
        ceiling = (date.fromisoformat(r["event_date"]) - timedelta(days=WALK_LAG_DAYS)).isoformat()
        prior = [x["resid"] for x in by_station_day[r["station_id"]] if x["event_date"] <= ceiling]
        if len(prior) < WALK_MIN_ROWS:
            unknown.append(r)
        elif st.mean(prior) > 0:
            white.append(r)
        else:
            black.append(r)
    walk_all = white + black + unknown
    s3 = {
        "floor": WALK_FLOOR, "days": len({r["event_date"] for r in walk_all}),
        "white": {"n": len(white), "roi": roi(white), "ci90": boot_days(white, roi)},
        "black": {"n": len(black), "roi": roi(black), "ci90": boot_days(black, roi)},
        "unknown": {"n": len(unknown), "roi": roi(unknown)},
        "all": {"n": len(walk_all), "roi": roi(walk_all)},
        "white_minus_black": (roi(white) - roi(black)) if (white and black) else None,
        "white_minus_black_ci90": boot_contrast(white, black, roi) if (white and black) else None,
    }
    s4 = {"test": eta_test(replay, lambda r: r["region"], lambda r: r["resid"], within_day=True),
          "table": {}}
    for region in sorted({r["region"] for r in replay}):
        a = [r for r in disc if r["region"] == region]
        b = [r for r in val if r["region"] == region]
        live = [t for t in settled if t["region"] == region]
        s4["table"][region] = {
            "n_discovery": len(a), "roi_discovery": roi(a),
            "n_validation": len(b), "roi_validation": roi(b), "ci90_validation": boot_days(b, roi),
            "live_n": len(live), "live_hits": sum(1 for t in live if t["hit"]),
            "live_pnl_usdc": sum(t["pnl_usdc"] for t in live),
        }
    result["S"] = {"S1": s1, "S2": {"stations": len(both), "rank_correlation": s2},
                   "S3": s3, "S4": s4, "table": table}
    print("\n== S 站点维度")
    print(f"  S1 站点间差别：η²={s1['eta2']:.3f}，日内打乱零分布均值 {s1['null_mean']:.3f}、"
          f"95 分位 {s1['null_p95']:.3f}，p={s1['p']:.3f}（{s1['groups']} 站）")
    print(f"  S2 两段稳定性：{len(both)} 站，发现期与验证期站点边际秩相关 {s2}")
    w, b = s3["white"], s3["black"]
    ci = s3["white_minus_black_ci90"]
    print(f"  S3 前推白名单（{s3['floor']} 起 {s3['days']} 日）：白 {w['n']} 行 {pct(w['roi'])}"
          f" [{pct(w['ci90'][0])},{pct(w['ci90'][1])}] | 黑 {b['n']} 行 {pct(b['roi'])}"
          f" [{pct(b['ci90'][0])},{pct(b['ci90'][1])}] | 未知 {s3['unknown']['n']} 行"
          f" {pct(s3['unknown']['roi'])} | 全体 {pct(s3['all']['roi'])}"
          f" | 白−黑 {pts(s3['white_minus_black'])} [{pts(ci[0])},{pts(ci[1])}]")
    t4 = s4["test"]
    print(f"  S4 地区：η²={t4['eta2']:.4f}，零分布均值 {t4['null_mean']:.4f}，p={t4['p']:.3f}")
    for region, x in s4["table"].items():
        ci = x["ci90_validation"]
        print(f"     {region:<8} 发现 {pct(x['roi_discovery'])}({x['n_discovery']}) → 验证"
              f" {pct(x['roi_validation'])}({x['n_validation']})"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
              f" | 实盘 {x['live_hits']}/{x['live_n']} {x['live_pnl_usdc']:+.2f}U")
    print("  站点表（回放全期边际，前后各 8；实盘笔数/盈亏）")
    ranked = sorted(table, key=lambda e: e["edge"], reverse=True)
    for e in ranked[:8] + ranked[-8:]:
        print(f"     {e['station_id']} {e['region']:<8} n={e['n']:3d} 边际 {pts(e['edge'])}点"
              f" 发现 {pts(e['edge_discovery'])}({e['n_discovery']})"
              f" 验证 {pts(e['edge_validation'])}({e['n_validation']})"
              f" | 实盘 {e['live_hits']}/{e['live_n']} {e['live_pnl_usdc']:+.2f}U")

    # -- L: the maker fills ---------------------------------------------------
    def at(text):
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))

    groups = defaultdict(list)
    for leg in data["legs"]:
        if "_maker" in str(leg["rule_version"]):
            groups[(leg["station_id"], leg["event_date"], leg["bucket"])].append(leg)
    fills = []
    for key, members in groups.items():
        rested = sorted((m for m in members if "POST_ACK" in m["events"]),
                        key=lambda m: str(m["created_at"]))
        shares = sum(m["filled_size"] for m in members)
        settled_bucket = members[0]["settled_bucket"]
        if not rested or shares <= 0 or settled_bucket is None:
            continue
        first_filled = next((i for i, m in enumerate(rested) if m["filled_size"] > 0), None)
        if first_filled is None:
            continue
        events = extra["leg_events"].get(rested[first_filled]["order_intent_id"], [])
        posted = next((e for e in events if e["event_type"] == "POST_ACK"), None)
        matched = next((e for e in events if e["event_type"] in ("PARTIAL_FILL", "FILL", "LATE_FILL")), None)
        minutes = (
            (at(matched["observed_at"]) - at(posted["observed_at"])).total_seconds() / 60.0
            if posted and matched else None
        )
        paid = sum(m["filled_notional_usdc"] for m in members) / shares
        fills.append({
            "station_id": key[0], "event_date": key[1], "bucket": key[2],
            "hit": int(settled_bucket == key[2]), "paid": paid, "edge": int(settled_bucket == key[2]) - paid,
            "posts_before_fill": first_filled, "minutes_to_fill": minutes,
        })
    mean_edge = lambda g: st.mean(x["edge"] for x in g) if g else None  # noqa: E731

    def side(g):
        return {"n": len(g), "hits": sum(x["hit"] for x in g),
                "avg_paid": st.mean(x["paid"] for x in g) if g else None,
                "edge": mean_edge(g), "ci90": boot_days(g, mean_edge) if len(g) >= 5 else None}

    first = [x for x in fills if x["posts_before_fill"] == 0]
    later = [x for x in fills if x["posts_before_fill"] > 0]
    timed = [x for x in fills if x["minutes_to_fill"] is not None]
    bins = (("<2 分钟", lambda m: m < 2), ("2–30 分钟", lambda m: 2 <= m < 30), (">30 分钟", lambda m: m >= 30))
    result["L"] = {
        "fills": len(fills),
        "L1": {"first_post": side(first), "repost": side(later),
               "contrast": (mean_edge(first) - mean_edge(later)) if (first and later) else None,
               "contrast_ci90": boot_contrast(first, later, mean_edge) if (first and later) else None},
        "L2": {label: side([x for x in timed if test(x["minutes_to_fill"])]) for label, test in bins},
        "L2_untimed": len(fills) - len(timed),
        "L2_fast_minus_slow_ci90": boot_contrast(
            [x for x in timed if x["minutes_to_fill"] < 2],
            [x for x in timed if x["minutes_to_fill"] >= 30], mean_edge,
        ),
    }
    print(f"\n== L 挂单成交（站日桶 {len(fills)} 个）")
    one = result["L"]["L1"]
    for label, x in (("首挂即成交", one["first_post"]), ("重挂后成交", one["repost"])):
        ci = x["ci90"]
        print(f"  L1 {label} {x['hits']}/{x['n']} 实付 {x['avg_paid']:.3f} 每份 {pts(x['edge'])}点"
              f" [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]")
    ci = one["contrast_ci90"]
    print(f"     首挂 − 重挂 {pts(one['contrast'])} [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]")
    for label, x in result["L"]["L2"].items():
        ci = x["ci90"]
        print(f"  L2 {label:<8} {x['hits']}/{x['n']} 实付 {(x['avg_paid'] or 0):.3f} 每份 {pts(x['edge'])}点"
              f" [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]")
    ci = result["L"]["L2_fast_minus_slow_ci90"]
    print(f"     快(<2) − 慢(>30) 区间 [{pts(ci[0]) if ci else 'n/a'},{pts(ci[1]) if ci else 'n/a'}]；"
          f"无时刻 {result['L']['L2_untimed']} 个")

    # -- the per-trade table --------------------------------------------------
    eval_by = {}
    for row in data["evaluations_fired"]:
        eval_by.setdefault((row["station_id"], row["event_date"], row["stage"]), row)
    fill_by = {(x["station_id"], x["event_date"], x["bucket"]): x for x in fills}
    with (HERE / "trades.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "事件日", "站点", "地区", "阶段", "规则版本", "买入桶", "实付价", "股数", "投入U",
            "真值桶", "差几档", "命中", "盈亏U", "模型份额", "市场归一份额", "点差", "卖一档股数",
            "距结算小时", "市场第二名侧", "当天全队偏移(档)", "日类型", "落空去向",
            "成交前重挂次数", "挂上到成交(分钟)",
        ])
        for t in sorted(trades, key=lambda t: (t["event_date"], t["station_id"])):
            row = eval_by.get((t["station_id"], t["event_date"], t["stage"])) or {}
            fill = fill_by.get((t["station_id"], t["event_date"], t["bucket"])) or {}
            f = lambda v, n=3: "" if v is None else round(float(v), n)  # noqa: E731
            writer.writerow([
                t["event_date"], t["station_id"], t["region"], t["stage"],
                str(t["rule_version"]).rsplit("_v", 1)[-1].replace("_", "."),
                t["bucket"], f(t["ask_paid"]), f(t["filled_size"], 2), f(t["filled_notional_usdc"], 2),
                t.get("settled_bucket") or "未结算", "" if t.get("offset") is None else t["offset"],
                "" if t["hit"] is None else int(bool(t["hit"])), f(t.get("pnl_usdc"), 2),
                f(row.get("model_share_on_model_mode")), f(row.get("market_share_on_model_mode")),
                f(row.get("spread_on_model_mode")), f(row.get("ask_depth_shares"), 1),
                f(t.get("tau"), 1), row.get("runner_up_side") or "",
                f(shift.get(t["event_date"]), 2), day_class(t["event_date"]),
                t.get("miss_tag", ""), fill.get("posts_before_fill", ""),
                f(fill.get("minutes_to_fill"), 1),
            ])
    (HERE / "results2.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
