#!/usr/bin/env python3
"""Measure whether the forecaster's cloud/rain/wind fields condition the tails.

Research-only, read-only.  ``apply_conditional_tail_skew`` conditions the tail
factors on two measured signals today: same-day climatological headroom (width)
and the TAF TX against the model centre (direction).  The same TAF witness also
carries cloud cover, precipitation probability and wind speed, and those three
are already served -- but as a *centre* shift
(``synoptic_cooling_index`` / ``forecast_radiation_shift_sigma``), never as a
tail term.  Whether they say anything about the tails *after* the centre shift,
the neighbour tilt, the conditional skew and the band factors have all run is
an open question, and the answer has to be measured before anything is wired:
the layer is multiplicative on claimed probability, so a guessed multiplier is
a systematically wrong probability.

Method mirrors the one behind ``CLIMATOLOGY_HEADROOM_BANDS`` and
``TAF_DIRECTION_*``: realised-over-claimed hit rate for tail buckets (claimed
under ``CONDITIONAL_SKEW_THRESHOLD``), split by side of the served centre,
binned by the physical field, with intervals bootstrapped over event dates --
stations on one day share weather, so the day is the independent unit.

Sample is the served-geometry OOS predictions
(``calibration_cross_native_ensemble_oos_prediction``), whose stored
distribution is the fully layered claim, joined to the historical TAF capture
strictly as of each prediction's decision time and to settled truth.  The
served envelopes themselves (``basket_probability_envelope``) look like the
obvious sample and are not: their four thousand P0 rows with a TAF witness are
sixty-six station-days of snapshots, three of them settled.

What the run answers as of 2026-08-16 is "not yet, and not close": the whole
live-vocabulary sample holds seven warm and twenty-seven cold tail events, so
a three-bin two-sided table would rest on nought to two events a cell.  The
reliability curve it prints on the way is worth more than the table it cannot
fill -- it reproduces the gate-lane band ratios in
``calibration_band_reliability_fit_journal`` to within noise, which is what
says the arithmetic here is right.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.cross_consensus import (  # noqa: E402
    CONDITIONAL_SKEW_THRESHOLD,
    TAF_DIRECTION_GATE_SIGMA,
    synoptic_cooling_index,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
PHYSICAL_FIELDS = (
    "cloud_cover_fraction",
    "precipitation_probability",
    "wind_speed_mps",
)
# The models' own fields carry the same physics for all 45 stations, where a
# TAF exists for seventeen -- and radiation is the reading production already
# prefers for the centre shift.  Measured alongside so a null on the TAF
# fields can be separated from a null for want of sample.
MODEL_PHYSICS_FIELDS = (
    "radiation_standardised",
    "cloudcover_mean",
    "precipitation_sum",
    "windspeed_10m_max",
)
# The day vocabulary (``d0``/``d1``/``d2``) was retired when the ladder moved
# to hour buckets, and its rows carry a +0.5 sigma centre bias the live lanes
# do not.  Mixing a retired lane into a tail measurement reads that bias as
# tail shape, so the default sample is the vocabulary production serves.
LIVE_LEAD_BUCKETS = ("h00_12", "h12_24", "h24_48")
BOOTSTRAP_REPS = 2000
BOOTSTRAP_SEED = 20260816


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _truth_by_station_day(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Highest accepted revision of each settled station-day."""

    best: dict[tuple[str, str], tuple[int, float]] = {}
    for row in conn.execute(
        "SELECT station_id,local_date,official_tmax_c,revision_no "
        "FROM calibration_station_daily_high_truth "
        "WHERE qc_status='ACCEPTED' AND official_tmax_c IS NOT NULL"
    ):
        key = (str(row["station_id"]), str(row["local_date"]))
        revision = int(row["revision_no"] or 0)
        if key not in best or revision > best[key][0]:
            best[key] = (revision, float(row["official_tmax_c"]))
    return {key: value for key, (_revision, value) in best.items()}


