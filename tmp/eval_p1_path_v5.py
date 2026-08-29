#!/usr/bin/env python3
"""Read-only: P1 METAR temperature-path accuracy, v5-physical era included.

Two selections per settled station-day:
  final    = last trajectory proof of the day (end-of-day accuracy)
  pre-peak = last proof whose own running_high_c was still below the settled
             Tmax (the day's high had not yet been observed) — the "early
             prophet" test the 2026-08-21 ruling demands.

For each selected proof three layers are scored on the same row:
  root      = the P0/P0-M root distribution the proof updated
  hard_only = root x hard mask, renormalised (what P1 emits when dark)
  P1        = the committed posterior

Bucket geometry and temperature->bucket come from production
(_anchor_bucket_for_temperature + the bridge row's own intervals).  The
hard_only reconstruction is self-checked bit-for-bit on strength=0 proofs.
Method split comes from activation_id ('-physical-' marks the v5 method).
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.materialization import _anchor_bucket_for_temperature

DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"


def _renorm(weights):
    total = sum(weights.values())
    if total <= 0.0:
        return {}
    return {bucket: value / total for bucket, value in weights.items()}


def main() -> None:
    conn = sqlite3.connect(DB, uri=True)
    conn.row_factory = sqlite3.Row

    truth = {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth order by revision_no"
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]

    conn.execute(
        "create temp table settled (station text, day text, tmax real,"
        " primary key (station, day))"
    )
    conn.executemany(
        "insert into settled values (?, ?, ?)",
        [(s, d, t) for (s, d), t in truth.items()],
    )

    intervals, ordinals = {}, {}
    for row in conn.execute(
        "select station_id, event_date, bucket_intervals_json, envelope_json"
        " from basket_probability_bridge_history where state='COMMITTED'"
        " order by decision_at_utc"
    ):
        key = (row["station_id"], row["event_date"])
        intervals[key] = row["bucket_intervals_json"]
        ordinals[key] = json.loads(row["envelope_json"])["bucket_ordinals"]

    roots = {}
    for row in conn.execute(
        "select checkpoint_id, probabilities_json from basket_probability_envelope"
        " where stage in ('P0','P0-M')"
    ):
        roots[row["checkpoint_id"]] = json.loads(row["probabilities_json"])

    def pick(prepeak: bool):
        cond = (
            " and cast(json_extract(p2.proof_json,'$.running_high_c') as real)"
            " < t.tmax - 0.05" if prepeak else ""
        )
        return conn.execute(
            "select p.station_id, p.event_date, p.proof_json, p.decision_at_utc,"
            " p.activation_id_or_none as act"
            " from basket_probability_update_proof p"
            " join (select p2.station_id s, p2.event_date d,"
            "       max(p2.decision_at_utc) m"
            "       from basket_probability_update_proof p2"
            "       join settled t on t.station=p2.station_id"
            "        and t.day=p2.event_date"
            "       where p2.proof_kind='METAR_TRAJECTORY_UPDATE'" + cond +
            "       group by 1,2) q"
            " on p.station_id=q.s and p.event_date=q.d and p.decision_at_utc=q.m"
            " where p.proof_kind='METAR_TRAJECTORY_UPDATE'"
        ).fetchall()

    selfcheck = {"n": 0, "max_abs_diff": 0.0, "mismatch": 0}

    def build(rows, run_selfcheck=False):
        records, missing_root = [], 0
        for row in rows:
            station, day = row["station_id"], row["event_date"]
            key = (station, day)
            settled = truth.get(key)
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
            hard_only = _renorm({
                bucket: root.get(bucket, 0.0) * mask.get(bucket, 1.0)
                for bucket in root
            })
            if not hard_only:
                continue
            strength = proof.get("applied_strength") or 0.0
            if run_selfcheck and strength <= 0.0:
                selfcheck["n"] += 1
                diff = max(
                    abs(hard_only.get(b, 0.0) - posterior.get(b, 0.0))
                    for b in set(hard_only) | set(posterior)
                )
                selfcheck["max_abs_diff"] = max(selfcheck["max_abs_diff"], diff)
                if diff > 1e-9:
                    selfcheck["mismatch"] += 1

            def score(dist):
                argmax = max(dist, key=lambda b: dist[b])
                return {
                    "hit": argmax == truth_bucket,
                    "dist": abs(order[argmax] - order[truth_bucket]),
                    "signed": order[argmax] - order[truth_bucket],
                    "p_truth": dist.get(truth_bucket, 0.0),
                }

            act = row["act"] or "NONE"
            method = (
                "dark" if strength <= 0.0
                else "physical" if "-physical-" in act
                else "curve"
            )
            records.append({
                "station": station,
                "day": day,
                "strength": strength,
                "method": method,
                "margin": settled - (proof.get("running_high_c") or -99.0),
                "root": score(root),
                "hard_only": score(hard_only),
                "p1": score(posterior),
            })
        return records, missing_root

    def report(name, subset):
        if not subset:
            print(f"{name}: 无样本")
            return
        n = len(subset)
        out = [f"{name}: n={n}"]
        for layer, label in (
            ("root", "P0根"), ("hard_only", "仅硬下界"), ("p1", "P1实际"),
        ):
            hit = sum(r[layer]["hit"] for r in subset) / n
            within = sum(r[layer]["dist"] <= 1 for r in subset) / n
            logp = sum(
                math.log(max(r[layer]["p_truth"], 1e-9)) for r in subset
            ) / n
            out.append(
                f"   {label:8s} 命中={hit:.3f}  ±1桶={within:.3f}  对数分={logp:.3f}"
            )
        fixed = sum(1 for r in subset if not r["hard_only"]["hit"] and r["p1"]["hit"])
        broke = sum(1 for r in subset if r["hard_only"]["hit"] and not r["p1"]["hit"])
        signed = sum(r["p1"]["signed"] for r in subset) / n
        signed_h = sum(r["hard_only"]["signed"] for r in subset) / n
        out.append(
            f"   软层 vs 硬下界: 救回 {fixed}, 弄坏 {broke}"
            f" | argmax带符号偏移(桶): 硬={signed_h:+.2f} P1={signed:+.2f}"
        )
        print("\n".join(out))

    for prepeak, title in ((False, "== 日终最后一条 proof =="),
                           (True, "== 峰值前最后一条 proof (running_high 仍低于结算真值) ==")):
        rows = pick(prepeak)
        records, missing_root = build(rows, run_selfcheck=not prepeak)
        print(title)
        print(f"可评估站日 {len(records)} (根缺失丢弃 {missing_root})")
        report("全部", records)
        for method, label in (("physical", "v5物理法生效"),
                              ("curve", "旧曲线法生效"),
                              ("dark", "层未生效 strength=0")):
            report(label, [r for r in records if r["method"] == method])
        if prepeak:
            print("-- 按距峰余量分档 (全部方法) --")
            for lo, hi, label in ((0.05, 1.0, "0-1°C"), (1.0, 2.0, "1-2°C"),
                                  (2.0, 99.0, ">=2°C")):
                report(f"距峰 {label}",
                       [r for r in records if lo <= r["margin"] < hi])
            print("-- 距峰 >=1°C, 按方法 --")
            for method in ("physical", "curve", "dark"):
                report(f"  {method}",
                       [r for r in records
                        if r["method"] == method and r["margin"] >= 1.0])
        print()

    print(
        f"自检 (strength=0 应逐位吻合): n={selfcheck['n']}"
        f"  最大偏差={selfcheck['max_abs_diff']:.3e}"
        f"  不吻合={selfcheck['mismatch']}"
    )


if __name__ == "__main__":
    main()
