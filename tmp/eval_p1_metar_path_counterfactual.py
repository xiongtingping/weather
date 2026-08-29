#!/usr/bin/env python3
"""Read-only: isolate the METAR soft-LR layer from the hard reported-high bound.

Each station-day's *last* trajectory update proof carries, in one row, the
posterior it produced, the strength it ran at, the hard-exclusion mask, and the
id of the P0 root it updated.  That makes an exact counterfactual available:

    hard-bound-only = P0 root distribution x hard mask, renormalised

which is precisely what P1 emits when the layer is dark.  The reconstruction is
self-checked against the ``applied_strength == 0`` proofs, where it must
reproduce the committed posterior bit-for-bit; only then is it applied to the
proofs where the layer carried strength.
"""
from __future__ import annotations

import collections
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.materialization import _anchor_bucket_for_temperature

DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"


def _renormalise(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0.0:
        return {}
    return {bucket: value / total for bucket, value in weights.items()}


def main() -> None:
    conn = sqlite3.connect(DB, uri=True)
    conn.row_factory = sqlite3.Row

    truth: dict[tuple[str, str], float] = {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth order by revision_no"
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]

    intervals: dict[tuple[str, str], str] = {}
    ordinals: dict[tuple[str, str], dict[str, int]] = {}
    for row in conn.execute(
        "select station_id, event_date, bucket_intervals_json, envelope_json"
        " from basket_probability_bridge_history where state='COMMITTED'"
        " order by decision_at_utc"
    ):
        key = (row["station_id"], row["event_date"])
        intervals[key] = row["bucket_intervals_json"]
        ordinals[key] = json.loads(row["envelope_json"])["bucket_ordinals"]

    roots: dict[str, dict[str, float]] = {}
    for row in conn.execute(
        "select checkpoint_id, probabilities_json from basket_probability_envelope"
        " where stage in ('P0','P0-M')"
    ):
        roots[row["checkpoint_id"]] = json.loads(row["probabilities_json"])

    # Last proof per station-day, resolved in SQL so the 74k JSON bodies are
    # never all parsed.
    last_proofs = conn.execute(
        "select p.station_id, p.event_date, p.proof_json, p.decision_at_utc"
        " from basket_probability_update_proof p"
        " join (select station_id, event_date, max(decision_at_utc) as m"
        "       from basket_probability_update_proof group by 1, 2) q"
        " on p.station_id = q.station_id and p.event_date = q.event_date"
        " and p.decision_at_utc = q.m"
    ).fetchall()

    records = []
    selfcheck = {"n": 0, "max_abs_diff": 0.0, "mismatch": 0}
    missing_root = 0
    for row in last_proofs:
        station, day = row["station_id"], row["event_date"]
        settled = truth.get((station, day))
        key = (station, day)
        if settled is None or key not in ordinals:
            continue
        proof = json.loads(row["proof_json"])
        root = roots.get(proof["prediction_root_checkpoint_id"])
        if root is None:
            missing_root += 1
            continue
        order = ordinals[key]
        truth_bucket = _anchor_bucket_for_temperature(
            settled,
            root_lineage={"bucket_intervals_c_json": intervals[key]},
            bucket_ordinals=order,
        )
        if truth_bucket is None:
            continue

        mask = proof["hard_bound_mask_by_bucket"]
        posterior = proof["posterior_probabilities"]
        hard_only = _renormalise({
            bucket: root.get(bucket, 0.0) * mask.get(bucket, 1.0)
            for bucket in root
        })
        if not hard_only:
            continue

        strength = proof.get("applied_strength") or 0.0
        if strength <= 0.0:
            selfcheck["n"] += 1
            diff = max(
                abs(hard_only.get(bucket, 0.0) - posterior.get(bucket, 0.0))
                for bucket in set(hard_only) | set(posterior)
            )
            selfcheck["max_abs_diff"] = max(selfcheck["max_abs_diff"], diff)
            if diff > 1e-9:
                selfcheck["mismatch"] += 1

        def score(dist):
            argmax = max(dist, key=lambda bucket: dist[bucket])
            return {
                "hit": argmax == truth_bucket,
                "dist": abs(order[argmax] - order[truth_bucket]),
                "p_truth": dist.get(truth_bucket, 0.0),
            }

        local_end = datetime.fromisoformat(
            f"{day}T23:59:59+00:00"
        ).replace(tzinfo=ZoneInfo(proof["settlement_timezone"]))
        decided = datetime.fromisoformat(
            row["decision_at_utc"].replace("Z", "+00:00")
        )
        records.append({
            "station": station,
            "day": day,
            "strength": strength,
            "hours_to_local_end": (local_end - decided).total_seconds() / 3600.0,
            "actual": score(posterior),
            "hard_only": score(hard_only),
            "root": score(root),
        })

    print(
        f"自检 (strength=0 应逐位吻合): n={selfcheck['n']}"
        f"  最大偏差={selfcheck['max_abs_diff']:.3e}"
        f"  不吻合={selfcheck['mismatch']}"
    )
    print(f"可评估站日: {len(records)}  (根缺失丢弃 {missing_root})")
    print()

    def report(name, subset):
        if not subset:
            print(f"{name}: 无样本")
            return
        n = len(subset)
        out = [f"{name}: n={n}"]
        for layer, label in (
            ("root", "P0根"),
            ("hard_only", "仅硬下界"),
            ("actual", "P1实际"),
        ):
            hit = sum(r[layer]["hit"] for r in subset) / n
            within = sum(r[layer]["dist"] <= 1 for r in subset) / n
            logp = sum(
                math.log(max(r[layer]["p_truth"], 1e-9)) for r in subset
            ) / n
            out.append(
                f"   {label:8s} 命中={hit:.3f}  ±1桶={within:.3f}  对数分={logp:.3f}"
            )
        print("\n".join(out))

    lit = [r for r in records if r["strength"] > 0]
    dark = [r for r in records if r["strength"] <= 0]
    report("全部", records)
    print()
    report("METAR 软似然层生效 (strength>0)", lit)
    if lit:
        fixed = sum(
            1 for r in lit if not r["hard_only"]["hit"] and r["actual"]["hit"]
        )
        broke = sum(
            1 for r in lit if r["hard_only"]["hit"] and not r["actual"]["hit"]
        )
        moved = sum(
            1 for r in lit
            if r["hard_only"]["dist"] != r["actual"]["dist"]
            or r["hard_only"]["hit"] != r["actual"]["hit"]
        )
        print(
            f"   软层相对硬下界: 移动 argmax {moved} 站日;救回 {fixed},弄坏 {broke}"
        )
    print()
    report("软似然层未生效 (strength=0)", dark)

    print()
    hours = sorted(r["hours_to_local_end"] for r in records)
    if hours:
        mid = hours[len(hours) // 2]
        print(
            f"最后一次 P1 更新距当地日终: 中位 {mid:.1f}h"
            f"  (四分位 {hours[len(hours) // 4]:.1f}h / "
            f"{hours[3 * len(hours) // 4]:.1f}h)"
        )
    print()
    print("按距日终时长分档 (生效组):")
    buckets = collections.defaultdict(list)
    for record in lit:
        gap = record["hours_to_local_end"]
        label = (
            "<2h" if gap < 2 else "2-6h" if gap < 6
            else "6-12h" if gap < 12 else ">=12h"
        )
        buckets[label].append(record)
    for label in ("<2h", "2-6h", "6-12h", ">=12h"):
        subset = buckets.get(label)
        if not subset:
            continue
        n = len(subset)
        actual = sum(r["actual"]["hit"] for r in subset) / n
        hard = sum(r["hard_only"]["hit"] for r in subset) / n
        print(f"  {label:6s} n={n:3d}  仅硬下界={hard:.3f} → P1实际={actual:.3f}")


if __name__ == "__main__":
    main()
