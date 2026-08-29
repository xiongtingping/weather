#!/usr/bin/env python3
"""Read-only: score the P1 envelope actually STANDING at fixed local times.

The prefix-replay chain stamps intermediate steps with late decision clocks,
so selecting "last proof whose running_high was still pre-peak" picks
microsecond-lived replay transients, not what production served.  The fair
early-prophet test is: at local noon (and 15:00), which committed P1 had the
latest decision_at_utc <= that instant?  That posterior is what the ledger
could trade on.  Scored against the same instant's root and hard-only
reconstruction, split by likelihood method.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

    timezones = {}
    for row in conn.execute(
        "select distinct station_id, event_date, settlement_timezone"
        " from basket_probability_update_proof"
        " where proof_kind='METAR_TRAJECTORY_UPDATE'"
    ):
        timezones[(row["station_id"], row["event_date"])] = (
            row["settlement_timezone"]
        )

    conn.execute(
        "create temp table cutoffs (station text, day text, label text,"
        " cutoff_utc text, primary key (station, day, label))"
    )
    rows = []
    for (station, day), tz_name in timezones.items():
        if (station, day) not in truth:
            continue
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            continue
        base = datetime.fromisoformat(day)
        for label, hour in (("noon", 12), ("15h", 15)):
            local = base.replace(hour=hour, tzinfo=tz)
            rows.append((
                station, day, label,
                local.astimezone(ZoneInfo("UTC")).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            ))
    conn.executemany("insert into cutoffs values (?,?,?,?)", rows)

    picked = conn.execute(
        "select p.station_id, p.event_date, c.label, p.proof_json,"
        " p.decision_at_utc, p.activation_id_or_none as act"
        " from basket_probability_update_proof p"
        " join (select p2.station_id s, p2.event_date d, c2.label l,"
        "       max(p2.decision_at_utc) m"
        "       from basket_probability_update_proof p2"
        "       join cutoffs c2 on c2.station=p2.station_id"
        "        and c2.day=p2.event_date"
        "       where p2.proof_kind='METAR_TRAJECTORY_UPDATE'"
        "        and p2.decision_at_utc <= c2.cutoff_utc"
        "       group by 1,2,3) q"
        " on p.station_id=q.s and p.event_date=q.d and p.decision_at_utc=q.m"
        " join cutoffs c on c.station=p.station_id and c.day=p.event_date"
        "  and c.label=q.l"
        " where p.proof_kind='METAR_TRAJECTORY_UPDATE'"
    ).fetchall()

    records = {"noon": [], "15h": []}
    for row in picked:
        station, day = row["station_id"], row["event_date"]
        key = (station, day)
        settled = truth.get(key)
        if settled is None or key not in ordinals:
            continue
        proof = json.loads(row["proof_json"])
        root = roots.get(proof["prediction_root_checkpoint_id"])
        if root is None:
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
        act = row["act"] or "NONE"
        method = (
            "dark" if strength <= 0.0
            else "physical" if "-physical-" in act
            else "curve"
        )

        def score(dist):
            argmax = max(dist, key=lambda b: dist[b])
            return {
                "hit": argmax == truth_bucket,
                "dist": abs(order[argmax] - order[truth_bucket]),
                "signed": order[argmax] - order[truth_bucket],
                "p_truth": dist.get(truth_bucket, 0.0),
                "maxp": dist[argmax],
            }

        records[row["label"]].append({
            "station": station,
            "day": day,
            "method": method,
            "margin": settled - (proof.get("running_high_c") or -99.0),
            "root": score(root),
            "hard_only": score(hard_only),
            "p1": score(posterior),
        })

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
            signed = sum(r[layer]["signed"] for r in subset) / n
            out.append(
                f"   {label:8s} 命中={hit:.3f}  ±1桶={within:.3f}"
                f"  对数分={logp:.3f}  带符号偏移={signed:+.2f}"
            )
        fixed = sum(
            1 for r in subset if not r["hard_only"]["hit"] and r["p1"]["hit"]
        )
        broke = sum(
            1 for r in subset if r["hard_only"]["hit"] and not r["p1"]["hit"]
        )
        collapsed = sum(1 for r in subset if r["p1"]["maxp"] > 0.99)
        out.append(
            f"   救回 {fixed}, 弄坏 {broke}; P1后验塌缩(>0.99) {collapsed} 条"
        )
        print("\n".join(out))

    for label, title in (("noon", "== 当地正午站立的 P1 =="),
                         ("15h", "== 当地 15:00 站立的 P1 ==")):
        subset = records[label]
        print(title)
        # 只评还有升温余地的站日 (正午 running_high 仍低于真值),
        # 与裁决的"峰值前"口径一致
        pre = [r for r in subset if r["margin"] >= 0.05]
        report("全部站立", subset)
        report("其中峰值前 (站立时 running_high < 真值)", pre)
        for method in ("physical", "curve", "dark"):
            report(f"  峰值前·{method}",
                   [r for r in pre if r["method"] == method])
        print()


if __name__ == "__main__":
    main()
