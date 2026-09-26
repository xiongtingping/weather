#!/usr/bin/env python3
"""Proper 下注线 attribution: ``Pi = Delta S + D - L`` per settled station-day.

Charter 231.  Read-only.  For every evaluation row of
``proper_bet_lane_evaluation`` whose station-day has settled:

* ``Delta S = log(p_y / q_y)`` -- the realised LogScore gap of the weather
  model over the market on the settled bucket ``y``;
* ``D = KL(q || p)`` -- the divergence, recorded when the row was judged;
* ``Delta S + D`` -- the paper's frictionless proper return per unit
  ``lambda`` on the WHOLE vector (the pointwise identity
  ``s . (1_y - q) = S(p, y) - S(q, y) + D``);
* for the legs that filled: ``frictionless`` = what the filled shares would
  have made at ``q`` (a YES share ``1_y - q_k``, a NO share
  ``q_k - 1_y``), ``realised`` = payout minus what was paid, and
  ``L = frictionless - realised`` -- the spread, depth and walk the fills
  paid over the market's own probability.

Usage::

    python3 scripts/proper_bet_attribution.py [--db PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import live_book  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.proper_bet_lane import EVALUATION_TABLE  # noqa: E402


def _fills(conn: sqlite3.Connection, intent_ids: list[str]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for intent_id in intent_ids:
        row = conn.execute(
            "SELECT COALESCE(filled_size,0), COALESCE(filled_notional_usdc,0)"
            " FROM basket_target_order_reconciliations WHERE order_intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is not None:
            out[intent_id] = (float(row[0]), float(row[1]))
    return out


def attribute(conn: sqlite3.Connection) -> dict[str, Any]:
    if not live_book.table_exists(conn, EVALUATION_TABLE):
        return {"rows": [], "totals": {}, "reason": "no_evaluation_table"}
    evaluations = conn.execute(
        f"SELECT station_id, event_date, stage, p_json, q_json, divergence_kl,"
        f" lambda_shares, legs_state_json, refusal FROM {EVALUATION_TABLE}"
        " WHERE p_json IS NOT NULL AND q_json IS NOT NULL"
        " ORDER BY event_date, station_id, stage"
    ).fetchall()
    keys = {(str(r[0]), str(r[1])) for r in evaluations}
    truth = live_book._truth(conn) if live_book.table_exists(
        conn, "calibration_station_daily_high_truth"
    ) else None
    settled = live_book.settled_buckets(conn, keys, truth=truth)
    rows: list[dict[str, Any]] = []
    totals = {
        "station_days": 0, "traded_station_days": 0, "delta_s": 0.0, "divergence": 0.0,
        "stake_usdc": 0.0, "realised_usdc": 0.0, "frictionless_usdc": 0.0, "l_usdc": 0.0,
    }
    for station, day, stage, p_json, q_json, divergence, lam, legs_json, refusal in evaluations:
        landed = settled.get((str(station), str(day)))
        if landed is None:
            continue
        y = str(landed[0])
        p = json.loads(p_json)
        q = json.loads(q_json)
        if y not in p or y not in q:
            continue
        delta_s = math.log(float(p[y]) / float(q[y]))
        d = float(divergence) if divergence is not None else None
        # Every rung a leg posted (charter 231 v2 re-posts until filled): the
        # leg is its side and bucket, its fills the sum over its rungs.
        rungs = [
            (key.split(":", 1)[0], key.split(":", 1)[1], intent_id)
            for key, leg_state in (json.loads(legs_json) if legs_json else {}).items()
            for intent_id in leg_state.get("intents") or []
        ]
        fills = _fills(conn, [intent_id for _side, _bucket, intent_id in rungs])
        stake = realised = frictionless = 0.0
        legs: list[dict[str, Any]] = []
        for side, bucket, intent_id in rungs:
            size, paid = fills.get(intent_id, (0.0, 0.0))
            if size <= 0.0:
                continue
            hit = 1.0 if bucket == y else 0.0
            if side.upper() == "NO":
                payout = size * (1.0 - hit)
                at_q = size * (float(q[bucket]) - hit)
            else:
                payout = size * hit
                at_q = size * (hit - float(q[bucket]))
            stake += paid
            realised += payout - paid
            frictionless += at_q
            legs.append({"bucket": bucket, "side": side, "shares": size, "paid_usdc": paid,
                         "payout_usdc": payout, "at_q_usdc": at_q})
        row = {
            "station_id": station, "event_date": day, "stage": stage, "settled_bucket": y,
            "delta_s": delta_s, "divergence_kl": d,
            "proper_return_per_lambda": (delta_s + d) if d is not None else None,
            "lambda_shares": lam, "refusal": refusal,
            "stake_usdc": stake, "realised_usdc": realised,
            "frictionless_usdc": frictionless, "l_usdc": frictionless - realised,
            "legs": legs,
        }
        rows.append(row)
        totals["station_days"] += 1
        totals["delta_s"] += delta_s
        totals["divergence"] += d or 0.0
        if legs:
            totals["traded_station_days"] += 1
            totals["stake_usdc"] += stake
            totals["realised_usdc"] += realised
            totals["frictionless_usdc"] += frictionless
            totals["l_usdc"] += frictionless - realised
    if totals["station_days"]:
        totals["mean_delta_s"] = totals["delta_s"] / totals["station_days"]
        totals["mean_divergence"] = totals["divergence"] / totals["station_days"]
    if totals["stake_usdc"] > 0:
        totals["roi"] = totals["realised_usdc"] / totals["stake_usdc"]
    return {"rows": rows, "totals": totals}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--db", default=str(DEFAULT_TARGET_DB))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    try:
        result = attribute(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    totals = result["totals"]
    print(f"已结算站日 {totals.get('station_days', 0)}，有成交 {totals.get('traded_station_days', 0)}")
    if totals.get("station_days"):
        print(f"平均 ΔS {totals['mean_delta_s']:+.4f}  平均 D {totals['mean_divergence']:+.4f}"
              f"  平均 ΔS+D {totals['mean_delta_s'] + totals['mean_divergence']:+.4f}")
    if totals.get("stake_usdc"):
        print(f"投入 {totals['stake_usdc']:.2f} U  实现 {totals['realised_usdc']:+.2f} U"
              f"  ROI {totals['roi']:+.1%}  按 q 无摩擦 {totals['frictionless_usdc']:+.2f} U"
              f"  L {totals['l_usdc']:+.2f} U")
    for row in result["rows"]:
        print(f"{row['event_date']} {row['station_id']} {row['stage']} 结算 {row['settled_bucket']}:"
              f" ΔS {row['delta_s']:+.3f} D {row['divergence_kl'] or 0:+.3f}"
              f" 投入 {row['stake_usdc']:.2f} 实现 {row['realised_usdc']:+.2f} L {row['l_usdc']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
