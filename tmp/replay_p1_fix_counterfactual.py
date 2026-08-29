#!/usr/bin/env python3
"""Replay the fixed P1 compile over the standing pre-peak physical proofs.

For every station-day whose noon / 15:00-local standing P1 ran the physical
method with strength, the committed posterior (compiled under the decision
clock, ceiling unguarded) is scored against a recompilation with the fixed
production compiler: evidence-clock path state, ceiling only on the
current-high step.  Everything is production code -- the compiler itself, the
registry geometry, the resolver's artifact payload, and the ceiling replay
table that gives each day only the curve it could have had that morning.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime
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
    _registry_bucket_bounds,
)

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
ARCHIVE = f"file:{ROOT}/data/archive.db?mode=ro"


def main() -> None:
    conn = sqlite3.connect(SSOT, uri=True)
    conn.row_factory = sqlite3.Row
    archive = sqlite3.connect(ARCHIVE, uri=True)
    archive.row_factory = sqlite3.Row

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

    root_cache: dict[str, ProbabilityEnvelope | None] = {}
    artifact_cache: dict[str, dict | None] = {}
    registry_cache: dict[tuple[str, str], list] = {}

    def load_root(checkpoint_id: str) -> ProbabilityEnvelope | None:
        if checkpoint_id not in root_cache:
            row = conn.execute(
                "select * from basket_probability_envelope"
                " where checkpoint_id=? and state='COMMITTED'"
                " order by decision_at_utc desc limit 1",
                (checkpoint_id,),
            ).fetchone()
            root_cache[checkpoint_id] = None if row is None else (
                ProbabilityEnvelope.from_mapping({
                    "stage": row["stage"],
                    "checkpoint_id": row["checkpoint_id"],
                    "station_id": row["station_id"],
                    "event_date": row["event_date"],
                    "settlement_timezone": row["settlement_timezone"],
                    "decision_at_utc": row["decision_at_utc"],
                    "fresh_until_utc": row["fresh_until_utc"],
                    "bucket_ordinals": json.loads(
                        row["bucket_ordinals_json"]
                    ),
                    "probabilities": json.loads(row["probabilities_json"]),
                    "posterior_version": row["posterior_version"],
                    "posterior_sha256": row["posterior_sha256"],
                    "bridge_sha256": row["bridge_sha256"],
                    "lineage": json.loads(row["lineage_json"]),
                })
            )
        return root_cache[checkpoint_id]

    def load_artifact_payload(artifact_id: str) -> dict | None:
        if artifact_id not in artifact_cache:
            row = conn.execute(
                "select artifact_json from basket_probability_evidence_artifact"
                " where artifact_id=?",
                (artifact_id,),
            ).fetchone()
            artifact_cache[artifact_id] = (
                None if row is None else json.loads(row["artifact_json"])
            )
        return artifact_cache[artifact_id]

    def geometry(root: ProbabilityEnvelope):
        key = (root.station_id, root.event_date)
        if key not in registry_cache:
            registry_cache[key] = load_started_registry_rows(
                conn,
                station_id=root.station_id,
                event_date=root.event_date,
            )
        unit, upper_by_bucket = _registry_bucket_bounds(
            registry_cache[key],
            expected_station_id=root.station_id,
            expected_event_date=root.event_date,
            expected_settlement_timezone=root.settlement_timezone,
        )
        return _bucket_geometry_c(
            root, unit=unit, upper_by_bucket=upper_by_bucket
        )

    def load_observations(proof: dict) -> list[dict] | None:
        wanted = list(proof["canonical_observation_ids"])
        marks = ",".join("?" for _ in wanted)
        found = {
            row["observation_id"]: row
            for row in archive.execute(
                "select observation_id, source_lane, station_id, report_type,"
                " valid_at, reported_temperature_c, raw_sha256, raw_text,"
                " canonical_committed_at"
                f" from reported_trajectory_observations"
                f" where observation_id in ({marks})",
                wanted,
            )
        }
        if set(found) != set(wanted):
            return None
        result = []
        for sequence, observation_id in enumerate(wanted, start=1):
            row = found[observation_id]
            result.append({
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
        return result

    records = {"noon": [], "15h": []}
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    original_loader = metar_ceiling.load_artifact
    for picked_row in picked:
        station, day = picked_row["station_id"], picked_row["event_date"]
        key = (station, day)
        settled = truth.get(key)
        if settled is None or key not in ordinals:
            skip("no_truth_or_geometry")
            continue
        proof = json.loads(picked_row["proof_json"])
        if (proof.get("applied_strength") or 0.0) <= 0.0:
            skip("strength_zero")
            continue
        margin = settled - (proof.get("running_high_c") or -99.0)
        root = load_root(proof["prediction_root_checkpoint_id"])
        if root is None:
            skip("root_missing")
            continue
        payload = load_artifact_payload(picked_row["artifact_id"])
        if payload is None:
            skip("artifact_missing")
            continue
        observations = load_observations(proof)
        if observations is None:
            skip("observations_missing")
            continue
        try:
            centers, uppers = geometry(root)
        except (ContractError, Exception) as exc:
            skip(f"geometry:{type(exc).__name__}")
            continue
        curve = metar_ceiling.replay_curve(day)
        metar_ceiling.load_artifact = (
            (lambda path=None, _c=curve: _c)
            if curve is not None
            else original_loader
        )
        try:
            recompiled = compile_metar_trajectory_update_proof(
                root,
                observations=observations,
                artifact=payload,
                bucket_centers_c=centers,
                bucket_upper_bounds_c=uppers,
                maturity_bin=proof["maturity_bin"],
                as_of_utc=proof["as_of_utc"],
                prefix_is_current=True,
            )
        except ContractError as exc:
            skip(f"compile:{exc}")
            continue
        finally:
            metar_ceiling.load_artifact = original_loader
        order = ordinals[key]
        truth_bucket = _anchor_bucket_for_temperature(
            settled,
            root_lineage={"bucket_intervals_c_json": intervals[key]},
            bucket_ordinals=order,
        )
        if truth_bucket is None:
            skip("truth_unbucketed")
            continue

        def score(dist):
            argmax = max(dist, key=lambda b: dist[b])
            return {
                "hit": argmax == truth_bucket,
                "signed": order[argmax] - order[truth_bucket],
                "p_truth": dist.get(truth_bucket, 0.0),
                "maxp": dist[argmax],
            }

        strength_delta = abs(
            (proof.get("applied_strength") or 0.0)
            - recompiled.applied_strength
        )
        records[picked_row["label"]].append({
            "station": station,
            "day": day,
            "margin": margin,
            "root": score(root.probabilities),
            "before": score(proof["posterior_probabilities"]),
            "after": score(dict(recompiled.posterior_probabilities)),
            "strength_delta": strength_delta,
        })

    def report(title, subset):
        print(title)
        if not subset:
            print("无样本")
            return
        n = len(subset)
        print(f"n={n}  strength逐行吻合(≤1e-9): "
              f"{sum(1 for r in subset if r['strength_delta'] <= 1e-9)}/{n}")
        for layer, name in (("root", "P0根"), ("before", "修前(已提交)"),
                            ("after", "修后(重放)")):
            hit = sum(r[layer]["hit"] for r in subset) / n
            signed = sum(r[layer]["signed"] for r in subset) / n
            logp = sum(
                math.log(max(r[layer]["p_truth"], 1e-9)) for r in subset
            ) / n
            collapsed = sum(1 for r in subset if r[layer]["maxp"] > 0.99)
            print(f"  {name:10s} 命中={hit:.3f}  带符号偏移={signed:+.2f}"
                  f"  对数分={logp:.3f}  塌缩>0.99={collapsed}")
        fixed = sum(
            1 for r in subset if not r["before"]["hit"] and r["after"]["hit"]
        )
        broke = sum(
            1 for r in subset if r["before"]["hit"] and not r["after"]["hit"]
        )
        print(f"  修后 vs 修前: 救回 {fixed}, 弄坏 {broke}")
        print()

    for label, name in (("noon", "当地正午站立·物理法"),
                        ("15h", "当地15:00站立·物理法")):
        subset = records[label]
        report(f"== {name}·全部 ==", subset)
        report(f"== {name}·峰值前 ==",
               [r for r in subset if r["margin"] >= 0.05])
    print("跳过统计:", dict(sorted(skips.items())))


if __name__ == "__main__":
    main()
