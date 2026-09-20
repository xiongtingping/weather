#!/usr/bin/env python3
"""After the tiebreak lane opens: what the market's price and the model's own
probability do to that bucket, hour by hour, until the market resolves.

Charter 164 (2026-09-20, user 「p0m不应该建仓，时间太早了，不确定太多，你去查这两桶
后面价格和概率的变化」).  The intuition is right about the uncertainty and
backwards about the money, and this script is the measurement that says so.

For every row the rule fires on it walks the station-day's committed WS
snapshots (one per hour) and every decision's envelope, and reports, split by
whether the bucket eventually landed:

  * the market's ask on the bucket we bought -- winners against losers, whose
    DIFFERENCE is how much the market knows at that hour;
  * the model's own probability on the same bucket, the same difference;
  * the per-share edge of buying that same bucket at that hour instead (the
    population's hit rate minus the average price then) -- the only column
    that answers "should we have waited";
  * how many of those station-days would still pass the rule at that hour.

2026-09-20, P0-M entries (153 rows, entry a median 39.4h out): at entry the
market separates eventual winners from losers by 1.1 points and the model by
3.5 -- neither knows anything, which is the user's point.  But the per-share
edge is +2.6 cents at entry, +2.8 at 36h, +1.6 at 30h and gone by 24h
(-0.3), because the market's separation grows to 17.0 points by 12h and the
winning bucket's price with it (0.519 -> 0.615), while the model's grows only
to 7.0.  Waiting is not available either: 59% of those station-days still
pass the rule at 24h, 39% at 18h, 10% at 12h.  The edge is earned in exactly
the hours that feel too early, and it is the market's ignorance being bought,
not the model's knowledge.

``--exit-test`` prices the constructive version of the same intuition -- sell
into the bid when the model later moves its mode off the bucket -- which is
worth nothing (+0.014 to +0.026 per share against +0.020 for holding): by the
time the model has changed its mind the market has already marked the bucket
down.  Read-only; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
from bayes_basket_target.tiebreak_cell_shadow import (  # noqa: E402
    DEFAULT_FLOOR_EVENT_DATE, PRODUCTION_SSOT_DB, replay_cell,
)

#: Hours before settlement the table reports.  Past roughly six hours the
#: station's own maximum is observable and the book is 0.99/0.01, so a price
#: there is a receipt, not a quote -- the marks stop at 12.
MARKS = (39, 36, 30, 24, 18, 12)
TOLERANCE_HOURS = 2.5
BELIEF_TOLERANCE_HOURS = 3.0


def _hours(event_date: str, at: str, zone: str | None) -> float | None:
    if not zone:
        return None
    try:
        return hours_to_settlement(event_date, as_of_utc=at, settlement_timezone=zone)
    except Exception:  # noqa: BLE001
        return None


def trace(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """One hourly price path and one per-decision belief path per fired row."""

    zones = {
        (str(s), str(d)): str(z)
        for s, d, z in conn.execute(
            "SELECT DISTINCT station_id,event_date,settlement_timezone"
            " FROM basket_policy_training_fact WHERE event_date>=?",
            (DEFAULT_FLOOR_EVENT_DATE,),
        )
    }
    out = []
    for row in rows:
        if row.get("best_ask") is None:
            continue
        key = (row["station_id"], row["event_date"])
        zone = zones.get(key)
        path = []
        for _bin, observed, snapshot_id in conn.execute(
            "SELECT substr(observed_at,1,13) b, MIN(observed_at), MIN(snapshot_id)"
            " FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " GROUP BY b ORDER BY b", key,
        ):
            hours = _hours(row["event_date"], str(observed), zone)
            if hours is None:
                continue
            snapshot = lane._snapshot(conn, str(snapshot_id))
            if not snapshot or not snapshot.get("buckets"):
                continue
            market = market_reference_v1(snapshot["buckets"])
            if market.get("state") != "complete":
                continue
            book = lane.book_features(next(
                (b for b in snapshot["buckets"]
                 if str(b.get("bucket")) == row["bucket"]), None,
            ))
            path.append({
                "tau": hours, "share": (market["vector"] or {}).get(row["bucket"]),
                "ask": book["best_ask"], "bid": book["best_bid"], "spread": book["spread"],
            })
        beliefs = []
        for stage, decided, envelope_id in conn.execute(
            "SELECT stage, decision_at_utc, probability_envelope_id"
            " FROM basket_policy_training_fact WHERE station_id=? AND event_date=?"
            " ORDER BY decision_at_utc", key,
        ):
            envelope = lane._envelope(conn, str(envelope_id))
            if not envelope or not envelope.get("probabilities"):
                continue
            probabilities = envelope["probabilities"]
            hours = _hours(row["event_date"], str(decided), zone)
            if hours is None or row["bucket"] not in probabilities:
                continue
            beliefs.append({
                "tau": hours, "stage": str(stage),
                "p": float(probabilities[row["bucket"]]),
                "mode": max(probabilities, key=lambda b: probabilities[b]),
            })
        out.append({**row, "entry_tau": _hours(row["event_date"], row["decision_at_utc"], zone),
                    "path": path, "beliefs": beliefs})
    return out


def _near(seq: list[dict], mark: float, key: str, tolerance: float) -> dict | None:
    close = [x for x in seq if abs(x["tau"] - mark) <= tolerance and x.get(key) is not None]
    return min(close, key=lambda x: abs(x["tau"] - mark)) if close else None


def table(traced: list[dict], stage: str) -> list[dict]:
    sel = [r for r in traced if r["stage"] == stage]
    out = []
    for mark in MARKS:
        priced = [(r, _near(r["path"], mark, "ask", TOLERANCE_HOURS)) for r in sel]
        priced = [(r, p) for r, p in priced if p]
        if not priced:
            continue
        believed = [(r, _near(r["beliefs"], mark, "p", BELIEF_TOLERANCE_HOURS)) for r in sel]
        believed = [(r, b) for r, b in believed if b]
        win = [p["ask"] for r, p in priced if r["hit"]]
        lose = [p["ask"] for r, p in priced if not r["hit"]]
        bwin = [b["p"] for r, b in believed if r["hit"]]
        blose = [b["p"] for r, b in believed if not r["hit"]]
        price = st.mean([p["ask"] for _r, p in priced])
        rate = sum(1 for r, _p in priced if r["hit"]) / len(priced)
        passes = sum(
            1 for _r, p in priced
            if p["share"] is not None and 0.40 <= p["share"] < 0.60
            and p["spread"] is not None and p["spread"] <= 0.031
        )
        out.append({
            "hours": mark, "n": len(priced),
            "ask_if_won": st.mean(win) if win else None,
            "ask_if_lost": st.mean(lose) if lose else None,
            "market_separation": (st.mean(win) - st.mean(lose)) if (win and lose) else None,
            "model_if_won": st.mean(bwin) if bwin else None,
            "model_if_lost": st.mean(blose) if blose else None,
            "model_separation": (st.mean(bwin) - st.mean(blose)) if (bwin and blose) else None,
            "avg_price": price, "hit_rate": rate, "edge_per_share": rate - price,
            "still_passes_rule": passes / len(priced),
        })
    return out


def exit_test(traced: list[dict], stage: str, checks: tuple[float, ...]) -> dict | None:
    """Sell into the bid at the first mark where the model's mode has moved off
    the bucket.  No look-ahead: both the mode and the bid are that hour's."""

    sel = [r for r in traced if r["stage"] == stage]
    results = []
    for row in sel:
        sold = None
        for mark in checks:
            belief = _near(row["beliefs"], mark, "p", BELIEF_TOLERANCE_HOURS)
            if not belief or belief["tau"] >= (row["entry_tau"] or 0) - 0.5:
                continue
            if belief["mode"] == row["bucket"]:
                continue
            quote = _near(row["path"], belief["tau"], "bid", 1.5)
            if quote and quote["bid"] and quote["bid"] > 0.01:
                sold = quote["bid"]
                break
        payoff = sold if sold is not None else float(row["hit"])
        results.append({"event_date": row["event_date"], "sold": sold is not None,
                        "net": payoff - row["best_ask"],
                        "roi": (payoff - row["best_ask"]) / row["best_ask"]})
    if not results:
        return None
    by_day: dict[str, list[float]] = defaultdict(list)
    for item in results:
        by_day[item["event_date"]].append(item["roi"])
    rng = random.Random(164)
    days = list(by_day)
    draws = []
    for _ in range(2000):
        pool = [v for d in (rng.choice(days) for _ in days) for v in by_day[d]]
        draws.append(st.mean(pool))
    draws.sort()
    return {"checks": list(checks), "n": len(results),
            "sold": sum(1 for x in results if x["sold"]),
            "net_per_share": st.mean([x["net"] for x in results]),
            "roi": st.mean([x["roi"] for x in results]),
            "ci_lo": draws[100], "ci_hi": draws[1900]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor", default=DEFAULT_FLOOR_EVENT_DATE)
    parser.add_argument("--stages", default="P0-M,P1")
    parser.add_argument("--exit-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{PRODUCTION_SSOT_DB}?mode=ro", uri=True, timeout=300.0)
    conn.execute("PRAGMA busy_timeout=300000")
    traced = trace(conn, replay_cell(conn, floor_event_date=args.floor))
    stages = [s for s in args.stages.split(",") if s]
    result = {"schema": "tiebreak_entry_path_v1", "rows": len(traced),
              "stages": {s: table(traced, s) for s in stages}}
    if args.exit_test:
        result["exit_test"] = {
            s: [x for x in (exit_test(traced, s, c) for c in ((), (24,), (18,), (12,), (30, 24, 18, 12)))
                if x]
            for s in stages
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    for stage in stages:
        sel = [r for r in traced if r["stage"] == stage]
        entry = [r["entry_tau"] for r in sel if r["entry_tau"] is not None]
        print(f"\n=== {stage} 进场 n={len(sel)}"
              f"（进场距结算中位 {st.median(entry):.1f}h）===")
        print("  τ   | 市场价 中/空/区分 | 模型概率 中/空/区分 | 此刻买同一桶 均价/每股边际 | 仍过规则")
        for r in result["stages"][stage]:
            f = lambda x: f"{x:.3f}" if x is not None else "  —  "  # noqa: E731
            print(f"  {r['hours']:2d}h | {f(r['ask_if_won'])} {f(r['ask_if_lost'])}"
                  f" {f(r['market_separation'])} | {f(r['model_if_won'])} {f(r['model_if_lost'])}"
                  f" {f(r['model_separation'])} | n={r['n']:3d} {r['avg_price']:.3f}"
                  f" {r['edge_per_share']:+.3f} | {r['still_passes_rule']:4.0%}")
        for row in (result.get("exit_test") or {}).get(stage, []):
            what = "一直持到结算" if not row["checks"] else f"改主意就卖 @{row['checks']}"
            print(f"    {what:34} 卖出 {row['sold']:3d}/{row['n']:3d}"
                  f" 每股净={row['net_per_share']:+.3f} ROI={row['roi']:+7.1%}"
                  f" [{row['ci_lo']:+.1%},{row['ci_hi']:+.1%}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
