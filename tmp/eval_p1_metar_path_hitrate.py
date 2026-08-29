#!/usr/bin/env python3
"""Read-only: end-to-end hit rate of the P1 METAR temperature path.

Pairs each station-day's final committed P1 envelope against the settled
truth, and against the same station-day's final P0 envelope, split by whether
the METAR trajectory layer actually carried strength that day.

Bucket geometry and the temperature->bucket rule come from production
(`_anchor_bucket_for_temperature` + the bridge row's own intervals), not from
a local reimplementation.
"""
from __future__ import annotations

import collections
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.materialization import _anchor_bucket_for_temperature

DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"


def main() -> None:
    conn = sqlite3.connect(DB, uri=True)
    conn.row_factory = sqlite3.Row

    truth: dict[tuple[str, str], float] = {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth order by revision_no"
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]

    strength: dict[tuple[str, str], float] = collections.defaultdict(float)
    for row in conn.execute(
        "select station_id, event_date,"
        " max(json_extract(proof_json, '$.applied_strength')) as s"
        " from basket_probability_update_proof group by 1, 2"
    ):
        strength[(row["station_id"], row["event_date"])] = row["s"] or 0.0

    last: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for row in conn.execute(
        "select station_id, event_date, stage, decision_at_utc,"
        " bucket_intervals_json, envelope_json"
        " from basket_probability_bridge_history where state='COMMITTED'"
        " order by decision_at_utc"
    ):
        last[(row["station_id"], row["event_date"], row["stage"])] = (
            row["bucket_intervals_json"],
            row["envelope_json"],
            row["decision_at_utc"],
        )

    def evaluate(row, tval):
        intervals_json, envelope_json, decided = row
        envelope = json.loads(envelope_json)
        probs = envelope["probabilities"]
        ordinals = envelope["bucket_ordinals"]
        truth_bucket = _anchor_bucket_for_temperature(
            tval,
            root_lineage={"bucket_intervals_c_json": intervals_json},
            bucket_ordinals=ordinals,
        )
        if truth_bucket is None:
            return None
        argmax = max(probs, key=lambda bucket: probs[bucket])
        return {
            "argmax": argmax,
            "truth_bucket": truth_bucket,
            "hit": argmax == truth_bucket,
            "dist": abs(ordinals[argmax] - ordinals[truth_bucket]),
            "p_truth": probs.get(truth_bucket, 0.0),
            "decided_at": decided,
        }

    records = []
    ungeometried = 0
    for (station, day, stage), row in last.items():
        if stage != "P1":
            continue
        settled = truth.get((station, day))
        if settled is None:
            continue
        p1 = evaluate(row, settled)
        if p1 is None:
            ungeometried += 1
            continue
        p0_row = last.get((station, day, "P0"))
        records.append({
            "station": station,
            "day": day,
            "strength": strength.get((station, day), 0.0),
            "p1": p1,
            "p0": evaluate(p0_row, settled) if p0_row else None,
        })

    print(f"可评估站日: {len(records)}  (无桶几何丢弃 {ungeometried})")
    print(f"其中有配对 P0: {sum(1 for r in records if r['p0'])}")
    print()

    def report(name, subset):
        if not subset:
            print(f"{name}: 无样本")
            return
        n = len(subset)
        hit = sum(r["p1"]["hit"] for r in subset)
        within = sum(r["p1"]["dist"] <= 1 for r in subset)
        logp = sum(
            math.log(max(r["p1"]["p_truth"], 1e-9)) for r in subset
        ) / n
        stderr = math.sqrt(max(hit / n * (1 - hit / n), 1e-9) / n)
        print(
            f"{name}: n={n}  argmax命中={hit / n:.3f}±{stderr:.3f} ({hit})"
            f"  ±1桶={within / n:.3f}  真值桶对数分={logp:.3f}"
        )
        paired = [r for r in subset if r["p0"]]
        if paired:
            m = len(paired)
            h0 = sum(r["p0"]["hit"] for r in paired)
            h1 = sum(r["p1"]["hit"] for r in paired)
            w0 = sum(r["p0"]["dist"] <= 1 for r in paired)
            w1 = sum(r["p1"]["dist"] <= 1 for r in paired)
            l0 = sum(math.log(max(r["p0"]["p_truth"], 1e-9)) for r in paired) / m
            l1 = sum(math.log(max(r["p1"]["p_truth"], 1e-9)) for r in paired) / m
            moved = sum(1 for r in paired if r["p0"]["argmax"] != r["p1"]["argmax"])
            fixed = sum(
                1 for r in paired if not r["p0"]["hit"] and r["p1"]["hit"]
            )
            broke = sum(
                1 for r in paired if r["p0"]["hit"] and not r["p1"]["hit"]
            )
            print(
                f"   配对 P0 (n={m}): 命中 {h0 / m:.3f} → {h1 / m:.3f}"
                f" | ±1桶 {w0 / m:.3f} → {w1 / m:.3f}"
                f" | 对数分 {l0:.3f} → {l1:.3f}"
            )
            print(
                f"   argmax 被移动 {moved} 站日;救回 {fixed},弄坏 {broke}"
            )

    report("全部", records)
    report("METAR 路径生效 (strength>0)", [r for r in records if r["strength"] > 0])
    report("未生效 (strength=0)", [r for r in records if r["strength"] <= 0])

    print()
    print("按强度分档:")
    for lo, hi in ((0.0, 0.0), (0.0, 0.25), (0.25, 0.4), (0.4, 1.0)):
        if lo == hi == 0.0:
            subset = [r for r in records if r["strength"] <= 0]
            label = "  =0     "
        else:
            subset = [
                r for r in records if lo < r["strength"] <= hi
            ]
            label = f"  {lo:.2f}-{hi:.2f}"
        if not subset:
            continue
        n = len(subset)
        hit = sum(r["p1"]["hit"] for r in subset)
        print(f"{label}: n={n:4d}  命中={hit / n:.3f}")


if __name__ == "__main__":
    main()
