#!/usr/bin/env python3
"""Three ceiling forms, scored on the SERVED distribution, strictly as-of.

The earlier three-arm test scored the trajectory proof's posterior, which no
consumer serves; since 5c680f1e the ceiling lives in the served envelope, so
the comparison is rerun end to end through the production compiler:

  A  hours-only rate on the burst population   (what production serves today)
  B  hours-only rate weighted by standing time
  C  B plus state slopes (momentum, drawdown)  in logit space

For every settled station-day and every local anchor hour, the compiler runs
on exactly the observations committed by that instant -- prefix_is_current,
the root, everything as-of -- and the last packet's expected envelope is the
belief production would have been serving.  Each day is fitted only on
strictly earlier days.  No outcome enters any input.
"""
from __future__ import annotations

import importlib.util
import json
import math
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import metar_ceiling
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.materialization import _anchor_bucket_for_temperature
from bayes_basket_target.market_registry import load_started_registry_rows
from bayes_basket_target.p1_compiler import (
    compile_station_p1_packets,
    select_p1_replay_base,
)
from bayes_basket_target.production_io import (
    load_materialized_probability_envelopes,
)

SSOT_PATH = ROOT / "data" / "bayes_basket_target_ssot.db"
ARCHIVE = f"file:{ROOT}/data/archive.db?mode=ro"
CACHE = Path(
    "/private/tmp/claude-501/-Users-xiong-weather/"
    "75cac755-ae4e-4198-ab16-0f8da9badbc5/scratchpad/ceiling_samples.pkl"
)
ANCHOR_HOURS = (15, 18, 21)
ARMS = ("A_今日生产", "B_按站立加权", "C_加权+状态")
MIN_TRAIN = 2000
SINCE = "2026-08-14"


