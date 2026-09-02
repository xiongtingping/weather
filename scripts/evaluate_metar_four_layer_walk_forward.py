#!/usr/bin/env python3
"""Score the four layers day by day, each day fitted only on the days before it.

The acceptance question is not whether the chain fits -- it is whether a rule
that could actually have been run beats the one production runs today.  So
every event day is scored under artifacts refitted on strictly earlier days,
and the arms are all the same code path with one switch moved:

  root            the P0/P0-M distribution as it stood
  hard            root under the observation hard bound          (P1 before 2026-08-23)
  hard+clock      hard, then the clock-only phase split          (what production serves today)
  four_layer      reanchor, hard, remaining warming, phase       (the proposal)
  -reanchor       four_layer with the translation switched off
  -remaining      four_layer with the likelihood ratio at one
  -phase          four_layer with the split switched off

``hard+clock`` is the cooling-side ceiling's own model form -- a rate per
hours-left bin and nothing else -- refitted here on this sample stream under
the same walk-forward rule.  Using it rather than the shipped artifact keeps
the comparison free of a sampling difference: the ceiling is fitted on whole
station-days and these samples are per report, and a gap between the two arms
should be the layers, not the bookkeeping.

Two views, because they answer different questions.  Every sample says how the
chain behaves over the whole day.  The standing view takes the last sample at
or before a fixed local hour, which is the seat the ledger could actually have
traded; the peak-not-yet-reached split inside it is the early-prophet test the
2026-08-21 ruling demands, and it is reported separately because a hit after
the day's high has been observed is a confirmation, not a prediction.

    python3 scripts/evaluate_metar_four_layer_walk_forward.py \
        --samples data/runtime/bayes_basket_target_nwp/metar_phase_hazard_samples.db \
        --min-train-days 7
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    _anchor_bucket_for_temperature,
    rehydrate_lineage_documents,
)
from bayes_basket_target.metar_four_layer import (  # noqa: E402
    compile_four_layer_posterior,
    feature_row,
)
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    hard_bound_excluded_buckets,
)
from bayes_basket_target.p1_compiler import _bucket_geometry_c  # noqa: E402
from bayes_basket_target.contracts import ProbabilityEnvelope  # noqa: E402
import scripts.fit_metar_phase_hazard_artifact as hazard_fit  # noqa: E402
import scripts.fit_metar_reanchor_artifact as reanchor_fit  # noqa: E402

ARMS = (
    "root", "hard", "hard+clock", "four_layer",
    "-reanchor", "-remaining", "-phase",
)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


class RootCache:
    """Root geometry by checkpoint, parsed once per evaluation."""

    def __init__(self, target: sqlite3.Connection) -> None:
        self._target = target
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, checkpoint_id: str) -> dict[str, Any] | None:
        if checkpoint_id in self._cache:
            return self._cache[checkpoint_id]
        row = self._target.execute(
            "SELECT stage, checkpoint_id, station_id, event_date,"
            " settlement_timezone, decision_at_utc, fresh_until_utc,"
            " bucket_ordinals_json, probabilities_json, posterior_version,"
            " posterior_sha256, bridge_sha256, lineage_json"
            " FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            self._cache[checkpoint_id] = None
            return None
        lineage = rehydrate_lineage_documents(
            self._target, json.loads(row[12] or "{}")
        )
        try:
            envelope = ProbabilityEnvelope.from_mapping({
                "stage": row[0], "checkpoint_id": row[1], "station_id": row[2],
                "event_date": row[3], "settlement_timezone": row[4],
                "decision_at_utc": row[5], "fresh_until_utc": row[6],
                "bucket_ordinals": json.loads(row[7]),
                "probabilities": json.loads(row[8]),
                "posterior_version": row[9], "posterior_sha256": row[10],
                "bridge_sha256": row[11], "lineage": lineage,
            })
            centers, uppers = _bucket_geometry_c(
                envelope, unit="C", upper_by_bucket={}
            )
            intervals = json.loads(
                str(lineage.get("bucket_intervals_c_json") or "{}")
            )
        except (ContractError, TypeError, ValueError, KeyError):
            self._cache[checkpoint_id] = None
            return None
        if not intervals:
            self._cache[checkpoint_id] = None
            return None
        view = {
            "probabilities": dict(envelope.probabilities),
            "ordinals": dict(envelope.bucket_ordinals),
            "centers": centers,
            "uppers": uppers,
            "intervals": intervals,
            "intervals_json": str(lineage.get("bucket_intervals_c_json")),
        }
        self._cache[checkpoint_id] = view
        return view


def clock_only_artifact(hazard: Mapping[str, Any]) -> dict[str, Any]:
    """The same artifact with every state coefficient set to zero.

    That is the cooling-side ceiling's model form exactly: a rate per bin of
    hours left, blind to cloud, drawdown, momentum and rain.
    """

    clone = json.loads(json.dumps(hazard))
    clone["phase"]["coefficients"] = {
        name: 0.0 for name in clone["phase"]["coefficients"]
    }
    return clone


def score(distribution: Mapping[str, float], *, truth_bucket: str,
          ordinals: Mapping[str, int], centers: Mapping[str, float],
          truth_c: float) -> dict[str, float]:
    argmax = max(distribution, key=lambda bucket: distribution[bucket])
    # The probability-weighted centre, which is what the basket actually seats
    # the ladder on (charter 54).  A layer can improve this while leaving the
    # bucket argmax alone -- after the hard bound and the split that argmax
    # sits on the running high's bucket -- so a chain scored only on argmax
    # cannot see the reanchor's channel at all.
    centre = sum(
        float(value) * float(centers[bucket])
        for bucket, value in distribution.items()
    )
    probability = float(distribution.get(truth_bucket, 0.0))
    brier = sum(
        (float(value) - (1.0 if bucket == truth_bucket else 0.0)) ** 2
        for bucket, value in distribution.items()
    )
    return {
        "hit": 1.0 if argmax == truth_bucket else 0.0,
        "within_one": 1.0 if abs(
            int(ordinals[argmax]) - int(ordinals[truth_bucket])
        ) <= 1 else 0.0,
        "log_score": math.log(max(probability, 1e-9)),
        "brier": brier,
        "signed": float(
            int(ordinals[argmax]) - int(ordinals[truth_bucket])
        ),
        "centre_error_c": centre - float(truth_c),
        "centre_abs_error_c": abs(centre - float(truth_c)),
        "centre_squared_error": (centre - float(truth_c)) ** 2,
    }


def walk_forward_artifacts(
    all_rows: Sequence[Mapping[str, Any]], day: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Both artifacts fitted on strictly earlier days than ``day``.

    Factored out so the ledger injector fits exactly what this evaluation
    scored.  Two fits of the same window that differ by a line are two
    different experiments wearing one name.
    """

    train = [row for row in all_rows if str(row["event_date"]) < day]
    if len(train) < 500:
        return None
    fallbacks = hazard_fit.fleet_means(train, hazard_fit.FULL_FEATURES)
    risers = [row for row in train if int(row["delta_c"]) >= 1]
    try:
        hazard = {
            "schema": hazard_fit.SCHEMA,
            "trained_through": max(str(row["event_date"]) for row in train),
            "feature_fallbacks": fallbacks,
            "phase": hazard_fit.fit_phase_rung(train, fallbacks),
            "continuation_full": hazard_fit.fit_continuation(
                risers, hazard_fit.FULL_FEATURES, fallbacks
            ),
            "continuation_phase_only": hazard_fit.fit_continuation(
                risers, hazard_fit.PHASE_FEATURES, fallbacks
            ),
            "content_sha256": "walkforward",
        }
    except SystemExit:
        return None
    bias_rows = [
        row for row in train
        if row["root_bias_recent_c"] is not None
        and row["root_center_c"] is not None
        and int(row["root_bias_report_count"]) >= 2
    ]
    pooled = reanchor_fit.fit_slope(bias_rows)
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in bias_rows:
        grouped[reanchor_fit.hours_bin(
            float(row["remaining_hours"])
        )].append(row)
    bins: dict[str, Any] = {}
    for index in range(len(reanchor_fit.HOURS_EDGES) - 1):
        bucket = grouped.get(index, [])
        if len(bucket) < reanchor_fit.MIN_BIN_ROWS:
            bins[str(index)] = {**pooled, "backed_off": True}
        else:
            bins[str(index)] = {
                **reanchor_fit.fit_slope(bucket), "backed_off": False
            }
    reanchor = {
        "schema": reanchor_fit.SCHEMA,
        "hours_edges": list(reanchor_fit.HOURS_EDGES),
        "max_shift_c": reanchor_fit.MAX_SHIFT_C,
        "minimum_bias_reports": reanchor_fit.MIN_BIAS_REPORTS,
        "bins": bins,
        "pooled": pooled,
        "content_sha256": "walkforward",
    }
    return hazard, reanchor


