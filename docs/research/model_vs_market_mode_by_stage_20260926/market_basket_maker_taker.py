#!/usr/bin/env python3
"""P0 opening only: the market's own basket (mode + mean bucket, or mode +/- 1),
built inside P0 and held to settlement -- at the ask, or resting at the best
bid plus one tick.  Does it make money?

熊先生 2026-09-26 追问：「如果是选择市场的均值和众数桶为篮子呢？分别按照 best
买一+1 挂单和吃市价吃单，是否能盈利？即篮子内保本策略，买 2-3 个篮子为温度组合。」
续：「不要 p1，只 p0 开仓建仓，其他阶段不加仓。p0 持有到结算」

Fills come from the maker shadow book (``data/maker_shadow.db``,
``scripts/maker_shadow_probe.py``): intents written before the market moved,
filled only if the live book later traded at or through the limit inside the
order's 60-minute life (the longest horizon the book records).  The book
writes a fresh set of intents about every three hours.

P0 window of a station-day: shadow placements matched to a P0 decision of the
L4 population (latest decision at or before the placement, within 90 minutes),
before its first P0-M/P1 decision and more than 24 hours before settlement.
The basket is fixed at the window's first placement (the opening) from that
decision's market vector; truth is the population's label.  One share per leg
(equal payout, the in-basket break-even shape); the break-even filter keeps
baskets whose summed entry price is under 1.  Arms:

- ``taker``: every leg at the opening ask.
- ``maker_open_60m``: bid+1t at the opening for one hour; unfilled legs lapse.
- ``maker_build_p0``: bid+1t re-posted at every P0 placement until the leg fills.
- ``maker_build_then_take``: as above, and legs still unfilled at the last P0
  placement pay that placement's ask, so every basket ends complete.
- ``bid_plus_one_nominal``: every leg at the opening bid+1t, as if all filled --
  the ceiling no fill process reaches.

Read-only; writes market_basket_results.json.

    python3 market_basket_maker_taker.py [population_cache.pkl [shadow_cache.pkl]]
"""
from __future__ import annotations

import json
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from measure import ROOT, load, order  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

SHADOW = f"file:{ROOT}/data/maker_shadow.db?mode=ro"
RULES = ("ask", "bid+1t", "bid+1t@then_take")
ARMS = ("taker", "maker_open_60m", "maker_build_p0", "maker_build_then_take", "bid_plus_one_nominal")
DRAWS = 1000
SEED = 20260926
MATCH_MINUTES = 90
BREAK_EVEN_BELOW = 0.995


