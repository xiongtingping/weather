"""Route 4 of PROTOCOL.md: does the market lag an observed high that rules
out its own mode?

    uv run python docs/research/skew_profit_routes_20261017/t4_obs_lag.py
"""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.station_day_board import bucket_of, ladder_axis  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SINCE = "2026-08-20"
FEE = 0.05
LOOKS = (("决策后 10 分钟内第一份", 0), ("30 分钟后", 30), ("60 分钟后", 60))
rng = random.Random(20261017)


def _t(s) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _z(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def snap(conn, station, day, at, after):
    q = ("SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
         " AND state='COMMITTED' AND observed_at" + (">=? ORDER BY observed_at" if after else "<=? ORDER BY observed_at DESC")
         + " LIMIT 1")
    row = conn.execute(q, (station, day, _z(at))).fetchone()
    return None if row is None else (_t(row[0]), json.loads(row[1]))


def lv(levels, side):
    px = [float(x["price"]) for x in levels or [] if float(x.get("shares") or 0) > 0]
    return (max(px) if side == "bid" else min(px)) if px else None


def roi(rows, label):
    if not rows:
        return f"{label}: 0 笔"
    by = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by[r["event_date"]][0] += r["pnl"]
        by[r["event_date"]][1] += r["cost"]
    days = list(by)
    sims = []
    for _ in range(2000):
        s = [by[days[rng.randrange(len(days))]] for _ in days]
        sims.append(sum(x[0] for x in s) / sum(x[1] for x in s))
    sims.sort()
    pnl, cost = sum(r["pnl"] for r in rows), sum(r["cost"] for r in rows)
    return (f"{label}: {len(rows)} 笔/{len(days)} 天  命中 {100 * sum(r['hit'] for r in rows) / len(rows):.1f}%"
            f"  均价 {sum(r['price'] for r in rows) / len(rows):.3f}  回报率 {100 * pnl / cost:+.1f}%"
            f" [{100 * sims[50]:+.1f}, {100 * sims[1949]:+.1f}]  合计 {pnl:+.2f}")


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    truth = {}
    for s, d, v in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' AND local_date>=? ORDER BY revision_no",
        (SINCE,),
    ):
        truth[(str(s), str(d))] = float(v)
    p1 = defaultdict(list)
    for s, d, at, high, ords, iv in conn.execute(
        "SELECT station_id, event_date, decision_at_utc, json_extract(lineage_json,'$.reported_high_c'),"
        " bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
        " FROM basket_probability_envelope WHERE stage='P1' AND state='COMMITTED' AND event_date>=?"
        " ORDER BY station_id, event_date, decision_at_utc", (SINCE,),
    ):
        if high is not None and iv:
            p1[(str(s), str(d))].append((str(at), float(high), ords, iv))
    events = []
    for (station, day), decisions in sorted(p1.items()):
        if (station, day) not in truth:
            continue
        axis = ladder_axis(json.loads(decisions[0][2]), json.loads(decisions[0][3]))
        order = {a["bucket"]: a["ordinal"] for a in axis}
        truth_bucket = bucket_of(axis, truth[(station, day)])
        prev = None
        for at, high, _o, _i in decisions:
            rose = prev is not None and high > prev + 1e-9
            prev = high if prev is None else max(prev, high)
            if not rose:
                continue
            floor_bucket = bucket_of(axis, high)
            if floor_bucket is None:
                continue
            moment = _t(at)
            before = snap(conn, station, day, moment, after=False)
            if before is None:
                continue
            ref = market_reference_v1(before[1])
            if ref.get("state") != "complete":
                continue
            vec = {str(k): float(v) for k, v in ref["vector"].items()}
            labels = sorted(vec, key=lambda b: order.get(b, 0))
            mode = max(labels, key=lambda b: (vec[b], -labels.index(b)))
            if mode not in order or order[mode] >= order[floor_bucket]:
                continue          # the market's mode is still possible
            ev = {"station_id": station, "event_date": day, "at": at, "high": high, "floor": floor_bucket,
                  "mode_before": mode, "looks": {}}
            for label, minutes in LOOKS:
                s = snap(conn, station, day, moment + timedelta(minutes=minutes), after=True)
                if s is None or (minutes == 0 and s[0] - moment > timedelta(minutes=10)):
                    continue
                books = {str(b["bucket"]): b for b in s[1]}
                dead = []
                for b in labels:
                    if order[b] < order[floor_bucket] and b in books:
                        bid = lv(books[b].get("bid_levels"), "bid")
                        if bid and bid >= 0.01:
                            q = 1 - bid
                            fee = FEE * q * (1 - q)
                            hit = int(b == truth_bucket)
                            dead.append({"event_date": day, "price": q, "cost": q + fee, "hit": 1 - hit,
                                         "pnl": (1 - hit) - q - fee, "bid": bid})
                live = None
                fb = books.get(floor_bucket)
                ask = lv((fb or {}).get("ask_levels"), "ask")
                if ask and 0 < ask < 1:
                    hit = int(floor_bucket == truth_bucket)
                    live = {"event_date": day, "price": ask, "cost": ask + FEE * ask * (1 - ask), "hit": hit,
                            "pnl": hit - ask - FEE * ask * (1 - ask)}
                ev["looks"][label] = {"after_min": round((s[0] - moment).total_seconds() / 60, 1),
                                      "dead": dead, "live": live}
            events.append(ev)
    with gzip.open(HERE / "t4_events.jsonl.gz", "wt", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"事件 {len(events)}：P1 实况最高上升且使市场当时众数已不可能")
    for label, _m in LOOKS:
        have = [e for e in events if label in e["looks"]]
        dead = [d for e in have for d in e["looks"][label]["dead"]]
        live = [e["looks"][label]["live"] for e in have if e["looks"][label]["live"]]
        with_bid = sum(1 for e in have if e["looks"][label]["dead"])
        print(f"\n## {label}（{len(have)} 个事件有盘口；其中 {with_bid} 个仍有已死桶挂着 ≥0.01 的买一）")
        if dead:
            bids = sorted(d["bid"] for d in dead)
            print(f"  已死桶买一：{len(dead)} 个，中位 {bids[len(bids) // 2]:.3f}，最大 {bids[-1]:.3f}")
        print("  " + roi(dead, "卖已死桶（买 NO）"))
        print("  " + roi(live, "买含实况最高的桶 YES"))


if __name__ == "__main__":
    main()
