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

Charter 158: the same line is then tempered per disagreement group
(``taf_likelihood.disagreement_group``).  Every forward TAF with a TX for a
settled station-day is paired with the P0 that was current when it became
available -- what P0-M multiplies at that moment -- and weighted 1/n within its
station-day.  A tempered group's lambda is the grid value with the best
weighted log score under the fitted line (``TEMPERED_GROUPS`` -- today only
the unreasoned cold TAF; every other group, and one under ``MIN_GROUP_WEIGHT``
station-days, keeps 1).  The tempering serves only when a walk-forward over the same
cases -- line and lambdas fitted on strictly earlier days -- beats lambda = 1
on average; otherwise it is written with state IDENTITY and every group reads 1.

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
LAMBDA_GRID = tuple(round(0.25 * i, 2) for i in range(13))   # 0 .. 3
MIN_GROUP_WEIGHT = 12.0
# The groups whose lambda is fitted; every other group reads 1.  Measured
# 2026-09-19 (walk-forward against lambda = 1, four variants of line and
# window): the cold TAF without rain or overcast is the one group that pays,
# +0.075..+0.094 nat on its own station-days at lambda 0.5 every time and
# +0.013..+0.017 overall; fitting all four groups gave -0.004..+0.009, the
# other three trading noise.
TEMPERED_GROUPS: tuple[str, ...] = (TL.GROUP_UNREASONED_COLD,)


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


def load_tempering_cases(
    conn: sqlite3.Connection, *, since: str, through: str
) -> list[dict[str, Any]]:
    """Every forward TAF with a TX and its weather, on the P0 current when it arrived."""

    truth = load_truth(conn)
    seen: set[tuple[str, str, str, float]] = set()
    raw: list[tuple[str, str, str, float, float, float]] = []
    for station, day, issued, available, received, tx, cloud, precip in conn.execute(
        "SELECT station_id, event_date, issued_at_utc, provider_available_at_utc,"
        " received_at_utc, tx_c, cloud_cover_fraction, precipitation_probability"
        " FROM calibration_forward_taf_capture"
        " WHERE tx_c IS NOT NULL AND event_date>=? AND event_date<=?",
        (since, through),
    ):
        key = (str(station), str(day), str(issued), float(tx))
        if (key[0], key[1]) not in truth or key in seen:
            continue
        seen.add(key)
        raw.append((
            key[0], key[1], str(available or received or issued), float(tx),
            float(cloud), float(precip),
        ))
    roots: dict[tuple[str, str], list[tuple[str, str]]] = {}
    geometry: dict[str, dict[str, Any] | None] = {}
    cases: list[dict[str, Any]] = []
    for station, day, when, tx, cloud, precip in sorted(raw):
        scope = (station, day)
        if scope not in roots:
            roots[scope] = sorted(
                (str(at), str(cid)) for at, cid in conn.execute(
                    "SELECT decision_at_utc, checkpoint_id"
                    " FROM basket_probability_envelope"
                    " WHERE station_id=? AND event_date=? AND stage='P0'"
                    " AND state='COMMITTED'",
                    scope,
                )
            )
        current = [cid for at, cid in roots[scope] if at <= when]
        if not current:
            continue
        cid = current[-1]
        if cid not in geometry:
            row = conn.execute(
                "SELECT json_extract(lineage_json,'$.model_center_c'),"
                " probabilities_json, bucket_ordinals_json,"
                " json_extract(lineage_json,'$.bucket_intervals_c_json')"
                " FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1",
                (cid,),
            ).fetchone()
            geometry[cid] = None
            if row is not None and row[0] not in (None, "", "NONE") and row[3]:
                probabilities = {
                    str(k): float(v) for k, v in json.loads(row[1]).items()
                }
                ordinals = {str(k): int(v) for k, v in json.loads(row[2]).items()}
                lineage = {"bucket_intervals_c_json": row[3]}
                axis = _bucket_centers_c(lineage, bucket_ordinals=ordinals)
                truth_bucket = _anchor_bucket_for_temperature(
                    truth[scope], root_lineage=lineage, bucket_ordinals=ordinals
                )
                mass = sum(probabilities.get(b, 0.0) for b in axis[0]) if axis else 0.0
                if axis is not None and truth_bucket is not None and mass > 0.0:
                    centers = dict(zip(axis[0], axis[1]))
                    geometry[cid] = {
                        "c0": float(row[0]),
                        "probabilities": probabilities,
                        "centers": centers,
                        "root_mean_c": sum(
                            probabilities.get(b, 0.0) * c for b, c in centers.items()
                        ) / mass,
                        "truth_bucket": truth_bucket,
                    }
        shape = geometry[cid]
        if shape is None:
            continue
        cases.append({
            "station_id": station,
            "event_date": day,
            "tx_c": tx,
            "cloud_cover": cloud,
            "precip_prob": precip,
            "group": TL.disagreement_group(tx - shape["root_mean_c"], cloud, precip),
            **shape,
        })
    per_day: dict[tuple[str, str], int] = defaultdict(int)
    for case in cases:
        per_day[(case["station_id"], case["event_date"])] += 1
    for case in cases:
        case["weight"] = 1.0 / per_day[(case["station_id"], case["event_date"])]
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


def _scored(
    line_artifact: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], dict[str, float]]]:
    """Each case with its untempered exponents under the line -- serving's function."""

    out = []
    for case in cases:
        exponents, _ = TL.exponent_by_bucket(
            line_artifact, station_id=case["station_id"], tx_c=case["tx_c"],
            root_center_c=case["c0"], bucket_centers_c=case["centers"],
        )
        out.append((case, exponents))
    return out