def instant(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def load_shadow(station_days):
    conn = sqlite3.connect(SHADOW, uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    out = {}
    for station, day in station_days:
        out[(station, day)] = conn.execute(
            "SELECT bucket, price_rule, placed_at_utc, limit_price, reference_bid, reference_ask,"
            " reference_tick, state, fill_kind, fill_limit_price, filled_at_utc FROM maker_shadow_order"
            " WHERE station_id=? AND event_date=? AND horizon_minutes=60"
            "   AND price_rule IN (?,?,?)", (station, day, *RULES)).fetchall()
    conn.close()
    return out


def baskets(m):
    """Two legs: the market's mode and mean bucket (the mean rounded on the
    ladder's index); when they coincide, the mode and its neighbour on the
    side the continuous mean leans to.  Three legs: the mode and both
    neighbours (shifted inward at the ladder's ends)."""

    n = len(m)
    mode = int(np.argmax(m))
    mean_c = float((m * np.arange(n)).sum())
    mean = int(round(mean_c))
    if mean != mode:
        two = sorted({mode, mean})
    else:
        side = 1 if mean_c > mode else -1
        other = mode + side if 0 <= mode + side < n else mode - side
        two = sorted({mode, other})
    low = min(max(mode - 1, 0), max(n - 3, 0))
    return {"two": two, "three": list(range(low, min(low + 3, n)))}


def p0_windows(rows, shadow):
    decisions = defaultdict(list)
    for r in rows:
        decisions[(r["station"], r["date"])].append(r)
    out = []
    for key, found in shadow.items():
        ds = sorted(decisions.get(key, []), key=lambda r: r["at"])
        if not found or not any(r["stage"] == "P0" for r in ds):
            continue
        times = [instant(r["at"]) for r in ds]
        later_stage = min((t for t, r in zip(times, ds) if r["stage"] != "P0"), default=None)
        book = defaultdict(dict)
        for bucket, rule, placed, limit, _bid, _ask, _tick, state, kind, fill_limit, _at in found:
            book[placed].setdefault(bucket, {})[rule] = {
                "limit": limit, "state": state, "kind": kind, "fill_limit": fill_limit}
        seq = []
        for placed in sorted(book):
            t = instant(placed)
            if later_stage is not None and t >= later_stage:
                break
            hours = hours_to_settlement_if_known(key[1], as_of_utc=placed,
                                                 settlement_timezone=ds[0].get("settlement_timezone"))
            if hours is None or hours <= 24:
                break
            j = next((i for i in range(len(ds) - 1, -1, -1) if times[i] <= t), None)
            if j is None or ds[j]["stage"] != "P0" or t - times[j] > timedelta(minutes=MATCH_MINUTES):
                continue
            seq.append({"placed": placed, "hours": hours, "decision": ds[j], "book": book[placed]})
        if not seq:
            continue
        opening = seq[0]["decision"]
        keys = sorted(opening["m"], key=order)
        m = np.array([float(opening["m"][k]) for k in keys])
        m = m / m.sum()
        out.append({"station": key[0], "date": key[1], "keys": keys, "y": keys.index(opening["label"]),
                    "seq": seq, **baskets(m)})
    return out


def run(entry, legs, name):
    keys, seq, y = entry["keys"], entry["seq"], entry["y"]
    first = seq[0]["book"]
    covered = float(y in legs)
    if name == "taker":
        if any("ask" not in first.get(keys[i], {}) for i in legs):
            return None
        cost = sum(first[keys[i]]["ask"]["limit"] for i in legs)
        return {"entry": cost, "cost": cost, "pay": covered, "filled": len(legs), "legs": len(legs)}
    if any("bid+1t" not in first.get(keys[i], {}) for i in legs):
        return None
    entry_price = sum(first[keys[i]]["bid+1t"]["limit"] for i in legs)
    if name == "bid_plus_one_nominal":
        return {"entry": entry_price, "cost": entry_price, "pay": covered, "filled": len(legs), "legs": len(legs)}
    if name == "maker_open_60m":
        rounds = seq[:1]
    else:
        rounds = seq
    fills = {}
    for step in rounds:
        for i in legs:
            if i in fills:
                continue
            order_row = step["book"].get(keys[i], {}).get("bid+1t")
            if order_row is None:
                continue
            if order_row["state"] == "OPEN":
                return None
            if order_row["state"] == "FILLED":
                fills[i] = order_row["limit"]
    if name in ("maker_open_60m", "maker_build_p0"):
        return {"entry": entry_price, "cost": sum(fills.values()), "pay": sum(float(y == i) for i in fills),
                "filled": len(fills), "legs": len(legs)}
    last = seq[-1]["book"]
    cost = sum(fills.values())
    for i in legs:
        if i not in fills:
            ask = last.get(keys[i], {}).get("ask")
            if ask is None:
                return None
            cost += ask["limit"]
    return {"entry": entry_price, "cost": cost, "pay": covered, "filled": len(fills), "legs": len(legs)}


def roi_ci(items, seed):
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    di = np.array([index[d["date"]] for d in items])
    a = np.bincount(di, weights=np.array([d["pay"] - d["cost"] for d in items]), minlength=len(days))
    b = np.bincount(di, weights=np.array([d["cost"] for d in items]), minlength=len(days))
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(DRAWS):
        pick = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days)).astype(float)
        boots.append(float(pick @ a / (pick @ b)))
    return [float(a.sum() / b.sum()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def table(entries):
    out = {}
    for b, basket in enumerate(("two", "three")):
        for a, name in enumerate(ARMS):
            for label, keep in (("all", lambda x: True), ("break_even", lambda x: x["entry"] < BREAK_EVEN_BELOW)):
                items = []
                for e in entries:
                    got = run(e, e[basket], name)
                    if got is not None and keep(got):
                        items.append(dict(got, date=e["date"]))
                paid = [d for d in items if d["cost"] > 0]
                if not paid:
                    continue
                out[f"{basket}:{name}:{label}"] = {
                    "station_days": len(items), "event_days": len({d["date"] for d in items}),
                    "entry_price": float(np.mean([d["entry"] for d in items])),
                    "paid_when_paid": float(np.mean([d["cost"] for d in paid])),
                    "all_legs_filled": float(np.mean([d["filled"] == d["legs"] for d in items])),
                    "no_leg_filled": float(np.mean([d["filled"] == 0 for d in items])),
                    "any_payout": float(np.mean([d["pay"] > 0 for d in items])),
                    "pnl_per_basket": float(np.mean([d["pay"] - d["cost"] for d in items])),
                    "roi": roi_ci(paid, SEED + 10 * b + a),
                }
    return out


def adverse_selection(entries):
    groups = {"filled": [], "not_filled": []}
    for e in entries:
        for i in e["two"]:
            order_row = e["seq"][0]["book"].get(e["keys"][i], {}).get("bid+1t")
            if order_row and order_row["state"] != "OPEN":
                groups["filled" if order_row["state"] == "FILLED" else "not_filled"].append(
                    (float(e["y"] == i), order_row["limit"]))
    return {k: {"legs": len(v), "hit": float(np.mean([h for h, _p in v])),
                "limit_price": float(np.mean([p for _h, p in v]))} for k, v in groups.items() if v}


def main():
    pop_cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    shadow_cache = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if pop_cache is not None and pop_cache.exists():
        rows = pickle.loads(pop_cache.read_bytes())
        rows = rows[0] if isinstance(rows, tuple) else rows
    else:
        rows = load(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro")
    if shadow_cache is not None and shadow_cache.exists():
        shadow = pickle.loads(shadow_cache.read_bytes())
    else:
        shadow = load_shadow(sorted({(r["station"], r["date"]) for r in rows}))
    entries = p0_windows(rows, shadow)
    result = {
        "question": "熊先生 2026-09-26：只 P0 开仓建仓、持有到结算；市场众数+均值桶为篮子，买一+1 挂单 vs 吃单",
        "station_days": len(entries), "event_days": len({e["date"] for e in entries}),
        "median_placements_in_p0": float(np.median([len(e["seq"]) for e in entries])),
        "median_opening_hours_to_settlement": float(np.median([e["seq"][0]["hours"] for e in entries])),
        "p0": table(entries),
        "p0_opening_bid_plus_one_legs": adverse_selection(entries),
    }
    (HERE / "market_basket_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key, row in result["p0"].items():
        print(key, row["station_days"], round(row["entry_price"], 3), round(row["paid_when_paid"], 3),
              "all", round(100 * row["all_legs_filled"]), "none", round(100 * row["no_leg_filled"]),
              "roi", [round(100 * x, 1) for x in row["roi"]])
    print(result["p0_opening_bid_plus_one_legs"])


if __name__ == "__main__":
    main()
