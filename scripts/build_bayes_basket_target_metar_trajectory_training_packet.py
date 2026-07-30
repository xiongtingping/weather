#!/usr/bin/env python3
"""Build strict-as-of RKPK METAR trajectory folds from local SSOT sources."""

from __future__ import annotations

import argparse
from datetime import datetime, time, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.resource_bounds import write_bounded_json


SCHEMA = "bayes_basket_metar_trajectory_training_packet_v1"
MAX_OBSERVATION_ROWS = 50_000
MAX_TRUTH_ROWS = 10_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
BUCKET_START_HOUR = {
    "NIGHT": 0,
    "MORNING": 6,
    "AFTERNOON": 12,
    "EVENING": 18,
}


def _parsed_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractError(
            "metar_trajectory_fold_timestamp_timezone_required"
        )
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _read_truth(
    db: Path,
    *,
    station_id: str,
) -> list[dict[str, Any]]:
    with sqlite3.connect(
        f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT truth_id,station_id,local_date,official_tmax_c,
                   source_lane,settlement_definition,revision_no,
                   canonical_committed_at_utc,timestamps_complete,
                   qc_status,authority_scope,content_sha256
              FROM calibration_station_daily_high_truth
             WHERE station_id=?
             ORDER BY local_date,revision_no,canonical_committed_at_utc
            """,
            (station_id,),
        ).fetchall()
    if len(rows) > MAX_TRUTH_ROWS:
        raise ContractError(
            "metar_trajectory_fold_truth_resource_limit_exceeded"
        )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = dict(row)
        if (
            str(value.get("source_lane") or "").upper() != "AWC"
            or str(value.get("qc_status") or "").upper() != "ACCEPTED"
            or int(value.get("timestamps_complete") or 0) != 1
            or not str(value.get("canonical_committed_at_utc") or "")
        ):
            continue
        latest[str(value["local_date"])] = value
    return [latest[key] for key in sorted(latest)]


def _read_observations(
    db: Path,
    *,
    station_id: str,
) -> list[dict[str, Any]]:
    with sqlite3.connect(
        f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT observation_id,source_lane,station_id,report_type,
                   valid_at,reported_temperature_c,raw_sha256,
                   canonical_committed_at,correction_suspected
              FROM reported_trajectory_observations
             WHERE station_id=?
               AND lower(source_lane)='awc'
               AND report_type IN ('METAR','SPECI')
             ORDER BY valid_at,canonical_committed_at,observation_id
            """,
            (station_id,),
        ).fetchall()
    if len(rows) > MAX_OBSERVATION_ROWS:
        raise ContractError(
            "metar_trajectory_fold_observation_resource_limit_exceeded"
        )
    return [dict(row) for row in rows]


