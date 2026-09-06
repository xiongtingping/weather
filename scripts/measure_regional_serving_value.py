#!/usr/bin/env python3
"""What a regional point system is worth where it would actually serve.

The 2026-08-28 admission sweep scored each candidate against the P0 root
at a previous-day lead and passed HRRR and the NBM at most US stations.
Both then measured harmful in P1 serving (charter 99, 2026-09-07), so
those gates do not predict serving value and cannot admit a new lane on
their own.  This is the gate that does: it scores the system where the
layer would actually speak, against the posterior it would actually
displace.

The window is the honest one.  A committed P1 prefix whose ceiling
receipt reads TOO_EARLY was served the posterior no layer touched, so the
served vector IS the candidate layer's input and the two arms differ by
exactly the layer under test -- nothing is reconstructed, inverted or
re-derived from production's rules.  Every event date reads the per-date
as-of curve, fitted only on strictly earlier days.

Reported per system: log loss, Brier, argmax accuracy, within-one-bucket,
and the point-forecast error of the system's own stored daily maximum
against settled truth, beside the incumbent posterior's.  A system that
does not beat the incumbent on the first three has nothing to add to P1
serving, whatever an admission sweep said about it.

    python3 scripts/measure_regional_serving_value.py
    python3 scripts/measure_regional_serving_value.py --system NCEP_NBM_POINT
    python3 scripts/measure_regional_serving_value.py --from 2026-08-24 --through 2026-09-05
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import regional_path_prior as R  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def bucket_of(intervals, value):
    for bucket, (low, high) in intervals.items():
        if (low is None or value >= float(low)) and (high is None or value < float(high)):
            return bucket
    return None


def centre(intervals, bucket, fallback):
    low, high = intervals[bucket]
    if low is None and high is None:
        return fallback
    if low is None:
        return float(high) - 0.5
    if high is None:
        return float(low) + 0.5
    return (float(low) + float(high)) / 2.0


def floored(vec, epsilon=1e-3):
    """Production's outermost layer, applied to every arm alike."""
    lifted = {k: max(v, epsilon) for k, v in vec.items()}
    total = sum(lifted.values())
    return {k: v / total for k, v in lifted.items()}


