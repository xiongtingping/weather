#!/usr/bin/env python3
"""Read-only canonical replay for the identifiable physical Bayes candidate."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identifiable_physical_state_bayes.core import (  # noqa: E402
    canonical_sha256,
    validate_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.contracts import PhysicalStatePosterior  # noqa: E402
from identifiable_physical_state_bayes.adapters import (  # noqa: E402
    drivers_from_weak_likelihood,
)
from identifiable_physical_state_bayes.serialization import (  # noqa: E402
    posterior_from_payload,
    posterior_payload,
    posterior_sha256,
)
from identifiable_physical_state_bayes.reporting import project_future_paths  # noqa: E402
from identifiable_physical_state_bayes.reporting import (  # noqa: E402
    validate_reporting_operator_artifact,
)
from production_probability_physical_state_composer import (  # noqa: E402
    _future_drivers,
    build_remaining_reporting_slots,
    compose_physical_canonical_event_extension,
)
from zuuu_metar_reporting_schedule import expected_regular_metar_slots_utc  # noqa: E402


SCHEMA_VERSION = "identifiable_physical_state_bayes_candidate_replay_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(ZoneInfo("UTC")) if parsed.tzinfo is not None else None


def _at_or_before(left: Any, right: Any) -> bool:
    left_instant = _instant(left)
    right_instant = _instant(right)
    return left_instant is not None and right_instant is not None and left_instant <= right_instant


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _distribution(value: Any) -> dict[int, float]:
    raw = _parse(value, {})
    if not isinstance(raw, Mapping):
        return {}
    result = {int(float(key)): float(probability) for key, probability in raw.items()}
    total = sum(result.values())
    return {key: probability / total for key, probability in result.items()} if total > 0.0 else {}


def _metrics(distribution: Mapping[int, float], actual: int) -> dict[str, float | int | bool]:
    if not distribution:
        return {"available": False, "actual_x": actual}
    probability = float(distribution.get(actual, 0.0))
    lower = min(min(distribution), actual) - 1
    upper = max(max(distribution), actual) + 1
    cdf = 0.0
    crps = 0.0
    for bucket in range(lower, upper + 1):
        cdf += float(distribution.get(bucket, 0.0))
        observation_cdf = 1.0 if bucket >= actual else 0.0
        crps += (cdf - observation_cdf) ** 2
    expected_absolute_error = sum(
        abs(bucket - actual) * probability_value
        for bucket, probability_value in distribution.items()
    )
    top = max(distribution, key=lambda bucket: (distribution[bucket], -abs(bucket - actual), -bucket))
    def central_interval(nominal: float) -> tuple[int, int]:
        tail = (1.0 - nominal) / 2.0
        cumulative = 0.0
        lower_bucket = min(distribution)
        upper_bucket = max(distribution)
        for bucket in sorted(distribution):
            cumulative += float(distribution[bucket])
            if cumulative + 1e-15 >= tail:
                lower_bucket = bucket
                break
        cumulative = 0.0
        for bucket in sorted(distribution, reverse=True):
            cumulative += float(distribution[bucket])
            if cumulative + 1e-15 >= tail:
                upper_bucket = bucket
                break
        return lower_bucket, upper_bucket

    intervals = {name: central_interval(level) for name, level in (("80", 0.80), ("90", 0.90), ("95", 0.95))}
    return {
        "available": True,
        "actual_x": actual,
        "actual_probability": probability,
        "nll": -math.log(max(probability, 1e-12)),
        "crps": crps,
        "expected_absolute_error_c": expected_absolute_error,
        "top_bucket": top,
        "top_bucket_hit": top == actual,
        "support_min_bucket": min(distribution),
        "support_max_bucket": max(distribution),
        "positive_support_bucket_count": len(distribution),
        "positive_support_buckets": sorted(distribution),
        "finite_support_contains_actual": min(distribution) <= actual <= max(distribution),
        "strict_positive_actual_support": probability > 0.0,
        "central_80_covered": intervals["80"][0] <= actual <= intervals["80"][1],
        "central_90_covered": intervals["90"][0] <= actual <= intervals["90"][1],
        "central_95_covered": intervals["95"][0] <= actual <= intervals["95"][1],
        "central_80_interval": list(intervals["80"]),
        "central_90_interval": list(intervals["90"]),
        "central_95_interval": list(intervals["95"]),
    }


def _prior_context_from_legacy_p0(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluation-only scenario-weight bridge; never becomes production state."""

    rows = _parse(checkpoint.get("joint_posterior_json"), [])
    marginal: dict[float, float] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("continuous_y_c") is None:
            continue
        value = float(row["continuous_y_c"])
        marginal[value] = marginal.get(value, 0.0) + float(row.get("probability") or 0.0)
    return {
        "continuous_distribution": {
            "grid": [
                {"temp_c": value, "mass": probability}
                for value, probability in sorted(marginal.items())
            ]
        },
        "evaluation_only_legacy_y_used_for_scenario_weighting": True,
        "legacy_y_not_stored_in_physical_state": True,
    }


