#!/usr/bin/env python3
"""End to end: what conditioning the ceiling on path state does to P1.

For every station-day, the P1 belief STANDING at a fixed local hour is
recompiled twice through the production compiler -- once with the replay
curve's state slopes taken away, once with them -- and both are scored
against the settled truth.  Nothing else differs between the arms: same
root, same artifact, same observations, same decision clock, same
``prefix_is_current`` as was committed.

Each day is served only the curve fitted on strictly earlier days.
"""
from __future__ import annotations

import importlib.util
import json
import math
import pickle
import sqlite3
import sys
from datetime import datetime, timedelta
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

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
ARCHIVE = f"file:{ROOT}/data/archive.db?mode=ro"
CACHE = Path(
    "/private/tmp/claude-501/-Users-xiong-weather/"
    "75cac755-ae4e-4198-ab16-0f8da9badbc5/scratchpad/ceiling_samples.pkl"
)
ANCHORS = tuple((f"{hour}h", hour) for hour in range(13, 23))
MIN_TRAIN = 2000
# A: what production serves today -- burst-weighted marginal, no state.
# B: the same marginal refitted on the population that actually stands.
# C: B plus the state slopes.
ARMS = ("A_今日生产", "B_按站立加权", "C_加权+状态")


def main() -> None:
    conn = sqlite3.connect(SSOT, uri=True)
    conn.row_factory = sqlite3.Row
    archive = sqlite3.connect(ARCHIVE, uri=True)
    archive.row_factory = sqlite3.Row
    metar_ceiling.CEILING_ENABLED = True
    spec = importlib.util.spec_from_file_location(
        "ceiling_fit", ROOT / "scripts" / "fit_metar_ceiling_artifact.py"
    )
    fitter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fitter)
    fit_samples = pickle.loads(CACHE.read_bytes())
    curves: dict[str, dict[str, dict] | None] = {}

    def curves_for(day: str) -> dict[str, dict] | None:
        """One curve per arm for this event date, fitted on earlier days only."""

        if day not in curves:
            earlier = [s for s in fit_samples if s["event_date"] < day]
            if len(earlier) < MIN_TRAIN:
                curves[day] = None
            else:
                plain = fitter.fit(earlier, standing_weighted=False)
                weighted = fitter.fit(earlier, standing_weighted=True)
                curves[day] = {
                    "A_今日生产": {
                        **{
                            key: value for key, value in plain.items()
                            if key != "state_coefficients"
                        },
                        "trained_through": "",
                    },
                    "B_按站立加权": {
                        **{
                            key: value for key, value in weighted.items()
                            if key != "state_coefficients"
                        },
                        "trained_through": "",
                    },
                    "C_加权+状态": {**weighted, "trained_through": ""},
                }
        return curves[day]

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
        "select checkpoint_id, stage, station_id, event_date,"
        " settlement_timezone, decision_at_utc, fresh_until_utc,"
        " bucket_ordinals_json, probabilities_json, posterior_version,"
        " posterior_sha256, bridge_sha256, lineage_json"
        " from basket_probability_envelope where stage in ('P0','P0-M')"
    ):
        roots[row["checkpoint_id"]] = ProbabilityEnvelope.from_mapping({
            "stage": row["stage"],
            "checkpoint_id": row["checkpoint_id"],
            "station_id": row["station_id"],
            "event_date": row["event_date"],
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
            zone = ZoneInfo(tz_name)
        except Exception:
            continue
        base = datetime.fromisoformat(day)
        for label, hour in ANCHORS:
            local = base.replace(hour=hour, tzinfo=zone)
            rows.append((
                station, day, label,
                local.astimezone(ZoneInfo("UTC")).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
            ))
    conn.executemany("insert into cutoffs values (?,?,?,?)", rows)

    picked = conn.execute(
        "select p.station_id, p.event_date, c.label, p.proof_json,"
        " p.artifact_id_or_none as artifact_id,"
        " p.activation_id_or_none as act"
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

    artifact_cache: dict[str, dict | None] = {}
    registry_cache: dict[tuple[str, str], list] = {}
    original_loader = metar_ceiling.load_artifact

    def payload_for(artifact_id: str) -> dict | None:
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
                conn, station_id=root.station_id, event_date=root.event_date
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

    def observations_for(proof: dict) -> list[dict] | None:
        wanted = list(proof["canonical_observation_ids"])
        marks = ",".join("?" for _ in wanted)
        found = {
            row["observation_id"]: row
            for row in archive.execute(
                "select observation_id, source_lane, report_type, valid_at,"
                " reported_temperature_c, raw_sha256, raw_text,"
                " canonical_committed_at"
                " from reported_trajectory_observations"
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

    records: dict[str, list[dict]] = {label: [] for label, _ in ANCHORS}
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for picked_row in picked:
        station, day = picked_row["station_id"], picked_row["event_date"]
        key = (station, day)
        settled = truth.get(key)
        if settled is None or key not in ordinals:
            skip("no_truth_or_geometry")
            continue
        arm_curves = curves_for(day)
        if arm_curves is None:
            skip("no_replay_curve")
            continue
        proof = json.loads(picked_row["proof_json"])
        root = roots.get(proof["prediction_root_checkpoint_id"])
        if root is None:
            skip("root_missing")
            continue
        payload = payload_for(picked_row["artifact_id"])
        if payload is None:
            # Identity fallback: no promoted artifact governed this step, and
            # the ceiling serves those steps too.  Dropping them would have
            # measured the layer on a third of the population it runs on.
            try:
                payload = _fallback_trajectory_artifact(
                    root,
                    source_lane=str(proof.get("source_lane") or "awc"),
                    maturity_bin=proof["maturity_bin"],
                    as_of_utc=proof["as_of_utc"],
                ).to_dict()
            except ContractError as exc:
                skip(f"fallback:{exc}")
                continue
        observations = observations_for(proof)
        if observations is None:
            skip("observations_missing")
            continue
        try:
            centers, uppers = geometry(root)
        except Exception as exc:
            skip(f"geometry:{type(exc).__name__}")
            continue
        order = ordinals[key]
        truth_bucket = _anchor_bucket_for_temperature(
            settled,
            root_lineage={"bucket_intervals_c_json": intervals[key]},
            bucket_ordinals=order,
        )
        if truth_bucket is None:
            skip("truth_unbucketed")
            continue
        arms = {}
        failed = None
        for label in ARMS:
            artifact_curve = arm_curves[label]
            metar_ceiling.load_artifact = (
                lambda path=None, _c=artifact_curve: _c
            )
            try:
                arms[label] = compile_metar_trajectory_update_proof(
                    root,
                    observations=observations,
                    artifact=payload,
                    bucket_centers_c=centers,
                    bucket_upper_bounds_c=uppers,
                    maturity_bin=proof["maturity_bin"],
                    as_of_utc=proof["as_of_utc"],
                    prefix_is_current=bool(
                        proof.get("prefix_is_current", True)
                    ),
                )
            except ContractError as exc:
                failed = f"compile:{exc}"
                break
            finally:
                metar_ceiling.load_artifact = original_loader
        if failed:
            skip(failed)
            continue

        def score(dist):
            argmax = max(dist, key=lambda b: dist[b])
            return {
                "hit": argmax == truth_bucket,
                "signed": order[argmax] - order[truth_bucket],
                "p_truth": dist.get(truth_bucket, 0.0),
            }

        posteriors = {
            label: dict(arms[label].posterior_probabilities) for label in ARMS
        }
        reference = posteriors[ARMS[0]]
        moved = max(
            max(
                abs(reference.get(b, 0.0) - posteriors[label].get(b, 0.0))
                for b in set(reference) | set(posteriors[label])
            )
            for label in ARMS[1:]
        )
        entry = {
            "station": station,
            "day": day,
            "margin": settled - (proof.get("running_high_c") or -99.0),
            "moved": moved,
            "hours_left": metar_ceiling.hours_left(
                day, proof["settlement_timezone"], proof["as_of_utc"]
            ),
        }
        for label in ARMS:
            entry[label] = score(posteriors[label])
        records[picked_row["label"]].append(entry)

    def report(title, subset):
        print(title)
        if not subset:
            print("  无样本")
            return
        n = len(subset)
        for arm in ARMS:
            hit = sum(r[arm]["hit"] for r in subset) / n
            signed = sum(r[arm]["signed"] for r in subset) / n
            logp = sum(
                math.log(max(r[arm]["p_truth"], 1e-9)) for r in subset
            ) / n
            brier = sum((1.0 - r[arm]["p_truth"]) ** 2 for r in subset) / n
            fixed = sum(
                1 for r in subset
                if not r[ARMS[0]]["hit"] and r[arm]["hit"]
            )
            broke = sum(
                1 for r in subset
                if r[ARMS[0]]["hit"] and not r[arm]["hit"]
            )
            tail = "" if arm == ARMS[0] else f"  救回{fixed}/弄坏{broke}"
            print(f"  {arm:14s} n={n:4d}  命中={hit:.3f}"
                  f"  偏移={signed:+.2f}  对数分={logp:.3f}"
                  f"  Brier={brier:.4f}{tail}")

    everything = [r for label, _ in ANCHORS for r in records[label]]
    moved_rows = [
        r for r in everything
        if r["moved"] > 1e-9 and r["hours_left"] is not None
    ]
    print("===== 全部锚点合并 =====")
    report("全部站立行", everything)
    report("天花板实际动了的行", moved_rows)
    report("  其中峰值前 (仍有升温余地)",
           [r for r in moved_rows if r["margin"] >= 0.05])
    print()
    print("===== 按距日终小时分档 (只算天花板动了的行) =====")
    edges = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 99)]
    for low, high in edges:
        subset = [
            r for r in moved_rows if low <= r["hours_left"] < high
        ]
        if not subset:
            continue
        report(f"{low}-{high}h", subset)
        pre = [r for r in subset if r["margin"] >= 0.05]
        if pre:
            report(f"  {low}-{high}h 峰值前", pre)
    print()
    print("跳过统计:", dict(sorted(skips.items())))


if __name__ == "__main__":
    main()
