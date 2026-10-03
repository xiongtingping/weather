#!/usr/bin/env python3
"""PROTOCOL_2.md section C: the four centres (model mode / model mean /
market mode / market mean) on the tiebreak cell.  Reads ``data.json`` and
``data3.json``; writes ``results3.json`` and adds columns to ``trades.csv``."""

from __future__ import annotations

import csv
import json
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
from analyze import boot_contrast, boot_days, pct, pts  # noqa: E402

DISCOVERY_CEILING = "2026-09-20"
LEAN = 0.15
TIGHT = 0.25
MIN_ROWS_PER_HALF = 8


def roi(rows):
    return shadow._roi(rows)


def side_of(delta) -> str:
    return "同桶" if delta == 0 else ("冷侧" if delta < 0 else "暖侧")


def enrich(row, centres):
    mo = centres["model_mode"]["ordinal"]
    a = centres["model_mu"] - mo
    b = centres["market_mu"] - mo
    out = dict(row)
    out.update({
        "a": a, "b": b, "a_minus_b": a - b, "four_same": bool(centres["four_same"]),
        "model_mean_side": side_of(centres["model_mean"]["ordinal"] - mo),
        "market_mean_side": side_of(centres["market_mean"]["ordinal"] - mo),
        "market_mode_same": centres["market_mode"]["ordinal"] == mo,
        "centres": centres,
        "miss_side": (
            None if (row.get("hit") in (1, True, None) or centres.get("settled_ordinal") is None)
            else ("冷侧" if centres["settled_ordinal"] < mo else "暖侧")
        ),
    })
    return out


def condition_arms():
    return [
        ("C1 四心同桶", lambda r: r["four_same"]),
        ("C1 四心不同桶", lambda r: not r["four_same"]),
        ("C2 模型均值桶 同桶", lambda r: r["model_mean_side"] == "同桶"),
        ("C2 模型均值桶 在冷侧", lambda r: r["model_mean_side"] == "冷侧"),
        ("C2 模型均值桶 在暖侧", lambda r: r["model_mean_side"] == "暖侧"),
        ("C3 市场均值桶 同桶", lambda r: r["market_mean_side"] == "同桶"),
        ("C3 市场均值桶 在冷侧", lambda r: r["market_mean_side"] == "冷侧"),
        ("C3 市场均值桶 在暖侧", lambda r: r["market_mean_side"] == "暖侧"),
        ("C4 模型均值更暖 (a−b>+0.15)", lambda r: r["a_minus_b"] > LEAN),
        ("C4 两均值接近", lambda r: abs(r["a_minus_b"]) <= LEAN),
        ("C4 模型均值更冷 (a−b<−0.15)", lambda r: r["a_minus_b"] < -LEAN),
        ("C6 两均值都贴着买入桶", lambda r: abs(r["a"]) < TIGHT and abs(r["b"]) < TIGHT),
        ("C6 至少一个均值偏出", lambda r: not (abs(r["a"]) < TIGHT and abs(r["b"]) < TIGHT)),
    ]


def swap_rows(rows, name):
    """The rows where centre ``name`` is a different bucket from the bought
    one and is quoted: the alternative purchase, and the bought bucket on the
    same rows."""

    alt, base = [], []
    for r in rows:
        c = r["centres"][name]
        if c["ordinal"] == r["centres"]["model_mode"]["ordinal"] or not c["best_ask"]:
            continue
        alt.append({"event_date": r["event_date"], "hit": c["hit"], "best_ask": c["best_ask"]})
        base.append(r)
    return alt, base