def _tempered_gain(
    case: Mapping[str, Any], exponents: Mapping[str, float], lam: float
) -> float | None:
    """Log score of P0 x L^lambda over P0, as the walk-forward above scores it."""

    probabilities = case["probabilities"]
    low = min(exponents.values())
    weighted = {
        b: p * math.exp(-lam * (exponents[b] - low))
        for b, p in probabilities.items() if b in exponents
    }
    total = sum(weighted.values())
    if total <= 0.0:
        return None
    posterior = {b: v / total for b, v in weighted.items()}
    return (
        _floored_log(posterior, case["truth_bucket"])
        - _floored_log(probabilities, case["truth_bucket"])
    )


def fit_tempering(
    line_artifact: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, float], dict[str, float]]:
    """Per group, the grid lambda with the best weighted log score (ties toward 1)."""

    scored = _scored(line_artifact, cases)
    lambdas: dict[str, float] = {}
    weights: dict[str, float] = {}
    for group in TL.TEMPERING_GROUPS:
        members = [(c, e) for c, e in scored if c["group"] == group]
        weights[group] = sum(c["weight"] for c, _ in members)
        if group not in TEMPERED_GROUPS or weights[group] < MIN_GROUP_WEIGHT:
            lambdas[group] = 1.0
            continue

        def total(lam: float) -> float:
            return sum(
                c["weight"] * gain
                for c, e in members
                if (gain := _tempered_gain(c, e, lam)) is not None
            )

        lambdas[group] = max(
            LAMBDA_GRID, key=lambda lam: (round(total(lam), 9), -abs(lam - 1.0))
        )
    return lambdas, weights


def walk_forward_tempering(
    line_cases: Sequence[Mapping[str, Any]],
    tempering_cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Tempered against lambda = 1, each day on a line and lambdas from earlier days only."""

    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in tempering_cases:
        by_day[case["event_date"]].append(case)
    gains_by_day: dict[str, float] = {}
    by_group: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for day in sorted(by_day):
        train_lines = [c for c in line_cases if c["event_date"] < day]
        if len({c["event_date"] for c in train_lines}) < MIN_TRAIN_DAYS:
            continue
        line = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE},
                **fit(train_lines)}
        lambdas, _ = fit_tempering(
            line, [c for c in tempering_cases if c["event_date"] < day]
        )
        num = den = 0.0
        for case, exponents in _scored(line, by_day[day]):
            tempered = _tempered_gain(case, exponents, lambdas[case["group"]])
            plain = _tempered_gain(case, exponents, 1.0)
            if tempered is None or plain is None:
                continue
            num += case["weight"] * (tempered - plain)
            den += case["weight"]
            by_group[case["group"]][0] += case["weight"] * (tempered - plain)
            by_group[case["group"]][1] += case["weight"]
        if den > 0.0:
            gains_by_day[day] = num / den
    means = list(gains_by_day.values())
    if len(means) < 5:
        return {"state": TL.GATE_IDENTITY, "reason": "too_few_days",
                "days": len(means)}
    rng = random.Random(20260919)
    boot = sorted(
        statistics.mean(rng.choice(means) for _ in means)
        for _ in range(BOOTSTRAP_DRAWS)
    )
    mean = statistics.mean(means)
    return {
        # Tempering replaces lambda = 1 only where it has done better on days
        # it had not seen; the interval is reported, not required.
        "state": TL.GATE_SERVE if mean > 0.0 else TL.GATE_IDENTITY,
        "by_day_mean_gain_vs_lambda_one_nat": mean,
        "ci_low": boot[int(0.025 * BOOTSTRAP_DRAWS)],
        "ci_high": boot[int(0.975 * BOOTSTRAP_DRAWS)],
        "days": len(means),
        "first_day": min(gains_by_day),
        "last_day": max(gains_by_day),
        "gain_by_group_nat": {
            g: (v[0] / v[1] if v[1] > 0 else None) for g, v in by_group.items()
        },
        "station_days_by_group": {g: v[1] for g, v in by_group.items()},
    }


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
    conn = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True, timeout=120)
    try:
        tempering_cases = load_tempering_cases(
            conn, since=args.since, through=args.through
        )
    finally:
        conn.close()
    line = fit(cases)
    served_line = {"schema": TL.SCHEMA, "gate": {"state": TL.GATE_SERVE}, **line}
    lambdas, group_weight = fit_tempering(served_line, tempering_cases)
    tempering_gate = walk_forward_tempering(cases, tempering_cases)
    body = {
        "schema": TL.SCHEMA,
        "rule": TL.RULE,
        **line,
        "gate": gate,
        TL.TEMPERING_KEY: {
            "rule": TL.TEMPERING_RULE,
            "state": tempering_gate["state"],
            "lambda_by_group": lambdas,
            "group_weight_station_days": group_weight,
            "tafs": len(tempering_cases),
            "grid": list(LAMBDA_GRID),
            "min_group_weight": MIN_GROUP_WEIGHT,
            "walk_forward": tempering_gate,
        },
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
        "tempering": {
            "state": tempering_gate["state"],
            "lambda_by_group": lambdas,
            "gain_vs_lambda_one": tempering_gate.get(
                "by_day_mean_gain_vs_lambda_one_nat"
            ),
            "ci": [tempering_gate.get("ci_low"), tempering_gate.get("ci_high")],
            "tafs": len(tempering_cases),
        },
        "out": str(out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
