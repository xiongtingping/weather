"""P1, the member-trajectory Bayes, the D0 observation model and the
deterministic forecasts: mode-bucket hit rate at local hours and the lead by
which each locks onto the settled bucket (user 2026-10-18 「分别统计：p1、成员轨迹
贝叶斯、D0 实况模型、确定性预报的命中率和提前量对比」).

Each source is read as of each instant, from what it stored then:
* P1: committed P1 envelopes (mode of the vector);
* 成员轨迹贝叶斯: data/board_readings/member_bayes_history (post_mode);
* D0: data/board_readings/d0_history (mode);
* 确定性预报: median of the six F systems' raw daily maximum, each the newest
  run knowable by then (calibration_regional_point_run), its bucket.
Hit at local hour h = the newest reading at or before h:30 names the settled
bucket.  Lock = the first reading from which every later reading of that
source on the day names the settled bucket; arrival = the first P1 decision
whose reported high sits in the settled bucket; lead = arrival − lock (h).

    python3 docs/research/four_estimators_hit_lead_20261018/compare.py [since] [through]
"""
from __future__ import annotations

import glob
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.deterministic_forecast_family import F_SYSTEM_IDS  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
HOURS = (8, 10, 12, 14, 16)
SOURCES = ("P1", "成员轨迹贝叶斯", "D0", "确定性预报")


def utc(t):
    d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def bucket_of(iv, v):
    for b, (lo, hi) in iv.items():
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b
    return None


def main(since, through):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    labels = {(str(k[0]), str(k[1])): str(v["bucket"]) for k, v in knowable_labels(conn).items()
              if since <= str(k[1]) <= through}
    series = defaultdict(lambda: defaultdict(list))  # (st, day) -> source -> [(t, mode)]
    meta = {}
    arrival = {}
    for st, day, at, probs, iv, tz, high in conn.execute(
            "SELECT station_id, event_date, decision_at_utc, probabilities_json,"
            " json_extract(lineage_json,'$.bucket_intervals_c_json'), json_extract(lineage_json,'$.settlement_timezone'),"
            " json_extract(lineage_json,'$.reported_high_c')"
            " FROM basket_probability_envelope WHERE stage='P1' AND state='COMMITTED' AND event_date BETWEEN ? AND ?"
            " ORDER BY decision_at_utc", (since, through)):
        key = (str(st), str(day))
        if key not in labels or not iv:
            continue
        p = json.loads(probs)
        if key not in meta:
            meta[key] = (json.loads(iv), tz)
        series[key]["P1"].append((utc(at), max(p, key=p.get)))
        if high is not None and key not in arrival and bucket_of(meta[key][0], float(high)) == labels[key]:
            arrival[key] = utc(at)
    for name, field, path in (("成员轨迹贝叶斯", "post_mode", "member_bayes_history"), ("D0", "mode", "d0_history")):
        for f in glob.glob(str(ROOT / "data" / "board_readings" / path / "*.jsonl")):
            for line in open(f):
                h = json.loads(line)
                key = (h["station"], h["event_date"])
                if key in meta and h.get(field):
                    series[key][name].append((utc(h["as_of_utc"]), h[field]))
    marks = ",".join("?" * len(F_SYSTEM_IDS))
    for key in meta:
        runs = defaultdict(list)
        for sysid, knowable, v in conn.execute(
                "SELECT ensemble_system_id, MIN(knowable_at_utc), forecast_tmax_c FROM calibration_regional_point_run"
                f" WHERE station_id=? AND target_local_date=? AND ensemble_system_id IN ({marks})"
                " AND knowable_at_utc IS NOT NULL AND forecast_tmax_c IS NOT NULL GROUP BY ensemble_system_id, run_init_utc",
                (*key, *F_SYSTEM_IDS)):
            runs[sysid].append((utc(knowable), float(v)))
        events = sorted({t for rs in runs.values() for t, _ in rs})
        for t in events:
            latest = [max((r for r in rs if r[0] <= t), key=lambda r: r[0])[1] for rs in runs.values() if any(r[0] <= t for r in rs)]
            if len(latest) >= 2:
                b = bucket_of(meta[key][0], statistics.median(latest))
                if b:
                    series[key]["确定性预报"].append((t, b))
    for key in series:
        for s in series[key]:
            series[key][s].sort()

    def evaluate(keys, title):
        print(f"\n== {title}：{len(keys)} 个站日，事件日 {min(k[1] for k in keys)}..{max(k[1] for k in keys)}"
              f"（{len({k[1] for k in keys})} 天）")
        print("来源 | " + " | ".join(f"{h}:00 命中" for h in HOURS) + " | 提前锁定 | 提前≥1h | 提前≥3h | 提前量中位 | 锁定站日")
        for s in SOURCES:
            hits = {h: [] for h in HOURS}
            leads = []
            for key in keys:
                truth = labels[key]
                iv, tz = meta[key]
                zone = ZoneInfo(tz)
                d = datetime.fromisoformat(key[1])
                seq = series[key][s]
                for h in HOURS:
                    cut = datetime(d.year, d.month, d.day, h, 30, tzinfo=zone).astimezone(timezone.utc)
                    prior = [m for t, m in seq if t <= cut]
                    if prior:
                        hits[h].append(prior[-1] == truth)
                end = datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(timezone.utc) + timedelta(days=1)
                day_seq = [(t, m) for t, m in seq if t < end]
                if not day_seq:
                    continue
                lock = None
                for t, m in reversed(day_seq):
                    if m != truth:
                        break
                    lock = t
                if lock is not None and key in arrival:
                    leads.append((arrival[key] - lock).total_seconds() / 3600.0)
                elif key in arrival:
                    leads.append(None)
            cells = [f"{100 * sum(v) / len(v):.0f}% ({len(v)})" if v else "—" for v in (hits[h] for h in HOURS)]
            locked = [x for x in leads if x is not None]
            n = len(leads)
            pos = lambda c: f"{100 * sum(1 for x in locked if x > c) / n:.0f}%" if n else "—"
            med = f"{statistics.median(locked):+.1f}h" if locked else "—"
            print(f"{s} | " + " | ".join(cells) + f" | {pos(0)} | {pos(1)} | {pos(3)} | {med} | {len(locked)}/{n}")

    common = [k for k in meta if all(series[k][s] for s in SOURCES)]
    if common:
        evaluate(common, "四方同场（四个来源都有读数的站日）")
    two = [k for k in meta if series[k]["P1"] and series[k]["确定性预报"]]
    evaluate(two, "P1 对确定性预报（长窗口）")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-09-12", sys.argv[2] if len(sys.argv) > 2 else "2026-10-18")
