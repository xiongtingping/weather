#!/usr/bin/env python3
"""How often did a live envelope give the settled truth bucket exactly zero?

Every committed P0 / P0-M / P1 envelope of every settled station-day, read
per station through the recency index.  The truth bucket is the envelope's
own ``bucket_intervals_c_json`` rung holding the latest accepted truth
revision (lower edge inclusive).  P0 rows are kept (ladder order, truth rung)
for the tail study.

    python3 zero_stats.py [--stations A,B]
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SSOT = HERE.parents[2] / "data" / "bayes_basket_target_ssot.db"
STAGES = ("P0", "P0-M", "P1")
TINY = (0.0, 1e-6, 1e-4)


def _intervals(lineage):
    raw = lineage.get("bucket_intervals_c_json")
    return json.loads(raw) if isinstance(raw, str) else raw


def _truth_rung(intervals, value):
    for key, (lo, hi) in intervals.items():
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return key
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stations")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    zones = dict(conn.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"))
    stations = args.stations.split(",") if args.stations else sorted(zones)
    counts = defaultdict(Counter)          # (stage, version) -> counters
    day_any = defaultdict(set)             # stage -> station-days with a zero
    day_all = defaultdict(set)             # stage -> station-days scored
    last_zero = Counter(); last_n = Counter()
    p1_class = Counter()
    zero_examples = []
    p0_rows = []
    for st in stations:
        truth = {}
        for day, value, rev in conn.execute(
                "SELECT local_date, official_tmax_c, revision_no FROM calibration_station_daily_high_truth "
                "WHERE station_id=? AND qc_status='ACCEPTED'", (st,)):
            if day not in truth or rev > truth[day][1]:
                truth[day] = (float(value), int(rev))
        zone = ZoneInfo(zones[st])
        p0_zero_times = defaultdict(list)  # event_date -> [(decision, zero?)] for P1 attribution
        for stage in STAGES:
            by_day = defaultdict(list)
            for day, at, probs, lineage, version in conn.execute(
                    "SELECT event_date, decision_at_utc, probabilities_json, lineage_json, posterior_version "
                    "FROM basket_probability_envelope INDEXED BY basket_probability_envelope_station_stage_recency_idx "
                    "WHERE station_id=? AND stage=? AND state='COMMITTED'", (st, stage)):
                if day not in truth:
                    continue
                intervals = _intervals(json.loads(lineage))
                if not intervals:
                    counts[(stage, "NO_INTERVALS")]["n"] += 1
                    continue
                rung = _truth_rung(intervals, truth[day][0])
                p = json.loads(probs)
                if rung is None or rung not in p:
                    counts[(stage, "NO_RUNG")]["n"] += 1
                    continue
                pt = float(p[rung])
                c = counts[(stage, version)]
                c["n"] += 1
                for t in TINY:
                    if pt <= t:
                        c[f"le_{t:g}"] += 1
                by_day[day].append((at, pt))
                day_all[stage].add((st, day))
                if pt <= 0.0:
                    day_any[stage].add((st, day))
                    if len(zero_examples) < 4000:
                        zero_examples.append({"station": st, "stage": stage, "event_date": day, "at": at, "version": version,
                                              "truth_c": truth[day][0], "rung": rung,
                                              "mode": max(p, key=p.get), "p_mode": round(max(p.values()), 4)})
                if stage == "P0":
                    order = sorted(intervals, key=lambda k: (float("-inf") if intervals[k][0] is None else intervals[k][0]))
                    p0_rows.append({"station": st, "tz": zones[st], "event_date": day, "at": at, "version": version,
                                    "keys": order, "p": [float(p.get(k, 0.0)) for k in order],
                                    "lo": [intervals[k][0] for k in order], "hi": [intervals[k][1] for k in order],
                                    "truth_c": truth[day][0], "truth_index": order.index(rung)})
                    p0_zero_times[day].append((at, pt <= 0.0))
                if stage == "P1" and pt <= 0.0:
                    prior = [z for (t0, z) in sorted(p0_zero_times.get(day, [])) if t0 <= at]
                    p1_class["root_P0_zero_too" if prior and prior[-1] else "P1_own_zero"] += 1
            for day, recs in by_day.items():
                end = (datetime.fromisoformat(day).replace(tzinfo=zone) + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
                before = [r for r in recs if datetime.fromisoformat(r[0].replace("Z", "+00:00")) < end]
                if before:
                    last = max(before)
                    last_n[stage] += 1
                    last_zero[stage] += last[1] <= 0.0
        print(st, flush=True)
    out = {
        "by_stage_version": {f"{s}|{v}": dict(c) for (s, v), c in sorted(counts.items())},
        "station_days": {s: len(day_all[s]) for s in STAGES},
        "station_days_with_a_zero": {s: len(day_any[s]) for s in STAGES},
        "last_envelope_before_day_end": {s: {"n": last_n[s], "zero": last_zero[s]} for s in STAGES},
        "p1_zero_attribution": dict(p1_class),
        "zero_examples": zero_examples,
    }
    (HERE / "zero_stats.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    with gzip.open(HERE / "p0_envelopes.jsonl.gz", "wt") as handle:
        for row in p0_rows:
            handle.write(json.dumps(row) + "\n")
    for s in STAGES:
        tot = Counter()
        for (stage, _v), c in counts.items():
            if stage == s:
                tot.update(c)
        print(f"{s}: envelopes {tot['n']}, P(truth)=0 {tot['le_0']} ({tot['le_0'] / max(tot['n'], 1):.2%}), <=1e-6 {tot['le_1e-06']}, <=1e-4 {tot['le_0.0001']} | "
              f"station-days {len(day_all[s])}, with a zero {len(day_any[s])} | last-before-day-end zero {last_zero[s]}/{last_n[s]}")
    print("P1 zero attribution:", dict(p1_class))


if __name__ == "__main__":
    main()