def halves(disc):
    days = sorted({r["event_date"] for r in disc})
    cut = days[len(days) // 2]
    return [r for r in disc if r["event_date"] < cut], [r for r in disc if r["event_date"] >= cut], cut


def reading(arm_disc, arm_val, control_val, first_ids, second_ids):
    a = [r for r in arm_disc if id(r) in first_ids]
    b = [r for r in arm_disc if id(r) in second_ids]
    selected = bool(
        len(a) >= MIN_ROWS_PER_HALF and len(b) >= MIN_ROWS_PER_HALF
        and roi(a) > 0 and roi(b) > 0 and roi(arm_disc) >= 0.10
    )
    rate = lambda g: (sum(r["hit"] for r in g) / len(g)) if g else None  # noqa: E731
    price = lambda g: st.mean(r["best_ask"] for r in g) if g else None  # noqa: E731
    return {
        "n_first": len(a), "n_second": len(b), "roi_first": roi(a), "roi_second": roi(b),
        "n_discovery": len(arm_disc), "roi_discovery": roi(arm_disc),
        "hit_discovery": rate(arm_disc), "ask_discovery": price(arm_disc),
        "n_validation": len(arm_val), "roi_validation": roi(arm_val),
        "hit_validation": rate(arm_val), "ask_validation": price(arm_val),
        "ci90_validation": boot_days(arm_val, roi) if len(arm_val) >= 5 else None,
        "roi_validation_control": roi(control_val), "selected": selected,
    }


def main() -> int:
    data = json.loads((HERE / "data.json").read_text())
    centres = json.loads((HERE / "data3.json").read_text())
    rows = []
    ask_differs = 0
    for row in data["replay"]:
        c = centres["replay_centres"]["|".join((row["station_id"], row["event_date"], row["stage"]))]
        if abs((c["model_mode"]["best_ask"] or 0) - row["best_ask"]) > 1e-9:
            ask_differs += 1
        rows.append(enrich(row, c))
    disc = [r for r in rows if r["event_date"] <= DISCOVERY_CEILING]
    val = [r for r in rows if r["event_date"] > DISCOVERY_CEILING]
    first, second, cut = halves(disc)
    first_ids, second_ids = {id(r) for r in first}, {id(r) for r in second}
    result = {"schema": "tiebreak_conditional_results3_v1", "rows": len(rows),
              "ask_differs_from_replay": ask_differs, "discovery_split": cut, "arms": [], "swaps": []}

    print(f"== C 四中心（{len(rows)} 行；发现期 {len(disc)} / 验证期 {len(val)}；"
          f"卖一与回放不一致 {ask_differs} 行）")
    share = lambda pred: sum(1 for r in rows if pred(r)) / len(rows)  # noqa: E731
    print(f"  构成：四心同桶 {pct(share(lambda r: r['four_same']))}；市场众数＝买入桶"
          f" {pct(share(lambda r: r['market_mode_same']))}；模型均值桶＝买入桶"
          f" {pct(share(lambda r: r['model_mean_side'] == '同桶'))}；市场均值桶＝买入桶"
          f" {pct(share(lambda r: r['market_mean_side'] == '同桶'))}")
    for name, pred in condition_arms():
        x = reading([r for r in disc if pred(r)], [r for r in val if pred(r)],
                    [r for r in val if not pred(r)], first_ids, second_ids)
        x["arm"] = name
        result["arms"].append(x)
        ci = x["ci90_validation"]
        print(f"  {name:<26} 发现 n={x['n_discovery']:3d} {pct(x['roi_first'])}/{pct(x['roi_second'])}"
              f" 合 {pct(x['roi_discovery'])}（中 {pct(x['hit_discovery'])}@{(x['ask_discovery'] or 0):.3f}）"
              f" → 验证 n={x['n_validation']:3d} {pct(x['roi_validation'])}"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
              f"（中 {pct(x['hit_validation'])}@{(x['ask_validation'] or 0):.3f}）"
              f" 补集 {pct(x['roi_validation_control'])} {'★入选' if x['selected'] else ''}")

    print("  -- 换桶臂（换买另一个中心，对同批行照旧买模型众数桶）")
    for label, name in (("T1 改买模型均值桶", "model_mean"), ("T2 改买市场均值桶", "market_mean"),
                        ("T3 改买市场众数桶", "market_mode")):
        alt_d, base_d = swap_rows(disc, name)
        alt_v, base_v = swap_rows(val, name)
        alt_first = {id(x) for x, b in zip(alt_d, base_d) if id(b) in first_ids}
        alt_second = {id(x) for x, b in zip(alt_d, base_d) if id(b) in second_ids}
        x = reading(alt_d, alt_v, base_v, alt_first, alt_second)
        x.update({"arm": label, "roi_discovery_bought": roi(base_d), "roi_validation_bought": roi(base_v),
                  "contrast_validation_ci90": boot_contrast(alt_v, base_v, roi) if alt_v else None})
        result["swaps"].append(x)
        ci = x["ci90_validation"]
        print(f"  {label:<14} 发现 n={x['n_discovery']:3d} {pct(x['roi_first'])}/{pct(x['roi_second'])}"
              f" 合 {pct(x['roi_discovery'])}（中 {pct(x['hit_discovery'])}@{(x['ask_discovery'] or 0):.3f}；"
              f"同批买众数桶 {pct(x['roi_discovery_bought'])}）"
              f" → 验证 n={x['n_validation']:3d} {pct(x['roi_validation'])}"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
              f"（中 {pct(x['hit_validation'])}@{(x['ask_validation'] or 0):.3f}；"
              f"同批买众数桶 {pct(x['roi_validation_bought'])}） {'★入选' if x['selected'] else ''}")

    picked = [x for x in result["arms"] + result["swaps"] if x["selected"]]
    k = len(picked)
    result["k"] = k
    print(f"  入选 {k} 个臂" + ("：" if k else "。"))
    for x in picked:
        name = x["arm"]
        pred = dict(condition_arms()).get(name)
        if pred is not None:
            arm_val = [r for r in val if pred(r)]
        else:
            centre = {"T1 改买模型均值桶": "model_mean", "T2 改买市场均值桶": "market_mean",
                      "T3 改买市场众数桶": "market_mode"}[name]
            arm_val, _base = swap_rows(val, centre)
        lower = boot_days(arm_val, roi, quantiles=(0.05 / k,))
        x["lower_bound_at_0.05_over_k"] = lower[0] if lower else None
        better = (
            x["roi_validation"] is not None and x["roi_validation_control"] is not None
            and x["roi_validation"] > x["roi_validation_control"]
        )
        x["found"] = bool(lower and lower[0] > 0 and better)
        print(f"   {name}: 验证 {pct(x['roi_validation'])}，0.05/{k} 下界"
              f" {pct(x['lower_bound_at_0.05_over_k'])}，好于对照 {better} → "
              f"{'找到' if x['found'] else '不过'}")

    # -- the direction of the misses -----------------------------------------
    print("  -- 方向归因：落空行里真值落在均值偏向的那一侧的比例")
    result["direction"] = {}
    misses = [r for r in rows if r["miss_side"] is not None]
    cold = sum(1 for r in misses if r["miss_side"] == "冷侧")
    print(f"     落空 {len(misses)} 行：冷侧 {cold} / 暖侧 {len(misses) - cold}")
    for label, key in (("模型均值偏移 a", "a"), ("市场均值偏移 b", "b"), ("模型减市场 a−b", "a_minus_b")):
        out = {}
        for window, pool in (("全期", misses),
                             ("发现期", [r for r in misses if r["event_date"] <= DISCOVERY_CEILING]),
                             ("验证期", [r for r in misses if r["event_date"] > DISCOVERY_CEILING])):
            sel = [dict(r, agree=int((r[key] < 0) == (r["miss_side"] == "冷侧")))
                   for r in pool if abs(r[key]) >= LEAN]
            stat = lambda g: (sum(x["agree"] for x in g) / len(g)) if g else None  # noqa: E731
            out[window] = {"n": len(sel), "agree": stat(sel),
                           "ci90": boot_days(sel, stat) if len(sel) >= 5 else None}
        result["direction"][label] = out
        print("     " + label + "：" + "；".join(
            f"{w} {pct(v['agree'])}（n={v['n']}）"
            f" [{pct(v['ci90'][0]) if v['ci90'] else 'n/a'},{pct(v['ci90'][1]) if v['ci90'] else 'n/a'}]"
            for w, v in out.items()))

    # -- the live trades, and the per-trade table -----------------------------
    trades = data["live"]["trades"]["rows"]
    live = []
    for t in trades:
        c = centres["live_centres"].get(str(t["order_intent_id"]))
        if c is None:
            continue
        live.append(enrich(dict(t, best_ask=t["ask_paid"]), c))
    settled = [t for t in live if t["hit"] is not None]
    result["live"] = []
    print(f"  -- 实盘 {len(settled)} 笔按同一批条件分摊（描述）")
    for name, pred in condition_arms():
        g = [t for t in settled if pred(t)]
        entry = {"arm": name, "n": len(g), "hits": sum(1 for t in g if t["hit"]),
                 "pnl_usdc": sum(t["pnl_usdc"] for t in g)}
        result["live"].append(entry)
        print(f"     {name:<26} {entry['hits']:2d}/{entry['n']:3d} 盈亏 {entry['pnl_usdc']:+7.2f}U")

    by_intent = {str(t["order_intent_id"]): t for t in live}
    path = HERE / "trades.csv"
    with path.open(newline="") as handle:
        table = list(csv.reader(handle))
    header, body = table[0], table[1:]
    added = ["模型均值桶", "市场众数桶", "市场均值桶", "模型均值偏移a(档)", "市场均值偏移b(档)", "四心同桶"]
    if added[0] not in header:
        order = sorted(trades, key=lambda t: (t["event_date"], t["station_id"]))
        assert len(order) == len(body)
        for line, t in zip(body, order):
            assert line[0] == t["event_date"] and line[1] == t["station_id"]
            e = by_intent.get(str(t["order_intent_id"]))
            line.extend(["", "", "", "", "", ""] if e is None else [
                e["centres"]["model_mean"]["bucket"], e["centres"]["market_mode"]["bucket"],
                e["centres"]["market_mean"]["bucket"], round(e["a"], 2), round(e["b"], 2),
                int(e["four_same"]),
            ])
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header + added)
            writer.writerows(body)
    (HERE / "results3.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
