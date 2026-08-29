#!/usr/bin/env python3
"""Does intra-hour MADIS evidence improve the P1 temperature path?

Two experiments, production functions only (no rule re-implementation):

E1 (posterior quality): for each settled station-day, take the last
committed physical standing proof at/before local noon and 15:00 -- same
root, same artifact, same as-of -- and recompile twice: once with the
proof's own METAR prefix, once with that prefix plus every qc-eligible
MADIS row whose REAL canonical commit was at/before the same as-of
(quantized to the METAR tenths grid, madis_hfmetar lane).  Score both
against the settled bucket.

E2 (hard-bound tightness and safety): for every settled station-day, on a
30-minute standing grid, compile the identity-fallback proof under each
evidence arm and compare hard-excluded bucket counts, exclusion lead time,
and the safety line: does the MADIS arm ever exclude the true bucket.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import metar_ceiling
from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.materialization import _anchor_bucket_for_temperature
from bayes_basket_target.market_registry import load_started_registry_rows
from bayes_basket_target.metar_trajectory import (
    compile_metar_trajectory_update_proof,
)
from bayes_basket_target.p1_compiler import (
    _bucket_geometry_c,
    _fallback_trajectory_artifact,
    _registry_bucket_bounds,
)
from bayes_basket_target.settlement_time import settlement_day_window

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
ARCHIVE = f"file:{ROOT}/data/archive.db?mode=ro"
STATIONS = ('KATL','KAUS','KDAL','KHOU','KLAX','KLGA','KMIA','KORD','KSEA','KSFO')


def tenths(v: float) -> float:
    return float(Decimal(str(float(v))).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def parse(t: str) -> datetime:
    return datetime.fromisoformat(str(t).replace("Z", "+00:00"))


def main() -> None:
    conn = sqlite3.connect(SSOT, uri=True)
    conn.row_factory = sqlite3.Row
    archive = sqlite3.connect(ARCHIVE, uri=True)
    archive.row_factory = sqlite3.Row

    truth = {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth"
        " where station_id in (%s) order by revision_no"
        % ",".join("?" * len(STATIONS)), STATIONS,
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]

    intervals, ordinals = {}, {}
    for row in conn.execute(
        "select station_id, event_date, bucket_intervals_json"
        " from basket_probability_bridge_history where state='COMMITTED'"
        " and station_id in (%s) and bucket_intervals_json is not null"
        " order by decision_at_utc"
        % ",".join("?" * len(STATIONS)), STATIONS,
    ):
        intervals[(row["station_id"], row["event_date"])] = row["bucket_intervals_json"]
    # bridge_history no longer carries envelope_json; ordinals live on the
    # committed envelopes themselves.
    for row in conn.execute(
        "select station_id, event_date, bucket_ordinals_json,"
        " max(decision_at_utc) from basket_probability_envelope"
        " where state='COMMITTED' and station_id in (%s)"
        " group by station_id, event_date"
        % ",".join("?" * len(STATIONS)), STATIONS,
    ):
        ordinals[(row["station_id"], row["event_date"])] = json.loads(
            row["bucket_ordinals_json"]
        )

    # MADIS rows with their REAL capture commits, quantized like production.
    madis_rows: dict[str, list] = defaultdict(list)
    for row in archive.execute(
        "select station_id, observed_at_utc, temperature_c,"
        " raw_message_sha256, canonical_committed_at_utc"
        " from reported_trajectory_madis_hfmetar_observations"
        " where qc_eligible=1 and station_id in (%s)"
        " order by observed_at_utc" % ",".join("?" * len(STATIONS)),
        STATIONS,
    ):
        madis_rows[row["station_id"]].append(row)

    def madis_events(station, day, tz_name, asof_utc):
        window = settlement_day_window(day, tz_name)
        start, end = window["target_day_start_utc"], window["target_day_end_utc"]
        out = []
        for row in madis_rows[station]:
            if not (start <= row["observed_at_utc"] < end):
                continue
            if row["canonical_committed_at_utc"] > asof_utc:
                continue
            out.append({
                "event": {
                    "observation_id": "madis_test_" + row["raw_message_sha256"][:24]
                    + row["observed_at_utc"][11:16].replace(":", ""),
                    "station_id": station,
                    "event_date": day,
                    "settlement_timezone": tz_name,
                    "source_lane": "madis_hfmetar",
                    "report_type": "ASOS_HF",
                    "sequence": 1,
                    "observed_at_utc": row["observed_at_utc"],
                    "received_at_utc": row["canonical_committed_at_utc"],
                    "raw_sha256": str(row["raw_message_sha256"]).lower(),
                    "qc_state": "ACCEPTED",
                    "supersedes_observation_id": None,
                },
                "canonical_committed_at_utc": row["canonical_committed_at_utc"],
                "temperature_c": tenths(row["temperature_c"]),
                "raw_text": None,
            })
        return out

    root_cache, artifact_cache, registry_cache = {}, {}, {}

    def load_root(checkpoint_id):
        if checkpoint_id not in root_cache:
            row = conn.execute(
                "select * from basket_probability_envelope"
                " where checkpoint_id=? and state='COMMITTED'"
                " order by decision_at_utc desc limit 1", (checkpoint_id,),
            ).fetchone()
            root_cache[checkpoint_id] = None if row is None else (
                ProbabilityEnvelope.from_mapping({
                    "stage": row["stage"], "checkpoint_id": row["checkpoint_id"],
                    "station_id": row["station_id"], "event_date": row["event_date"],
                    "settlement_timezone": row["settlement_timezone"],
                    "decision_at_utc": row["decision_at_utc"],
                    "fresh_until_utc": row["fresh_until_utc"],
                    "bucket_ordinals": json.loads(row["bucket_ordinals_json"]),
                    "probabilities": json.loads(row["probabilities_json"]),
                    "posterior_version": row["posterior_version"],
                    "posterior_sha256": row["posterior_sha256"],
                    "bridge_sha256": row["bridge_sha256"],
                    "lineage": json.loads(row["lineage_json"]),
                })
            )
        return root_cache[checkpoint_id]

    def load_artifact_payload(artifact_id):
        if artifact_id not in artifact_cache:
            row = conn.execute(
                "select artifact_json from basket_probability_evidence_artifact"
                " where artifact_id=?", (artifact_id,),
            ).fetchone()
            artifact_cache[artifact_id] = (
                None if row is None else json.loads(row["artifact_json"])
            )
        return artifact_cache[artifact_id]

    def geometry(root):
        key = (root.station_id, root.event_date)
        if key not in registry_cache:
            registry_cache[key] = load_started_registry_rows(
                conn, station_id=root.station_id, event_date=root.event_date,
            )
        unit, upper_by_bucket = _registry_bucket_bounds(
            registry_cache[key],
            expected_station_id=root.station_id,
            expected_event_date=root.event_date,
            expected_settlement_timezone=root.settlement_timezone,
        )
        return _bucket_geometry_c(root, unit=unit, upper_by_bucket=upper_by_bucket)

    def load_observations(proof):
        wanted = list(proof["canonical_observation_ids"])
        marks = ",".join("?" for _ in wanted)
        found = {
            row["observation_id"]: row
            for row in archive.execute(
                "select observation_id, source_lane, station_id, report_type,"
                " valid_at, reported_temperature_c, raw_sha256, raw_text,"
                " canonical_committed_at"
                f" from reported_trajectory_observations"
                f" where observation_id in ({marks})", wanted,
            )
        }
        if set(found) != set(wanted):
            return None
        out = []
        for sequence, observation_id in enumerate(wanted, start=1):
            row = found[observation_id]
            out.append({
                "event": {
                    "observation_id": observation_id,
                    "station_id": proof["station_id"],
                    "event_date": proof["event_date"],
                    "settlement_timezone": proof["settlement_timezone"],
                    "source_lane": str(row["source_lane"]).lower(),
                    "report_type": str(row["report_type"]).upper(),
                    "sequence": sequence,
                    "observed_at_utc": row["valid_at"],
                    "received_at_utc": row["canonical_committed_at"],
                    "raw_sha256": str(row["raw_sha256"]).lower(),
                    "qc_state": "ACCEPTED",
                    "supersedes_observation_id": None,
                },
                "canonical_committed_at_utc": row["canonical_committed_at"],
                "temperature_c": float(row["reported_temperature_c"]),
                "raw_text": row["raw_text"],
            })
        return out

    timezones = {}
    for row in conn.execute(
        "select distinct station_id, event_date, settlement_timezone"
        " from basket_probability_update_proof"
        " where proof_kind='METAR_TRAJECTORY_UPDATE'"
        " and station_id in (%s)" % ",".join("?" * len(STATIONS)), STATIONS,
    ):
        timezones[(row["station_id"], row["event_date"])] = row["settlement_timezone"]

    # ---------------- E1: standing physical proofs ----------------
    conn.execute(
        "create temp table cutoffs (station text, day text, label text,"
        " cutoff_utc text, primary key (station, day, label))"
    )
    cut_rows = []
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
            cut_rows.append((
                station, day, label,
                local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ))
    conn.executemany("insert into cutoffs values (?,?,?,?)", cut_rows)

    picked = conn.execute(
        "select p.station_id, p.event_date, c.label, p.proof_json,"
        " p.artifact_id_or_none as artifact_id"
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
        "  and p.activation_id_or_none like '%physical%'"
    ).fetchall()

    metar_ceiling.CEILING_ENABLED = True
    original_loader = metar_ceiling.load_artifact
    records = {"noon": [], "15h": []}
    skips: dict[str, int] = {}

    def skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

    for pk in picked:
        station, day = pk["station_id"], pk["event_date"]
        key = (station, day)
        settled = truth.get(key)
        if settled is None or key not in ordinals:
            skip("no_truth_or_geometry"); continue
        proof = json.loads(pk["proof_json"])
        if (proof.get("applied_strength") or 0.0) <= 0.0:
            skip("strength_zero"); continue
        root = load_root(proof["prediction_root_checkpoint_id"])
        if root is None:
            skip("root_missing"); continue
        payload = load_artifact_payload(pk["artifact_id"])
        if payload is None:
            skip("artifact_missing"); continue
        observations = load_observations(proof)
        if observations is None:
            skip("observations_missing"); continue
        try:
            centers, uppers = geometry(root)
        except Exception as exc:
            skip(f"geometry:{type(exc).__name__}"); continue
        asof = proof["as_of_utc"]
        extra = madis_events(station, day, proof["settlement_timezone"], asof)
        curve = metar_ceiling.replay_curve(day)
        metar_ceiling.load_artifact = (
            (lambda path=None, _c=curve: _c) if curve is not None else original_loader
        )
        try:
            try:
                arm_a = compile_metar_trajectory_update_proof(
                    root, observations=observations, artifact=payload,
                    bucket_centers_c=centers, bucket_upper_bounds_c=uppers,
                    maturity_bin=proof["maturity_bin"], as_of_utc=asof,
                    prefix_is_current=True,
                )
            except ContractError as exc:
                skip(f"compileA:{str(exc)[:40]}"); continue
            try:
                arm_b = compile_metar_trajectory_update_proof(
                    root, observations=observations + extra, artifact=payload,
                    bucket_centers_c=centers, bucket_upper_bounds_c=uppers,
                    maturity_bin=proof["maturity_bin"], as_of_utc=asof,
                    prefix_is_current=True,
                )
            except ContractError as exc:
                skip(f"compileB:{str(exc)[:40]}"); continue
        finally:
            metar_ceiling.load_artifact = original_loader
        order = ordinals[key]
        truth_bucket = _anchor_bucket_for_temperature(
            settled, root_lineage={"bucket_intervals_c_json": intervals[key]},
            bucket_ordinals=order,
        )
        if truth_bucket is None:
            skip("truth_unbucketed"); continue

        margin = settled - (proof.get("running_high_c") or -99.0)

        def score(dist, excluded):
            argmax = max(dist, key=lambda b: dist[b])
            return {
                "hit": argmax == truth_bucket,
                "signed": order[argmax] - order[truth_bucket],
                "p_truth": dist.get(truth_bucket, 0.0),
                "truth_killed": truth_bucket in set(excluded),
            }

        records[pk["label"]].append({
            "station": station, "day": day, "margin": margin,
            "madis_rows_added": len(extra),
            "a": score(dict(arm_a.posterior_probabilities), arm_a.hard_excluded_buckets),
            "b": score(dict(arm_b.posterior_probabilities), arm_b.hard_excluded_buckets),
            "rh_a": max((o["temperature_c"] for o in observations), default=None),
            "rh_b": max((o["temperature_c"] for o in observations + extra), default=None),
            "ex_a": len(arm_a.hard_excluded_buckets),
            "ex_b": len(arm_b.hard_excluded_buckets),
        })

    def report(title, subset):
        print(title)
        if not subset:
            print("  无样本"); return
        n = len(subset)
        print(f"  n={n}  平均新增MADIS行={sum(r['madis_rows_added'] for r in subset)/n:.1f}")
        for arm, name in (("a", "METAR-only"), ("b", "+MADIS盘中")):
            hit = sum(r[arm]["hit"] for r in subset) / n
            signed = sum(r[arm]["signed"] for r in subset) / n
            logp = sum(math.log(max(r[arm]["p_truth"], 1e-9)) for r in subset) / n
            killed = sum(r[arm]["truth_killed"] for r in subset)
            print(f"  {name:12s} 命中={hit:.3f} 带符号偏移={signed:+.2f}"
                  f" 对数分={logp:.3f} 真值桶被硬杀={killed}")
        fixed = sum(1 for r in subset if not r["a"]["hit"] and r["b"]["hit"])
        broke = sum(1 for r in subset if r["a"]["hit"] and not r["b"]["hit"])
        drh = [r["rh_b"] - r["rh_a"] for r in subset if r["rh_a"] is not None]
        dex = [r["ex_b"] - r["ex_a"] for r in subset]
        print(f"  救回 {fixed}, 弄坏 {broke};"
              f" 硬下界平均抬高 {sum(drh)/len(drh):+.2f}C;"
              f" 平均多杀桶 {sum(dex)/len(dex):+.2f}")
        print()

    print("========== E1 · 站立物理后验 ==========")
    for label, name in (("noon", "当地正午"), ("15h", "当地15:00")):
        subset = records[label]
        report(f"== {name}·全部 ==", subset)
        report(f"== {name}·峰值前(margin>=0.05) ==",
               [r for r in subset if r["margin"] >= 0.05])
    print("E1 跳过:", dict(sorted(skips.items())))

    # ---------------- E2: hard-bound grid, identity arm ----------------
    print("\n========== E2 · 硬下界网格(identity臂, 30分钟站立) ==========")
    day_roots = {}
    for row in conn.execute(
        "select station_id, event_date, checkpoint_id,"
        " max(decision_at_utc) from basket_probability_envelope"
        " where stage='P0' and state='COMMITTED'"
        " and station_id in (%s) group by station_id, event_date"
        % ",".join("?" * len(STATIONS)), STATIONS,
    ):
        day_roots[(row["station_id"], row["event_date"])] = row["checkpoint_id"]

    metars_by_day = defaultdict(list)
    for row in archive.execute(
        "select station_id, valid_at, reported_temperature_c, report_type,"
        " canonical_committed_at from reported_trajectory_observations"
        " where source_lane='awc' and report_type in ('METAR','SPECI')"
        " and station_id in (%s) and valid_at >= '2026-08-05'"
        % ",".join("?" * len(STATIONS)), STATIONS,
    ):
        metars_by_day[row["station_id"]].append(row)

    grid_stats = []
    e2_skips: dict[str, int] = {}
    safety_violations = []
    lead_minutes_all = []

    for (station, day), tz_name in sorted(timezones.items()):
        key = (station, day)
        if key not in truth or key not in ordinals or key not in day_roots:
            continue
        settled = truth[key]
        root = load_root(day_roots[key])
        if root is None:
            e2_skips["root"] = e2_skips.get("root", 0) + 1; continue
        try:
            centers, uppers = geometry(root)
        except Exception:
            e2_skips["geometry"] = e2_skips.get("geometry", 0) + 1; continue
        order = ordinals[key]
        truth_bucket = _anchor_bucket_for_temperature(
            settled, root_lineage={"bucket_intervals_c_json": intervals[key]},
            bucket_ordinals=order,
        )
        if truth_bucket is None:
            e2_skips["truth_unbucketed"] = e2_skips.get("truth_unbucketed", 0) + 1
            continue
        window = settlement_day_window(day, tz_name)
        start = parse(window["target_day_start_utc"])
        end = parse(window["target_day_end_utc"])
        awc = [
            {
                "event": {
                    "observation_id": f"awc-{i}", "station_id": station,
                    "event_date": day, "settlement_timezone": tz_name,
                    "source_lane": "awc",
                    "report_type": str(m["report_type"]).upper(),
                    "sequence": 1, "observed_at_utc": m["valid_at"],
                    "received_at_utc": m["canonical_committed_at"],
                    "raw_sha256": "a" * 64, "qc_state": "ACCEPTED",
                    "supersedes_observation_id": None,
                },
                "canonical_committed_at_utc": m["canonical_committed_at"],
                "temperature_c": float(m["reported_temperature_c"]),
                "raw_text": None,
            }
            for i, m in enumerate(metars_by_day[station])
            if window["target_day_start_utc"] <= m["valid_at"] < window["target_day_end_utc"]
        ]
        if not awc:
            e2_skips["no_awc"] = e2_skips.get("no_awc", 0) + 1; continue
        mad = madis_events(station, day, tz_name, "9999")
        first_excl_a, first_excl_b = {}, {}
        anchor_counts = {}
        t = start + timedelta(hours=8)
        while t <= min(end, start + timedelta(hours=22)):
            asof = t.isoformat(timespec="microseconds").replace("+00:00", "Z")
            for arm, rows_all, sink in (
                ("a", awc, first_excl_a),
                ("b", awc + mad, first_excl_b),
            ):
                visible = [r for r in rows_all if r["canonical_committed_at_utc"] <= asof]
                if not visible:
                    continue
                artifact = _fallback_trajectory_artifact(
                    root, source_lane="awc", maturity_bin="D0", as_of_utc=asof,
                )
                try:
                    proof = compile_metar_trajectory_update_proof(
                        root, observations=visible, artifact=artifact,
                        bucket_centers_c=centers, bucket_upper_bounds_c=uppers,
                        maturity_bin="D0", as_of_utc=asof,
                        prefix_is_current=True,
                    )
                except ContractError:
                    continue
                for bucket in proof.hard_excluded_buckets:
                    sink.setdefault(bucket, t)
                if truth_bucket in set(proof.hard_excluded_buckets):
                    safety_violations.append((station, day, arm, asof))
                anchor_counts[(arm, t)] = len(proof.hard_excluded_buckets)
            t += timedelta(minutes=30)
        for bucket, tb in first_excl_b.items():
            ta = first_excl_a.get(bucket)
            if ta is not None:
                lead_minutes_all.append((ta - tb).total_seconds() / 60.0)
            else:
                lead_minutes_all.append(float("inf"))
        grid_stats.append({
            "station": station, "day": day,
            "final_a": len(first_excl_a), "final_b": len(first_excl_b),
        })

    n = len(grid_stats)
    finite = sorted(x for x in lead_minutes_all if x != float("inf"))
    only_b = sum(1 for x in lead_minutes_all if x == float("inf"))
    print(f"站日 n={n}")
    print(f"日终排除桶数: METAR-only 平均 {sum(g['final_a'] for g in grid_stats)/n:.2f}"
          f" vs +MADIS {sum(g['final_b'] for g in grid_stats)/n:.2f}")
    if finite:
        print(f"同一桶的排除提前量: 中位 {finite[len(finite)//2]:.0f} 分钟,"
              f" p90 {finite[int(0.9*(len(finite)-1))]:.0f} 分钟 (n={len(finite)})")
    print(f"只有 +MADIS 才排除掉的桶次: {only_b}")
    a_viol = [v for v in safety_violations if v[2] == "a"]
    b_viol = [v for v in safety_violations if v[2] == "b"]
    print(f"安全红线·真值桶被硬杀的(站日,时刻)次数: METAR-only={len(a_viol)}  +MADIS={len(b_viol)}")
    for v in (b_viol or [])[:8]:
        print("   +MADIS违例:", v[0], v[1], v[3])
    print("E2 跳过:", e2_skips)


if __name__ == "__main__":
    main()