def _attested_p0_extension(
    *,
    checkpoint: Mapping[str, Any],
    target_identity: str,
    weak_likelihood: Mapping[str, Any],
    state_artifact: Mapping[str, Any],
    reporting_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Project from the committed production P0, without rebuilding Z0.

    Candidate replay must start from the same attested physical P0 as the
    production lineage.  Reconstructing P0 from legacy Y (or a default
    temperature) changes the initial state and makes the replay comparison
    measure the harness, not the candidate transition.
    """

    payload = _parse(checkpoint.get("physical_state_posterior_json"), {})
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("candidate_replay_attested_p0_physical_state_missing")
    declared_sha = _text(checkpoint.get("physical_state_posterior_sha256"))
    if len(declared_sha) != 64 or posterior_sha256(payload) != declared_sha:
        raise ValueError("candidate_replay_attested_p0_physical_state_hash_mismatch")
    p0_state_artifact_sha256 = _text(checkpoint.get("physical_state_model_artifact_sha256"))
    posterior = posterior_from_payload(payload)
    candidate_draws = [
        row
        for row in state_artifact.get("bounded_parameter_draws") or []
        if isinstance(row, Mapping) and _text(row.get("draw_id"))
    ]
    candidate_draw_ids = { _text(row.get("draw_id")) for row in candidate_draws }
    if candidate_draws and any(
        item.bounded_parameter_draw_id not in candidate_draw_ids
        for item in posterior.components
    ):
        rebound = []
        for item in posterior.components:
            for draw in candidate_draws:
                draw_id = _text(draw.get("draw_id"))
                rebound.append(
                    replace(
                        item,
                        component_id="phys-"
                        + canonical_sha256(
                            {
                                "p0_component_id": item.component_id,
                                "candidate_draw_id": draw_id,
                            }
                        ),
                        normalized_weight=item.normalized_weight
                        * float(draw.get("normalized_weight") or 0.0),
                        bounded_parameter_draw_id=draw_id,
                    )
                )
        posterior = PhysicalStatePosterior(
            components=tuple(rebound),
            representation=posterior.representation,
            state_schema=posterior.state_schema,
            normalization_error=posterior.normalization_error,
            uncertainty_breakdown=posterior.uncertainty_breakdown,
        )
    reference = _text(checkpoint.get("last_event_valid_at"))
    slots = build_remaining_reporting_slots(
        target_identity=target_identity,
        state_reference_valid_at=reference,
        reporting_operator_artifact=reporting_artifact,
        include_reference=True,
    )
    scenarios = tuple(sorted({item.forcing_scenario_id for item in posterior.components}))
    projection = project_future_paths(
        posterior=posterior,
        state_reference_valid_at=reference,
        reporting_slots=slots,
        drivers_by_scenario=_future_drivers(
            weak_likelihood=weak_likelihood,
            scenario_ids=scenarios,
            slots=slots,
            state_model_artifact=state_artifact,
        ),
        observed_contract_max_c=None,
        state_model_artifact=state_artifact,
        reporting_operator_artifact=reporting_artifact,
    )
    rebound_payload = posterior_payload(posterior)
    rebound_sha = posterior_sha256(rebound_payload)
    return {
        "schema": "identifiable_physical_state_bayes_v1",
        "physical_composer_schema": "identifiable_physical_state_bayes_v1_pre_event_predict_update",
        "physical_state_posterior_schema": payload.get("state_schema"),
        "physical_state_posterior_json": json.dumps(
            rebound_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "physical_state_posterior_sha256": rebound_sha,
        "physical_path_projection_json": json.dumps(
            asdict(projection), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "physical_path_projection_sha256": projection.path_projection_sha256,
        "physical_state_model_artifact_sha256": state_artifact["artifact_sha256"],
        "physical_reporting_operator_sha256": projection.reporting_operator_sha256,
        "physical_driver_generation_sha256": _text(
            checkpoint.get("physical_driver_generation_sha256")
        ),
        "p0_state_model_artifact_sha256": p0_state_artifact_sha256,
        "candidate_transition_artifact_sha256": _text(state_artifact.get("artifact_sha256")),
        "reported_physical_x_distribution": dict(projection.reported_x_distribution),
        "initialization_lineage": {
            "source": "attested_production_p0",
            "p0_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
            "p0_physical_state_posterior_sha256": declared_sha,
            "legacy_y_used_for_physical_state": False,
            "source_temperature_path_consumed": False,
            "lineage_sha256": canonical_sha256(
                {
                    "p0_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
                    "p0_physical_state_posterior_sha256": declared_sha,
                }
            ),
        },
        "places_orders": False,
    }


def _selected_generation_p0(
    conn: sqlite3.Connection, target: str, *, as_of: str
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT p.* FROM production_initial_posterior_lineage l "
        "JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id "
        "JOIN weak_forecast_evidence_snapshots w "
        "ON w.forecast_evidence_snapshot_id=p.forecast_evidence_snapshot_id "
        "WHERE p.target_identity=? "
        "AND COALESCE(length(w.forecast_scenario_paths_json),0)>2 "
        "AND COALESCE(length(w.candidate_scenario_responsibilities_json),0)>2 "
        "AND COALESCE(length(p.physical_state_posterior_json),0)>2 "
        "ORDER BY p.replay_generation DESC,p.rowid DESC",
        (target,),
    ).fetchall()
    for row in rows:
        value = dict(row)
        if not _at_or_before(value.get("created_at"), as_of):
            continue
        # Re-read the weak snapshot timestamp rather than trusting a later
        # materialization.  A snapshot created after the first OOS observation
        # would leak future source state into the initial condition.
        snapshot = conn.execute(
            "SELECT decision_as_of FROM weak_forecast_evidence_snapshots "
            "WHERE forecast_evidence_snapshot_id=?",
            (_text(value.get("forecast_evidence_snapshot_id")),),
        ).fetchone()
        if snapshot is None or not _at_or_before(snapshot[0], as_of):
            continue
        return value
    return None


def _weak(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ValueError("candidate_replay_weak_snapshot_missing")
    value = dict(row)
    return {
        "artifact_sha256": _text(value.get("weak_likelihood_artifact_sha256")),
        "forecast_scenario_paths": _parse(value.get("forecast_scenario_paths_json"), {}),
        "candidate_scenario_responsibilities": _parse(
            value.get("candidate_scenario_responsibilities_json"), {}
        ),
        "source_revisions": _parse(value.get("source_revisions_json"), []),
        "evaluation_only_allow_legacy_scenario_weight_adapter": True,
    }


def _events(conn: sqlite3.Connection, target: str, event_date: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    scheduled = set(expected_regular_metar_slots_utc(event_date))
    result = []
    for row in conn.execute(
        "SELECT * FROM canonical_observation_events WHERE target_identity=? "
        "AND station_id='ZUUU' AND selected_source_id='zuuu_metar_raw_amsc' "
        "AND state='accepted_primary' ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id",
        (target,),
    ).fetchall():
        value = dict(row)
        value["parsed_observation"] = _parse(value.get("parsed_observation_json"), {})
        value["is_contract_report_slot"] = (
            _text(value.get("report_type")).upper() == "METAR"
            and _text(value.get("valid_time")) in scheduled
        )
        result.append(value)
    return result


def _actual_x(events: list[Mapping[str, Any]]) -> int | None:
    temperatures = []
    for event in events:
        payload = event.get("parsed_observation")
        if event.get("is_contract_report_slot") and isinstance(payload, Mapping):
            value = payload.get("temperature_c")
            if value is not None:
                temperatures.append(int(float(value)))
    return max(temperatures) if temperatures else None


def _legacy_by_event(
    conn: sqlite3.Connection, *, target: str, replay_generation: int
) -> dict[str, dict[int, float]]:
    conn.row_factory = sqlite3.Row
    result = {}
    for row in conn.execute(
        "SELECT last_canonical_event_id,reported_physical_x_distribution_json FROM posterior_checkpoints "
        "WHERE target_identity=? AND replay_generation=? AND last_canonical_event_id<>'' ORDER BY rowid",
        (target, replay_generation),
    ).fetchall():
        result[_text(row[0])] = _distribution(row[1])
    return result


def _average(rows: list[Mapping[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get("available") is True and row.get(field) is not None]
    return sum(values) / len(values) if values else None


def _rate(rows: list[Mapping[str, Any]], field: str) -> float | None:
    values = [bool(row[field]) for row in rows if row.get("available") is True and field in row]
    return sum(values) / len(values) if values else None


def _posterior_state_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize the internal state without making it a second probability path."""

    payload = _parse(value.get("physical_state_posterior_json"), {})
    if not isinstance(payload, Mapping):
        return {
            "available": False,
            "expected_latent_temperature_c": None,
            "expected_net_temperature_tendency_c_per_hour": None,
        }
    rows = payload.get("components")
    if not isinstance(rows, list) or not rows:
        return {
            "available": False,
            "expected_latent_temperature_c": None,
            "expected_net_temperature_tendency_c_per_hour": None,
        }
    total = 0.0
    temperature = 0.0
    tendency = 0.0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        mean = row.get("continuous_state_mean")
        if not isinstance(mean, Mapping):
            continue
        try:
            weight = float(row.get("normalized_weight"))
            latent = float(mean.get("latent_temperature"))
            net = float(mean.get("net_temperature_tendency"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(item) for item in (weight, latent, net)) or weight < 0.0:
            continue
        total += weight
        temperature += weight * latent
        tendency += weight * net
    if total <= 0.0:
        return {
            "available": False,
            "expected_latent_temperature_c": None,
            "expected_net_temperature_tendency_c_per_hour": None,
        }
    return {
        "available": True,
        "expected_latent_temperature_c": temperature / total,
        "expected_net_temperature_tendency_c_per_hour": tendency / total,
    }


def _driver_availability_diagnostics(
    *,
    weak_likelihood: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    state_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure missing non-temperature controls across the replay window."""

    paths = weak_likelihood.get("forecast_scenario_paths")
    if not isinstance(paths, Mapping):
        return {}
    scenario_ids = tuple(
        sorted(
            _text(row.get("scenario_id"))
            for row in paths.get("paths") or []
            if isinstance(row, Mapping) and _text(row.get("scenario_id"))
        )
    )
    samples: dict[str, list[Any]] = {scenario: [] for scenario in scenario_ids}
    for event in events:
        valid_at = _text(event.get("valid_time"))
        if not valid_at:
            continue
        try:
            drivers = drivers_from_weak_likelihood(
                weak_likelihood=weak_likelihood,
                valid_at=valid_at,
                scenario_ids=scenario_ids,
                state_model_artifact=state_artifact,
            )
        except (TypeError, ValueError):
            continue
        for scenario in scenario_ids:
            driver = drivers.get(scenario)
            if driver is not None:
                samples[scenario].append(driver)
    result: dict[str, Any] = {}
    for scenario, rows in samples.items():
        if not rows:
            result[scenario] = {"available": False}
            continue
        availability = [float(row.availability) for row in rows]
        missing = sorted({reason for row in rows for reason in row.na_reasons})
        result[scenario] = {
            "available": True,
            "mean": sum(availability) / len(availability),
            "minimum": min(availability),
            "latest": availability[-1],
            "missing_fields": missing,
            "sample_count": len(rows),
        }
    return result


def build_state_space_root_cause_diagnostics(
    *,
    state_artifact: Mapping[str, Any],
    weak_likelihood: Mapping[str, Any],
    event_date: str,
    events: Sequence[Mapping[str, Any]],
    final_extension: Mapping[str, Any],
    candidate_final: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose why a candidate differs without changing candidate probabilities."""

    calibration_state = _text(state_artifact.get("calibration_state")) or "unknown"
    parameter_calibration = state_artifact.get("parameter_draw_calibration")
    parameter_state = (
        _text(parameter_calibration.get("state"))
        if isinstance(parameter_calibration, Mapping)
        else ""
    )
    if not parameter_state:
        parameter_calibration_surface = state_artifact.get("parameter_calibration")
        if isinstance(parameter_calibration_surface, Mapping):
            parameter_state = _text(parameter_calibration_surface.get("state"))
    final_state = _posterior_state_summary(final_extension)
    return {
        "state_model_calibration_state": calibration_state,
        "state_model_parameter_calibration_state": parameter_state or "unknown",
        "transition_parameters_are_scaffold": calibration_state == "untrained_scaffold"
        or parameter_state in {"", "untrained"},
        "temperature_forecast_not_consumed_by_contract": True,
        "temperature_peak_trajectory_and_slope_external_fields_consumed": False,
        "source_forecast_peaks_read_for_diagnostic_only": False,
        "source_forecast_peak_diagnostics": "excluded_from_replay",
        "driver_availability_by_scenario": _driver_availability_diagnostics(
            weak_likelihood=weak_likelihood,
            events=events,
            state_artifact=state_artifact,
        ),
        "latest_internal_state": final_state,
        "primary_root_cause": (
            "untrained_endogenous_state_transition_after_observation_update"
            if calibration_state == "untrained_scaffold"
            else "state_transition_requires_calibration_review"
        ),
        "root_cause_boundary": (
            "official_METAR_observation_transition_calibration_only;"
            "do_not_fit_TAF_or_NWP_temperature_peaks"
        ),
    }


def evaluate_promotion_policy(
    *,
    aligned_event_count: int,
    replayed_day_count: int,
    candidate_summary: Mapping[str, Any],
    p95_compose_ms: float | None,
) -> dict[str, Any]:
    """Separate activation blockers from calibration diagnostics.

    NLL and CRPS stay visible for model monitoring and regression triage, but
    production policy does not allow either metric to veto activation.
    """

    diagnostics = {
        "no_worse_mean_nll_than_legacy": bool(
            candidate_summary.get("candidate_mean_nll") is not None
            and candidate_summary.get("legacy_v6_mean_nll") is not None
            and float(candidate_summary["candidate_mean_nll"])
            <= float(candidate_summary["legacy_v6_mean_nll"])
        ),
        "no_worse_mean_crps_than_legacy": bool(
            candidate_summary.get("candidate_mean_crps") is not None
            and candidate_summary.get("legacy_v6_mean_crps") is not None
            and float(candidate_summary["candidate_mean_crps"])
            <= float(candidate_summary["legacy_v6_mean_crps"])
        ),
    }
    gates = {
        "minimum_oos_aligned_events_100": aligned_event_count >= 100,
        "minimum_independent_replayed_days_30": replayed_day_count >= 30,
        "strict_positive_actual_support_rate_one": (
            candidate_summary.get("candidate_positive_actual_support_rate") == 1.0
        ),
        "p95_compose_under_3000ms": bool(
            p95_compose_ms is not None and p95_compose_ms < 3000.0
        ),
    }
    return {
        "blocking_gates": gates,
        "non_blocking_diagnostics": diagnostics,
        "policy": {
            "nll_activation_role": "non_blocking_diagnostic_only",
            "crps_activation_role": "non_blocking_diagnostic_only",
            "diagnostic_regression_may_alert": True,
            "diagnostic_regression_may_block_activation": False,
        },
        "promotion_ready": all(gates.values()),
    }


def replay(
    *,
    db_path: Path,
    days: int,
    end_date: str,
    state_artifact_path: Path,
    observation_artifact_path: Path,
    reporting_artifact_path: Path,
) -> dict[str, Any]:
    state_artifact = validate_state_model_artifact(json.loads(state_artifact_path.read_text()))
    observation_artifact = validate_observation_artifact(json.loads(observation_artifact_path.read_text()))
    reporting_artifact = validate_reporting_operator_artifact(json.loads(reporting_artifact_path.read_text()))
    end = datetime.fromisoformat(end_date).date()
    requested_days = [(end - timedelta(days=offset)).isoformat() for offset in range(days)]
    calibration = state_artifact.get("parameter_draw_calibration")
    calibration = calibration if isinstance(calibration, Mapping) else {}
    holdout_start = _text(calibration.get("holdout_start"))[:10]
    strict_oos_boundary_available = (
        _text(state_artifact.get("calibration_state"))
        == "strict_oos_metar_transition_candidate"
        and bool(holdout_start)
        and bool(_text(calibration.get("training_end")))
        and _text(calibration.get("training_end"))[:10] < holdout_start
    )
    oos_days = [
        day
        for day in requested_days
        if strict_oos_boundary_available and day >= holdout_start
    ]
    excluded_training_days = [day for day in requested_days if day not in oos_days]
    day_rows = []
    all_candidate = []
    all_legacy = []
    compose_elapsed_ms: list[float] = []
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
        for day in sorted(oos_days):
            target = f"physical_weather_day_v1:ZUUU:{day}"
            try:
                events = _events(conn, target, day)
                actual = _actual_x(events)
                first_actionable_values = [
                    _instant(event.get("first_actionable_seen_at"))
                    for event in events
                    if _instant(event.get("first_actionable_seen_at")) is not None
                ]
                if actual is None:
                    day_rows.append({"event_date": day, "state": "na", "reason": "final_contract_x_missing"})
                    continue
                if not first_actionable_values:
                    day_rows.append({"event_date": day, "state": "na", "reason": "first_metar_asof_missing"})
                    continue
                first_actionable = min(first_actionable_values)
                p0 = _selected_generation_p0(
                    conn,
                    target,
                    as_of=first_actionable.isoformat().replace("+00:00", "Z"),
                )
                if p0 is None:
                    has_p0 = conn.execute(
                        "SELECT 1 FROM posterior_checkpoints WHERE target_identity=? LIMIT 1",
                        (target,),
                    ).fetchone() is not None
                    day_rows.append(
                        {
                            "event_date": day,
                            "state": "na",
                            "reason": "strict_oos_p0_missing_before_first_metar"
                            if has_p0
                            else "legacy_p0_missing",
                        }
                    )
                    continue
                weak = _weak(conn, _text(p0.get("forecast_evidence_snapshot_id")))
                extension = _attested_p0_extension(
                    checkpoint=p0,
                    target_identity=target,
                    weak_likelihood=weak,
                    state_artifact=state_artifact,
                    reporting_artifact=reporting_artifact,
                )
                previous = {
                    **extension,
                    "target_identity": target,
                    "last_event_valid_at": _text(p0.get("last_event_valid_at")),
                    "reported_x_lower_bound_c": None,
                }
                legacy = _legacy_by_event(
                    conn,
                    target=target,
                    replay_generation=int(p0.get("replay_generation") or 0),
                )
                aligned = []
                candidate_final = _distribution(extension["reported_physical_x_distribution"])
                for event in events:
                    compose_started = time.perf_counter()
                    extension = compose_physical_canonical_event_extension(
                        previous_checkpoint=previous,
                        canonical_event=event,
                        weak_likelihood=weak,
                        state_model_artifact=state_artifact,
                        observation_artifact=observation_artifact,
                        reporting_operator_artifact=reporting_artifact,
                    )
                    event_elapsed_ms = (time.perf_counter() - compose_started) * 1000.0
                    compose_elapsed_ms.append(event_elapsed_ms)
                    candidate_distribution = _distribution(
                        extension["reported_physical_x_distribution"]
                    )
                    candidate_final = candidate_distribution
                    event_id = _text(event.get("canonical_observation_event_id"))
                    candidate_metrics = _metrics(candidate_distribution, actual)
                    all_candidate.append(candidate_metrics)
                    legacy_metrics = _metrics(legacy.get(event_id, {}), actual)
                    if event_id in legacy:
                        all_legacy.append(legacy_metrics)
                    aligned.append(
                        {
                            "canonical_event_id": event_id,
                            "valid_time": _text(event.get("valid_time")),
                            "candidate": candidate_metrics,
                            "legacy_v6": legacy_metrics,
                            "candidate_compose_elapsed_ms": event_elapsed_ms,
                        }
                    )
                    previous = {
                        **previous,
                        **extension,
                        "reported_x_lower_bound_c": extension.get("reported_x_lower_bound_c"),
                        "last_event_valid_at": extension["last_event_valid_at"],
                    }
                final_candidate = _metrics(candidate_final, actual)
                final_legacy_distribution = legacy.get(
                    _text(events[-1].get("canonical_observation_event_id")), {}
                ) if events else {}
                final_legacy = _metrics(final_legacy_distribution, actual)
                root_cause_diagnostics = build_state_space_root_cause_diagnostics(
                    state_artifact=state_artifact,
                    weak_likelihood=weak,
                    event_date=day,
                    events=events,
                    final_extension=extension,
                    candidate_final=final_candidate,
                )
                day_rows.append(
                    {
                        "event_date": day,
                        "state": "replayed",
                        "strict_oos": True,
                        "canonical_event_count": len(events),
                        "aligned_event_count": len(aligned),
                        "actual_x": actual,
                        "final_candidate": final_candidate,
                        "final_legacy_v6": final_legacy,
                        "state_space_root_cause_diagnostics": root_cause_diagnostics,
                        "aligned_events": aligned,
                    }
                )
            except Exception as exc:
                day_rows.append(
                    {
                        "event_date": day,
                        "state": "blocked",
                        "reason": f"{type(exc).__name__}:{str(exc)[:300]}",
                    }
                )
    replayed = [row for row in day_rows if row.get("state") == "replayed"]
    candidate_summary = {
        "aligned_event_count": len(all_candidate),
        "candidate_mean_nll": _average(all_candidate, "nll"),
        "legacy_v6_mean_nll": _average(all_legacy, "nll"),
        "candidate_mean_crps": _average(all_candidate, "crps"),
        "legacy_v6_mean_crps": _average(all_legacy, "crps"),
        "candidate_mean_expected_absolute_error_c": _average(
            all_candidate, "expected_absolute_error_c"
        ),
        "legacy_v6_mean_expected_absolute_error_c": _average(
            all_legacy, "expected_absolute_error_c"
        ),
        "candidate_positive_actual_support_rate": _rate(
            all_candidate, "strict_positive_actual_support"
        ),
        "legacy_v6_positive_actual_support_rate": _rate(
            all_legacy, "strict_positive_actual_support"
        ),
        "candidate_central_80_coverage": _rate(all_candidate, "central_80_covered"),
        "candidate_central_90_coverage": _rate(all_candidate, "central_90_covered"),
        "candidate_central_95_coverage": _rate(all_candidate, "central_95_covered"),
        "legacy_v6_central_80_coverage": _rate(all_legacy, "central_80_covered"),
        "legacy_v6_central_90_coverage": _rate(all_legacy, "central_90_covered"),
        "legacy_v6_central_95_coverage": _rate(all_legacy, "central_95_covered"),
    }
    p95_compose_ms = (
        sorted(compose_elapsed_ms)[
            max(0, math.ceil(len(compose_elapsed_ms) * 0.95) - 1)
        ]
        if compose_elapsed_ms
        else None
    )
    promotion = evaluate_promotion_policy(
        aligned_event_count=len(all_candidate),
        replayed_day_count=len(replayed),
        candidate_summary=candidate_summary,
        p95_compose_ms=p95_compose_ms,
    )
    replayed_diagnostics = [
        row.get("state_space_root_cause_diagnostics")
        for row in replayed
        if isinstance(row.get("state_space_root_cause_diagnostics"), Mapping)
    ]
    latest_diagnostics = replayed_diagnostics[-1] if replayed_diagnostics else {}
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(replayed) and not any(row.get("state") == "blocked" for row in day_rows),
        "state": "candidate_replay_complete" if replayed else "candidate_replay_unavailable",
        "read_only": True,
        "places_orders": False,
        "requested_days": requested_days,
        "oos_days": sorted(oos_days),
        "replayed_day_count": len(replayed),
        "candidate_artifacts": {
            "state_model": state_artifact["artifact_sha256"],
            "observation": observation_artifact["artifact_sha256"],
            "reporting_operator": reporting_artifact["artifact_sha256"],
            "production_runtime_eligible": False,
        },
        "evaluation_boundary": {
            "strict_oos": True,
            "strict_oos_boundary_available": strict_oos_boundary_available,
            "training_end": _text(calibration.get("training_end")) or None,
            "holdout_start": holdout_start or None,
            "training_days_excluded_from_scoring": sorted(excluded_training_days),
            "only_historical_official_metar_transition_calibration": True,
            "taf_nwp_temperature_peaks_fitted": False,
            "taf_nwp_temperature_peaks_read_for_replay_diagnostics": False,
            "legacy_y_used_only_to_weight_initial_weak_scenarios": True,
            "legacy_y_never_enters_physical_checkpoint_state": True,
            "not_promotion_evidence_until_strict_asof_oos_rebuilt": True,
        },
        "aligned_summary": candidate_summary,
        "latency_summary_ms": {
            "event_count": len(compose_elapsed_ms),
            "mean": sum(compose_elapsed_ms) / len(compose_elapsed_ms) if compose_elapsed_ms else None,
            "p95": p95_compose_ms,
            "maximum": max(compose_elapsed_ms) if compose_elapsed_ms else None,
        },
        "promotion_gates": promotion["blocking_gates"],
        "non_blocking_diagnostics": promotion["non_blocking_diagnostics"],
        "promotion_policy": promotion["policy"],
        "promotion_ready": promotion["promotion_ready"],
        "state_space_root_cause_diagnostics": latest_diagnostics,
        "promotion_diagnostics_non_blocking": True,
        "days": day_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--end-date",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    )
    parser.add_argument(
        "--state-artifact",
        type=Path,
        default=ROOT / "config/artifacts/identifiable_physical_state_model_v1.candidate.json",
    )
    parser.add_argument(
        "--observation-artifact",
        type=Path,
        default=ROOT / "config/artifacts/identifiable_physical_observation_v1.candidate.json",
    )
    parser.add_argument(
        "--reporting-artifact",
        type=Path,
        default=ROOT / "config/artifacts/amsc_frozen_reporting_operator_v1.candidate.json",
    )
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = replay(
        db_path=args.db,
        days=max(1, args.days),
        end_date=args.end_date,
        state_artifact_path=args.state_artifact,
        observation_artifact_path=args.observation_artifact,
        reporting_artifact_path=args.reporting_artifact,
    )
    if args.summary:
        result["days"] = [
            {key: value for key, value in row.items() if key != "aligned_events"}
            for row in result.get("days") or []
        ]
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