def evaluate_day(
    *,
    samples: Sequence[Mapping[str, Any]],
    roots: RootCache,
    hazard: Mapping[str, Any],
    hazard_clock: Mapping[str, Any],
    reanchor: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample in samples:
        view = roots.get(str(sample["root_checkpoint_id"]))
        if view is None:
            continue
        running_high = float(sample["running_high_c"])
        excluded = hard_bound_excluded_buckets(
            sorted(view["ordinals"], key=lambda b: view["ordinals"][b]),
            view["uppers"],
            running_high,
        )
        truth_bucket = _anchor_bucket_for_temperature(
            float(sample["truth_tmax_c"]),
            root_lineage={"bucket_intervals_c_json": view["intervals_json"]},
            bucket_ordinals=view["ordinals"],
        )
        high_bucket = _anchor_bucket_for_temperature(
            running_high,
            root_lineage={"bucket_intervals_c_json": view["intervals_json"]},
            bucket_ordinals=view["ordinals"],
        )
        if truth_bucket is None or high_bucket is None:
            continue
        if truth_bucket in set(excluded):
            # The observation and the truth disagree; the hard bound would be
            # scored against a bucket it has already ruled out.  That is the
            # correction lane's row, not this evaluation's.
            continue
        features = feature_row(
            remaining_hours=float(sample["remaining_hours"]),
            drawdown_from_high_c=float(sample["drawdown_from_high_c"]),
            momentum_c_per_hour=float(sample["momentum_c_per_hour"]),
            cloud_cover_fraction=float(sample["cloud_cover_fraction"]),
            precipitation_present=bool(sample["precipitation_present"]),
            dewpoint_depression_c=float(sample["dewpoint_depression_c"]),
            wind_speed_mps=float(sample["wind_speed_mps"]),
            warming_so_far_c=float(sample["warming_so_far_c"]),
            curve_shape_realized=sample["curve_shape_realized"],
        )
        common = {
            "root_probabilities": view["probabilities"],
            "bucket_ordinals": view["ordinals"],
            "bucket_centers_c": view["centers"],
            "bucket_intervals_c": view["intervals"],
            "hard_excluded_buckets": excluded,
            "running_high_c": running_high,
            "running_high_bucket": high_bucket,
            "features": features,
            "bias_recent_c": sample["root_bias_recent_c"],
            "bias_report_count": int(sample["root_bias_report_count"]),
        }
        arms: dict[str, Mapping[str, float]] = {}
        arms["root"] = view["probabilities"]
        try:
            arms["hard"], _ = compile_four_layer_posterior(
                **common, hazard_artifact=None, reanchor_artifact=None,
                reanchor_enabled=False, remaining_enabled=False,
                phase_enabled=False,
            )
            arms["hard+clock"], _ = compile_four_layer_posterior(
                **common, hazard_artifact=hazard_clock,
                reanchor_artifact=None, reanchor_enabled=False,
                remaining_enabled=False, phase_enabled=True,
            )
            arms["four_layer"], _ = compile_four_layer_posterior(
                **common, hazard_artifact=hazard, reanchor_artifact=reanchor,
            )
            arms["-reanchor"], _ = compile_four_layer_posterior(
                **common, hazard_artifact=hazard, reanchor_artifact=reanchor,
                reanchor_enabled=False,
            )
            arms["-remaining"], _ = compile_four_layer_posterior(
                **common, hazard_artifact=hazard, reanchor_artifact=reanchor,
                remaining_enabled=False,
            )
            arms["-phase"], _ = compile_four_layer_posterior(
                **common, hazard_artifact=hazard, reanchor_artifact=reanchor,
                phase_enabled=False,
            )
        except ContractError:
            continue
        rows.append({
            "station_id": sample["station_id"],
            "event_date": sample["event_date"],
            "as_of_utc": sample["as_of_utc"],
            "settlement_timezone": sample["settlement_timezone"],
            "remaining_hours": float(sample["remaining_hours"]),
            "weight": float(sample["station_day_weight"]),
            "pre_peak": int(sample["delta_c"]) >= 1,
            "scores": {
                arm: score(
                    distribution,
                    truth_bucket=truth_bucket,
                    ordinals=view["ordinals"],
                    centers=view["centers"],
                    truth_c=float(sample["truth_tmax_c"]),
                )
                for arm, distribution in arms.items()
            },
        })
    return rows


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    total = sum(row["weight"] for row in rows) or 1.0
    for arm in ARMS:
        out[arm] = {
            metric: sum(
                row["weight"] * row["scores"][arm][metric] for row in rows
            ) / total
            for metric in (
                "hit", "within_one", "log_score", "brier", "signed",
                "centre_error_c", "centre_abs_error_c",
                "centre_squared_error",
            )
        }
        out[arm]["n"] = float(len(rows))
    return out


def print_block(title: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        print(f"{title}: 无样本")
        return
    summary = summarise(rows)
    base = summary["hard+clock"]
    print(f"\n{title}  n={len(rows)}")
    print(f"  {'arm':<12}{'命中':>8}{'±1桶':>8}{'对数分':>9}{'Brier':>8}"
          f"{'中心MAE':>9}{'中心RMSE':>9}{'中心偏':>8}{'vs今天':>8}")
    for arm in ARMS:
        value = summary[arm]
        delta = value["hit"] - base["hit"]
        print(
            f"  {arm:<12}{value['hit']:8.3f}{value['within_one']:8.3f}"
            f"{value['log_score']:9.3f}{value['brier']:8.3f}"
            f"{value['centre_abs_error_c']:9.3f}"
            f"{math.sqrt(max(value['centre_squared_error'], 0.0)):9.3f}"
            f"{value['centre_error_c']:+8.2f}{delta:+8.3f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", required=True)
    parser.add_argument("--target-db", default=str(
        ROOT / "data" / "bayes_basket_target_ssot.db"
    ))
    parser.add_argument("--min-train-days", type=int, default=7)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.samples}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    all_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM metar_phase_hazard_sample"
            " ORDER BY event_date, station_id, as_of_utc"
        )
    ]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in all_rows:
        by_day[str(row["event_date"])].append(row)
    days = sorted(by_day)
    target = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True)
    roots = RootCache(target)

    scored: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        if index < args.min_train_days:
            continue
        fitted = walk_forward_artifacts(all_rows, day)
        if fitted is None:
            print(f"  {day}: too little history to fit", file=sys.stderr)
            continue
        hazard, reanchor = fitted
        train = [row for row in all_rows if str(row["event_date"]) < day]
        scored.extend(evaluate_day(
            samples=by_day[day],
            roots=roots,
            hazard=hazard,
            hazard_clock=clock_only_artifact(hazard),
            reanchor=reanchor,
        ))
        print(
            f"  {day}: trained on {len(train)} samples through"
            f" {days[index - 1]}, scored {len(by_day[day])}",
            file=sys.stderr,
        )

    print(f"\n走查评估：{len(scored)} 个已评分样本，"
          f"每日只用严格更早的日子拟合")
    print_block("== 全部样本 ==", scored)
    print_block(
        "== 峰值前（当时的最高仍低于真值）==",
        [row for row in scored if row["pre_peak"]],
    )
    for low, high, label in (
        (0.0, 3.0, "剩余 0-3h"), (3.0, 6.0, "剩余 3-6h"),
        (6.0, 10.0, "剩余 6-10h"), (10.0, 99.0, "剩余 10h+"),
    ):
        print_block(
            f"== {label} ==",
            [row for row in scored if low <= row["remaining_hours"] < high],
        )

    standing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_scope: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in scored:
        zone = ZoneInfo(str(row["settlement_timezone"]))
        instant = _parse(str(row["as_of_utc"]))
        base = datetime.fromisoformat(str(row["event_date"]))
        for label, hour in (("noon", 12), ("15h", 15), ("17h", 17)):
            cutoff = base.replace(hour=hour, tzinfo=zone)
            if instant <= cutoff:
                key = (label, str(row["station_id"]), str(row["event_date"]))
                held = by_scope.get(key)
                if held is None or _parse(str(held["as_of_utc"])) < instant:
                    by_scope[key] = row
    for (label, _station, _day), row in by_scope.items():
        standing[label].append(row)
    for label, title in (
        ("noon", "== 当地正午站立 =="),
        ("15h", "== 当地 15:00 站立 =="),
        ("17h", "== 当地 17:00 站立 =="),
    ):
        rows = standing.get(label, [])
        print_block(title, rows)
        print_block(
            f"{title[:-3]}·峰值前 ==",
            [row for row in rows if row["pre_peak"]],
        )

    if args.out:
        Path(args.out).write_text(json.dumps({
            "scored_n": len(scored),
            "all": summarise(scored) if scored else {},
            "standing": {
                label: summarise(rows)
                for label, rows in standing.items() if rows
            },
        }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
