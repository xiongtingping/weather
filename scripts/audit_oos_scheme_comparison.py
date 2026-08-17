#!/usr/bin/env python3
"""Score three forecast schemes on one fixed candidate set, and interval the gap.

Every scope's OOS evaluation now carries the same holdout day scored three
ways -- climatology, the seated systems weighted alike, and the learned
weights over those same systems -- so the only thing that moves between the
last two is the weighting.  This pools those samples and reports CRPS, Brier
and log score for each scheme, plus a bootstrap interval on equal minus
learned.

The resampling unit is the **day**, not the station-day.  Forty-five stations
share one atmosphere, so their errors on a given date are anything but
independent; resampling station-days would shrink the interval by roughly the
square root of the station count and manufacture a significance that is not
there.  Days are drawn with replacement and every station-day belonging to a
drawn day comes with it.

Read-only.  Places no orders and writes nothing but its report.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# Always present.  Candidate weightings (min_variance, inverse_variance, ...)
# ride in the same per-day record and are picked up by name wherever every
# sampled day carries them, so a scheme added upstream needs no change here.
SCHEMES = ("climate", "equal_weight", "learned")
METRICS = ("crps", "brier", "nll")
# Log score is reported in bits because that is the one figure comparable
# across scopes with different bucket counts; a percentage of nats is not.
LN2 = math.log(2.0)


def _load(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    state: str | None,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT station_id, season_bin, lead_bucket, run_cycle, oos_state,",
        "       oos_metrics_json",
        "FROM calibration_cross_native_ensemble_oos_evaluation",
        "WHERE json_extract(oos_metrics_json,",
        "  '$.equal_weight_diagnostic.scheme_daily_scores') IS NOT NULL",
    ]
    params: list[Any] = []
    if since:
        sql.append("AND evaluated_at_utc >= ?")
        params.append(since)
    if state:
        sql.append("AND oos_state = ?")
        params.append(state)
    rows: list[dict[str, Any]] = []
    for station, season, lead, cycle, oos_state, metrics_json in conn.execute(
        "\n".join(sql), params
    ):
        try:
            diagnostic = json.loads(metrics_json)["equal_weight_diagnostic"]
        except (KeyError, TypeError, ValueError):
            continue
        for day in diagnostic.get("scheme_daily_scores") or []:
            if not all(scheme in day for scheme in SCHEMES):
                continue
            rows.append({
                "station_id": station,
                "scope": (station, season, lead, cycle),
                "oos_state": oos_state,
                "event_date": str(day["event_date"]),
                "candidate_set": tuple(
                    diagnostic.get("fixed_candidate_system_ids") or ()
                ),
                **{
                    key: value for key, value in day.items()
                    if key != "event_date"
                },
            })
    return rows


def _schemes_present(rows: list[dict[str, Any]]) -> list[str]:
    """Scheme names every sampled day carries.

    A scheme that only some days scored cannot be pooled against the others:
    its mean would be taken over a different set of weather than the mean it
    is compared with.  Requiring presence everywhere keeps every comparison
    paired on the same days.
    """

    shared = set(rows[0]) - {
        "station_id", "scope", "oos_state", "event_date", "candidate_set",
    }
    for row in rows[1:]:
        shared &= set(row)
    ordered = [scheme for scheme in SCHEMES if scheme in shared]
    return ordered + sorted(shared - set(ordered))


def _bootstrap(
    by_day: dict[str, list[float]],
    *,
    draws: int,
    seed: int,
) -> dict[str, float]:
    """Percentile interval over days resampled with replacement."""

    days = sorted(by_day)
    observed = statistics.mean(
        value for day in days for value in by_day[day]
    )
    if len(days) < 2:
        return {
            "mean": observed,
            "ci_low": float("nan"),
            "ci_high": float("nan"),
            "day_count": len(days),
            "draws": 0,
        }
    rng = random.Random(seed)
    means: list[float] = []
    for _draw in range(draws):
        pooled: list[float] = []
        for _index in range(len(days)):
            pooled.extend(by_day[days[rng.randrange(len(days))]])
        if pooled:
            means.append(statistics.mean(pooled))
    means.sort()
    return {
        "mean": observed,
        "ci_low": means[int(0.025 * (len(means) - 1))],
        "ci_high": means[int(0.975 * (len(means) - 1))],
        "day_count": len(days),
        "draws": len(means),
    }


def compare(
    rows: list[dict[str, Any]],
    *,
    draws: int,
    seed: int,
) -> dict[str, Any]:
    schemes = _schemes_present(rows)
    scheme_means = {
        scheme: {
            metric: statistics.mean(row[scheme][metric] for row in rows)
            for metric in METRICS
        }
        for scheme in schemes
    }
    for scheme in schemes:
        scheme_means[scheme]["log_score_bits"] = (
            scheme_means[scheme]["nll"] / LN2
        )

    def _gap(challenger: str, incumbent: str) -> dict[str, Any]:
        """``challenger`` minus ``incumbent``, intervalled over days.

        Every score is a loss, so a positive difference means the challenger
        scored worse and the incumbent held its place.
        """

        out: dict[str, Any] = {}
        for metric in METRICS:
            by_day: dict[str, list[float]] = defaultdict(list)
            for row in rows:
                by_day[row["event_date"]].append(
                    row[challenger][metric] - row[incumbent][metric]
                )
            out[metric] = _bootstrap(dict(by_day), draws=draws, seed=seed)
        by_day_bits: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            by_day_bits[row["event_date"]].append(
                (row[challenger]["nll"] - row[incumbent]["nll"]) / LN2
            )
        out["log_score_bits"] = _bootstrap(
            dict(by_day_bits), draws=draws, seed=seed
        )
        out["incumbent_beats_challenger_station_day_share"] = (
            sum(
                1 for row in rows
                if row[incumbent]["nll"] < row[challenger]["nll"]
            ) / len(rows)
        )
        return out

    gap = _gap("equal_weight", "learned")
    # Every candidate weighting against the one actually served, and against
    # equal weight, so a scheme that only beats the incumbent by being closer
    # to flat is visible as such.
    challengers = {
        scheme: {
            "vs_learned": _gap(scheme, "learned"),
            "vs_equal_weight": _gap(scheme, "equal_weight"),
        }
        for scheme in schemes
        if scheme not in ("climate", "equal_weight", "learned")
    }
    station_days = len(rows)
    learned_wins = sum(
        1 for row in rows
        if row["learned"]["nll"] < row["equal_weight"]["nll"]
    )
    return {
        "schema": "oos_scheme_comparison_v1",
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
        "station_day_count": station_days,
        "scope_count": len({row["scope"] for row in rows}),
        "station_count": len({row["station_id"] for row in rows}),
        "day_count": len({row["event_date"] for row in rows}),
        "resampling_unit": "event_date",
        "distinct_candidate_sets": sorted(
            "+".join(candidate_set)
            for candidate_set in {row["candidate_set"] for row in rows}
        ),
        "schemes_compared": schemes,
        "scheme_means": scheme_means,
        "equal_minus_learned": gap,
        "candidate_weightings": challengers,
        "learned_beats_equal_station_day_share": (
            learned_wins / station_days if station_days else 0.0
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since", help="Earliest evaluated_at_utc.")
    parser.add_argument(
        "--state",
        help="Restrict to one oos_state, e.g. PRODUCTION_NO_LIVE_OOS_PASS.",
    )
    parser.add_argument(
        "--candidate-set",
        help=(
            "Restrict to scopes whose fixed candidate set is exactly this "
            "'+'-joined list of system ids.  Without it every candidate set "
            "is pooled and the report names the ones it found."
        ),
    )
    parser.add_argument("--bootstrap-draws", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        rows = _load(conn, since=args.since, state=args.state)
    finally:
        conn.close()
    if args.candidate_set:
        wanted = tuple(args.candidate_set.split("+"))
        rows = [row for row in rows if row["candidate_set"] == wanted]
    if not rows:
        print(json.dumps({
            "schema": "oos_scheme_comparison_v1",
            "state": "NO_SAMPLES",
            "calls_order_gateway": False,
        }, ensure_ascii=False, sort_keys=True))
        return 1
    report = compare(rows, draws=args.bootstrap_draws, seed=args.seed)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