def main() -> None:
    conn = sqlite3.connect(f"file:{SSOT_PATH}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    archive = sqlite3.connect(ARCHIVE, uri=True, timeout=120)
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
        if day not in curves:
            earlier = [s for s in fit_samples if s["event_date"] < day]
            if len(earlier) < MIN_TRAIN:
                curves[day] = None
            else:
                plain = fitter.fit(earlier, standing_weighted=False)
                weighted = fitter.fit(earlier, standing_weighted=True)
                strip = lambda a: {
                    k: v for k, v in a.items() if k != "state_coefficients"
                }
                curves[day] = {
                    "A_今日生产": {**strip(plain), "trained_through": ""},
                    "B_按站立加权": {**strip(weighted), "trained_through": ""},
                    "C_加权+状态": {**weighted, "trained_through": ""},
                }
        return curves[day]

    truth = {}
    for row in conn.execute(
        "select station_id, local_date, official_tmax_c"
        " from calibration_station_daily_high_truth order by revision_no"
    ):
        truth[(row["station_id"], row["local_date"])] = row["official_tmax_c"]

    intervals, ordinals, timezones = {}, {}, {}
    for row in conn.execute(
        "select station_id, event_date, bucket_intervals_json, envelope_json,"
        " settlement_timezone"
        " from basket_probability_bridge_history where state='COMMITTED'"
        " order by decision_at_utc"
    ):
        key = (row["station_id"], row["event_date"])
        intervals[key] = row["bucket_intervals_json"]
        ordinals[key] = json.loads(row["envelope_json"])["bucket_ordinals"]
        timezones[key] = row["settlement_timezone"]

    registry_cache: dict[tuple[str, str], list] = {}

    def registry_for(station: str, day: str) -> list:
        key = (station, day)
        if key not in registry_cache:
            registry_cache[key] = load_started_registry_rows(
                conn, station_id=station, event_date=day
            )
        return registry_cache[key]

    def day_observations(station: str, day: str, tz: ZoneInfo, tz_name: str) -> list[dict]:
        start = datetime.fromisoformat(day).replace(tzinfo=tz)
        end = start + timedelta(days=1)
        rows = archive.execute(
            "select observation_id, valid_at, canonical_committed_at,"
            " reported_temperature_c, raw_sha256, raw_text, report_type,"
            " source_lane, correction_suspected"
            " from reported_trajectory_observations"
            " where station_id=? and source_lane='awc'"
            " and julianday(valid_at)>=julianday(?)"
            " and julianday(valid_at)<julianday(?)"
            " order by valid_at, canonical_committed_at, observation_id",
            (station, start.isoformat(), end.isoformat()),
        ).fetchall()
        return [
            {
                "observation_id": r["observation_id"],
                "station_id": station,
                "event_date_local": day,
                "settlement_timezone": tz_name,
                "reported_temperature_c": float(r["reported_temperature_c"]),
                "valid_at_utc": r["valid_at"],
                "canonical_committed_at_utc": r["canonical_committed_at"],
                "raw_sha256": str(r["raw_sha256"]).lower(),
                "raw_text": r["raw_text"],
                "source_lane": "AWC",
                "report_type": str(r["report_type"]).upper(),
                "evidence_role": "FORMAL",
                "qc_status": "ACCEPTED",
                "correction_suspected": bool(r["correction_suspected"]),
            }
            for r in rows
        ]

    keys = sorted(
        key for key in truth
        if key in ordinals and key[1] >= SINCE
    )
    print(f"候选站日 {len(keys)} (自 {SINCE})", flush=True)

    records: list[dict] = []
    skips: dict[str, int] = defaultdict(int)
    original_loader = metar_ceiling.load_artifact

    for index, (station, day) in enumerate(keys):
        if index % 40 == 0:
            print(f"  … {index}/{len(keys)}  已得 {len(records)} 行", flush=True)
        arm_curves = curves_for(day)
        if arm_curves is None:
            skips["no_curve"] += 1
            continue
        tz_name = timezones[(station, day)]
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            skips["timezone"] += 1
            continue
        observations = day_observations(station, day, tz, tz_name)
        if len(observations) < 3:
            skips["few_observations"] += 1
            continue
        try:
            envelopes = load_materialized_probability_envelopes(
                conn,
                station_id=station,
                event_date=day,
                evaluation_at_utc="2099-01-01T00:00:00Z",
                expected_source_lane=None,
                include_future_decisions=True,
                stages=("P0", "P0-M"),
            )
        except ContractError as exc:
            skips[f"load:{exc}"] += 1
            continue
        settled = truth[(station, day)]
        order = ordinals[(station, day)]
        truth_bucket = _anchor_bucket_for_temperature(
            settled,
            root_lineage={
                "bucket_intervals_c_json": intervals[(station, day)]
            },
            bucket_ordinals=order,
        )
        if truth_bucket is None:
            skips["truth_unbucketed"] += 1
            continue
        registry = registry_for(station, day)
        if not registry:
            skips["no_registry"] += 1
            continue

        base_date = datetime.fromisoformat(day)
        for hour in ANCHOR_HOURS:
            anchor = (
                base_date.replace(hour=hour, tzinfo=tz)
                .astimezone(ZoneInfo("UTC"))
            )
            anchor_text = anchor.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            visible = [
                o for o in observations
                if o["canonical_committed_at_utc"] <= anchor_text
                and o["valid_at_utc"] <= anchor_text
            ]
            if len(visible) < 3:
                skips["anchor_few_obs"] += 1
                continue
            try:
                root, _, _, _ = select_p1_replay_base(
                    [e for e in envelopes
                     if e.decision_at_utc <= anchor_text],
                    as_of_utc=anchor_text,
                )
            except ContractError:
                skips["no_root_at_anchor"] += 1
                continue
            entry = {
                "station": station, "day": day, "hour": hour,
                "running_high": max(
                    o["reported_temperature_c"] for o in visible
                ),
            }
            failed = None
            for arm in ARMS:
                curve = arm_curves[arm]
                metar_ceiling.load_artifact = (
                    lambda path=None, _c=curve: _c
                )
                try:
                    packets = compile_station_p1_packets(
                        predecessor=root,
                        observations=visible,
                        registry_rows=registry,
                        compilation_at_utc=anchor_text,
                        trajectory_artifact=None,
                        ceiling_artifact=curve,
                    )
                except (ContractError, Exception) as exc:
                    failed = f"compile:{type(exc).__name__}"
                    break
                finally:
                    metar_ceiling.load_artifact = original_loader
                if not packets:
                    failed = "no_packets"
                    break
                served = packets[-1]["expected_envelope"]["probabilities"]
                argmax = max(served, key=lambda b: served[b])
                entry[arm] = {
                    "hit": argmax == truth_bucket,
                    "signed": order[argmax] - order[truth_bucket],
                    "p_truth": served.get(truth_bucket, 0.0),
                }
            if failed:
                skips[failed] += 1
                continue
            entry["margin"] = settled - entry["running_high"]
            records.append(entry)

    print(f"\n可评行 {len(records)}   跳过: {dict(skips)}", flush=True)

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
                1 for r in subset if not r[ARMS[0]]["hit"] and r[arm]["hit"]
            )
            broke = sum(
                1 for r in subset if r[ARMS[0]]["hit"] and not r[arm]["hit"]
            )
            tail = "" if arm == ARMS[0] else f"  救回{fixed}/弄坏{broke}"
            print(f"  {arm:14s} n={n:4d}  命中={hit:.3f}  偏移={signed:+.2f}"
                  f"  对数分={logp:.3f}  Brier={brier:.4f}{tail}")

    for hour in ANCHOR_HOURS:
        subset = [r for r in records if r["hour"] == hour]
        report(f"===== 当地 {hour}:00 as-of 编译·服务信封 =====", subset)
        report("  其中峰值前 (锚点时 running_high < 真值)",
               [r for r in subset if r["margin"] >= 0.05])
        print()
    report("===== 全锚点合并 =====", records)
    report("  其中峰值前", [r for r in records if r["margin"] >= 0.05])


if __name__ == "__main__":
    main()
