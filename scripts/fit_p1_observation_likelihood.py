#!/usr/bin/env python3
"""Fit P1's joint observation likelihood: g(d | s) and its tempering alpha.

Charter 150 (user ruling 2026-09-19).  One artifact, refitted daily:

1. **g** -- the ordered hazard chain over the bucket increment above the
   running high, rung one (does the day cross into another bucket) on the
   clock bins plus ``p1_observation_likelihood.PHASE_FEATURES``, rungs two and
   up on ``FULL_FEATURES``.  Same estimator as the four-layer fit
   (``fit_metar_phase_hazard_artifact.fit_phase_rung`` / ``fit_continuation``),
   different covariates: the innovations against the root curve and the
   regional run join the state.

2. **alpha** per remaining-hours bin (>=10h, 6-10h, <6h), chosen on a held-out
   day split: g is refitted on the earlier 70% of days and every alpha on the
   grid is scored as ``root x mask x g**alpha`` on the later 30% against the
   settled bucket, bin by bin.  The root is the P0/P0-M envelope the sample
   was compiled on, read out of the store by checkpoint.

3. **veto** -- the candidate is written only if, on that same held-out
   split, the tempered likelihood beats the root under the hard bound alone
   by at least ``--margin`` nat per sample, and (when the incumbent's own
   training window ends before the held-out days, so it can be scored out of
   sample) it is not worse than the incumbent by more than the margin.  A
   vetoed refit leaves the served file untouched and says why.

The written g is refitted on every day in the window; alpha is the split's.

    python3 scripts/fit_p1_observation_likelihood.py \\
        --samples data/runtime/bayes_basket_target_nwp/p1_observation_samples.db \\
        --through 2026-09-18 --out data/runtime/bayes_basket_target_nwp/p1_observation_likelihood_artifact.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    _anchor_bucket_for_temperature,
)
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    hard_bound_excluded_buckets,
)
import scripts.fit_metar_phase_hazard_artifact as hazard_fit  # noqa: E402
from scripts.evaluate_metar_four_layer_walk_forward import RootCache  # noqa: E402

ALPHA_GRID = tuple(round(0.125 * step, 3) for step in range(25))
HOLDOUT_SHARE = 0.30
MIN_SAMPLES = 2000
MIN_DAYS = 10
DEFAULT_MARGIN = 0.01


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def features_of(sample: Mapping[str, Any]) -> dict[str, float | None]:
    return OL.feature_vector(
        remaining_hours=sample["remaining_hours"],
        drawdown_from_high_c=sample["drawdown_from_high_c"],
        momentum_c_per_hour=sample["momentum_c_per_hour"],
        cloud_cover_fraction=sample["cloud_cover_fraction"],
        precipitation_present=sample["precipitation_present"],
        dewpoint_depression_c=sample["dewpoint_depression_c"],
        wind_speed_mps=sample["wind_speed_mps"],
        warming_so_far_c=sample["warming_so_far_c"],
        curve_shape_realized=sample["curve_shape_realized"],
        root_bias_recent_c=sample["root_bias_recent_c"],
        regional_headroom_c=sample["regional_headroom_c"],
        regional_live_gap_c=sample["regional_live_gap_c"],
    )


def fit_g(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """g on these samples; the body the artifact publishes, alpha excluded."""

    fallbacks = hazard_fit.fleet_means(samples, OL.FULL_FEATURES)
    risers = [sample for sample in samples if int(sample["delta_c"]) >= 1]
    return {
        "schema": OL.SCHEMA,
        "rule": OL.RULE,
        "feature_fallbacks": fallbacks,
        "phase_features": list(OL.PHASE_FEATURES),
        "full_features": list(OL.FULL_FEATURES),
        "phase": hazard_fit.fit_phase_rung(samples, fallbacks, OL.PHASE_FEATURES),
        "continuation_full": hazard_fit.fit_continuation(
            risers, OL.FULL_FEATURES, fallbacks
        ),
        "trained_from": min(str(s["event_date"]) for s in samples),
        "trained_through": max(str(s["event_date"]) for s in samples),
        "sample_n": len(samples),
        "riser_sample_n": len(risers),
        "station_day_n": len({
            (s["station_id"], s["event_date"]) for s in samples
        }),
    }


def scorable_rows(
    samples: Sequence[Mapping[str, Any]], roots: RootCache
) -> list[dict[str, Any]]:
    """Each held-out sample with its root, hard bound and settled bucket."""

    rows: list[dict[str, Any]] = []
    for sample in samples:
        view = roots.get(str(sample["root_checkpoint_id"]))
        if view is None:
            continue
        high = float(sample["running_high_c"])
        ordered = sorted(view["ordinals"], key=lambda b: view["ordinals"][b])
        excluded = set(
            hard_bound_excluded_buckets(ordered, view["uppers"], high)
        )
        lineage = {"bucket_intervals_c_json": view["intervals_json"]}
        truth = _anchor_bucket_for_temperature(
            float(sample["truth_tmax_c"]), root_lineage=lineage,
            bucket_ordinals=view["ordinals"],
        )
        running = _anchor_bucket_for_temperature(
            high, root_lineage=lineage, bucket_ordinals=view["ordinals"],
        )
        if truth is None or running is None or truth in excluded:
            continue
        rows.append({
            "sample": sample,
            "view": view,
            "excluded": excluded,
            "truth": truth,
            "running": running,
            "high": high,
            "bin": OL.alpha_bin(float(sample["remaining_hours"])),
        })
    return rows


def _log_score(
    prior: Mapping[str, float], evidence: Mapping[str, float] | None,
    excluded: set[str], truth: str, alpha: float,
) -> float | None:
    weighted = {
        bucket: (
            0.0 if bucket in excluded
            else float(value) * (
                1.0 if evidence is None
                else max(0.0, float(evidence[bucket])) ** alpha
            )
        )
        for bucket, value in prior.items()
    }
    total = sum(weighted.values())
    if total <= 1e-12:
        return None
    return math.log(max(weighted.get(truth, 0.0) / total, 1e-9))


def evidence_of(row: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, float]:
    evidence, _ = OL.evidence_by_bucket(
        features_of(row["sample"]), artifact,
        bucket_ordinals=row["view"]["ordinals"],
        bucket_centers_c=row["view"]["centers"],
        running_high_c=row["high"],
        running_high_bucket=row["running"],
    )
    return evidence


def score_split(
    rows: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    incumbent: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per-bin alpha choice and the arms the veto reads, on held-out rows."""

    per_bin: dict[int, dict[str, Any]] = {
        index: {"weight": 0.0, "root": 0.0, "grid": defaultdict(float),
                "incumbent": 0.0, "n": 0}
        for index in range(len(OL.ALPHA_EDGES_HOURS) + 1)
    }
    for row in rows:
        prior = row["view"]["probabilities"]
        root_score = _log_score(prior, None, row["excluded"], row["truth"], 1.0)
        if root_score is None:
            continue
        evidence = evidence_of(row, candidate)
        scores = {
            alpha: _log_score(prior, evidence, row["excluded"], row["truth"], alpha)
            for alpha in ALPHA_GRID
        }
        if any(value is None for value in scores.values()):
            continue
        incumbent_score = None
        if incumbent is not None:
            incumbent_score = _log_score(
                prior, evidence_of(row, incumbent), row["excluded"],
                row["truth"],
                OL.alpha_for(float(row["sample"]["remaining_hours"]), incumbent),
            )
            if incumbent_score is None:
                continue
        weight = float(row["sample"]["station_day_weight"])
        cell = per_bin[row["bin"]]
        cell["weight"] += weight
        cell["n"] += 1
        cell["root"] += weight * root_score
        for alpha, value in scores.items():
            cell["grid"][alpha] += weight * value
        if incumbent_score is not None:
            cell["incumbent"] += weight * incumbent_score
    alphas: list[float] = []
    report: dict[str, Any] = {"bins": []}
    totals = {"weight": 0.0, "root": 0.0, "candidate": 0.0, "incumbent": 0.0}
    for index in sorted(per_bin):
        cell = per_bin[index]
        if cell["weight"] <= 0.0:
            alphas.append(1.0)
            report["bins"].append({"bin": index, "n": 0, "alpha": 1.0})
            continue
        best = max(ALPHA_GRID, key=lambda alpha: cell["grid"][alpha])
        alphas.append(float(best))
        totals["weight"] += cell["weight"]
        totals["root"] += cell["root"]
        totals["candidate"] += cell["grid"][best]
        totals["incumbent"] += cell["incumbent"]
        report["bins"].append({
            "bin": index,
            "n": cell["n"],
            "alpha": float(best),
            "root_nat": cell["root"] / cell["weight"],
            "candidate_nat": cell["grid"][best] / cell["weight"],
            **(
                {"incumbent_nat": cell["incumbent"] / cell["weight"]}
                if incumbent is not None else {}
            ),
        })
    weight = max(totals["weight"], 1e-12)
    report["alpha_by_bin"] = alphas
    report["root_nat"] = totals["root"] / weight
    report["candidate_nat"] = totals["candidate"] / weight
    if incumbent is not None:
        report["incumbent_nat"] = totals["incumbent"] / weight
    report["station_day_weight"] = totals["weight"]
    return report