def score(vec, ordinals, target_bucket):
    p = max(float(vec.get(target_bucket, 0.0)), 1e-12)
    top = max(vec.items(), key=lambda kv: kv[1])[0]
    return (
        -math.log(p),
        sum((v - (1.0 if k == target_bucket else 0.0)) ** 2 for k, v in vec.items()),
        abs(int(ordinals[top]) - int(ordinals[target_bucket])),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--system", default=None,
                        help="only this ensemble_system_id (default: each one separately)")
    parser.add_argument("--from", dest="start", default="2026-08-24")
    parser.add_argument("--through", default="2026-09-05")
    parser.add_argument("--weight", type=float, default=None,
                        help="blend weight to test (default: the module's)")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=300)
    truth = {
        (r[0], r[1]): float(r[2]) for r in conn.execute(
            "SELECT station_id,local_date,official_tmax_c"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND source_lane='AWC'")
    }
    lane = collections.defaultdict(set)
    for station, day in conn.execute(
        "SELECT DISTINCT station_id,target_local_date FROM calibration_regional_point_run"
    ):
        lane[day].add(station)

    systems = [args.system] if args.system else [
        r[0] for r in conn.execute(
            "SELECT DISTINCT ensemble_system_id FROM calibration_regional_point_run ORDER BY 1")
    ]

    runs_cache: dict[tuple, list] = {}
    replay_cache: dict[str, dict] = {}
    acc = collections.defaultdict(lambda: {a: collections.Counter() for a in ("none", "layer")})
    point = collections.defaultdict(list)
    seen = collections.Counter()
    paired = collections.defaultdict(list)

    prefixes = list(conn.execute(
        "SELECT station_id,event_date,decision_at_utc,probabilities_json,"
        " bucket_ordinals_json,lineage_json,settlement_timezone"
        " FROM basket_probability_envelope"
        " WHERE stage='P1' AND event_date BETWEEN ? AND ?",
        (args.start, args.through)))

    for station, day, at, pj, oj, lj, zone in prefixes:
        if station not in lane.get(day, ()):
            continue
        actual = truth.get((station, day))
        if actual is None:
            continue
        lineage = json.loads(lj or "{}")
        receipt = json.loads(lineage.get("metar_ceiling_json") or "{}")
        if receipt.get("metar_ceiling_skip_reason") != "TOO_EARLY":
            continue
        try:
            served = {str(k): float(v) for k, v in json.loads(pj).items()}
            ordinals = {str(k): int(v) for k, v in json.loads(oj).items()}
            intervals = {str(k): (v[0], v[1]) for k, v in
                         json.loads(lineage["bucket_intervals_c_json"]).items()}
            running_c = float(lineage["metar_trajectory_running_high_c"])
        except (KeyError, TypeError, ValueError):
            continue
        running_bucket = bucket_of(intervals, running_c)
        settled_bucket = bucket_of(intervals, actual)
        if running_bucket is None or settled_bucket is None or running_bucket not in ordinals:
            continue
        key = (station, day)
        if key not in runs_cache:
            runs_cache[key] = R.load_runs_for_station_day(
                conn, station_id=station, event_date=day, as_of_utc="2100-01-01T00:00:00Z")
        if day not in replay_cache:
            replay_cache[day] = R.replay_curve(day) or {}
        artifact = replay_cache[day]
        if not artifact:
            continue
        for system in systems:
            rows = [r for r in runs_cache[key]
                    if str(r["ensemble_system_id"]) == system
                    and str(r["knowable_at_utc"]) <= str(at)]
            if not rows:
                continue
            resolved = R.resolve_serving_input(
                rows, station_id=station, event_date=day, as_of_utc=str(at),
                settlement_timezone=str(zone), artifact=artifact)
            if resolved is None:
                continue
            upper = {b: (None if hi is None else float(hi))
                     for b, (lo, hi) in intervals.items()}
            blended, note = R.apply_regional_path_prior(
                served, ordinals=ordinals, upper_bounds_c=upper,
                running_high_bucket=running_bucket, running_high_c=running_c,
                remaining_peak_c=float(resolved["remaining_peak_c"]),
                cell=resolved["cell"], lineage=resolved.get("lineage"),
                weight=args.weight, enabled=True)
            if note.get("regional_path_prior_applied") != "true":
                seen[f"{system}:{note.get('regional_path_prior_skip_reason') or '?'}"] += 1
                continue
            anchor = max(running_c, float(resolved["remaining_peak_c"]))
            incumbent_mean = sum(centre(intervals, b, running_c) * p
                                 for b, p in served.items())
            point[system].append((
                anchor + float(resolved["cell"]["bias_c"]) - actual,
                incumbent_mean - actual,
            ))
            scored = {}
            for arm, vec in (("none", served), ("layer", blended)):
                lg, br, dist = score(floored(vec), ordinals, settled_bucket)
                scored[arm] = lg
                bucketed = acc[system][arm]
                bucketed["logloss"] += lg
                bucketed["brier"] += br
                bucketed["hit"] += 1 if dist == 0 else 0
                bucketed["within1"] += 1 if dist <= 1 else 0
                bucketed["n"] += 1
            # Paired, because both arms score the same prefix: the spread
            # that matters is the spread of the difference, not of either
            # arm.  Clustered by station-day -- prefixes inside one
            # station-day share a truth and a posterior lineage and are
            # nowhere near independent, so the block bootstrap resamples
            # station-days, not rows.
            paired[system].append((key, scored["layer"] - scored["none"]))

    print(f"区域系统的服务价值 · {args.start}..{args.through} · "
          f"窗口＝天花板报 TOO_EARLY 的 P1 前缀（无任何重建）")
    print(f"权重＝{args.weight if args.weight is not None else R.WEIGHT}\n")
    if not acc:
        print("  没有可比前缀：该区间内没有站日同时具备车道 run、结算真值与 TOO_EARLY 回执。")
        return 1
    for system in systems:
        arms = acc.get(system)
        if not arms or not arms["none"]["n"]:
            print(f"  {system}: 无可比前缀 "
                  f"{ {k.split(':',1)[1]: v for k, v in seen.items() if k.startswith(system)} }")
            continue
        n = arms["none"]["n"]
        print(f"  {system}  n={n}")
        print(f"    {'臂':8s} {'对数损失':>9s} {'Brier':>8s} {'命中':>7s} {'±1桶':>7s}")
        for arm, label in (("none", "无层"), ("layer", "接入后")):
            a = arms[arm]
            print(f"    {label:8s} {a['logloss']/n:9.4f} {a['brier']/n:8.4f}"
                  f" {100*a['hit']/n:6.1f}% {100*a['within1']/n:6.1f}%")
        delta = arms["layer"]["logloss"] / n - arms["none"]["logloss"] / n
        hit_delta = 100 * (arms["layer"]["hit"] - arms["none"]["hit"]) / n
        verdict = "有增益，可接" if (delta < 0 and hit_delta > 0) else "无增益，不可接"
        print(f"    Δ对数损失 {delta:+.4f} nat · Δ命中 {hit_delta:+.1f} 点 → {verdict}")
        pairs = paired.get(system) or []
        if pairs:
            by_day = collections.defaultdict(list)
            for k, d in pairs:
                by_day[k].append(d)
            days = sorted(by_day)
            means = [statistics.mean(by_day[k]) for k in days]
            centre_est = statistics.mean(means)
            spread = (statistics.pstdev(means) / math.sqrt(len(means))) if len(means) > 1 else 0.0
            lo, hi = centre_est - 1.96 * spread, centre_est + 1.96 * spread
            verdict2 = ("显著更好" if hi < 0 else "显著更差" if lo > 0 else "与零无区别")
            print(f"    按站日聚类（{len(days)} 个站日）：站日均值差 {centre_est:+.4f}"
                  f" · 95% 区间 [{lo:+.4f}, {hi:+.4f}] → {verdict2}")
        pts = point.get(system) or []
        if pts:
            cand = [x for x, _ in pts]
            inc = [y for _, y in pts]
            better = 100 * sum(1 for a, b in pts if abs(a) < abs(b)) / len(pts)
            print(f"    点预报 MAE：本系统 {sum(map(abs, cand))/len(cand):.3f}°C"
                  f"（偏差 {statistics.mean(cand):+.3f}）"
                  f" · 在任后验均值 {sum(map(abs, inc))/len(inc):.3f}°C"
                  f" · 更准的比例 {better:.1f}%")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
