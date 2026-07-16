"""Projection contract from latent continuous daily high Z to reported AMSC X.

The strong model may retain a continuous latent temperature grid for physical
reachability.  The production target ``X`` is different: it is the maximum
whole-degree temperature reported by scheduled primary AMSC METAR messages.
This module keeps those types explicit and prevents SPECI or METAR rounding
intervals from silently redefining the final-X label.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "reported_physical_x_projection_v1"
TARGET_TYPE = "amsc_scheduled_regular_metar_reported_daily_max_c"
LATENT_TYPE = "continuous_scheduled_metar_slot_daily_high_z_c"
EPSILON = 1e-12
CHECKPOINT_PROJECTION_SCHEMA_VERSION = "posterior_checkpoint_reported_x_projection_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def round_to_reported_c(value: Any) -> int:
    """Round a continuous Celsius value to METAR whole-degree semantics.

    Exact half degrees use half-away-from-zero instead of Python's banker
    rounding.  This makes the discrete projection deterministic at grid
    boundaries while matching the interval contract ``[n-0.5,n+0.5)`` away
    from the measure-zero tie.
    """

    parsed = _finite(value)
    if parsed is None:
        raise ValueError("latent_temperature_non_finite")
    if parsed >= 0.0:
        return int(math.floor(parsed + 0.5))
    return int(math.ceil(parsed - 0.5))


def scheduled_metar_reported_max(
    canonical_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the exact monotone X lower bound from regular METAR only."""

    eligible: list[dict[str, Any]] = []
    ignored_speci_ids: list[str] = []
    blockers: list[str] = []
    for raw in canonical_events:
        event = _mapping(raw)
        report_type = _text(event.get("report_type")).upper()
        event_id = _text(event.get("canonical_observation_event_id"))
        parsed = _mapping(event.get("parsed_observation"))
        temperature = _finite(parsed.get("temperature_c"))
        if report_type == "SPECI":
            if event_id:
                ignored_speci_ids.append(event_id)
            continue
        if report_type != "METAR":
            blockers.append("canonical_report_type_invalid")
            continue
        if temperature is None:
            blockers.append("scheduled_metar_reported_temperature_missing")
            continue
        if abs(temperature - round(temperature)) > EPSILON:
            blockers.append("scheduled_metar_temperature_not_whole_degree_report")
            continue
        eligible.append(
            {
                "canonical_observation_event_id": event_id,
                "temperature_c": int(round(temperature)),
                "valid_time": _text(event.get("valid_time")),
            }
        )
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "target_type": TARGET_TYPE,
            "read_only": True,
        }
    if not eligible:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "no_scheduled_metar_observed",
            "reported_x_lower_bound_c": None,
            "eligible_metar_event_ids": [],
            "ignored_speci_event_ids": ignored_speci_ids,
            "target_type": TARGET_TYPE,
            "read_only": True,
        }
    maximum = max(row["temperature_c"] for row in eligible)
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "reported_x_lower_bound_c": maximum,
        "eligible_metar_event_ids": [row["canonical_observation_event_id"] for row in eligible],
        "max_forming_event_ids": [
            row["canonical_observation_event_id"]
            for row in eligible
            if row["temperature_c"] == maximum
        ],
        "ignored_speci_event_ids": ignored_speci_ids,
        "target_type": TARGET_TYPE,
        "read_only": True,
    }


def advance_reported_x_lower_bound(
    previous_lower_bound_c: Any,
    canonical_event: Mapping[str, Any],
) -> dict[str, Any]:
    """Advance the exact X bound for one event without letting SPECI mutate it."""

    previous = _finite(previous_lower_bound_c)
    if previous is not None and abs(previous - round(previous)) > EPSILON:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["previous_reported_x_lower_bound_not_whole_degree"],
            "read_only": True,
        }
    event = _mapping(canonical_event)
    report_type = _text(event.get("report_type")).upper()
    event_id = _text(event.get("canonical_observation_event_id"))
    if report_type == "SPECI":
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "unchanged_speci_soft_evidence_only",
            "reported_x_lower_bound_c": int(round(previous)) if previous is not None else None,
            "canonical_observation_event_id": event_id,
            "report_type": report_type,
            "target_type": TARGET_TYPE,
            "read_only": True,
        }
    if report_type != "METAR":
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["canonical_report_type_invalid"],
            "read_only": True,
        }
    temperature = _finite(_mapping(event.get("parsed_observation")).get("temperature_c"))
    if temperature is None or abs(temperature - round(temperature)) > EPSILON:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["scheduled_metar_reported_temperature_invalid"],
            "read_only": True,
        }
    reported = int(round(temperature))
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "advanced_or_retained",
        "reported_x_lower_bound_c": (
            max(int(round(previous)), reported) if previous is not None else reported
        ),
        "canonical_observation_event_id": event_id,
        "report_type": report_type,
        "target_type": TARGET_TYPE,
        "read_only": True,
    }


