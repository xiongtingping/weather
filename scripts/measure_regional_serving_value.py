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
import bisect
from datetime import datetime, timezone
import collections
import json
import math
import sqlite3
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import regional_path_prior as R  # noqa: E402
from bayes_basket_target import regional_point_debias as D  # noqa: E402
from bayes_basket_target import regional_live_gap as G  # noqa: E402

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
    parser.add_argument(
        "--archive", type=Path, default=ROOT / "data" / "archive.db",
        help="observation archive, for the live-gap arm only",
    )
    parser.add_argument(
        "--live-gap", action="store_true",
        help="add charter 95's live-gap arm: the served configuration plus "
             "today's own model error, everything else held fixed",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="also write the per-system verdicts as JSON (the board reads "
             "data/runtime/bayes_basket_target_nwp/regional_admission_gate.json)",
    )
    parser.add_argument(
        "--dump-rows", type=Path, default=None,
        help="write one JSON line per scored prefix (anchor, sigma, truth, "
             "incumbent centre and spread, both arms' scores) for diagnosis",
    )
    parser.add_argument(
        "--other-side-replay-table", type=Path, default=None,
        help="path prior replay table fitted with charter 95's debias set the "
             "other way; supplying it adds that arm, each arm scored against "
             "the residual table fitted on its own input",
    )
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
    debias_cache: dict[str, dict] = {}
    raw_replay_cache: dict[str, dict] = {}
    raw_table = None
    if args.other_side_replay_table:
        raw_table = json.loads(
            args.other_side_replay_table.read_text(encoding="utf-8"))
    arms = ("none", "layer") if raw_table is None else ("none", "other", "layer")
    if args.live_gap:
        arms = arms + ("livegap",)
    live_gap_cache: dict[str, dict] = {}
    # The newest formal report at each prefix's clock.  Production reads it
    # off the trajectory it just compiled, and since 2026-09-05 it also rides
    # in the lineage as ``metar_trajectory_latest_formal_temperature_c`` --
    # but every event date before that has no such key, which is the whole
    # measurement window.  So this one input is rebuilt from the observation
    # archive, filtered strictly to reports at or before the decision instant.
    # It is the only rebuilt quantity here: the posterior, the runs and the
    # fitted tables are all read as they were.
    formal_by_station: dict[str, list] = {}
    if args.live_gap:
        archive = sqlite3.connect(
            f"file:{args.archive}?mode=ro", uri=True, timeout=300)
        for st_id, valid_at, temp in archive.execute(
            "SELECT station_id,valid_at,reported_temperature_c"
            " FROM reported_trajectory_observations"
            " WHERE reported_temperature_c IS NOT NULL"
            " AND report_type IN ('METAR','SPECI')"
        ):
            formal_by_station.setdefault(str(st_id), []).append(
                (str(valid_at), float(temp)))
        for series in formal_by_station.values():
            series.sort()

    def latest_formal_at(station_id: str, as_of: str):
        series = formal_by_station.get(str(station_id)) or []
        index = bisect.bisect_right(series, (str(as_of), float("inf")))
        return series[index - 1][1] if index else None
    # The layer arm is production's own configuration; the other arm is
    # charter 95's switch flipped.  A gate whose headline arm is not what
    # production serves is measuring a system nobody is running.
    other_debias = not D.ENABLED
    acc = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    point = collections.defaultdict(list)
    seen = collections.Counter()
    paired = collections.defaultdict(list)
    by_station = collections.defaultdict(list)

    dump = open(args.dump_rows, "w", encoding="utf-8") if args.dump_rows else None
    prefixes = list(conn.execute(
        "SELECT station_id,event_date,decision_at_utc,probabilities_json,"
        " bucket_ordinals_json,lineage_json,settlement_timezone"
        " FROM basket_probability_envelope"
        " WHERE stage='P1' AND event_date BETWEEN ? AND ?"
        # A prefilter, not the test: the receipt is parsed and checked below.
        # Without it every P1 lineage in the window is JSON-parsed to find the
        # few thousand that qualify.
        " AND lineage_json LIKE '%TOO_EARLY%'",
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
            latest_c = lineage.get(
                "metar_trajectory_latest_formal_temperature_c")
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
            debias_cache[day] = D.replay_curve(day) or {}
            live_gap_cache[day] = G.replay_curve(day) or {}
            raw_replay_cache[day] = (
                R.replay_curve(day, table=raw_table) or {}
            ) if raw_table is not None else {}
        artifact = replay_cache[day]
        if not artifact:
            continue
        for system in systems:
            rows = [r for r in runs_cache[key]
                    if str(r["ensemble_system_id"]) == system
                    and str(r["knowable_at_utc"]) <= str(at)]
            if not rows:
                continue
            # The candidate is passed explicitly: this gate exists to score
            # systems that are not admitted yet, and the admission list
            # would otherwise refuse exactly the question being asked.
            #
            # Each arm resolves its own serving input and scores against the
            # residual table fitted on that same input: charter 95 debiases
            # the curve before charter 99 fuses it, so the raw arm has to
            # carry the residual fit that belonged to the raw curve or the
            # comparison is between two mismatched halves.
            upper = {b: (None if hi is None else float(hi))
                     for b, (lo, hi) in intervals.items()}
            def _blend(art, admit_debias, live_gap=False):
                if not art:
                    return None, None, {}
                got = R.resolve_serving_input(
                    rows, station_id=station, event_date=day, as_of_utc=str(at),
                    settlement_timezone=str(zone), artifact=art,
                    systems={system},
                    debias_artifact=debias_cache.get(day),
                    # The gate must be able to ask about a layer production
                    # has switched off -- that is the question it exists for.
                    # ``None`` means "whatever production does".
                    debias_systems={system},
                    debias_enabled=admit_debias)
                if got is None:
                    return None, None, {}
                correction, gap_note = (0.0, {})
                if live_gap:
                    correction, gap_note = G.resolve_correction_for(
                        live_gap_cache.get(day),
                        system_id=system, latest_observed_c=latest_c,
                        curve_now_c=got.get("curve_now_c"),
                        systems={system}, enabled=True)
                vec, receipt = R.apply_regional_path_prior(
                    served, ordinals=ordinals, upper_bounds_c=upper,
                    running_high_bucket=running_bucket, running_high_c=running_c,
                    remaining_peak_c=float(got["remaining_peak_c"]),
                    cell=got["cell"], lineage=got.get("lineage"),
                    weight=args.weight, enabled=True,
                    live_gap_correction_c=correction, live_gap_note=gap_note)
                return got, vec, receipt
            resolved, blended, note = _blend(artifact, None)
            if resolved is None or note.get("regional_path_prior_applied") != "true":
                seen[f"{system}:{note.get('regional_path_prior_skip_reason') or '?'}"] += 1
                continue
            vectors = {"none": served, "layer": blended}
            if "livegap" in arms:
                if latest_c is None:
                    latest_c = latest_formal_at(station, str(at))
                lg_got, lg_vec, lg_note = _blend(artifact, None, live_gap=True)
                if lg_got is None or lg_note.get(
                        "regional_path_prior_applied") != "true":
                    continue
                vectors["livegap"] = lg_vec
                seen[f"{system}:livegap:"
                     f"{lg_note.get('regional_live_gap_skip_reason') or 'APPLIED'}"] += 1
                point[f"{system}::livegap"].append((
                    max(running_c, float(lg_got["remaining_peak_c"]))
                    + float(lg_got["cell"]["bias_c"])
                    + float(lg_note.get("regional_live_gap_correction_c") or 0.0)
                    - actual, 0.0))
            if "other" in arms:
                raw_got, raw_vec, raw_note = _blend(
                    raw_replay_cache.get(day), other_debias)
                if raw_got is None or raw_note.get(
                        "regional_path_prior_applied") != "true":
                    continue
                vectors["other"] = raw_vec
                raw_anchor = max(running_c, float(raw_got["remaining_peak_c"]))
                point[f"{system}::other"].append((
                    raw_anchor + float(raw_got["cell"]["bias_c"]) - actual,
                    0.0,
                ))
            anchor = max(running_c, float(resolved["remaining_peak_c"]))
            incumbent_mean = sum(centre(intervals, b, running_c) * p
                                 for b, p in served.items())
            point[system].append((
                anchor + float(resolved["cell"]["bias_c"]) - actual,
                incumbent_mean - actual,
            ))
            scored = {}
            for arm in arms:
                vec = vectors[arm]
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
            if "livegap" in arms:
                paired[f"{system}::livegap"].append(
                    (key, scored["livegap"] - scored["layer"]))
            by_station[(system, station)].append(scored["layer"] - scored["none"])
            if "other" in arms:
                paired[f"{system}::other"].append(
                    (key, scored["layer"] - scored["other"]))
            if dump is not None:
                incumbent_sd = math.sqrt(max(0.0, sum(
                    p * (centre(intervals, b, running_c) - incumbent_mean) ** 2
                    for b, p in served.items())))
                dump.write(json.dumps({
                    "system": system, "station": station, "day": day,
                    "at": str(at), "hours_left": resolved.get("hours_left"),
                    "running_c": running_c,
                    "remaining_peak_c": float(resolved["remaining_peak_c"]),
                    "cell_bias_c": float(resolved["cell"]["bias_c"]),
                    "sigma_c": float(resolved["cell"]["sigma_c"]),
                    "cell_source": resolved["cell"].get("source"),
                    "truth_c": actual, "incumbent_mean_c": incumbent_mean,
                    "incumbent_sd_c": incumbent_sd,
                    "run_init_utc": (resolved.get("lineage") or {}).get(
                        "regional_path_prior_run_init_utc"),
                    "logloss_none": scored["none"],
                    "logloss_layer": scored["layer"],
                }) + "\n")

    if dump is not None:
        dump.close()
    print(f"区域系统的服务价值 · {args.start}..{args.through} · "
          f"窗口＝天花板报 TOO_EARLY 的 P1 前缀（无任何重建）")
    print(f"权重＝{args.weight if args.weight is not None else R.WEIGHT}\n")
    if not acc:
        print("  没有可比前缀：该区间内没有站日同时具备车道 run、结算真值与 TOO_EARLY 回执。")
        return 1
    report: dict[str, dict] = {}
    for system in systems:
        arms = acc.get(system)
        if not arms or not arms.get("none", {}).get("n"):
            print(f"  {system}: 无可比前缀 "
                  f"{ {k.split(':',1)[1]: v for k, v in seen.items() if k.startswith(system)} }")
            report[system] = {"n": 0, "verdict": "NO_COMPARABLE_PREFIX"}
            continue
        n = arms["none"]["n"]
        entry = report.setdefault(system, {"n": n})
        for arm in ("none", "layer"):
            a = arms.get(arm) or {}
            if a.get("n"):
                entry[arm] = {
                    "logloss": a["logloss"] / n, "brier": a["brier"] / n,
                    "hit": a["hit"] / n, "within1": a["within1"] / n,
                }
        print(f"  {system}  n={n}")
        print(f"    {'臂':8s} {'对数损失':>9s} {'Brier':>8s} {'命中':>7s} {'±1桶':>7s}")
        served_label = "去偏后融合" if D.ENABLED else "生偏融合"
        other_label = "生偏融合" if D.ENABLED else "去偏后融合"
        for arm, label in (("none", "无层"), ("other", other_label),
                           ("layer", served_label), ("livegap", "＋今日活偏差")):
            a = arms.get(arm)
            if not a or not a["n"]:
                continue
            print(f"    {label:8s} {a['logloss']/n:9.4f} {a['brier']/n:8.4f}"
                  f" {100*a['hit']/n:6.1f}% {100*a['within1']/n:6.1f}%")
        delta = arms["layer"]["logloss"] / n - arms["none"]["logloss"] / n
        hit_delta = 100 * (arms["layer"]["hit"] - arms["none"]["hit"]) / n
        verdict = "有增益，可接" if (delta < 0 and hit_delta > 0) else "无增益，不可接"
        print(f"    Δ对数损失 {delta:+.4f} nat · Δ命中 {hit_delta:+.1f} 点 → {verdict}")
        entry["delta_logloss"] = delta
        entry["delta_hit_points"] = hit_delta
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
            rng = random.Random(11)
            boot = sorted(
                statistics.mean([means[rng.randrange(len(means))] for _ in means])
                for _ in range(4000)
            )
            p_worse = sum(1 for b in boot if b > 0) / len(boot)
            entry.update({
                "station_days": len(days), "clustered_delta": centre_est,
                "ci_low": lo, "ci_high": hi, "bootstrap_low": boot[100],
                "bootstrap_high": boot[3900], "p_worse": p_worse,
                # The admission rule the list was set by (2026-09-07): the
                # clustered interval must sit below zero.
                "verdict": ("ADMIT" if hi < 0 else "REFUSE_WORSE" if lo > 0
                            else "REFUSE_STRADDLES_ZERO"),
            })
            print(f"    按站日聚类（{len(days)} 个站日）：站日均值差 {centre_est:+.4f}"
                  f" · 95% 区间 [{lo:+.4f}, {hi:+.4f}] → {verdict2}")
            print(f"    自助法（重抽站日 4000 次）区间 [{boot[100]:+.4f}, {boot[3900]:+.4f}]"
                  f" · P(接入后更差)={p_worse:.3f}")
        stations_here = sorted(k[1] for k in by_station if k[0] == system)
        entry["by_station"] = {
            st: {"delta": statistics.mean(by_station[(system, st)]),
                 "n": len(by_station[(system, st)])}
            for st in stations_here
        }
        if len(stations_here) > 1:
            print(f"    逐站 Δ对数损失（负＝更好）：", end="")
            print("  ".join(
                f"{st} {statistics.mean(by_station[(system, st)]):+.3f}(n={len(by_station[(system, st)])})"
                for st in stations_here))
        lg_pairs = paired.get(f"{system}::livegap") or []
        if lg_pairs:
            by_day_g = collections.defaultdict(list)
            for k, d in lg_pairs:
                by_day_g[k].append(d)
            means_g = [statistics.mean(by_day_g[k]) for k in sorted(by_day_g)]
            centre_g = statistics.mean(means_g)
            spread_g = (statistics.pstdev(means_g) / math.sqrt(len(means_g))
                        if len(means_g) > 1 else 0.0)
            lo_g, hi_g = centre_g - 1.96 * spread_g, centre_g + 1.96 * spread_g
            verdict_g = ("显著更好" if hi_g < 0 else "显著更差" if lo_g > 0
                         else "与零无区别")
            rng_g = random.Random(29)
            boot_g = sorted(
                statistics.mean(
                    [means_g[rng_g.randrange(len(means_g))] for _ in means_g])
                for _ in range(4000)
            )
            worse_g = sum(1 for b in boot_g if b > 0) / len(boot_g)
            print(f"    活偏差层本身（＋今日活偏差 − {served_label}）：站日均值差 "
                  f"{centre_g:+.4f} · 95% 区间 [{lo_g:+.4f}, {hi_g:+.4f}]"
                  f" → {verdict_g}")
            print(f"    自助法（重抽 {len(means_g)} 个站日 4000 次）区间 "
                  f"[{boot_g[100]:+.4f}, {boot_g[3900]:+.4f}]"
                  f" · P(加了更差)={worse_g:.3f}")
            lg_pts = point.get(f"{system}::livegap") or []
            if lg_pts:
                gc = [x for x, _ in lg_pts]
                print(f"    锚点 MAE：＋活偏差 {sum(map(abs, gc))/len(gc):.3f}°C"
                      f"（偏差 {statistics.mean(gc):+.3f}）")
        raw_pairs = paired.get(f"{system}::other") or []
        if raw_pairs:
            by_day_r = collections.defaultdict(list)
            for k, d in raw_pairs:
                by_day_r[k].append(d)
            means_r = [statistics.mean(by_day_r[k]) for k in sorted(by_day_r)]
            centre_r = statistics.mean(means_r)
            spread_r = (statistics.pstdev(means_r) / math.sqrt(len(means_r))
                        if len(means_r) > 1 else 0.0)
            print(f"    开关本身（{served_label} − {other_label}）：站日均值差 {centre_r:+.4f}"
                  f" · 95% 区间 [{centre_r - 1.96*spread_r:+.4f},"
                  f" {centre_r + 1.96*spread_r:+.4f}]")
            raw_pts = point.get(f"{system}::other") or []
            if raw_pts:
                rc = [x for x, _ in raw_pts]
                print(f"    锚点 MAE：{other_label} {sum(map(abs, rc))/len(rc):.3f}°C"
                      f"（偏差 {statistics.mean(rc):+.3f}）")
        pts = point.get(system) or []
        if pts:
            cand = [x for x, _ in pts]
            inc = [y for _, y in pts]
            better = 100 * sum(1 for a, b in pts if abs(a) < abs(b)) / len(pts)
            entry["point_mae_c"] = sum(map(abs, cand)) / len(cand)
            entry["point_bias_c"] = statistics.mean(cand)
            entry["incumbent_mae_c"] = sum(map(abs, inc)) / len(inc)
            print(f"    点预报 MAE：本系统 {sum(map(abs, cand))/len(cand):.3f}°C"
                  f"（偏差 {statistics.mean(cand):+.3f}）"
                  f" · 在任后验均值 {sum(map(abs, inc))/len(inc):.3f}°C"
                  f" · 更准的比例 {better:.1f}%")
        print()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({
            "schema": "regional_admission_gate_v1",
            "measured_at_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "window": {"from": args.start, "through": args.through},
            "residual_table_covered": [
                (R.load_replay_table() or {}).get("covered_from"),
                (R.load_replay_table() or {}).get("covered_through"),
            ],
            "weight": args.weight if args.weight is not None else R.WEIGHT,
            "rule": "clustered 95% interval of the per-station-day log-loss "
                    "difference below zero admits",
            "admitted_now": sorted(R.SERVING_SYSTEMS),
            "systems": report,
        }, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
