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
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.metar_path_state import (
    PATH_STATE_SCHEMA,
    compile_conditional_metar_path_state,
)
from bayes_basket_target.metar_trajectory import (
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
)
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.settlement_time import settlement_day_window


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


def _poll_receipt_verified(row: Mapping[str, Any]) -> bool:
    """Require an immutable official AWC poll receipt for production folds.

    Historical observation rows predate this guard and the small pure-domain
    unit fixtures intentionally omit poll columns.  Callers opt in through
    ``require_source_poll_receipt``; when enabled, a row is usable only when
    its joined poll is an AWC HTTP-200 response, completed at a valid UTC
    time, and contains the exact raw report/issue time already bound to the
    observation.  No transport or report timestamp is inferred.
    """

    if str(row.get("source_lane") or "").strip().lower() != "awc":
        return False
    if str(row.get("poll_source_lane") or "").strip().lower() != "awc":
        return False
    try:
        if int(row.get("poll_response_status")) != 200:
            return False
    except (TypeError, ValueError):
        return False
    completed = str(row.get("poll_response_completed_at") or "").strip()
    try:
        completed_at = _parsed_utc(completed)
    except (TypeError, ValueError):
        return False
    raw_text = str(row.get("raw_text") or "")
    if not raw_text:
        return False
    poll_json = row.get("poll_json")
    try:
        payload = json.loads(str(poll_json or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    reports = payload.get("reports") if isinstance(payload, Mapping) else None
    if not isinstance(reports, list):
        return False
    try:
        valid_at = _parsed_utc(str(row.get("valid_at") or ""))
    except (TypeError, ValueError):
        return False
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        if str(report.get("station_id") or "").strip().upper() != str(
            row.get("station_id") or ""
        ).strip().upper():
            continue
        if str(report.get("raw_text") or "") != raw_text:
            continue
        issue = str(report.get("source_report_time") or "").strip()
        if not issue:
            continue
        try:
            if _parsed_utc(issue) != valid_at:
                continue
        except (ContractError, TypeError, ValueError):
            continue
        return True
    return False


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
            SELECT o.observation_id,o.source_lane,o.station_id,o.report_type,
                   o.valid_at,o.reported_temperature_c,o.raw_sha256,o.raw_text,
                   o.canonical_committed_at,o.correction_suspected,
                   o.source_poll_id,p.source_lane AS poll_source_lane,
                   p.response_status AS poll_response_status,
                   p.response_completed_at AS poll_response_completed_at,
                   p.poll_json
              FROM reported_trajectory_observations o
              LEFT JOIN reported_trajectory_source_polls p
                ON p.poll_id=o.source_poll_id
             WHERE o.station_id=?
               AND lower(o.source_lane)='awc'
               AND o.report_type IN ('METAR','SPECI')
             ORDER BY o.valid_at,o.canonical_committed_at,o.observation_id
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
                   bucket_ordinals_json,probabilities_json,posterior_sha256,lineage_json,
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
    anchor_to_first_report: bool = False,
    require_source_poll_receipt: bool = False,
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
    # ``decided_at_utc`` is the strict historical evaluation cut.  A fold is
    # OOS only when its target settlement day is already closed and its truth
    # was committed by this cut.  Future/forward rows are retained below as
    # observational diagnostics, but never enter ``folds`` or any training
    # count.
    evaluation_as_of = _parsed_utc(decided_at_utc)
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
    observational_candidates: list[dict[str, str]] = []
    ordered_truth = sorted(
        truth_rows,
        key=lambda row: (
            str(row.get("local_date") or ""),
            int(row.get("revision_no") or 0),
        ),
    )
    for truth in ordered_truth:
        event_date = str(truth.get("local_date") or "")
        bucket_start = datetime.combine(
            datetime.fromisoformat(event_date).date(),
            time(BUCKET_START_HOUR[bucket], 0),
            tzinfo=zone,
        )
        local_cutoff = bucket_start
        cutoff = local_cutoff.astimezone(timezone.utc)
        truth_settled = _parsed_utc(
            str(truth.get("canonical_committed_at_utc") or "")
        )
        settlement_closed = _parsed_utc(
            settlement_day_window(event_date, settlement_timezone)[
                "target_day_end_utc"
            ]
        )
        if settlement_closed > evaluation_as_of:
            candidate = {
                "truth_id": str(truth.get("truth_id") or ""),
                "event_date": event_date,
                "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
                "settlement_day_end_utc": _iso_utc(settlement_closed),
                "truth_settled_at_utc": _iso_utc(truth_settled),
                "training_cutoff_utc": _iso_utc(cutoff),
                "evidence_role": "observational_candidate_not_oos",
                "reason": "future_event_not_oos",
            }
            observational_candidates.append(candidate)
            excluded.append({
                "event_date": event_date,
                "reason": "future_event_not_oos",
            })
            continue
        if truth_settled > evaluation_as_of:
            candidate = {
                "truth_id": str(truth.get("truth_id") or ""),
                "event_date": event_date,
                "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
                "settlement_day_end_utc": _iso_utc(settlement_closed),
                "truth_settled_at_utc": _iso_utc(truth_settled),
                "training_cutoff_utc": _iso_utc(cutoff),
                "evidence_role": "observational_candidate_not_oos",
                "reason": "future_truth_not_oos",
            }
            observational_candidates.append(candidate)
            excluded.append({
                "event_date": event_date,
                "reason": "future_truth_not_oos",
            })
            continue
        if cutoff > evaluation_as_of:
            candidate = {
                "truth_id": str(truth.get("truth_id") or ""),
                "event_date": event_date,
                "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
                "settlement_day_end_utc": _iso_utc(settlement_closed),
                "truth_settled_at_utc": _iso_utc(truth_settled),
                "training_cutoff_utc": _iso_utc(cutoff),
                "evidence_role": "observational_candidate_not_oos",
                "reason": "future_training_cutoff_not_oos",
            }
            observational_candidates.append(candidate)
            excluded.append({
                "event_date": event_date,
                "reason": "future_training_cutoff_not_oos",
            })
            continue
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
        if anchor_to_first_report:
            # Runtime P1 is evaluated after a real report enters the AWC
            # lane, not at an arbitrary local 00/06/12/18 boundary.  Anchor
            # each historical fold to the first report whose valid time is at
            # or after the bucket start and whose canonical receipt is already
            # visible.  This remains strict-as-of and prevents the NIGHT
            # bucket from requiring a report that cannot exist before 00:00.
            candidates = [
                row
                for row in observations_by_date.get(event_date, ())
                if _parsed_utc(str(row.get("valid_at") or "")) >= cutoff
                and _parsed_utc(str(row.get("canonical_committed_at") or ""))
                <= evaluation_as_of
                and (
                    not require_source_poll_receipt
                    or _poll_receipt_verified(row)
                )
            ]
            if candidates:
                cutoff = min(
                    _parsed_utc(str(row.get("canonical_committed_at") or ""))
                    for row in candidates
                )
                prefix = [
                    row
                    for row in observations_by_date.get(event_date, ())
                    if _parsed_utc(str(row.get("valid_at") or "")) <= cutoff
                    and _parsed_utc(
                        str(row.get("canonical_committed_at") or "")
                    )
                    <= cutoff
                    and (
                        not require_source_poll_receipt
                        or _poll_receipt_verified(row)
                    )
                ]
        elif require_source_poll_receipt:
            prefix = [row for row in prefix if _poll_receipt_verified(row)]
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
            "raw_text": str(row.get("raw_text") or ""),
            "source_poll_id": str(row.get("source_poll_id") or ""),
            "poll_source_lane": str(row.get("poll_source_lane") or ""),
            "poll_response_status": row.get("poll_response_status"),
            "poll_response_completed_at": str(
                row.get("poll_response_completed_at") or ""
            ),
        } for row in prefix]
        fold = {
            "fold_id": f"{station}-{bucket}-{event_date}",
            "target_local_date": event_date,
            "evaluation_truth_id": str(truth["truth_id"]),
            "evaluation_truth_settled_at_utc": _iso_utc(
                truth_settled
            ),
            "training_cutoff_utc": _iso_utc(cutoff),
            "training_cutoff_policy": (
                "FIRST_OFFICIAL_AWC_REPORT_RECEIPT_AFTER_BUCKET_START"
                if anchor_to_first_report
                else "LOCAL_BUCKET_START"
            ),
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
                if not centers:
                    # The dominant native P0 path publishes the bucket
                    # geometry as intervals only -- 426 of 482 committed P0
                    # roots carry ``bucket_intervals_c_json`` with no
                    # ``bucket_centers_c_json`` -- and requiring the centers
                    # key silently disqualified every such root, which left
                    # every fold without a conditional path state and the
                    # hourly-curve OOS gate permanently unsatisfiable.  The
                    # centers are fully determined by the intervals: the
                    # midpoint, or the finite edge offset by half a degree for
                    # an open-ended bucket, matching the runtime fallback in
                    # ``p1_compiler._bucket_geometry_c``.
                    root_intervals = json.loads(
                        str(
                            lineage.get("bucket_intervals_c_json")
                            or "{}"
                        )
                    )
                    if isinstance(root_intervals, dict):
                        derived: dict[str, float] = {}
                        for interval_bucket, bounds in root_intervals.items():
                            if (
                                not isinstance(bounds, (list, tuple))
                                or len(bounds) != 2
                            ):
                                derived = {}
                                break
                            lower, upper = bounds
                            if lower is not None and upper is not None:
                                derived[str(interval_bucket)] = (
                                    float(lower) + float(upper)
                                ) / 2.0
                            elif upper is not None:
                                derived[str(interval_bucket)] = (
                                    float(upper) - 0.5
                                )
                            elif lower is not None:
                                derived[str(interval_bucket)] = (
                                    float(lower) + 0.5
                                )
                            else:
                                derived = {}
                                break
                        centers = derived
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
                future_curve = None
                future_curve_raw = lineage.get(
                    "conditional_future_temperature_curve_json"
                )
                if future_curve_raw:
                    future_curve = json.loads(str(future_curve_raw))
                    declared_curve_sha = str(
                        lineage.get(
                            "conditional_future_temperature_curve_sha256"
                        )
                        or ""
                    ).strip().lower()
                    if (
                        not isinstance(future_curve, dict)
                        or str(
                            future_curve.get("content_sha256") or ""
                        ).lower()
                        != declared_curve_sha
                    ):
                        raise ContractError(
                            "metar_trajectory_fold_future_curve_sha256_mismatch"
                        )
                    curve_root_stage = str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_stage"
                        )
                        or root.get("stage")
                        or ""
                    )
                    curve_root_checkpoint = str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_checkpoint_id"
                        )
                        or root.get("checkpoint_id")
                        or ""
                    )
                    curve_root_probability = str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_probability_sha256"
                        )
                        or root.get("posterior_sha256")
                        or ""
                    )
                    direct_binding = (
                        curve_root_stage == str(root.get("stage") or "")
                        and curve_root_checkpoint
                        == str(root.get("checkpoint_id") or "")
                        and curve_root_probability
                        == str(root.get("posterior_sha256") or "")
                    )
                    p0_ancestor_binding = (
                        str(root.get("stage") or "") == "P0-M"
                        and curve_root_stage == "P0"
                        and curve_root_checkpoint
                        == str(
                            lineage.get("p0_root_checkpoint_id") or ""
                        )
                        and curve_root_probability
                        == str(
                            lineage.get("p0_root_posterior_sha256") or ""
                        )
                    )
                    if not direct_binding and not p0_ancestor_binding:
                        raise ContractError(
                            "metar_trajectory_fold_future_curve_binding_invalid"
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
                path_state = compile_conditional_metar_path_state(
                    [
                        {
                            "observation_id": str(row["observation_id"]),
                            "observed_at_utc": str(row["valid_at"]),
                            "temperature_c": float(
                                row["reported_temperature_c"]
                            ),
                            "raw_text": str(row.get("raw_text") or ""),
                        }
                        for row in prefix
                    ],
                    as_of_utc=_iso_utc(cutoff),
                    settlement_timezone=settlement_timezone,
                    root_probabilities=probabilities,
                    bucket_centers_c=centers,
                    future_curve=future_curve,
                    root_checkpoint_id=str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_checkpoint_id"
                        )
                        or root.get("checkpoint_id")
                        or ""
                    ),
                    root_probability_sha256=str(
                        lineage.get(
                            "conditional_future_temperature_curve_root_probability_sha256"
                        )
                        or root.get("posterior_sha256")
                        or ""
                    ),
                    station_id=station,
                    event_date=event_date,
                )
                fold["conditional_path_state"] = path_state.to_dict()
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
        "evaluation_as_of_utc": _iso_utc(evaluation_as_of),
        "training_cutoff_policy": (
            "FIRST_OFFICIAL_AWC_REPORT_RECEIPT_AFTER_BUCKET_START"
            if anchor_to_first_report
            else "LOCAL_BUCKET_START"
        ),
        "source_poll_receipt_required": bool(require_source_poll_receipt),
        "eligible_fold_count": len(folds),
        "v3_eligible_fold_count": sum(
            bool(
                fold.get("conditional_path_state", {}).get(
                    "future_curve_complete"
                )
            )
            for fold in folds
        ),
        "v3_incomplete_future_curve_fold_count": sum(
            not bool(
                fold.get("conditional_path_state", {}).get(
                    "future_curve_complete"
                )
            )
            for fold in folds
        ),
        "observational_candidate_count": len(observational_candidates),
        "observational_candidates": observational_candidates,
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
                "conditional_path_state": (
                    "strict-as-of remaining time, full-prefix temperature "
                    "momentum and acceleration, METAR physical state, and "
                    "model-root expected remaining warming"
                ),
                "conditional_future_temperature_curve": (
                    "same-capture, content-addressed hourly curve combined "
                    "from at least two activated independent model families"
                ),
            },
            "missing_observation_policy": "DEGRADE_STRENGTH",
            "expected_interval_minutes": 60,
            "long_interval_minutes": 90,
            "max_missing_intervals": 4,
            "missing_interval_strength_factor": 0.8,
            "long_interval_strength_factor": 0.5,
            "path_feature_schema": PATH_STATE_SCHEMA,
            "path_model_coefficients": {
                "base_distribution_mean": 0.3,
                "root_expected_remaining_warming": 0.5,
                "momentum_c_per_hour": 0.35,
                "acceleration_c_per_hour2": 0.1,
                "drawdown_from_high_c": -0.5,
                "dewpoint_depression_c": 0.01,
                "cloud_cover_fraction": -0.8,
                "precipitation_present": -1.0,
                "wind_speed_mps": -0.02,
                "remaining_hours": 0.02,
                "predicted_peak_remaining_warming_c": 0.35,
                "minutes_to_predicted_peak": 0.02,
                "next_curve_delta_from_high_c": 0.15,
                "future_curve_slope_c_per_hour": 0.10,
            },
            "path_model_sigma_c": 0.8,
            "hard_exclusion_baseline_distribution": support,
            "base_strength": 0.8,
            "maturity_strength": 0.75,
            "likelihood_ratio_floor": 0.25,
            "likelihood_ratio_ceiling": 4.0,
            "method_version": (
                METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
            ),
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
    parser.add_argument(
        "--anchor-to-first-report",
        action="store_true",
        help=(
            "Anchor each historical fold to the first official AWC report "
            "receipt after the bucket start instead of the nominal bucket "
            "boundary."
        ),
    )
    parser.add_argument(
        "--require-source-poll-receipt",
        action="store_true",
        help=(
            "Require the joined AWC HTTP-200 poll receipt and exact raw "
            "report-time binding for every fold observation."
        ),
    )
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
        anchor_to_first_report=bool(args.anchor_to_first_report),
        require_source_poll_receipt=bool(args.require_source_poll_receipt),
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