def load_samples(path: Path, *, since: str | None, through: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    clause, params = "WHERE event_date<=?", [through]
    if since:
        clause += " AND event_date>=?"
        params.append(since)
    try:
        return [
            dict(row) for row in conn.execute(
                f"SELECT * FROM metar_phase_hazard_sample {clause}"
                " ORDER BY event_date, station_id, as_of_utc", params,
            )
        ]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", default=str(OL.SAMPLES_PATH))
    parser.add_argument("--target-db", default=str(
        ROOT / "data" / "bayes_basket_target_ssot.db"
    ))
    parser.add_argument("--through", required=True)
    parser.add_argument("--since", default=None)
    parser.add_argument("--out", default=str(OL.ARTIFACT_PATH))
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    parser.add_argument("--fitted-at-utc", default=None)
    parser.add_argument(
        "--force", action="store_true",
        help="write even when the veto refuses (first deployment only)",
    )
    args = parser.parse_args()

    samples = load_samples(Path(args.samples), since=args.since, through=args.through)
    days = sorted({str(sample["event_date"]) for sample in samples})
    if len(samples) < MIN_SAMPLES or len(days) < MIN_DAYS:
        print(json.dumps({"state": "REFUSED_THIN", "samples": len(samples),
                          "days": len(days)}))
        return 3
    cut = days[int(len(days) * (1.0 - HOLDOUT_SHARE))]
    train = [s for s in samples if str(s["event_date"]) < cut]
    held = [s for s in samples if str(s["event_date"]) >= cut]
    target = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True)
    rows = scorable_rows(held, RootCache(target))
    split_g = fit_g(train)
    split_g["alpha_by_bin"] = [1.0] * (len(OL.ALPHA_EDGES_HOURS) + 1)
    out_path = Path(args.out)
    incumbent = OL.load_artifact(out_path)
    incumbent_scorable = (
        incumbent is not None
        and str(incumbent.get("trained_through") or "9999") < cut
    )
    split = score_split(
        rows, split_g, incumbent if incumbent_scorable else None
    )
    target.close()
    veto: list[str] = []
    if split["candidate_nat"] - split["root_nat"] < args.margin:
        veto.append(
            f"does_not_beat_root_by_margin:{split['candidate_nat'] - split['root_nat']:+.4f}"
        )
    if incumbent_scorable and (
        split["candidate_nat"] < split["incumbent_nat"] - args.margin
    ):
        veto.append(
            f"worse_than_incumbent:{split['candidate_nat'] - split['incumbent_nat']:+.4f}"
        )
    body = fit_g(samples)
    body["alpha_by_bin"] = split["alpha_by_bin"]
    body["alpha_edges_hours"] = list(OL.ALPHA_EDGES_HOURS)
    body["alpha_grid"] = list(ALPHA_GRID)
    body["holdout"] = {
        "from": cut,
        "through": days[-1],
        "scored_rows": len(rows),
        **{key: value for key, value in split.items() if key != "alpha_by_bin"},
        "incumbent_scored": bool(incumbent_scorable),
        "margin": args.margin,
        "veto": veto,
    }
    body["fitted_at_utc"] = args.fitted_at_utc or _iso(datetime.now(timezone.utc))
    body["content_sha256"] = content_sha256(body)
    written = False
    if not veto or args.force:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        staging = out_path.with_suffix(out_path.suffix + ".new")
        staging.write_text(json.dumps(body, indent=2, sort_keys=True))
        os.replace(staging, out_path)
        written = True
    print(json.dumps({
        "schema": OL.SCHEMA,
        "state": "WRITTEN" if written else "VETOED",
        "trained_through": body["trained_through"],
        "sample_n": body["sample_n"],
        "alpha_by_bin": body["alpha_by_bin"],
        "holdout_from": cut,
        "root_nat": round(split["root_nat"], 4),
        "candidate_nat": round(split["candidate_nat"], 4),
        **(
            {"incumbent_nat": round(split["incumbent_nat"], 4)}
            if incumbent_scorable else {}
        ),
        "veto": veto,
        "out": str(out_path),
    }, ensure_ascii=False))
    # A veto is the lane working, not failing: the served file stands.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
