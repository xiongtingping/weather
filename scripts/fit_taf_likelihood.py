#!/usr/bin/env python3
"""Fit P0-M's TAF likelihood and decide, walk-forward, whether it may serve.

Charter 150 (user ruling 2026-09-19).  For every settled station-day with a
TAF TX, the day's first P0-M decision gives three numbers: the TX, the P0
root's centre ``c0`` and the root's bucket probabilities.  The likelihood is

    x = TX - c0  |  y = truth - c0   ~   N(a_s + b_s * y, tau_s)

with each station's line shrunk toward the pool by ``K = 15`` station-days.

The gate is walk-forward: each event day is scored with parameters fitted on
strictly earlier days only, as ``root`` against ``root x L`` (both under the
production zero floor), and the per-day mean log-score gains are
bootstrapped by day.  The artifact is marked SERVE only when the lower end of
that interval is above zero; otherwise it is written with gate IDENTITY and
P0-M stays P0 -- the numbers are kept so the board can show how close it is.

    python3 scripts/fit_taf_likelihood.py --through 2026-09-18
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import taf_likelihood as TL  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    PROBABILITY_MODEL_ZERO_FLOOR,
    _anchor_bucket_for_temperature,
    _bucket_centers_c,
)

MIN_TRAIN_DAYS = 7
MIN_STATION_ROWS = 4
BOOTSTRAP_DRAWS = 4000


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    return {
        (str(row[0]), str(row[1])): float(row[2])
        for row in conn.execute(
            "SELECT t.station_id, t.local_date, t.official_tmax_c"
            " FROM calibration_station_daily_high_truth t"
            " WHERE t.source_lane='AWC' AND t.qc_status='ACCEPTED'"
            " AND t.revision_no=(SELECT MAX(revision_no)"
            "  FROM calibration_station_daily_high_truth t2"
            "  WHERE t2.station_id=t.station_id AND t2.local_date=t.local_date"
            "  AND t2.source_lane='AWC' AND t2.qc_status='ACCEPTED')"
        )
    }


def load_cases(
    conn: sqlite3.Connection, *, since: str, through: str
) -> list[dict[str, Any]]:
    """The first P0-M with a TX per settled station-day, on its P0 root."""

    truth = load_truth(conn)
    first: dict[tuple[str, str], tuple[str, float, float, str]] = {}
    for station, day, at, tx, centre, root_id in conn.execute(
        "SELECT station_id, event_date, decision_at_utc,"
        " json_extract(lineage_json,'$.taf_observed_tx_c'),"
        " json_extract(lineage_json,'$.model_center_c'),"
        " json_extract(lineage_json,'$.p0_root_checkpoint_id')"
        " FROM basket_probability_envelope"
        " WHERE stage='P0-M' AND state='COMMITTED'"
        " AND event_date>=? AND event_date<=?",
        (since, through),
    ):
        if tx in (None, "", "NONE") or centre in (None, "", "NONE") or not root_id:
            continue
        key = (str(station), str(day))
        if key not in truth:
            continue
        if key not in first or str(at) < first[key][0]:
            first[key] = (str(at), float(tx), float(centre), str(root_id))
    cases: list[dict[str, Any]] = []
    for (station, day), (_at, tx, centre, root_id) in sorted(first.items()):
        row = conn.execute(
            "SELECT probabilities_json, bucket_ordinals_json,"
            " json_extract(lineage_json,'$.bucket_intervals_c_json')"
            " FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1",
            (root_id,),
        ).fetchone()
        if row is None or not row[2]:
            continue
        probabilities = {str(k): float(v) for k, v in json.loads(row[0]).items()}
        ordinals = {str(k): int(v) for k, v in json.loads(row[1]).items()}
        lineage = {"bucket_intervals_c_json": row[2]}
        axis = _bucket_centers_c(lineage, bucket_ordinals=ordinals)
        truth_bucket = _anchor_bucket_for_temperature(
            truth[(station, day)], root_lineage=lineage, bucket_ordinals=ordinals
        )
        if axis is None or truth_bucket is None:
            continue
        cases.append({
            "station_id": station,
            "event_date": day,
            "tx_c": tx,
            "c0": centre,
            "truth_c": truth[(station, day)],
            "truth_bucket": truth_bucket,
            "probabilities": probabilities,
            "centers": dict(zip(axis[0], axis[1])),
        })
    return cases


def _line(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    my, mx = statistics.mean(ys), statistics.mean(xs)
    vy = sum((y - my) ** 2 for y in ys)
    b = (
        sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vy
        if vy > 0 else 0.0
    )
    a = mx - b * my
    resid = sum((x - a - b * y) ** 2 for x, y in zip(xs, ys))
    tau = math.sqrt(max(resid / max(len(xs) - 2, 1), TL.MIN_TAU_C ** 2))
    return a, b, tau


def fit(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    xs = [c["tx_c"] - c["c0"] for c in cases]
    ys = [c["truth_c"] - c["c0"] for c in cases]
    pa, pb, pt = _line(xs, ys)
    by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        by_station[case["station_id"]].append(case)
    stations: dict[str, dict[str, float]] = {}
    for station, rows in sorted(by_station.items()):
        if len(rows) < MIN_STATION_ROWS:
            continue
        a, b, tau = _line(
            [r["tx_c"] - r["c0"] for r in rows],
            [r["truth_c"] - r["c0"] for r in rows],
        )
        weight = len(rows) / (len(rows) + TL.SHRINK_K)
        stations[station] = {
            "a": pa + (a - pa) * weight,
            "b": pb + (b - pb) * weight,
            "tau": pt + (tau - pt) * weight,
            "n": len(rows),
        }
    return {
        "pool": {"a": pa, "b": pb, "tau": pt, "n": len(cases)},
        "stations": stations,
        "shrink_k": TL.SHRINK_K,
    }


def _floored_log(probabilities: Mapping[str, float], truth: str) -> float:
    lifted = {
        bucket: max(float(value), PROBABILITY_MODEL_ZERO_FLOOR)
        for bucket, value in probabilities.items()
    }
    return math.log(lifted[truth] / sum(lifted.values()))


def walk_forward(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        by_day[case["event_date"]].append(case)
    days = sorted(by_day)
    gains_by_day: dict[str, list[float]] = {}
    hits = {"root": 0, "taf": 0}
    scored = 0
    for index, day in enumerate(days):
        train = [c for d in days[:index] for c in by_day[d]]
        if len({c["event_date"] for c in train}) < MIN_TRAIN_DAYS:
            continue
        params = fit(train)
        artifact = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **params}
        for case in by_day[day]:
            likelihood, receipt = TL.compile_taf_likelihood(
                artifact, station_id=case["station_id"], tx_c=case["tx_c"],
                root_center_c=case["c0"], bucket_centers_c=case["centers"],
                buckets=case["probabilities"],
            )
            if receipt["state"] != "APPLIED":
                continue
            weighted = {
                b: p * likelihood[b] for b, p in case["probabilities"].items()
            }
            total = sum(weighted.values())
            if total <= 0.0:
                continue
            posterior = {b: v / total for b, v in weighted.items()}
            gain = (
                _floored_log(posterior, case["truth_bucket"])
                - _floored_log(case["probabilities"], case["truth_bucket"])
            )
            gains_by_day.setdefault(day, []).append(gain)
            scored += 1
            hits["root"] += int(
                max(case["probabilities"], key=case["probabilities"].get)
                == case["truth_bucket"]
            )
            hits["taf"] += int(
                max(posterior, key=posterior.get) == case["truth_bucket"]
            )
    means = [statistics.mean(values) for values in gains_by_day.values()]
    if len(means) < 5:
        return {"state": TL.GATE_IDENTITY, "reason": "too_few_days",
                "days": len(means), "station_days": scored}
    rng = random.Random(20260919)
    boot = sorted(
        statistics.mean(rng.choice(means) for _ in means)
        for _ in range(BOOTSTRAP_DRAWS)
    )
    low = boot[int(0.025 * BOOTSTRAP_DRAWS)]
    high = boot[int(0.975 * BOOTSTRAP_DRAWS)]
    return {
        "state": TL.GATE_SERVE if low > 0.0 else TL.GATE_IDENTITY,
        "by_day_mean_gain_nat": statistics.mean(means),
        "ci_low": low,
        "ci_high": high,
        "days": len(means),
        "station_days": scored,
        "mode_hit_root": hits["root"] / max(scored, 1),
        "mode_hit_taf": hits["taf"] / max(scored, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target-db", default=str(
        ROOT / "data" / "bayes_basket_target_ssot.db"
    ))
    parser.add_argument("--since", default="2026-08-16")
    parser.add_argument("--through", required=True)
    parser.add_argument("--out", default=str(TL.ARTIFACT_PATH))
    parser.add_argument("--fitted-at-utc", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True, timeout=120)
    try:
        cases = load_cases(conn, since=args.since, through=args.through)
    finally:
        conn.close()
    if len(cases) < 50:
        print(json.dumps({"state": "REFUSED_THIN", "cases": len(cases)}))
        return 3
    gate = walk_forward(cases)
    body = {
        "schema": TL.SCHEMA,
        "rule": TL.RULE,
        **fit(cases),
        "gate": gate,
        "trained_from": min(c["event_date"] for c in cases),
        "trained_through": max(c["event_date"] for c in cases),
        "fitted_at_utc": args.fitted_at_utc or _iso(datetime.now(timezone.utc)),
    }
    body["content_sha256"] = content_sha256(body)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.with_suffix(out.suffix + ".new")
    staging.write_text(json.dumps(body, indent=2, sort_keys=True))
    os.replace(staging, out)
    print(json.dumps({
        "schema": TL.SCHEMA,
        "gate": gate["state"],
        "cases": len(cases),
        "pool": body["pool"],
        **{k: (round(v, 4) if isinstance(v, float) else v)
           for k, v in gate.items() if k != "state"},
        "out": str(out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
