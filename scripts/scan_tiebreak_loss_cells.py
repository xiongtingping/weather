#!/usr/bin/env python3
"""Where does the tiebreak cell LOSE -- and what does the same search find in
noise?

Charter 164 (2026-09-20, user question 「秤砣线亏损的共有特征是什么？」).  The
question invites a search, and a search over this replay manufactures
answers: the module's own docstring measured that a shuffled label produces a
"confirmed in both halves" PROFITABLE cell every single time.  This script is
the mirror of that measurement for the losing side, so a claim about "what
the losses have in common" has to clear the bar its own search sets.

Three things it prints, in order:
  1. a candidate set DECLARED in the source (``CELLS``), never grown while
     reading the output -- the bar rises with the number of cells tried;
  2. the cells whose ROI is negative in BOTH halves of the event days, split
     on the median event day: discovery and confirmation;
  3. the same scan run against labels shuffled INSIDE each event day, which
     holds every day's own difficulty fixed and destroys only the pattern.
     If noise reaches the observed cell as often as not, the cell is noise.

2026-09-20, 450 firing rows / 37 event days, 46 declared cells: two cells
survive both halves (normalised mid >=0.55 at -10.1%, ask depth 20-60 shares
at -9.8%), the shuffled scan produces at least one survivor in 99% of draws,
and its worst survivor is a median -19.3% -- worse than either.  p=0.88.
There is no pre-identifiable feature the losses share.  Read-only.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
from bayes_basket_target.tiebreak_cell_shadow import (  # noqa: E402
    DEFAULT_FLOOR_EVENT_DATE, PRODUCTION_SSOT_DB, _roi, replay_cell,
)

#: Rows a cell must hold in EACH half before it is even looked at: a cell of
#: three rows is not a measurement, which is the whole reason this file exists.
MIN_ROWS_PER_HALF = 8
SHUFFLE_DRAWS = 400
SEED = 164


def _bins(name: str, key: str, cuts: list[float]) -> list[tuple]:
    out, low = [], None
    for cut in cuts:
        out.append((
            f"{name} <{cut}" if low is None else f"{name} {low}–{cut}",
            (lambda k, a, b: (lambda r: r.get(k) is not None
                              and (a is None or r[k] >= a) and r[k] < b))(key, low, cut),
        ))
        low = cut
    out.append((f"{name} >={low}",
                (lambda k, a: (lambda r: r.get(k) is not None and r[k] >= a))(key, low)))
    return out


def candidate_cells() -> list[tuple]:
    """Declared up front.  Adding one here raises the bar for all of them."""

    cells: list[tuple] = []
    cells += [(f"stage {s}", (lambda s: (lambda r: r["stage"] == s))(s))
              for s in ("P0", "P0-M", "P1")]
    cells += [("modes agree (distance 0)", lambda r: r["mode_distance"] == 0),
              ("modes adjacent (distance 1)", lambda r: r["mode_distance"] == 1)]
    cells += _bins("normalised mid", "market_share", [0.45, 0.50, 0.55])
    cells += _bins("ask", "best_ask", [0.45, 0.50, 0.55, 0.60])
    cells += _bins("spread", "spread", [0.015, 0.025])
    cells += _bins("ask minus normalised", "gap", [0.015, 0.03, 0.04])
    cells += _bins("model minus market share", "edge", [-0.05, 0.0, 0.05])
    cells += _bins("ask depth shares", "ask_depth_shares", [20, 60, 150])
    cells += _bins("hours to settlement", "tau", [18, 30, 42])
    cells += _bins("market move", "market_move_on_model_mode", [0.0, 0.03])
    cells += _bins("minutes since previous decision", "prev_gap_minutes", [60, 400])
    cells += [("fahrenheit station", lambda r: r["unit"] == "°F"),
              ("celsius station", lambda r: r["unit"] == "°C")]
    cells += [("model share >=0.5", lambda r: (r["model_share"] or 0) >= 0.5),
              ("model share <0.4", lambda r: (r["model_share"] or 1) < 0.4)]
    cells += [(f"stage {s} x normalised >=0.55",
               (lambda s: (lambda r: r["stage"] == s
                           and (r.get("market_share") or 0) >= 0.55))(s))
              for s in ("P0", "P0-M", "P1")]
    return cells


def derive(rows: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """The axes the replay does not store: hours to settlement by the
    production clock, the gap between what the gate reads and what the money
    pays, the model's own disagreement, and the station's unit."""

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
        zone = zones.get((row["station_id"], row["event_date"]))
        try:
            tau = hours_to_settlement(
                row["event_date"], as_of_utc=row["decision_at_utc"],
                settlement_timezone=zone,
            ) if zone else None
        except Exception:  # noqa: BLE001
            tau = None
        share = row.get("market_share")
        out.append(dict(
            row, tau=tau,
            gap=(row["best_ask"] - share) if share is not None else None,
            edge=((row["model_share"] - share)
                  if (row.get("model_share") is not None and share is not None) else None),
            unit=("°F" if "°F" in row["bucket"] else "°C"),
        ))
    return out


def structure(rows: list[dict], conn: sqlite3.Connection) -> dict:
    """The part of the answer that is NOT a search.

    Two descriptions, both arithmetic rather than a cell: how far the misses
    miss by (read off the envelope's own ordinal map, never a parsed label),
    and the hit rate against the price actually paid.  A loss here is a bucket
    that did not land, so 46% of the rows lose by construction; what decides
    the lane is whether the hit rate clears the price, and that is a curve,
    not a feature of the losers.
    """

    envelopes = {
        (str(s), str(d), str(stage)): str(env)
        for s, d, stage, _dec, env in conn.execute(
            "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id"
            " FROM basket_policy_training_fact WHERE event_date>=?"
            " GROUP BY station_id,event_date,stage", (DEFAULT_FLOOR_EVENT_DATE,),
        )
    }
    rungs: dict[int, int] = defaultdict(int)
    warmer = colder = 0
    for row in rows:
        if row["hit"]:
            continue
        env = lane._envelope(conn, envelopes.get(
            (row["station_id"], row["event_date"], row["stage"]), "")) or {}
        ordinals = env.get("ordinals") or {}
        if row["bucket"] not in ordinals or row["settled_bucket"] not in ordinals:
            continue
        delta = ordinals[row["settled_bucket"]] - ordinals[row["bucket"]]
        rungs[abs(delta)] += 1
        if delta > 0:
            warmer += 1
        else:
            colder += 1
    curve = []
    for low, high in ((0.30, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 1.0)):
        sel = [r for r in rows if low <= r["best_ask"] < high]
        if not sel:
            continue
        rate = sum(r["hit"] for r in sel) / len(sel)
        paid = sum(r["best_ask"] for r in sel) / len(sel)
        curve.append({"paid_from": low, "paid_to": high, "n": len(sel),
                      "hit_rate": rate, "avg_paid": paid, "roi": (rate - paid) / paid})
    return {"misses": sum(rungs.values()), "by_rungs": dict(sorted(rungs.items())),
            "settled_warmer": warmer, "settled_colder": colder, "price_curve": curve}


def survivors(first: list[dict], second: list[dict], cells: list[tuple]) -> list[tuple]:
    """Cells negative in both halves, worst first."""

    out = []
    for name, pred in cells:
        a = [r for r in first if pred(r)]
        b = [r for r in second if pred(r)]
        if len(a) < MIN_ROWS_PER_HALF or len(b) < MIN_ROWS_PER_HALF:
            continue
        ra, rb = _roi(a), _roi(b)
        if ra is None or rb is None or ra >= 0 or rb >= 0:
            continue
        out.append((name, len(a) + len(b), _roi(a + b), ra, rb))
    return sorted(out, key=lambda x: x[2])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--floor", default=DEFAULT_FLOOR_EVENT_DATE)
    parser.add_argument("--draws", type=int, default=SHUFFLE_DRAWS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{PRODUCTION_SSOT_DB}?mode=ro", uri=True, timeout=180.0)
    conn.execute("PRAGMA busy_timeout=180000")
    rows = derive(replay_cell(conn, floor_event_date=args.floor), conn)
    cells = candidate_cells()
    days = sorted({r["event_date"] for r in rows})
    cut = days[len(days) // 2]
    first = [r for r in rows if r["event_date"] < cut]
    second = [r for r in rows if r["event_date"] >= cut]
    found = survivors(first, second, cells)
    shape = structure(rows, conn)

    rng = random.Random(SEED)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_day[row["event_date"]].append(row)
    truth = {id(r): r["hit"] for r in rows}
    reached, worst = 0, []
    for _ in range(args.draws):
        for day_rows in by_day.values():
            hits = [r["hit"] for r in day_rows]
            rng.shuffle(hits)
            for row, hit in zip(day_rows, hits):
                row["hit"] = hit
        got = survivors(first, second, cells)
        if got:
            reached += 1
            worst.append(got[0][2])
        for row in rows:
            row["hit"] = truth[id(row)]
    worst.sort()
    observed = found[0][2] if found else None
    result = {
        "schema": "tiebreak_loss_cell_scan_v1",
        "rows": len(rows), "event_days": len(days), "split_event_date": cut,
        "cells_declared": len(cells),
        "survivors": [
            {"cell": n, "n": k, "roi": v, "roi_first_half": a, "roi_second_half": b}
            for n, k, v, a, b in found
        ],
        "shuffled_draws": args.draws,
        "shuffled_any_survivor": reached / args.draws if args.draws else None,
        "shuffled_worst_median": worst[len(worst) // 2] if worst else None,
        "shuffled_worst_p05": worst[int(0.05 * len(worst))] if worst else None,
        "structure": shape,
        "p_observed_reached_by_chance": (
            (sum(1 for w in worst if w <= observed) / len(worst))
            if (worst and observed is not None) else None
        ),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(f"{result['rows']} 行 / {result['event_days']} 事件日，候选格 {len(cells)} 个，"
          f"前后半分界 {cut}")
    print(f"落空 {shape['misses']} 行：差几档 {shape['by_rungs']}，"
          f"结算更热 {shape['settled_warmer']} / 更冷 {shape['settled_colder']}")
    for band in shape["price_curve"]:
        print(f"  付 {band['paid_from']:.2f}–{band['paid_to']:.2f}: n={band['n']:4d}"
              f" 命中={band['hit_rate']:5.1%} 均价={band['avg_paid']:.3f}"
              f" ROI={band['roi']:+7.1%}")
    print("前后半都为负的格：")
    for n, k, v, a, b in found or []:
        print(f"  {n:34} n={k:4d} ROI={v:+7.1%} 前半={a:+7.1%} 后半={b:+7.1%}")
    if not found:
        print("  （无）")
    print(f"日内洗牌 {args.draws} 次：{result['shuffled_any_survivor']:.0%} 的抽样也能翻出"
          f"至少一个，随机最负格中位 {result['shuffled_worst_median']:+.1%}"
          f"（5 分位 {result['shuffled_worst_p05']:+.1%}）")
    if result["p_observed_reached_by_chance"] is not None:
        print(f"实测最负的 {found[0][0]} = {observed:+.1%}，"
              f"随机达到这么负 p={result['p_observed_reached_by_chance']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