def _read_probability_roots(
    db: Path,
    *,
    station_id: str,
) -> list[dict[str, Any]]:
    with sqlite3.connect(
        f"file:{db.resolve()}?mode=ro", uri=True, timeout=5.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT stage,checkpoint_id,station_id,event_date,
                   settlement_timezone,decision_at_utc,
                   bucket_ordinals_json,probabilities_json,lineage_json,
                   content_sha256
              FROM basket_probability_envelope
             WHERE station_id=? AND stage IN ('P0','P0-M')
               AND state='COMMITTED'
             ORDER BY event_date,decision_at_utc,checkpoint_id
            """,
            (station_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def build_training_packet(
    *,
    truth_rows: list[dict[str, Any]],
    observation_rows: list[dict[str, Any]],
    probability_rows: list[dict[str, Any]],
    station_id: str,
    settlement_timezone: str,
    local_time_bucket: str,
    minimum_prior_truths: int,
    minimum_effective_sample_n: float,
    decided_at_utc: str,
    valid_from_utc: str,
    valid_until_utc: str,
    generation: int,
) -> dict[str, Any]:
    station = station_id.strip().upper()
    bucket = local_time_bucket.strip().upper()
    if bucket not in BUCKET_START_HOUR:
        raise ContractError(
            "metar_trajectory_fold_local_time_bucket_invalid"
        )
    if (
        isinstance(minimum_prior_truths, bool)
        or minimum_prior_truths < 1
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ContractError(
            "metar_trajectory_fold_parameter_invalid"
        )
    zone = ZoneInfo(settlement_timezone)
    observations_by_date: dict[str, list[dict[str, Any]]] = {}
    correction_dates: set[str] = set()
    for row in observation_rows:
        valid = _parsed_utc(str(row.get("valid_at") or ""))
        local_date = valid.astimezone(zone).date().isoformat()
        observations_by_date.setdefault(local_date, []).append(row)
        if int(row.get("correction_suspected") or 0) == 1:
            correction_dates.add(local_date)
    folds: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    ordered_truth = sorted(
        truth_rows,
        key=lambda row: (
            str(row.get("local_date") or ""),
            int(row.get("revision_no") or 0),
        ),
    )
    for truth in ordered_truth:
        event_date = str(truth.get("local_date") or "")
        local_cutoff = datetime.combine(
            datetime.fromisoformat(event_date).date(),
            time(BUCKET_START_HOUR[bucket], 0),
            tzinfo=zone,
        )
        cutoff = local_cutoff.astimezone(timezone.utc)
        truth_settled = _parsed_utc(
            str(truth.get("canonical_committed_at_utc") or "")
        )
        if truth_settled <= cutoff:
            excluded.append({
                "event_date": event_date,
                "reason": "evaluation_truth_visible_at_cutoff",
            })
            continue
        if event_date in correction_dates:
            excluded.append({
                "event_date": event_date,
                "reason": "correction_suspected",
            })
            continue
        prefix = [
            row
            for row in observations_by_date.get(event_date, ())
            if _parsed_utc(str(row.get("valid_at") or "")) <= cutoff
            and _parsed_utc(
                str(row.get("canonical_committed_at") or "")
            )
            <= cutoff
        ]
        if not prefix:
            excluded.append({
                "event_date": event_date,
                "reason": "canonical_prefix_missing_at_bucket_start",
            })
            continue
        prior_truths = [
            row
            for row in ordered_truth
            if str(row.get("local_date") or "") < event_date
            and _parsed_utc(
                str(row.get("canonical_committed_at_utc") or "")
            )
            <= cutoff
        ]
        if len(prior_truths) < minimum_prior_truths:
            excluded.append({
                "event_date": event_date,
                "reason": "strict_asof_prior_truth_count_insufficient",
            })
            continue
        running_high = max(
            float(row["reported_temperature_c"]) for row in prefix
        )
        remaining = max(
            0.0, float(truth["official_tmax_c"]) - running_high
        )
        quantized = float(round(remaining))
        if quantized > 12.0 or abs(remaining - quantized) > 0.5000001:
            excluded.append({
                "event_date": event_date,
                "reason": "remaining_warming_outside_support",
            })
            continue
        sequence_identity = [{
            "observation_id": str(row["observation_id"]),
            "valid_at_utc": str(row["valid_at"]),
            "canonical_committed_at_utc": str(
                row["canonical_committed_at"]
            ),
            "temperature_c": float(row["reported_temperature_c"]),
            "raw_sha256": str(row["raw_sha256"]),
        } for row in prefix]
        fold = {
            "fold_id": f"{station}-{bucket}-{event_date}",
            "target_local_date": event_date,
            "evaluation_truth_id": str(truth["truth_id"]),
            "evaluation_truth_settled_at_utc": _iso_utc(
                truth_settled
            ),
            "training_cutoff_utc": _iso_utc(cutoff),
            "trajectory_training_truths": [{
                "truth_id": str(row["truth_id"]),
                "target_local_date": str(row["local_date"]),
                "settled_at_utc": _iso_utc(
                    _parsed_utc(
                        str(row["canonical_committed_at_utc"])
                    )
                ),
            } for row in prior_truths],
            "canonical_observation_sequence_sha256": content_sha256(
                sequence_identity
            ),
            "remaining_warming_c": quantized,
            "weight": 1.0,
        }
        eligible_roots = [
            row
            for row in probability_rows
            if str(row.get("event_date") or "") == event_date
            and str(row.get("settlement_timezone") or "")
            == settlement_timezone
            and _parsed_utc(
                str(row.get("decision_at_utc") or "")
            )
            <= cutoff
        ]
        if eligible_roots:
            ranked_roots = sorted(
                eligible_roots,
                key=lambda row: (
                    1 if str(row.get("stage")) == "P0-M" else 0,
                    _parsed_utc(str(row.get("decision_at_utc"))),
                    str(row.get("checkpoint_id") or ""),
                ),
                reverse=True,
            )
            for root in ranked_roots:
                probabilities = json.loads(
                    str(root.get("probabilities_json") or "{}")
                )
                lineage = json.loads(
                    str(root.get("lineage_json") or "{}")
                )
                centers = json.loads(
                    str(
                        lineage.get("bucket_centers_c_json")
                        or "{}"
                    )
                )
                if not (
                    isinstance(probabilities, dict)
                    and isinstance(centers, dict)
                    and probabilities
                    and set(probabilities) == set(centers)
                ):
                    continue
                ordered = sorted(
                    centers, key=lambda key: (float(centers[key]), key)
                )
                truth_bucket = min(
                    ordered,
                    key=lambda key: (
                        abs(
                            float(centers[key])
                            - float(truth["official_tmax_c"])
                        ),
                        float(centers[key]),
                        key,
                    ),
                )
                fold.update({
                    "root_checkpoint_id": str(
                        root.get("checkpoint_id") or ""
                    ),
                    "root_content_sha256": str(
                        root.get("content_sha256") or ""
                    ),
                    "root_stage": str(root.get("stage") or ""),
                    "root_probabilities": probabilities,
                    "bucket_centers_c": centers,
                    "bucket_upper_bounds_c": {
                        key: float(centers[key]) + 0.499999
                        for key in ordered
                    },
                    "truth_bucket": truth_bucket,
                    "running_high_c": running_high,
                })
                break
        folds.append(fold)
    support = {str(value): 1.0 / 13.0 for value in range(13)}
    trained_through = max(
        (
            _parsed_utc(
                str(row["canonical_committed_at_utc"])
            )
            for row in ordered_truth
            if any(
                fold["evaluation_truth_id"] == row["truth_id"]
                for fold in folds
            )
        ),
        default=_parsed_utc(decided_at_utc),
    )
    source_manifest = {
        "schema": "bayes_basket_metar_trajectory_fold_source_v1",
        "station_id": station,
        "settlement_timezone": settlement_timezone,
        "local_time_bucket": bucket,
        "truth_row_count": len(truth_rows),
        "observation_row_count": len(observation_rows),
        "probability_root_row_count": len(probability_rows),
        "eligible_fold_count": len(folds),
        "excluded": excluded,
    }
    source_manifest["content_sha256"] = content_sha256(
        source_manifest
    )
    identity = {
        "schema": SCHEMA,
        "source_manifest": source_manifest,
        "folds": folds,
        "smoothing_candidates": [0.1, 0.5, 1.0, 2.0, 5.0, 10.0],
        "minimum_training_folds": 2,
        "minimum_effective_sample_n": float(
            minimum_effective_sample_n
        ),
        "artifact_parameters": {
            "activation_id": (
                f"metar-trajectory-{station}-{bucket.lower()}-v1"
            ),
            "station_id": station,
            "source_lane": "awc",
            "season_bin": "JJA",
            "local_time_bucket": bucket,
            "maturity_bin": "D0",
            "settlement_definition": "official_daily_tmax_c",
            "settlement_timezone": settlement_timezone,
            "feature_definitions": {
                "running_high_c": (
                    "max canonical AWC METAR/SPECI temperature available "
                    "at local bucket start"
                ),
                "remaining_warming_c": (
                    "final official tmax minus running high, nearest 1C"
                ),
            },
            "missing_observation_policy": "DEGRADE_STRENGTH",
            "expected_interval_minutes": 60,
            "long_interval_minutes": 90,
            "max_missing_intervals": 4,
            "missing_interval_strength_factor": 0.8,
            "long_interval_strength_factor": 0.5,
            "hard_exclusion_baseline_distribution": support,
            "base_strength": 0.8,
            "maturity_strength": 0.75,
            "likelihood_ratio_floor": 0.25,
            "likelihood_ratio_ceiling": 4.0,
            "trained_through_utc": _iso_utc(trained_through),
            "valid_from_utc": valid_from_utc,
            "valid_until_utc": valid_until_utc,
        },
        "generation": generation,
        "decided_at_utc": decided_at_utc,
        "boundary": {
            "execution_mode": "production_no_live",
            "writes_production_database": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        },
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target-db",
        type=Path,
        default=ROOT / "data/bayes_basket_target_ssot.db",
    )
    parser.add_argument(
        "--archive-db",
        type=Path,
        default=ROOT / "data/archive.db",
    )
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--settlement-timezone", default="Asia/Seoul")
    parser.add_argument(
        "--local-time-bucket",
        choices=tuple(BUCKET_START_HOUR),
        required=True,
    )
    parser.add_argument("--minimum-prior-truths", type=int, default=3)
    parser.add_argument(
        "--minimum-effective-sample-n", type=float, default=20.0
    )
    parser.add_argument("--decided-at-utc", required=True)
    parser.add_argument("--valid-from-utc", required=True)
    parser.add_argument("--valid-until-utc", required=True)
    parser.add_argument("--generation", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = build_training_packet(
        truth_rows=_read_truth(
            args.target_db.expanduser().resolve(),
            station_id=args.station_id.upper(),
        ),
        observation_rows=_read_observations(
            args.archive_db.expanduser().resolve(),
            station_id=args.station_id.upper(),
        ),
        probability_rows=_read_probability_roots(
            args.target_db.expanduser().resolve(),
            station_id=args.station_id.upper(),
        ),
        station_id=args.station_id,
        settlement_timezone=args.settlement_timezone,
        local_time_bucket=args.local_time_bucket,
        minimum_prior_truths=args.minimum_prior_truths,
        minimum_effective_sample_n=args.minimum_effective_sample_n,
        decided_at_utc=args.decided_at_utc,
        valid_from_utc=args.valid_from_utc,
        valid_until_utc=args.valid_until_utc,
        generation=args.generation,
    )
    write_bounded_json(
        args.output.expanduser().resolve(),
        packet,
        limit=MAX_OUTPUT_BYTES,
    )
    print(packet["content_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