def project_latent_high_to_reported_x(
    latent_distribution: Mapping[Any, Any],
    *,
    reported_x_lower_bound_c: Any = None,
) -> dict[str, Any]:
    """Marginalize latent Z onto integer reported X, then apply the hard mask."""

    lower_bound = _finite(reported_x_lower_bound_c)
    if lower_bound is not None and abs(lower_bound - round(lower_bound)) > EPSILON:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["reported_x_lower_bound_not_whole_degree"],
            "read_only": True,
        }
    projected: dict[int, float] = {}
    blockers: list[str] = []
    for raw_temperature, raw_mass in latent_distribution.items():
        temperature = _finite(raw_temperature)
        mass = _finite(raw_mass)
        if temperature is None or mass is None or mass < 0.0:
            blockers.append("latent_distribution_row_invalid")
            continue
        reported = round_to_reported_c(temperature)
        projected[reported] = projected.get(reported, 0.0) + mass
    if blockers or not projected:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers or ["latent_distribution_empty"])),
            "read_only": True,
        }
    total = sum(projected.values())
    if total <= 0.0 or not math.isfinite(total):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["latent_distribution_not_normalizable"],
            "read_only": True,
        }
    projected = {key: value / total for key, value in projected.items()}
    masked = {
        key: value
        for key, value in projected.items()
        if lower_bound is None or key >= int(round(lower_bound))
    }
    retained = sum(masked.values())
    if retained <= 0.0:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["reported_x_hard_mask_zeroed_all_mass"],
            "read_only": True,
        }
    distribution = {
        str(key): value / retained for key, value in sorted(masked.items())
    }
    core = {
        "schema": SCHEMA_VERSION,
        "latent_type": LATENT_TYPE,
        "target_type": TARGET_TYPE,
        "reported_x_lower_bound_c": int(round(lower_bound)) if lower_bound is not None else None,
        "reported_x_distribution": distribution,
        "latent_mass_removed_by_reported_x_hard_mask": max(0.0, 1.0 - retained),
        "speci_changes_reported_x_hard_bound": False,
        "projection_rule": "continuous_z_round_half_away_from_zero_then_exact_regular_metar_max_mask",
    }
    return {
        **core,
        "projection_sha256": _sha(core),
        "ok": True,
        "state": "available",
        "read_only": True,
        "creates_probability": False,
        "places_orders": False,
    }


def project_joint_latent_high_to_reported_x(
    joint_rows: Iterable[Mapping[str, Any]],
    *,
    reported_x_lower_bound_c: Any = None,
) -> dict[str, Any]:
    """Marginalize a latent temperature×state posterior onto reported X."""

    latent: dict[str, float] = {}
    for raw in joint_rows:
        row = _mapping(raw)
        temperature = _finite(row.get("temperature_c"))
        probability = _finite(row.get("probability"))
        if temperature is None or probability is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["joint_latent_distribution_row_invalid"],
                "read_only": True,
            }
        key = f"{temperature:g}"
        latent[key] = latent.get(key, 0.0) + probability
    return project_latent_high_to_reported_x(
        latent,
        reported_x_lower_bound_c=reported_x_lower_bound_c,
    )


def build_reported_x_termination_marginal(
    reported_x_distribution: Mapping[Any, Any],
    *,
    reported_x_lower_bound_c: Any,
) -> dict[str, Any]:
    """Answer whether a future scheduled METAR can raise the final reported X."""

    lower_bound = _finite(reported_x_lower_bound_c)
    if lower_bound is None or abs(lower_bound - round(lower_bound)) > EPSILON:
        return {
            "schema": "reported_x_termination_marginal_v1",
            "ok": True,
            "state": "na",
            "na_reason": "no_regular_metar_reported_x_lower_bound",
            "updates_posterior": False,
            "read_only": True,
        }
    bound = int(round(lower_bound))
    no_new = 0.0
    new = 0.0
    for raw_x, raw_probability in reported_x_distribution.items():
        x_value = _finite(raw_x)
        probability = _finite(raw_probability)
        if x_value is None or probability is None or probability < 0.0:
            return {
                "schema": "reported_x_termination_marginal_v1",
                "ok": False,
                "state": "blocked",
                "block_reasons": ["reported_x_termination_distribution_invalid"],
                "read_only": True,
            }
        if x_value <= bound + EPSILON:
            no_new += probability
        else:
            new += probability
    total = no_new + new
    if total <= 0.0 or not math.isfinite(total):
        return {
            "schema": "reported_x_termination_marginal_v1",
            "ok": False,
            "state": "blocked",
            "block_reasons": ["reported_x_termination_distribution_empty"],
            "read_only": True,
        }
    return {
        "schema": "reported_x_termination_marginal_v1",
        "ok": True,
        "state": "available",
        "reported_x_lower_bound_c": bound,
        "probability_no_future_reported_new_high": no_new / total,
        "probability_future_reported_new_high": new / total,
        "same_posterior_marginal": True,
        "creates_probability": False,
        "updates_posterior": False,
        "speci_changes_reported_x_hard_bound": False,
        "read_only": True,
    }


