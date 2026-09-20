#!/usr/bin/env python3
"""Re-scan the whole fleet at each hour before settlement: how many
station-days still hold a bucket the market cannot decide, and what buying it
THEN would have paid.

Charter 164 (2026-09-20, user 「当天不确定的桶还会有纠缠」).  The correction
this script exists for: ``trace_tiebreak_entry_paths.py`` follows the bucket
the lane bought and reports that only 10% of those are still inside the band
twelve hours out, which says nothing about whether OTHER buckets are -- the
cell re-forms on other station-days and other rungs all day.  Measured here,
it does: 27-31% of evaluable station-days carry a qualifying bucket at every
mark from 42h to 24h, and 8.8% still do at 12h (67 station-days over 35 event
days, about two a day).  So "wait and there is nothing left to bet on" was
wrong.

What the hours are worth is another matter, and the answer is a U: buying at
42h/36h returns +5.3%, at 30h/24h -8.1%, at 12h/9h +7.5%, on essentially the
same price (0.516 / 0.512 / 0.530) -- the difference is the hit rate (54.2% /
47.0% / 57.1%).  The mechanism that would explain it, and the user's intuition
stated precisely: a bucket priced 0.40-0.60 thirty hours out is often merely
UNPRICED, while one still priced there at twelve hours is a real coin toss.
Neither end clears its own chance bar (shuffling the labels inside each event
day, the worst of the seven marks reaches -14.4% by chance against -10.5%
observed, p=0.66; the three-way late-minus-middle contrast is +15.6 points at
p=0.125), and the middle band is a population the lane never trades: it only
ever buys a stage's FIRST decision, and inside those the 24-36h P0-M entries
read +5.5% against +2.5% for the 36-48h ones.  Recorded, nothing changed.

One entry per station-day per mark, the lane's own grain.  Read-only.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
from bayes_basket_target.tiebreak_cell_shadow import (  # noqa: E402
    PRODUCTION_SSOT_DB, _roi, day_clustered_interval, settled_buckets,
)

MARKS = (42, 36, 30, 24, 18, 12, 9)
#: How far a decision may sit from a mark and still stand for that hour.
TOLERANCE_HOURS = 2.0
FLOOR_EVENT_DATE = "2026-08-01"
GROUPS = (("早 42h+36h", (42, 36)), ("中段 30h+24h", (30, 24)), ("晚 12h+9h", (12, 9)))
SHUFFLE_DRAWS = 2000
SEED = 164


def rescan(conn: sqlite3.Connection, *, floor: str) -> list[dict]:
    labels = settled_buckets(conn, floor_event_date=floor)
    decisions: dict[tuple[str, str], list[tuple[str, ...]]] = defaultdict(list)
    for station, day, stage, decided, envelope, snapshot, zone in conn.execute(
        "SELECT station_id,event_date,stage,decision_at_utc,probability_envelope_id,"
        " ws_snapshot_id,settlement_timezone"
        " FROM basket_policy_training_fact WHERE event_date>=?", (floor,),
    ):
        key = (str(station), str(day))
        if key in labels:
            decisions[key].append((str(stage), str(decided), str(envelope), str(snapshot), str(zone)))
    out = []
    for key, items in decisions.items():
        items.sort(key=lambda x: x[1])
        zone = items[0][4]
        try:
            hours = [hours_to_settlement(key[1], as_of_utc=i[1], settlement_timezone=zone)
                     for i in items]
        except Exception:  # noqa: BLE001
            continue
        for mark in MARKS:
            near = [(abs(h - mark), item, h) for item, h in zip(items, hours)
                    if abs(h - mark) <= TOLERANCE_HOURS]
            if not near:
                continue
            _gap, (stage, _decided, envelope_id, snapshot_id, _z), hour = min(
                near, key=lambda x: x[0])
            envelope = lane._envelope(conn, envelope_id)
            snapshot = lane._snapshot(conn, snapshot_id)
            if not envelope or not envelope.get("probabilities"):
                continue
            if not snapshot or not snapshot.get("buckets"):
                continue
            market = market_reference_v1(snapshot["buckets"])
            if market.get("state") != "complete":
                continue
            pre = lane.evaluate_rule(
                envelope["probabilities"], envelope["ordinals"], market["vector"],
                min_market_share=0.40, max_market_share=0.60, rule=lane.RULE_V2)
            mode = pre.get("model_mode")
            if not mode:
                continue
            book = lane.book_features(next(
                (b for b in snapshot["buckets"] if str(b.get("bucket")) == mode), None))
            reading = lane.evaluate_rule(
                envelope["probabilities"], envelope["ordinals"], market["vector"],
                min_market_share=0.40, max_market_share=0.60, rule=lane.RULE_V21,
                spread=book["spread"], max_spread=0.031)
            out.append({
                "station_id": key[0], "event_date": key[1], "mark": mark, "tau": hour,
                "stage": stage, "fired": int(bool(reading["fired"])), "bucket": mode,
                "settled": labels[key], "hit": int(labels[key] == mode),
                "best_ask": book["best_ask"], "share": reading.get("market_share_on_model_mode"),
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor", default=FLOOR_EVENT_DATE)
    parser.add_argument("--draws", type=int, default=SHUFFLE_DRAWS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{PRODUCTION_SSOT_DB}?mode=ro", uri=True, timeout=300.0)
    conn.execute("PRAGMA busy_timeout=300000")
    rows = rescan(conn, floor=args.floor)
    fired = [r for r in rows if r["fired"] and r["best_ask"]]

    per_mark = []
    for mark in MARKS:
        pool = [r for r in rows if r["mark"] == mark]
        hit_rows = [r for r in fired if r["mark"] == mark]
        if not pool or not hit_rows:
            continue
        interval = day_clustered_interval(hit_rows)
        rate = sum(r["hit"] for r in hit_rows) / len(hit_rows)
        price = st.mean([r["best_ask"] for r in hit_rows])
        per_mark.append({
            "hours": mark, "evaluable": len(pool), "fired": len(hit_rows),
            "fire_rate": len(hit_rows) / len(pool), "hit_rate": rate, "avg_price": price,
            "edge_per_share": rate - price, "roi": _roi(hit_rows),
            "ci_lo": interval["ci_lo"], "ci_hi": interval["ci_hi"],
            "event_days": interval["event_days"],
            "stage_mix": dict(Counter(r["stage"] for r in hit_rows)),
        })
    groups = []
    for label, marks in GROUPS:
        sel = [r for r in fired if r["mark"] in marks]
        if not sel:
            continue
        groups.append({"group": label, "n": len(sel),
                       "hit_rate": sum(r["hit"] for r in sel) / len(sel),
                       "avg_price": st.mean([r["best_ask"] for r in sel]),
                       "roi": _roi(sel)})

    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in fired:
        by_day[row["event_date"]].append(row)
    truth = {id(r): r["hit"] for r in fired}
    rng = random.Random(SEED)
    worst, contrast = [], []
    late = [r for r in fired if r["mark"] in (12, 9)]
    middle = [r for r in fired if r["mark"] in (30, 24)]
    for _ in range(args.draws):
        for day_rows in by_day.values():
            hits = [r["hit"] for r in day_rows]
            rng.shuffle(hits)
            for row, hit in zip(day_rows, hits):
                row["hit"] = hit
        marks = [_roi([r for r in fired if r["mark"] == m]) for m in MARKS]
        marks = [m for m in marks if m is not None]
        if marks:
            worst.append(min(marks))
        if late and middle:
            contrast.append((_roi(late) or 0.0) - (_roi(middle) or 0.0))
    for row in fired:
        row["hit"] = truth[id(row)]
    worst.sort()
    contrast.sort()
    observed_worst = min(r["roi"] for r in per_mark) if per_mark else None
    observed_contrast = ((_roi(late) or 0.0) - (_roi(middle) or 0.0)) if (late and middle) else None
    result = {
        "schema": "tiebreak_entry_hour_scan_v1", "rows": len(rows), "fired": len(fired),
        "per_mark": per_mark, "groups": groups,
        "chance": {
            "draws": args.draws,
            "worst_mark_median": worst[len(worst) // 2] if worst else None,
            "worst_mark_p05": worst[int(0.05 * len(worst))] if worst else None,
            "observed_worst": observed_worst,
            "p_worst": (sum(1 for w in worst if observed_worst is not None and w <= observed_worst)
                        / len(worst)) if worst else None,
            "late_minus_middle": observed_contrast,
            "late_minus_middle_p95": contrast[int(0.95 * len(contrast))] if contrast else None,
            "p_late_minus_middle": (
                sum(1 for c in contrast if observed_contrast is not None and c >= observed_contrast)
                / len(contrast)) if contrast else None,
        },
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(" τ  | 可评估站日 | 触发 | 触发率 | 命中 | 均价 | 每股边际 | ROI | 阶段构成")
    for row in per_mark:
        band = ("-" if row["ci_lo"] is None
                else f"[{row['ci_lo']:+.1%},{row['ci_hi']:+.1%}]")
        print(f"{row['hours']:3d}h | {row['evaluable']:6d} | {row['fired']:4d} |"
              f" {row['fire_rate']:5.1%} | {row['hit_rate']:5.1%} | {row['avg_price']:.3f} |"
              f" {row['edge_per_share']:+.3f} | {row['roi']:+7.1%} {band} | {row['stage_mix']}")
    for row in groups:
        print(f"  {row['group']:14} n={row['n']:4d} 命中={row['hit_rate']:5.1%}"
              f" 均价={row['avg_price']:.3f} ROI={row['roi']:+7.1%}")
    chance = result["chance"]
    print(f"日内洗牌 {chance['draws']} 次：7 个时刻里最负的一个中位"
          f" {chance['worst_mark_median']:+.1%}，实测最负 {chance['observed_worst']:+.1%}"
          f" → p={chance['p_worst']:.3f}")
    print(f"晚 − 中段 = {chance['late_minus_middle']:+.1%}，洗牌 95 分位"
          f" {chance['late_minus_middle_p95']:+.1%} → p={chance['p_late_minus_middle']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
