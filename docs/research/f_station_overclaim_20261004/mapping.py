#!/usr/bin/env python3
"""PROTOCOL.md, part C: do the F stations' settled bucket, truth temperature
and envelope intervals agree?  Read-only, short reads."""
import json
import os
import sqlite3
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
US = ("KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO")

conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30, isolation_level=None)
conn.execute("PRAGMA query_only=1")
out = {"c1": {}, "c2": {}, "c3": {}}
inside = outside = missing = 0
frac = Counter()
bad = []
for st in US:
    labels = conn.execute(
        "SELECT event_date, settled_bucket, settled_tmax_c FROM basket_station_day_settlement_label l"
        " WHERE station_id=? AND truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x"
        "   WHERE x.station_id=l.station_id AND x.event_date=l.event_date) ORDER BY event_date", (st,)).fetchall()
    for day, bucket, tmax_c in labels:
        row = conn.execute(
            "SELECT registry_json FROM basket_target_market_registry WHERE station_id=? AND event_date=? AND outcome=?"
            " AND registry_json IS NOT NULL ORDER BY refreshed_at DESC LIMIT 1", (st, day, bucket)).fetchone()
        if row is None:
            missing += 1
            continue
        reg = json.loads(row[0])
        f = float(tmax_c) * 9.0 / 5.0 + 32.0
        frac[round(abs(f - round(f)), 2)] += 1
        whole = round(f)
        lo, hi = reg.get("lower"), reg.get("upper")
        ok = (lo is None or whole >= float(lo)) and (hi is None or whole <= float(hi))
        inside += ok
        outside += not ok
        if not ok:
            bad.append((st, day, bucket, tmax_c, round(f, 2)))
print(f"C1 结算温度在结算桶区间内：{inside}，不在：{outside}，登记表查不到：{missing}")
for b in bad[:12]:
    print("   不在区间：", b)
print("C2 换回 °F 后离最近整数的距离（次数）：", dict(sorted(frac.items())))
out["c1"] = {"inside": inside, "outside": outside, "missing": missing, "examples": bad[:20]}
out["c2"] = {str(k): v for k, v in sorted(frac.items())}

checked = mismatched = 0
worst = 0.0
examples = []
for st in US:
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT event_date FROM basket_policy_training_fact WHERE station_id=? AND stage='P0' ORDER BY event_date DESC LIMIT 5", (st,))]
    for day in days:
        eid = conn.execute(
            "SELECT probability_envelope_id FROM basket_policy_training_fact WHERE station_id=? AND event_date=? AND stage='P0'"
            " ORDER BY decision_at_utc LIMIT 1", (st, day)).fetchone()[0]
        lineage = json.loads(conn.execute("SELECT lineage_json FROM basket_probability_envelope WHERE envelope_id=?", (eid,)).fetchone()[0])
        intervals = json.loads(lineage["bucket_intervals_c_json"]) if isinstance(lineage.get("bucket_intervals_c_json"), str) else lineage["bucket_intervals_c_json"]
        for bucket, (lo_c, hi_c) in intervals.items():
            row = conn.execute(
                "SELECT registry_json FROM basket_target_market_registry WHERE station_id=? AND event_date=? AND outcome=?"
                " AND registry_json IS NOT NULL ORDER BY refreshed_at DESC LIMIT 1", (st, day, bucket)).fetchone()
            if row is None:
                continue
            reg = json.loads(row[0])
            want_lo = None if reg.get("lower") is None else (float(reg["lower"]) - 0.5 - 32.0) / 1.8
            want_hi = None if reg.get("upper") is None else (float(reg["upper"]) + 0.5 - 32.0) / 1.8
            checked += 1
            diff = max(abs((lo_c if lo_c is not None else 0.0) - (want_lo if want_lo is not None else 0.0)) if (lo_c is None) == (want_lo is None) else 9.0,
                       abs((hi_c if hi_c is not None else 0.0) - (want_hi if want_hi is not None else 0.0)) if (hi_c is None) == (want_hi is None) else 9.0)
            worst = max(worst, diff)
            if diff > 0.02:
                mismatched += 1
                if len(examples) < 8:
                    examples.append((st, day, bucket, [lo_c, hi_c], [None if want_lo is None else round(want_lo, 3), None if want_hi is None else round(want_hi, 3)]))
print(f"C3 信封桶区间对登记表（[下界−0.5°F, 上界+0.5°F] 换成 °C）：核对 {checked} 个桶，不一致 {mismatched}，最大差 {worst:.4f}°C")
for e in examples:
    print("   ", e)
out["c3"] = {"checked": checked, "mismatched": mismatched, "worst_c": worst, "examples": examples}
json.dump(out, open(os.path.join(HERE, "results_mapping.json"), "w"), ensure_ascii=False, indent=1, default=str)
