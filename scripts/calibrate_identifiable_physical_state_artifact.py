#!/usr/bin/env python3
"""Build a shadow-only state-transition candidate from historical METAR only.

The calibration contract is deliberately narrower than the production physical
composer: it estimates one-step temperature state transitions from the first
official AMSC METAR seen for each valid time.  TAF, NWP, forecast curves,
forecast peaks, market data and settlement labels are not read by this module.
The last chronological days are held out strictly OOS and are never used to
fit the candidate parameters.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identifiable_physical_state_bayes.core import (  # noqa: E402
    canonical_sha256,
    validate_state_model_artifact,
)


SCHEMA_VERSION = "identifiable_physical_metar_transition_calibration_v2"
DIRECT_AMSC_PARSER_VERSION = "amsc_zuuu_metar_taf_fetcher_v1"
METAR_SOURCE_ID = "zuuu_metar_raw_amsc"
METAR_STATION_ID = "ZUUU"
MAX_TRANSITION_HOURS = 6.0
# A one-step METAR temperature difference is an observation residual, not a
# persistent daily warming rate. Keep the fitted level bounded; the observed
# spread belongs in process noise. Otherwise a q90 value such as +2 C/h is
# reapplied at every future slot and creates impossible multi-hour paths.
MAX_PERSISTENT_TENDENCY_BIAS_C_PER_HOUR = 0.5
DEFAULT_MINIMUM_TRAINING_DAYS = 14
DEFAULT_MINIMUM_HOLDOUT_DAYS = 4
QUANTILES = (
    ("transition_tendency_q10", 0.10, 0.10),
    ("transition_tendency_q30", 0.30, 0.20),
    ("transition_tendency_q50", 0.50, 0.40),
    ("transition_tendency_q70", 0.70, 0.20),
    ("transition_tendency_q90", 0.90, 0.10),
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _strictly_before(left: Any, right: Any) -> bool:
    left_instant = _instant(left)
    right_instant = _instant(right)
    return (
        left_instant is not None
        and right_instant is not None
        and left_instant < right_instant
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    rows = sorted(float(value) for value in values)
    if not rows:
        raise ValueError("metar_transition_calibration_empty_quantile")
    position = probability * (len(rows) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return rows[lower]
    fraction = position - lower
    return rows[lower] * (1.0 - fraction) + rows[upper] * fraction


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _metar_observation(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate a direct official METAR row without touching forecast columns."""

    if (
        _text(row.get("source_id")) != METAR_SOURCE_ID
        or _text(row.get("station_id")) != METAR_STATION_ID
        or _text(row.get("parser_version")) != DIRECT_AMSC_PARSER_VERSION
        or not _text(row.get("role")).startswith("metar_hard_observation")
        or not _text(row.get("raw_ref")).startswith("METAR ZUUU ")
        or not _text(row.get("raw_sha256"))
    ):
        return None
    valid_at = _instant(row.get("valid_time"))
    available_at = _instant(row.get("response_completed_at"))
    if valid_at is None or available_at is None:
        return None
    try:
        temperature = float(row.get("temperature_c"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(temperature):
        return None
    event_date = _text(row.get("event_date"))[:10]
    if not event_date:
        return None
    return {
        "source_observation_id": _text(row.get("source_observation_id")),
        "event_date": event_date,
        "valid_time": valid_at.isoformat().replace("+00:00", "Z"),
        "response_completed_at": available_at.isoformat().replace("+00:00", "Z"),
        "temperature_c": temperature,
        "raw_sha256": _text(row.get("raw_sha256")),
    }


def _direct_metar_observations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the first-seen immutable METAR for each valid time.

    The database is append-only and polling can store the same report more than
    once.  Selecting the earliest transport completion is the causal as-of view;
    later re-fetches and corrections cannot silently rewrite a training input.
    """

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT source_observation_id,source_id,station_id,event_date,valid_time,"
        "response_completed_at,temperature_c,raw_ref,raw_sha256,parser_version,role "
        "FROM source_observations WHERE source_id=? AND station_id=? "
        "AND parser_version=? AND raw_ref LIKE 'METAR ZUUU %'",
        (METAR_SOURCE_ID, METAR_STATION_ID, DIRECT_AMSC_PARSER_VERSION),
    ).fetchall()
    by_valid_time: dict[str, dict[str, Any]] = {}
    for raw in rows:
        candidate = _metar_observation(dict(raw))
        if candidate is None:
            continue
        key = candidate["valid_time"]
        previous = by_valid_time.get(key)
        if previous is None or (
            _instant(candidate["response_completed_at"]),
            candidate["source_observation_id"],
        ) < (
            _instant(previous["response_completed_at"]),
            previous["source_observation_id"],
        ):
            by_valid_time[key] = candidate
    return sorted(
        by_valid_time.values(),
        key=lambda row: (_instant(row["valid_time"]), row["source_observation_id"]),
    )


def collect_metar_transition_samples(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Build causal one-step transition rows using official METAR only."""

    observations = _direct_metar_observations(conn)
    samples: list[dict[str, Any]] = []
    for previous, current in zip(observations, observations[1:]):
        if previous["event_date"] != current["event_date"]:
            continue
        previous_valid = _instant(previous["valid_time"])
        current_valid = _instant(current["valid_time"])
        previous_available = _instant(previous["response_completed_at"])
        current_available = _instant(current["response_completed_at"])
        if None in (previous_valid, current_valid, previous_available, current_available):
            continue
        delta_hours = (current_valid - previous_valid).total_seconds() / 3600.0
        if not 0.0 < delta_hours <= MAX_TRANSITION_HOURS:
            continue
        # The next METAR must be observed strictly after the previous one.  This
        # prevents a backfilled row from becoming a false chronological transition.
        if not previous_available < current_available:
            continue
        delta_c = float(current["temperature_c"]) - float(previous["temperature_c"])
        samples.append(
            {
                "schema": "official_amsc_metar_one_step_transition_v1",
                "event_date": current["event_date"],
                "previous_valid_time": previous["valid_time"],
                "current_valid_time": current["valid_time"],
                "previous_response_completed_at": previous["response_completed_at"],
                "current_response_completed_at": current["response_completed_at"],
                "previous_temperature_c": float(previous["temperature_c"]),
                "current_temperature_c": float(current["temperature_c"]),
                "temperature_delta_c": delta_c,
                "delta_hours": delta_hours,
                "observed_tendency_c_per_hour": delta_c / delta_hours,
                "previous_source_observation_id": previous["source_observation_id"],
                "current_source_observation_id": current["source_observation_id"],
                "previous_raw_sha256": previous["raw_sha256"],
                "current_raw_sha256": current["raw_sha256"],
                "strict_asof": True,
                "source_lineage_sha256": canonical_sha256(
                    {
                        "previous_source_observation_id": previous["source_observation_id"],
                        "current_source_observation_id": current["source_observation_id"],
                        "previous_raw_sha256": previous["raw_sha256"],
                        "current_raw_sha256": current["raw_sha256"],
                    }
                ),
            }
        )
    return samples


def collect_strict_asof_samples(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compatibility name for readiness callers; it is now METAR transitions."""

    return collect_metar_transition_samples(conn)


def fit_bounded_parameter_draws(
    samples: Sequence[Mapping[str, Any]],
    *,
    minimum_training_days: int = DEFAULT_MINIMUM_TRAINING_DAYS,
    minimum_holdout_days: int = DEFAULT_MINIMUM_HOLDOUT_DAYS,
) -> dict[str, Any]:
    """Fit bounded transition draws on chronological days and score strict OOS."""

    by_day: dict[str, list[dict[str, Any]]] = {}
    seen_transition_ids: set[str] = set()
    for raw in samples:
        if raw.get("strict_asof") is not True:
            continue
        day = _text(raw.get("event_date"))[:10]
        transition_id = _text(raw.get("current_source_observation_id"))
        try:
            tendency = float(raw.get("observed_tendency_c_per_hour"))
            delta_hours = float(raw.get("delta_hours"))
        except (TypeError, ValueError):
            continue
        if (
            not day
            or not transition_id
            or transition_id in seen_transition_ids
            or not math.isfinite(tendency)
            or not math.isfinite(delta_hours)
            or not 0.0 < delta_hours <= MAX_TRANSITION_HOURS
        ):
            continue
        seen_transition_ids.add(transition_id)
        by_day.setdefault(day, []).append(
            {**dict(raw), "event_date": day, "observed_tendency_c_per_hour": tendency}
        )
    ordered_days = sorted(by_day)
    required = minimum_training_days + minimum_holdout_days
    if len(ordered_days) < required:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_insufficient_strict_asof_metar_days",
            "available_unique_days": len(ordered_days),
            "available_transition_count": sum(len(rows) for rows in by_day.values()),
            "minimum_training_days": minimum_training_days,
            "minimum_holdout_days": minimum_holdout_days,
            "minimum_total_days": required,
            "block_reasons": ["official_metar_transition_training_sample_too_small"],
            "writes_artifact": False,
            "places_orders": False,
        }

    holdout_count = max(minimum_holdout_days, math.ceil(len(ordered_days) * 0.20))
    training_days = ordered_days[:-holdout_count]
    holdout_days = ordered_days[-holdout_count:]
    if len(training_days) < minimum_training_days:
        holdout_count = len(ordered_days) - minimum_training_days
        training_days = ordered_days[:-holdout_count]
        holdout_days = ordered_days[-holdout_count:]
    training_rows = [row for day in training_days for row in by_day[day]]
    holdout_rows = [row for day in holdout_days for row in by_day[day]]
    training_tendencies = [float(row["observed_tendency_c_per_hour"]) for row in training_rows]
    holdout_tendencies = [float(row["observed_tendency_c_per_hour"]) for row in holdout_rows]
    training_median = _quantile(training_tendencies, 0.50)
    centered = [value - training_median for value in training_tendencies]
    robust_sigma = 1.4826 * _quantile([abs(value) for value in centered], 0.50)
    process_scale = _clip((robust_sigma / 0.75) ** 2, 0.25, 4.0)
    persistent_tendency_bias = _clip(
        training_median,
        -MAX_PERSISTENT_TENDENCY_BIAS_C_PER_HOUR,
        MAX_PERSISTENT_TENDENCY_BIAS_C_PER_HOUR,
    )
    draws = [
        {
            "draw_id": draw_id,
            "normalized_weight": weight,
            # No daily maximum or forecast temperature is observed by this fit.
            "latent_temperature_bias_c": 0.0,
            # The quantile rows retain an explicit parameter mixture, but the
            # one-step tendency spread is not a repeated temperature drift.
            # Its uncertainty is represented by process noise instead.
            "net_temperature_tendency_bias_c_per_hour": persistent_tendency_bias,
            "initial_covariance_scale": 1.0,
            "process_noise_scale_multiplier": process_scale,
        }
        for draw_id, _probability, weight in QUANTILES
    ]
    # Keep the OOS interval diagnostic tied to the observed one-step sample.
    # The production parameter draws are intentionally centered and therefore
    # must not be reused as an empirical q10/q90 interval.
    observed_lower = _quantile(training_tendencies, 0.10)
    observed_upper = _quantile(training_tendencies, 0.90)
    holdout_bias = sum(holdout_tendencies) / len(holdout_tendencies)
    holdout_mae = sum(abs(value - training_median) for value in holdout_tendencies) / len(
        holdout_tendencies
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "metar_transition_candidate_fitted_strict_oos",
        "calibration_source": "official_amsc_metar_only",
        "identified_quantity": "one_step_official_metar_temperature_state_transition",
        "persistent_tendency_bias_c_per_hour": persistent_tendency_bias,
        "persistent_tendency_bias_bound_c_per_hour": MAX_PERSISTENT_TENDENCY_BIAS_C_PER_HOUR,
        "one_step_tendency_spread_consumed_as": "process_noise_not_repeated_drift",
        "available_unique_days": len(ordered_days),
        "available_transition_count": len(training_rows) + len(holdout_rows),
        "minimum_training_days": minimum_training_days,
        "minimum_holdout_days": minimum_holdout_days,
        "minimum_total_days": required,
        "identifiability_boundary": {
            "daily_maximum_not_used": True,
            "forecast_peak_not_used": True,
            "taf_not_read": True,
            "nwp_not_read": True,
            "latent_temperature_bias_fixed_zero": True,
            "settlement_s_not_consumed": True,
            "market_price_not_consumed": True,
            "legacy_y_not_consumed": True,
        },
        "training_day_count": len(training_days),
        "holdout_day_count": len(holdout_days),
        "training_transition_count": len(training_rows),
        "holdout_transition_count": len(holdout_rows),
        "sample_volume_policy": {
            "training_days": DEFAULT_MINIMUM_TRAINING_DAYS,
            "strict_oos_days": DEFAULT_MINIMUM_HOLDOUT_DAYS,
            "configured_training_days": minimum_training_days,
            "configured_strict_oos_days": minimum_holdout_days,
            "production_gate": False,
            "production_unblocked_by_sample_volume": True,
        },
        "training_start": training_days[0],
        "training_end": training_days[-1],
        "holdout_start": holdout_days[0],
        "holdout_end": holdout_days[-1],
        "bounded_parameter_draws": draws,
        "holdout_metrics": {
            "q10_q90_tendency_interval_coverage": sum(
                observed_lower <= value <= observed_upper for value in holdout_tendencies
            )
            / len(holdout_tendencies),
            "training_observed_tendency_q10_c_per_hour": observed_lower,
            "training_observed_tendency_q90_c_per_hour": observed_upper,
            "mean_observed_tendency_c_per_hour": holdout_bias,
            "mean_absolute_tendency_error_against_training_median": holdout_mae,
        },
        "sample_lineage_sha256": canonical_sha256(
            [row for day in ordered_days for row in by_day[day]]
        ),
        "writes_artifact": False,
        "places_orders": False,
    }


def build_candidate_artifact(
    base_artifact: Mapping[str, Any], calibration: Mapping[str, Any]
) -> dict[str, Any]:
    if calibration.get("ok") is not True:
        raise ValueError("metar_transition_calibration_not_ready")
    value = dict(base_artifact)
    value.pop("artifact_sha256", None)
    value.update(
        {
            "state": "candidate",
            "production_runtime_eligible": False,
            "shadow_only": True,
            "calibration_state": "strict_oos_metar_transition_candidate",
            "promotion_forbidden_until_oos_gates": True,
            "bounded_parameter_draws": list(calibration["bounded_parameter_draws"]),
            "parameter_draw_calibration": {
                key: calibration.get(key)
                for key in (
                    "schema",
                    "state",
                    "calibration_source",
                    "identified_quantity",
                    "persistent_tendency_bias_c_per_hour",
                    "persistent_tendency_bias_bound_c_per_hour",
                    "one_step_tendency_spread_consumed_as",
                    "identifiability_boundary",
                    "training_day_count",
                    "holdout_day_count",
                    "training_transition_count",
                    "holdout_transition_count",
                    "sample_volume_policy",
                    "training_start",
                    "training_end",
                    "holdout_start",
                    "holdout_end",
                    "holdout_metrics",
                    "sample_lineage_sha256",
                )
            },
            "calibration_input_contract": {
                "source_id": METAR_SOURCE_ID,
                "station_id": METAR_STATION_ID,
                "parser_version": DIRECT_AMSC_PARSER_VERSION,
                "observation_kind": "official_METAR_temperature_only",
                "fit_target": "one_step_state_transition",
                "strict_asof_first_seen_per_valid_time": True,
                "excluded_inputs": [
                    "TAF",
                    "NWP",
                    "forecast_peak_c",
                    "forecast_scenario_paths",
                    "daily_maximum_label",
                    "market_data",
                    "settlement_data",
                ],
            },
        }
    )
    value["artifact_sha256"] = canonical_sha256(value)
    return validate_state_model_artifact(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument(
        "--base-artifact",
        type=Path,
        default=ROOT / "config/artifacts/identifiable_physical_state_model_v1.candidate.json",
    )
    parser.add_argument(
        "--minimum-training-days",
        type=int,
        default=DEFAULT_MINIMUM_TRAINING_DAYS,
    )
    parser.add_argument(
        "--minimum-holdout-days",
        type=int,
        default=DEFAULT_MINIMUM_HOLDOUT_DAYS,
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--confirm-write-candidate", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True) as conn:
        samples = collect_metar_transition_samples(conn)
    result = fit_bounded_parameter_draws(
        samples,
        minimum_training_days=max(1, args.minimum_training_days),
        minimum_holdout_days=max(1, args.minimum_holdout_days),
    )
    result["strict_asof_samples"] = samples
    if args.output is not None:
        if not args.confirm_write_candidate:
            result = {
                **result,
                "ok": False,
                "state": "blocked_write_confirmation_missing",
                "block_reasons": ["confirm_write_candidate_required"],
            }
        elif result.get("ok") is True:
            base = validate_state_model_artifact(
                json.loads(args.base_artifact.read_text(encoding="utf-8"))
            )
            candidate = build_candidate_artifact(base, result)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result["writes_artifact"] = True
            result["candidate_artifact_path"] = str(args.output.resolve())
            result["candidate_artifact_sha256"] = candidate["artifact_sha256"]
    if args.audit_output is not None:
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