def _taf_captures(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every historical TAF capture, ordered by when a decision could see it."""

    captures: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT station_id,event_date,provider_available_at_utc,tx_c,"
        "cloud_cover_fraction,precipitation_probability,wind_speed_mps "
        "FROM calibration_historical_taf_capture "
        "ORDER BY station_id,event_date,provider_available_at_utc"
    ):
        captures[(str(row["station_id"]), str(row["event_date"]))].append(
            {
                "available_at": str(row["provider_available_at_utc"]),
                "tx_c": float(row["tx_c"]),
                "cloud_cover_fraction": float(row["cloud_cover_fraction"]),
                "precipitation_probability": float(
                    row["precipitation_probability"]
                ),
                "wind_speed_mps": float(row["wind_speed_mps"]),
            }
        )
    return captures


def _model_physics(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, float | None]]:
    """Model physics per station-day, radiation standardised per station.

    Standardised the way ``forecast_radiation_shift_sigma`` does it -- against
    the station's own captured history, leaving the day itself out -- because
    radiation's scale is set by latitude and season, not by the fleet.
    """

    rows: dict[tuple[str, str], dict[str, float | None]] = {}
    history: dict[str, list[float]] = defaultdict(list)
    for row in conn.execute(
        "SELECT station_id,local_date,shortwave_radiation_sum,cloudcover_mean,"
        "precipitation_sum,windspeed_10m_max "
        "FROM calibration_station_forecast_physics WHERE reference_only=1"
    ):
        station_id = str(row["station_id"])
        key = (station_id, str(row["local_date"]))
        radiation = row["shortwave_radiation_sum"]
        rows[key] = {
            "shortwave_radiation_sum": (
                None if radiation is None else float(radiation)
            ),
            "cloudcover_mean": (
                None
                if row["cloudcover_mean"] is None
                else float(row["cloudcover_mean"])
            ),
            "precipitation_sum": (
                None
                if row["precipitation_sum"] is None
                else float(row["precipitation_sum"])
            ),
            "windspeed_10m_max": (
                None
                if row["windspeed_10m_max"] is None
                else float(row["windspeed_10m_max"])
            ),
        }
        if radiation is not None:
            history[station_id].append(float(radiation))
    for (station_id, _local_date), payload in rows.items():
        radiation = payload["shortwave_radiation_sum"]
        values = history.get(station_id) or []
        payload["radiation_standardised"] = None
        if radiation is None or len(values) < 2:
            continue
        others = list(values)
        others.remove(radiation)
        if len(others) < 2:
            continue
        mean = sum(others) / len(others)
        variance = sum((value - mean) ** 2 for value in others) / len(others)
        deviation = math.sqrt(variance)
        if deviation > 0.0:
            payload["radiation_standardised"] = (radiation - mean) / deviation
    return rows


def _as_of_capture(
    captures: Sequence[Mapping[str, Any]], *, decision_at_utc: str
) -> Mapping[str, Any] | None:
    """The last capture a decision at ``decision_at_utc`` could have read."""

    visible = [
        capture
        for capture in captures
        if str(capture["available_at"]) <= decision_at_utc
    ]
    return visible[-1] if visible else None


def _truth_bucket(ordered: Sequence[str], centers: Mapping[str, float], tmax_c: float) -> str:
    """The served bucket a settled high lands in.

    The ladder is one-degree wide with half-degree edges and closed ends, so
    the bucket is the nearest centre with the two edge buckets absorbing
    everything beyond them.
    """

    lowest, highest = ordered[0], ordered[-1]
    if tmax_c < float(centers[lowest]) - 0.5:
        return lowest
    if tmax_c >= float(centers[highest]) - 0.5:
        return highest
    return min(ordered, key=lambda key: abs(float(centers[key]) - tmax_c))


def _load_cases(
    conn: sqlite3.Connection,
    *,
    one_per_day: bool,
    lead_buckets: Sequence[str] | None = LIVE_LEAD_BUCKETS,
) -> list[dict[str, Any]]:
    """Served claims joined to their as-of TAF witness and settled truth."""

    truth = _truth_by_station_day(conn)
    captures = _taf_captures(conn)
    physics = _model_physics(conn)
    cases: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT station_id,event_date,lead_bucket,run_cycle,decision_at_utc,"
        "probabilities_json,bucket_centers_c_json "
        "FROM calibration_cross_native_ensemble_oos_prediction "
        "ORDER BY station_id,event_date,decision_at_utc"
    ):
        station_id = str(row["station_id"])
        event_date = str(row["event_date"])
        key = (station_id, event_date)
        if key not in truth:
            continue
        if lead_buckets and str(row["lead_bucket"]) not in lead_buckets:
            continue
        # A TAF witness is optional: seventeen stations publish a TX, and the
        # model physics below covers all forty-five.
        capture = _as_of_capture(
            captures.get(key) or [], decision_at_utc=str(row["decision_at_utc"])
        )
        probability = {
            str(k): float(v) for k, v in json.loads(row["probabilities_json"]).items()
        }
        centers = {
            str(k): float(v)
            for k, v in json.loads(row["bucket_centers_c_json"]).items()
        }
        ordered = sorted(probability, key=lambda k: float(centers[k]))
        ordinals = {key_: index for index, key_ in enumerate(ordered)}
        center_bucket = max(probability, key=lambda k: probability[k])
        center_ordinal = ordinals[center_bucket]
        landed = _truth_bucket(ordered, centers, truth[key])
        warm_claimed = 0.0
        cold_claimed = 0.0
        warm_hit = 0
        cold_hit = 0
        for bucket, value in probability.items():
            if not (0.0 < value < CONDITIONAL_SKEW_THRESHOLD):
                continue
            if ordinals[bucket] > center_ordinal:
                warm_claimed += value
                if bucket == landed:
                    warm_hit = 1
            elif ordinals[bucket] < center_ordinal:
                cold_claimed += value
                if bucket == landed:
                    cold_hit = 1
        # The distribution's own spread is the scale the served TAF gap uses.
        mean_c = sum(probability[k] * float(centers[k]) for k in ordered)
        variance = sum(
            probability[k] * (float(centers[k]) - mean_c) ** 2 for k in ordered
        )
        sigma_c = math.sqrt(variance)
        model_center_c = float(centers[center_bucket])
        taf_gap_sigma = None
        cooling = None
        if capture is not None and sigma_c > 1e-9:
            taf_gap_sigma = (float(capture["tx_c"]) - model_center_c) / sigma_c
        if capture is not None:
            cooling, _receipt = synoptic_cooling_index(
                cloud_cover_fraction=capture["cloud_cover_fraction"],
                precipitation_probability=capture["precipitation_probability"],
                wind_speed_mps=capture["wind_speed_mps"],
            )
        station_physics = physics.get(key) or {}
        cases.append(
            {
                "station_id": station_id,
                "event_date": event_date,
                "decision_at_utc": str(row["decision_at_utc"]),
                "lead_bucket": str(row["lead_bucket"]),
                "warm_claimed": warm_claimed,
                "cold_claimed": cold_claimed,
                "warm_hit": warm_hit,
                "cold_hit": cold_hit,
                "claim_by_bucket": probability,
                "landed_bucket": landed,
                "bucket_centers_c": centers,
                # Whether a physical field is a *tail* signal or just an
                # under-scaled centre shift is settled by this: a centre still
                # displaced in the field's direction says the shift is too
                # small, not that the tails need their own term.
                "residual_sigma": (
                    (truth[key] - mean_c) / sigma_c if sigma_c > 1e-9 else None
                ),
                "taf_gap_sigma": taf_gap_sigma,
                "synoptic_cooling_index": cooling,
                **{
                    field: (None if capture is None else capture[field])
                    for field in PHYSICAL_FIELDS
                },
                **{
                    field: station_physics.get(field)
                    for field in MODEL_PHYSICS_FIELDS
                },
            }
        )
    if one_per_day:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for case in cases:
            key = (case["station_id"], case["event_date"])
            held = latest.get(key)
            if held is None or case["decision_at_utc"] > held["decision_at_utc"]:
                latest[key] = case
        cases = sorted(
            latest.values(), key=lambda c: (c["event_date"], c["station_id"])
        )
    return cases


def _quantile_edges(values: Sequence[float], bins: int) -> list[float]:
    ordered = sorted(values)
    if not ordered:
        return []
    edges: list[float] = []
    for index in range(1, bins):
        position = index * len(ordered) / bins
        edges.append(ordered[min(int(position), len(ordered) - 1)])
    return edges


def _bin_of(value: float, edges: Sequence[float]) -> int:
    for index, edge in enumerate(edges):
        if value < edge:
            return index
    return len(edges)


def _ratio(cases: Iterable[Mapping[str, Any]], side: str) -> tuple[float | None, float, int]:
    claimed = 0.0
    realised = 0
    count = 0
    for case in cases:
        claimed += float(case[f"{side}_claimed"])
        realised += int(case[f"{side}_hit"])
        count += 1
    if claimed <= 0.0:
        return None, claimed, count
    return realised / claimed, claimed, count


def _bootstrap_interval(
    cases: Sequence[Mapping[str, Any]], side: str, *, reps: int, seed: int
) -> tuple[float, float] | None:
    """Percentile interval resampling event dates, not station-days."""

    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        by_day[case["event_date"]].append(case)
    days = sorted(by_day)
    if len(days) < 3:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(reps):
        claimed = 0.0
        realised = 0
        for _ in days:
            for case in by_day[days[rng.randrange(len(days))]]:
                claimed += float(case[f"{side}_claimed"])
                realised += int(case[f"{side}_hit"])
        if claimed > 0.0:
            draws.append(realised / claimed)
    if len(draws) < reps // 2:
        return None
    draws.sort()
    return (
        draws[int(0.025 * len(draws))],
        draws[min(int(0.975 * len(draws)), len(draws) - 1)],
    )


def _format_ratio(value: float | None) -> str:
    return "  n/a " if value is None else f"{value:5.2f}"


def _format_interval(interval: tuple[float, float] | None) -> str:
    if interval is None:
        return "      --      "
    return f"[{interval[0]:5.2f}, {interval[1]:5.2f}]"


def _report_field(
    cases: Sequence[Mapping[str, Any]],
    field: str,
    *,
    bins: int,
    reps: int,
) -> None:
    usable = [case for case in cases if case.get(field) is not None]
    if not usable:
        print(f"\n{field}  -- no cases carry this field")
        return
    edges = _quantile_edges([float(case[field]) for case in usable], bins)
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for case in usable:
        grouped[_bin_of(float(case[field]), edges)].append(case)
    print(f"\n{field}  (quantile bins over {len(usable)} cases)")
    header = (
        f"  {'range':>22}  {'n':>5}  {'hits':>7}  {'resid':>6}  "
        f"{'warm':>5} {'warm 95%':>16}"
        f"  {'cold':>5} {'cold 95%':>16}"
    )
    print(header)
    for index in sorted(grouped):
        bucket = grouped[index]
        low = "-inf" if index == 0 else f"{edges[index - 1]:.3f}"
        high = "+inf" if index == len(edges) else f"{edges[index]:.3f}"
        hits = (
            sum(int(case["warm_hit"]) for case in bucket),
            sum(int(case["cold_hit"]) for case in bucket),
        )
        residuals = [
            float(case["residual_sigma"])
            for case in bucket
            if case.get("residual_sigma") is not None
        ]
        residual = (
            sum(residuals) / len(residuals) if residuals else float("nan")
        )
        warm_ratio, _warm_claimed, _n = _ratio(bucket, "warm")
        cold_ratio, _cold_claimed, _n = _ratio(bucket, "cold")
        warm_interval = _bootstrap_interval(
            bucket, "warm", reps=reps, seed=BOOTSTRAP_SEED + index
        )
        cold_interval = _bootstrap_interval(
            bucket, "cold", reps=reps, seed=BOOTSTRAP_SEED + 500 + index
        )
        print(
            f"  {low:>10} .. {high:>8}  {len(bucket):5d}  "
            f"{hits[0]:3d}/{hits[1]:3d}  {residual:+6.3f}  "
            f"{_format_ratio(warm_ratio)} {_format_interval(warm_interval)}  "
            f"{_format_ratio(cold_ratio)} {_format_interval(cold_interval)}"
        )


RELIABILITY_BANDS = (
    (0.0, 0.02),
    (0.02, 0.05),
    (0.05, 0.12),
    (0.12, 0.20),
    (0.20, 0.35),
    (0.35, 1.01),
)


def _report_reliability(cases: Sequence[Mapping[str, Any]]) -> None:
    """Realised over claimed across the whole claim, not just the tails.

    This is the cross-check on the arithmetic above: the bands here are the
    ones ``calibration_band_reliability_fit_journal`` fits, so its gate-lane
    ratios are the answer this should reproduce.  Claimed mass sums to the
    case count by construction and hits sum to it too, since every station-day
    lands in exactly one bucket -- if those two totals disagree, the bucket
    geometry is being read wrong and nothing else on the page is worth reading.
    """

    claimed: dict[tuple[float, float], float] = defaultdict(float)
    hits: dict[tuple[float, float], int] = defaultdict(int)
    for case in cases:
        for bucket, value in (case.get("claim_by_bucket") or {}).items():
            for band in RELIABILITY_BANDS:
                if band[0] <= value < band[1]:
                    claimed[band] += value
                    if bucket == case["landed_bucket"]:
                        hits[band] += 1
                    break
    print("\nreliability of the served claim, all bands")
    print(f"  {'band':>14}  {'claimed':>9}  {'hits':>5}  {'realised/claimed':>16}")
    for band in RELIABILITY_BANDS:
        mass = claimed[band]
        ratio = hits[band] / mass if mass > 0.0 else None
        print(
            f"  [{band[0]:.2f}, {band[1]:.2f})  {mass:9.2f}  {hits[band]:5d}  "
            f"{_format_ratio(ratio):>16}"
        )
    print(
        f"  totals: claimed {sum(claimed.values()):.2f}, "
        f"hits {sum(hits.values())}, cases {len(cases)}"
    )


def _report_halves(
    cases: Sequence[Mapping[str, Any]], field: str, *, bins: int
) -> None:
    """Does the gradient hold its sign in each half of the calendar?

    The house hurdle for serving a cell: a factor that only exists in one
    stretch of weather is a stretch of weather, not a factor.
    """

    usable = [case for case in cases if case.get(field) is not None]
    if not usable:
        return
    edges = _quantile_edges([float(case[field]) for case in usable], bins)
    days = sorted({case["event_date"] for case in usable})
    midpoint = days[len(days) // 2]
    print(f"\n{field}  split halves (early < {midpoint} <= late)")
    print(f"  {'bin':>3}  {'side':>4}  {'early':>6}  {'late':>6}  {'full':>6}")
    for index in range(bins):
        for side in ("warm", "cold"):
            bucket = [
                case
                for case in usable
                if _bin_of(float(case[field]), edges) == index
            ]
            early = [case for case in bucket if case["event_date"] < midpoint]
            late = [case for case in bucket if case["event_date"] >= midpoint]
            values = [
                _ratio(part, side)[0] for part in (early, late, bucket)
            ]
            print(
                f"  {index:3d}  {side:>4}  "
                + "  ".join(_format_ratio(value) + " " for value in values)
            )


def _taf_direction_stratum(case: Mapping[str, Any]) -> str:
    gap = case.get("taf_gap_sigma")
    if gap is None:
        return "absent"
    if gap > TAF_DIRECTION_GATE_SIGMA:
        return "taf_warm"
    if gap < -TAF_DIRECTION_GATE_SIGMA:
        return "taf_cold"
    return "taf_neutral"


def _report_increment(
    cases: Sequence[Mapping[str, Any]], field: str, *, bins: int, reps: int
) -> None:
    """The same split inside each served TAF-direction stratum.

    A physical gradient that only exists because cloudy days are also days the
    forecaster called cold is the direction term already being served, read
    twice.  It has to survive inside the stratum to be its own signal.
    """

    usable = [case for case in cases if case.get(field) is not None]
    edges = _quantile_edges([float(case[field]) for case in usable], bins)
    print(f"\n{field}  within served TAF-direction strata")
    print(
        f"  {'stratum':>12}  {'bin':>3}  {'n':>5}  {'warm':>5} {'warm 95%':>16}"
        f"  {'cold':>5} {'cold 95%':>16}"
    )
    strata: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for case in usable:
        strata[
            (_taf_direction_stratum(case), _bin_of(float(case[field]), edges))
        ].append(case)
    for stratum in ("taf_warm", "taf_neutral", "taf_cold", "absent"):
        for index in range(bins):
            bucket = strata.get((stratum, index))
            if not bucket:
                continue
            warm_ratio, _wc, _n = _ratio(bucket, "warm")
            cold_ratio, _cc, _n = _ratio(bucket, "cold")
            warm_interval = _bootstrap_interval(
                bucket, "warm", reps=reps, seed=BOOTSTRAP_SEED + 1000 + index
            )
            cold_interval = _bootstrap_interval(
                bucket, "cold", reps=reps, seed=BOOTSTRAP_SEED + 1500 + index
            )
            print(
                f"  {stratum:>12}  {index:3d}  {len(bucket):5d}  "
                f"{_format_ratio(warm_ratio)} {_format_interval(warm_interval)}  "
                f"{_format_ratio(cold_ratio)} {_format_interval(cold_interval)}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bins", type=int, default=3)
    parser.add_argument("--reps", type=int, default=BOOTSTRAP_REPS)
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help="keep every prediction row instead of one per station-day",
    )
    parser.add_argument(
        "--lead-buckets",
        default=",".join(LIVE_LEAD_BUCKETS),
        help="comma-separated lead buckets to keep, or 'all'",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    conn = _connect(args.db)
    lead_buckets = (
        None
        if args.lead_buckets.strip().lower() == "all"
        else tuple(part for part in args.lead_buckets.split(",") if part)
    )
    cases = _load_cases(
        conn, one_per_day=not args.all_rows, lead_buckets=lead_buckets
    )
    if not cases:
        print("no joinable cases", file=sys.stderr)
        return 1
    days = sorted({case["event_date"] for case in cases})
    stations = sorted({case["station_id"] for case in cases})
    warm_ratio, warm_claimed, _n = _ratio(cases, "warm")
    cold_ratio, cold_claimed, _n = _ratio(cases, "cold")
    print(
        f"sample: {len(cases)} cases, {len(stations)} stations, "
        f"{len(days)} days ({days[0]}..{days[-1]}); "
        f"lead buckets {lead_buckets or 'all'}"
    )
    residuals = [
        float(case["residual_sigma"])
        for case in cases
        if case.get("residual_sigma") is not None
    ]
    if residuals:
        print(
            "mean standardised residual (truth - served centre): "
            f"{sum(residuals) / len(residuals):+.3f} sigma"
        )
    print(
        f"pooled realised/claimed  warm {_format_ratio(warm_ratio)} "
        f"(claimed {warm_claimed:.2f})   "
        f"cold {_format_ratio(cold_ratio)} (claimed {cold_claimed:.2f})"
    )
    print(
        f"pooled 95%  warm "
        f"{_format_interval(_bootstrap_interval(cases, 'warm', reps=args.reps, seed=BOOTSTRAP_SEED))}"
        f"  cold "
        f"{_format_interval(_bootstrap_interval(cases, 'cold', reps=args.reps, seed=BOOTSTRAP_SEED + 7))}"
    )
    print(
        "\ntail events available: "
        f"warm {sum(int(case['warm_hit']) for case in cases)}, "
        f"cold {sum(int(case['cold_hit']) for case in cases)} "
        "-- the ratio's precision is set by these, not by the case count"
    )
    _report_reliability(cases)
    for field in (
        *PHYSICAL_FIELDS,
        "synoptic_cooling_index",
        *MODEL_PHYSICS_FIELDS,
    ):
        _report_field(cases, field, bins=args.bins, reps=args.reps)
    for field in MODEL_PHYSICS_FIELDS:
        _report_halves(cases, field, bins=args.bins)
    for field in (*PHYSICAL_FIELDS, "synoptic_cooling_index"):
        _report_increment(cases, field, bins=args.bins, reps=args.reps)
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(cases, indent=2, sort_keys=True))
        print(f"\ncases written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