def build_report_defined_checkpoint_projection(
    checkpoint: Mapping[str, Any],
    *,
    canonical_events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Split an internal latent checkpoint from its public reported-X surface.

    This is a pure successor-epoch adapter.  It intentionally does not retain
    the ambiguous ``physical_high_distribution`` name: the continuous
    scheduled-slot state is ``Z`` and only the whole-degree projection is
    public ``X`` or eligible for the resolution bridge.
    """

    value = _mapping(checkpoint)
    joint = value.get("joint_posterior")
    if not isinstance(joint, list) or not joint:
        return {
            "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["checkpoint_joint_posterior_missing"],
            "read_only": True,
        }
    events = [dict(_mapping(raw)) for raw in canonical_events]
    bound = scheduled_metar_reported_max(events)
    if bound.get("ok") is not True:
        return {
            "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(bound.get("block_reasons") or ["reported_x_bound_invalid"]),
            "read_only": True,
        }
    reported_bound = bound.get("reported_x_lower_bound_c")
    expected_latent_bound = (
        float(reported_bound) - 0.5 if reported_bound is not None else None
    )
    checkpoint_latent_bound = _finite(value.get("observed_max_lower_bound_c"))
    if (
        (expected_latent_bound is None and checkpoint_latent_bound is not None)
        or (
            expected_latent_bound is not None
            and (
                checkpoint_latent_bound is None
                or abs(checkpoint_latent_bound - expected_latent_bound) > EPSILON
            )
        )
    ):
        return {
            "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["latent_hard_bound_not_derived_from_regular_metar_only"],
            "expected_latent_observed_lower_bound_c": expected_latent_bound,
            "checkpoint_latent_observed_lower_bound_c": checkpoint_latent_bound,
            "read_only": True,
        }
    latent_distribution: dict[str, float] = {}
    for raw in joint:
        row = _mapping(raw)
        temperature = _finite(row.get("temperature_c"))
        probability = _finite(row.get("probability"))
        if temperature is None or probability is None or probability < 0.0:
            return {
                "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "block_reasons": ["checkpoint_joint_posterior_row_invalid"],
                "read_only": True,
            }
        key = f"{temperature:g}"
        latent_distribution[key] = latent_distribution.get(key, 0.0) + probability
    latent_total = sum(latent_distribution.values())
    if abs(latent_total - 1.0) > 1e-8:
        return {
            "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["checkpoint_joint_posterior_not_normalized"],
            "read_only": True,
        }
    projection = project_latent_high_to_reported_x(
        latent_distribution,
        reported_x_lower_bound_c=reported_bound,
    )
    if projection.get("ok") is not True:
        return {
            "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(projection.get("block_reasons") or ["reported_x_projection_blocked"]),
            "read_only": True,
        }
    reported_distribution = dict(projection["reported_x_distribution"])
    termination = build_reported_x_termination_marginal(
        reported_distribution,
        reported_x_lower_bound_c=reported_bound,
    )
    core = {
        "schema": CHECKPOINT_PROJECTION_SCHEMA_VERSION,
        "posterior_checkpoint_id": _text(value.get("posterior_checkpoint_id")),
        "joint_posterior_sha256": _text(value.get("joint_posterior_sha256")),
        "latent_type": LATENT_TYPE,
        "target_type": TARGET_TYPE,
        "latent_scheduled_slot_high_distribution": dict(
            sorted(latent_distribution.items(), key=lambda item: float(item[0]))
        ),
        "reported_physical_x_distribution": reported_distribution,
        "latent_observed_lower_bound_c": checkpoint_latent_bound,
        "reported_x_lower_bound_c": reported_bound,
        "reported_x_termination": termination,
        "resolution_bridge_input_field": "reported_physical_x_distribution",
        "public_probability_field": "reported_physical_x_distribution",
        "ambiguous_physical_high_distribution_retired": True,
        "speci_is_soft_evidence_only": True,
    }
    return {
        **core,
        "projection_sha256": _sha(core),
        "ok": True,
        "state": "successor_checkpoint_projection_ready",
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = [
    "CHECKPOINT_PROJECTION_SCHEMA_VERSION",
    "LATENT_TYPE",
    "SCHEMA_VERSION",
    "TARGET_TYPE",
    "advance_reported_x_lower_bound",
    "build_report_defined_checkpoint_projection",
    "build_reported_x_termination_marginal",
    "project_latent_high_to_reported_x",
    "project_joint_latent_high_to_reported_x",
    "round_to_reported_c",
    "scheduled_metar_reported_max",
]
