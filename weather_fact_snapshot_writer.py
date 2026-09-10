from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from lineage_identity_runtime import build_correlation_id, build_fact_snapshot_id, build_posterior_run_id
from phase_weighting_runtime import DEFAULT_STATION_TIMEZONE, build_phase_weighting_snapshot
from source_observation_runtime import PRIMARY_METAR_SOURCE_ID, SOURCE_CLASSES
from temperature_posterior_runtime import temperature_bucket_bounds


SCHEMA_VERSION = "weather_fact_snapshot_writer_plan_v1"
APPLY_RESULT_SCHEMA_VERSION = "weather_fact_snapshot_writer_apply_result_v1"
APPLY_RESULT_SUMMARY_SCHEMA_VERSION = "weather_fact_snapshot_writer_apply_result_summary_v1"
PARSER_VERSION = "weather_fact_snapshot_writer_v1"
WRITE_OWNER = "weather_fact_snapshot_writer"
POSTERIOR_FACT_PAYLOAD_SCHEMA = "hierarchical_posterior_fact_payload_v1"
POSTERIOR_SSOT_FACT_SCHEMA = "bayes_posterior_ssot_fact_v1"
POSTERIOR_SSOT_PRODUCTION_IDENTITY_KEYS = (
    "production_model_family",
    "production_model_version",
    "production_model_ready",
    "production_probability_policy",
    "legacy_model_fallback_allowed",
)
POSTERIOR_SSOT_FACT_PLACEHOLDER_SCHEMA = "bayes_posterior_ssot_fact_placeholder_v1"
POSTERIOR_PIPELINE_SOURCE_TRACE_SCHEMA = "posterior_pipeline_source_lineage_trace_v1"
FORECAST_WEAK_ATTRIBUTION_TRACE_SCHEMA = "forecast_weak_attribution_trace_v1"
PRIOR_LAYER_CONTRACT_SCHEMA = "temperature_prior_layer_contract_v1"
SUMMARY_CACHE_COMPAT_VERSION = "event_fact_summary_cache_v8_posterior_ssot_no_target_fallback"
LATEST_REPOINT_PLAN_SCHEMA = "weather_fact_latest_repoint_plan_v1"
SUMMARY_CACHE_REPAIR_PLAN_SCHEMA = "weather_fact_summary_cache_repair_plan_v1"
EVENT_FACT_HISTORY_MERGE_FILL_REPAIR_CONTRACT_SCHEMA = "daily_review_lineage_repair_apply_contract_v1"
EVENT_FACT_HISTORY_MERGE_FILL_REPAIR_RESULT_SCHEMA = "weather_event_fact_history_merge_fill_repair_result_v1"
HISTORY_FACT_COMPACTION_VERSION = "event_fact_history_compact_v1"
UI_SNAPSHOT_DIRTY_STAMP_PATH = Path(__file__).resolve().parent / "data" / "ui_snapshot_dirty.stamp"
EXTRA_PAYLOAD_PRESERVE_MAX_VALUE_BYTES = 4096
EXTRA_PAYLOAD_PRESERVE_MAX_TOTAL_BYTES = 65536

_SLUG_DATE_RE = re.compile(r"-on-([a-z]+)-(\d{1,2})-(\d{4})")
_SLUG_MONTH_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

TRACE_KEYS: tuple[str, ...] = (
    "source_observations",
    "source_acquisition_trace",
    "source_latency_trace",
)

PHASE0_FACT_JSON_CONTRACT_SCHEMA = "phase0_fact_json_contract_v1"
FACT_WRITER_PERSISTED_COVERAGE_CONTRACT_SCHEMA = "fact_writer_persisted_coverage_contract_v1"
PHASE0_FACT_JSON_CONTRACT_KEYS: tuple[str, ...] = (
    "source_observations",
    "source_acquisition_trace",
    "source_latency_trace",
    "source_observation_ids",
    "fact_layer_state",
    "db_write_guard",
    "lineage_state",
    "phase",
    "beta_weak",
    "beta_strong",
    "phase_reason",
    "hard_mask_reason",
    "posterior_ssot_fact",
    "hard_source_acceptance_state",
    "signal_to_order_timing",
    "daily_review_state",
)

HARD_FACT_MERGE_FILL_KEYS: tuple[str, ...] = (
    "settlement_actual",
    "settlement_actual_at",
    "final_bucket_lock",
    "hard_source_direct",
    "hard_source_direct_ssot_write",
    "hard_source_observed_value_c",
    "hard_source_observed_source",
    "hard_source_acceptance_state",
    "hard_source_posterior_override",
    "hard_source_posterior_override_applied",
    "observed_today_max_c",
    "observed_today_max_source",
    "observed_today_max_at",
    "zuuu_metar_raw_awc_today_max_c",
    "zuuu_metar_raw_awc_today_max_at",
    "zuuu_metar_raw_awc_latest_at",
    "weather_likelihood_features",
    "weather_likelihood_features_compact",
    "prior_distribution",
    "prior_meta",
    "prior_na_reason",
)

PHASE_PLACEHOLDER_VALUES = {"", "unknown", "phase_unknown"}
PHASE_REASON_PLACEHOLDER_VALUES = {"", "phase_not_materialized_for_this_writer_path", "phase_reason_missing"}


def _json_size_bytes(value: Any) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        )
    except Exception:
        return len(str(value or "").encode("utf-8", errors="replace"))


def _preserved_conflict_value(key: str, value: Any) -> Any:
    size_bytes = _json_size_bytes(value)
    if size_bytes <= EXTRA_PAYLOAD_PRESERVE_MAX_VALUE_BYTES:
        return copy.deepcopy(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except Exception:
        encoded = str(value or "").encode("utf-8", errors="replace")
    return {
        "schema": "extra_payload_conflict_digest_v1",
        "path": str(key or ""),
        "omitted": True,
        "reason": "conflicting_value_exceeds_preserve_limit",
        "original_size_bytes": int(size_bytes),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _normalize_date_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split("T", 1)[0] if "T" in text else text


def _event_date_from_slug(slug: Any) -> str:
    match = _SLUG_DATE_RE.search(str(slug or "").lower())
    if not match:
        return ""
    month = _SLUG_MONTH_MAP.get(str(match.group(1) or "").lower())
    if month is None:
        return ""
    try:
        return datetime(int(match.group(3)), int(month), int(match.group(2))).date().isoformat()
    except Exception:
        return ""


def _canonical_fact_run_date(*, slug: Any, event_date: Any, run_date: Any) -> str:
    event_key = _normalize_date_key(event_date)
    run_key = _normalize_date_key(run_date)
    slug_key = _event_date_from_slug(slug)
    if event_key and slug_key and slug_key == event_key:
        return event_key
    return run_key or event_key


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _touch_ui_snapshot_dirty_stamp(reason: str = "") -> None:
    try:
        UI_SNAPSHOT_DIRTY_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_SNAPSHOT_DIRTY_STAMP_PATH.write_text(
            json.dumps(
                {
                    "updated_at": utc_now_iso(),
                    "reason": str(reason or "")[:300],
                    "source": WRITE_OWNER,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _non_empty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _source_event_ids(trace: Mapping[str, Any] | None) -> list[str]:
    source_ids: list[str] = []
    trace_map = _as_mapping(trace)
    observations = trace_map.get("source_observations")
    if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes, bytearray)):
        for obs in observations:
            obs_map = _as_mapping(obs)
            source_event_id = str(obs_map.get("source_event_id") or "").strip()
            if source_event_id and source_event_id not in source_ids:
                source_ids.append(source_event_id)
    for key in ("source_event_ids", "hard_observation_candidate_ids", "diagnostic_current_ids", "forecast_weak_ids"):
        values = trace_map.get(key)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            for value in values:
                text = str(value or "").strip()
                if text and text not in source_ids:
                    source_ids.append(text)
    return source_ids


def _source_observation_ids(trace: Mapping[str, Any] | None) -> list[str]:
    source_ids: list[str] = []
    trace_map = _as_mapping(trace)
    observations = trace_map.get("source_observations")
    if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes, bytearray)):
        for obs in observations:
            obs_map = _as_mapping(obs)
            source_observation_id = str(obs_map.get("source_observation_id") or "").strip()
            if source_observation_id and source_observation_id not in source_ids:
                source_ids.append(source_observation_id)
    values = trace_map.get("source_observation_ids")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for value in values:
            text = str(value or "").strip()
            if text and text not in source_ids:
                source_ids.append(text)
    return source_ids


def _unique_texts(values: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return out
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _payload_source_event_ids(payload: Mapping[str, Any]) -> list[str]:
    out: list[str] = []

    def add_many(values: Any) -> None:
        for text in _unique_texts(values):
            if text not in out:
                out.append(text)

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    add_many(payload.get("source_event_ids"))
    for row in payload.get("source_observations") or []:
        add(_as_mapping(row).get("source_event_id"))
    for row in payload.get("source_latency_trace") or []:
        add(_as_mapping(row).get("source_event_id"))
    add_many(_source_event_ids(_as_mapping(payload.get("source_acquisition_trace"))))
    for key in ("lineage_state", "posterior_ssot_fact", "signal_to_order_timing", "hard_source_acceptance_state"):
        nested = _as_mapping(payload.get(key))
        add(nested.get("source_event_id"))
        add_many(nested.get("source_event_ids"))
        add_many(_as_mapping(nested.get("lineage")).get("source_event_ids"))
    return out


def _payload_source_observation_ids(payload: Mapping[str, Any]) -> list[str]:
    out: list[str] = []

    def add_many(values: Any) -> None:
        for text in _unique_texts(values):
            if text not in out:
                out.append(text)

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    add_many(payload.get("source_observation_ids"))
    for row in payload.get("source_observations") or []:
        add(_as_mapping(row).get("source_observation_id"))
    for row in payload.get("source_latency_trace") or []:
        add(_as_mapping(row).get("source_observation_id"))
    add_many(_source_observation_ids(_as_mapping(payload.get("source_acquisition_trace"))))
    for key in ("lineage_state", "posterior_ssot_fact", "signal_to_order_timing", "hard_source_acceptance_state"):
        nested = _as_mapping(payload.get(key))
        add(nested.get("source_observation_id"))
        add_many(nested.get("source_observation_ids"))
        add_many(_as_mapping(nested.get("lineage")).get("source_observation_ids"))
    return out


def _runtime_output(output: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    runtimes = _as_mapping(output.get("runtime_outputs"))
    return _as_mapping(runtimes.get(key))


def _forecast_weak_attribution_trace(
    weak_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the consumed weak-forecast path into the immutable fact payload.

    Capped precision is deliberately reported as an *within-weak-layer* share,
    rather than a posterior weight: the latter is only defined after the
    sequential state mixture, observation likelihood, phase beta, and hard
    mask have all been applied.
    """

    runtime = _as_mapping(weak_runtime)
    pooling = _as_mapping(runtime.get("forecast_group_pooling"))
    likelihood = _as_mapping(runtime.get("forecast_weak_likelihood_map"))
    joint_experts = runtime.get("source_expert_trace")
    joint_expert_rows = (
        joint_experts
        if isinstance(joint_experts, Sequence) and not isinstance(joint_experts, (str, bytes, bytearray))
        else []
    )
    raw_groups = runtime.get("source_group_trace")
    group_rows = raw_groups if isinstance(raw_groups, Sequence) and not isinstance(raw_groups, (str, bytes, bytearray)) else []
    capped_precision_total = 0.0
    for raw_group in group_rows:
        capped = _float_or_none(_as_mapping(raw_group).get("capped_precision"))
        if capped is not None and capped > 0.0:
            capped_precision_total += capped

    source_group_contributions: list[dict[str, Any]] = []
    for raw_group in group_rows:
        group = _as_mapping(raw_group)
        capped_precision = _float_or_none(group.get("capped_precision"))
        source_group_contributions.append(
            {
                "group": str(group.get("group") or ""),
                "source_count": int(group.get("source_count") or 0),
                "included_source_event_ids": _unique_texts(group.get("included_source_event_ids")),
                "raw_precision": group.get("raw_precision"),
                "capped_precision": group.get("capped_precision"),
                "capped_precision_share_within_weak_likelihood": (
                    round(float(capped_precision) / capped_precision_total, 12)
                    if capped_precision is not None and capped_precision_total > 0.0
                    else None
                ),
                "precision_cap": group.get("precision_cap"),
                "cap_hit": bool(group.get("cap_hit")),
                "shrinkage_alpha": group.get("shrinkage_alpha"),
                "forecast_peak_mean_c": group.get("forecast_peak_mean_c"),
                "forecast_sigma_effective_c": group.get("forecast_sigma_effective_c"),
                "distribution": copy.deepcopy(dict(_as_mapping(group.get("distribution")))),
                "shrunk_distribution": copy.deepcopy(dict(_as_mapping(group.get("shrunk_distribution")))),
            }
        )

    if not source_group_contributions and joint_expert_rows:
        information_total = sum(
            max(0.0, float(_float_or_none(_as_mapping(row).get("information_weight")) or 0.0))
            for row in joint_expert_rows
        )
        for raw_expert in joint_expert_rows:
            expert = _as_mapping(raw_expert)
            information_weight = max(0.0, float(_float_or_none(expert.get("information_weight")) or 0.0))
            source_group_contributions.append(
                {
                    "group": str(expert.get("provider_family") or "forecast_expert"),
                    "source_id": str(expert.get("source_id") or ""),
                    "source_count": 1,
                    "included_source_event_ids": [str(expert.get("source_revision_id") or "")],
                    "information_weight": information_weight,
                    "information_weight_share_within_weak_likelihood": (
                        round(information_weight / information_total, 12) if information_total > 0.0 else None
                    ),
                    "forecast_peak_c": expert.get("forecast_peak_c"),
                    "calibration_bias_c": expert.get("calibration_bias_c"),
                    "calibrated_mean_c": expert.get("calibrated_mean_c"),
                    "calibrated_sigma_c": expert.get("calibrated_sigma_c"),
                    "bias_state_id": expert.get("bias_state_id"),
                    "bias_level": expert.get("bias_level"),
                }
            )

    source_rows = runtime.get("source_trace")
    raw_source_rows = source_rows if isinstance(source_rows, Sequence) and not isinstance(source_rows, (str, bytes, bytearray)) else []
    source_contribution_trace: list[dict[str, Any]] = []
    for raw_source in raw_source_rows:
        source = _as_mapping(raw_source)
        source_contribution_trace.append(
            {
                "source_event_id": str(source.get("source_event_id") or ""),
                "source_observation_id": str(source.get("source_observation_id") or ""),
                "source_id": str(source.get("source_id") or ""),
                "source_class": str(source.get("source_class") or ""),
                "role": str(source.get("role") or ""),
                "group": str(source.get("group") or ""),
                "event_date": str(source.get("event_date") or ""),
                "valid_time": str(source.get("valid_time") or ""),
                "fetched_at": str(source.get("fetched_at") or ""),
                "included": source.get("included") is True,
                "exclusion_reason": str(source.get("exclusion_reason") or ""),
                "forecast_peak_c": source.get("forecast_peak_c"),
                "distribution_source": str(source.get("distribution_source") or ""),
                "sigma_c": source.get("sigma_c"),
                "confidence": source.get("confidence"),
                "precision_raw": source.get("precision_raw"),
                "distribution": copy.deepcopy(dict(_as_mapping(source.get("distribution")))),
                "hard_observation_fields_ignored": source.get("hard_observation_fields_ignored") is True,
            }
        )

    return {
        "schema": FORECAST_WEAK_ATTRIBUTION_TRACE_SCHEMA,
        "state": "available" if likelihood else "not_available",
        "read_only": True,
        "weak_likelihood_consumed": bool(likelihood),
        "weak_likelihood_distribution": copy.deepcopy(dict(likelihood)),
        "source_contribution_semantics": (
            "joint_expert_information_share_or_legacy_capped_precision_share_is_within_weak_likelihood_only; "
            "posterior_consumes_the_one_joint_weak_distribution_once"
        ),
        "source_group_contributions": source_group_contributions,
        "source_contribution_trace": source_contribution_trace,
        "forecast_group_pooling": copy.deepcopy(dict(pooling)),
        "pooling_decision": {
            "state": str(pooling.get("state") or ("joint_weak_available" if joint_expert_rows else "not_available")),
            "pooling_mode": str(
                pooling.get("pooling_mode")
                or ("correlation_aware_tempered_mixture_of_calibrated_experts" if joint_expert_rows else "")
            ),
            "included_groups": _unique_texts(pooling.get("included_groups")),
            "source_group_count": pooling.get("source_group_count"),
            "legacy_model_taf_special_case_retired": pooling.get("legacy_model_taf_special_case_retired") is True,
        },
        "na_reasons": copy.deepcopy(runtime.get("na_reasons") or []),
        "boundaries": {
            "trace_describes_actual_runtime_inputs_not_shadow_candidates": True,
            "weak_sources_never_hard_lock": True,
            "hard_observation_fields_remain_excluded_from_weak_layer": True,
            "does_not_recompute_or_change_posterior": True,
            "legacy_model_taf_special_case_retired": True,
        },
    }


def _metar_recoil_ssot_payload(strong_runtime: Mapping[str, Any]) -> dict[str, Any]:
    recoil = _as_mapping(strong_runtime.get("recoil_state_ssot"))
    evidence = _as_mapping(strong_runtime.get("physical_cutoff_evidence"))
    nested_evidence = _as_mapping(recoil.get("physical_cutoff_evidence"))
    if not evidence and nested_evidence:
        evidence = nested_evidence
    out: dict[str, Any] = {}
    if evidence:
        out["physical_cutoff_evidence"] = copy.deepcopy(dict(evidence))
    if recoil:
        recoil_out = copy.deepcopy(dict(recoil))
        if evidence and not isinstance(recoil_out.get("physical_cutoff_evidence"), Mapping):
            recoil_out["physical_cutoff_evidence"] = copy.deepcopy(dict(evidence))
        out["recoil_state_ssot"] = recoil_out
    return out


def _daily_high_observation_candidate_shadow_payload(strong_runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Persist a compact, non-production candidate trace without raw METAR text."""

    candidate = _as_mapping(strong_runtime.get("daily_high_observation_candidate"))
    if not candidate:
        return {
            "schema": "zuuu_daily_high_observation_candidate_shadow_fact_v1",
            "state": "missing",
            "candidate_only": True,
            "production_applied": False,
            "na_reason": "candidate_runtime_output_missing",
            "guards": {
                "writes_posterior": False,
                "places_orders": False,
                "changes_parameters": False,
            },
        }
    if candidate.get("candidate_only") is not True or candidate.get("production_applied") is not False:
        return {
            "schema": "zuuu_daily_high_observation_candidate_shadow_fact_v1",
            "state": "blocked",
            "candidate_only": True,
            "production_applied": False,
            "na_reason": "candidate_runtime_boundary_invalid",
            "candidate_schema": str(candidate.get("schema") or ""),
            "candidate_policy_version": str(candidate.get("policy_version") or ""),
            "guards": {
                "writes_posterior": False,
                "places_orders": False,
                "changes_parameters": False,
            },
        }
    snapshot = _as_mapping(candidate.get("canonical_observation_snapshot"))
    compact_points: list[dict[str, Any]] = []
    for point in snapshot.get("selected_points") or []:
        row = _as_mapping(point)
        compact_points.append(
            {
                key: copy.deepcopy(row.get(key))
                for key in (
                    "source_id",
                    "source_observation_id",
                    "raw_sha256",
                    "raw_object_ref",
                    "verified_metar_valid_time",
                    "first_seen_at",
                    "arrival_time_quality",
                    "temperature_c",
                    "temperature_interval_c",
                    "quality_flags",
                )
                if row.get(key) not in (None, "", [], {})
            }
        )
    return {
        "schema": "zuuu_daily_high_observation_candidate_shadow_fact_v1",
        "state": "available",
        "candidate_schema": str(candidate.get("schema") or ""),
        "policy_version": str(candidate.get("policy_version") or ""),
        "candidate_only": True,
        "production_applied": False,
        "event_date": str(candidate.get("event_date") or ""),
        "as_of": str(candidate.get("as_of") or ""),
        "canonical_observation_snapshot": {
            "schema": str(snapshot.get("schema") or ""),
            "event_date": str(snapshot.get("event_date") or ""),
            "as_of": str(snapshot.get("as_of") or ""),
            "selected_point_count": snapshot.get("distinct_valid_time_count"),
            "selected_source_observation_ids": copy.deepcopy(snapshot.get("selected_source_observation_ids") or []),
            "latest_valid_time": str(snapshot.get("latest_valid_time") or ""),
            "quality_flags": copy.deepcopy(snapshot.get("quality_flags") or []),
            "na_reasons": copy.deepcopy(snapshot.get("na_reasons") or []),
            "selected_points": compact_points,
            "superseded_observations": copy.deepcopy(snapshot.get("superseded_observations") or []),
            "excluded_observations": copy.deepcopy(snapshot.get("excluded_observations") or []),
        },
        "temperature_trajectory_posterior": copy.deepcopy(_as_mapping(candidate.get("temperature_trajectory_posterior"))),
        "physical_environment_state_posterior": copy.deepcopy(_as_mapping(candidate.get("physical_environment_state_posterior"))),
        "remaining_daily_high_distribution": copy.deepcopy(_as_mapping(candidate.get("remaining_daily_high_distribution"))),
        "metar_strong_likelihood_candidate": copy.deepcopy(_as_mapping(candidate.get("metar_strong_likelihood_candidate"))),
        "source_observation_ids": copy.deepcopy(candidate.get("source_observation_ids") or []),
        "na_reasons": copy.deepcopy(candidate.get("na_reasons") or []),
        "guards": {
            "writes_posterior": False,
            "places_orders": False,
            "changes_parameters": False,
            "historical_backfill": False,
            "raw_metar_text_persisted_in_fact": False,
        },
    }


def _daily_high_candidate_live_rollout_payload(
    pipeline_output: Mapping[str, Any],
    strong_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist the formal-consumption decision without copying raw METAR text."""

    raw = _as_mapping(pipeline_output.get("daily_high_candidate_live_rollout"))
    if not raw:
        raw = _as_mapping(strong_runtime.get("daily_high_candidate_live_rollout"))
    if not raw:
        return {
            "schema": "zuuu_daily_high_candidate_live_rollout_fact_v1",
            "state": "missing",
            "applied_to_metar_strong_likelihood": False,
            "na_reason": "candidate_live_rollout_trace_missing",
        }
    rollback = _as_mapping(raw.get("rollback"))
    guards = _as_mapping(raw.get("guards"))
    return {
        "schema": "zuuu_daily_high_candidate_live_rollout_fact_v1",
        "mode": str(raw.get("mode") or ""),
        "mode_reason": str(raw.get("mode_reason") or ""),
        "state": str(raw.get("state") or ""),
        "reason": str(raw.get("reason") or ""),
        "applied_to_metar_strong_likelihood": raw.get("applied_to_metar_strong_likelihood") is True,
        "candidate_schema": str(raw.get("candidate_schema") or ""),
        "candidate_policy_version": str(raw.get("candidate_policy_version") or ""),
        "candidate_only_producer": raw.get("candidate_only_producer") is True,
        "candidate_production_applied_flag": raw.get("candidate_production_applied_flag") is True,
        "candidate_source_observation_ids": copy.deepcopy(raw.get("candidate_source_observation_ids") or []),
        "candidate_contract_ok": raw.get("candidate_contract_ok") is True,
        "candidate_contract_issues": copy.deepcopy(raw.get("candidate_contract_issues") or []),
        "rollback": {
            "mode_env": str(rollback.get("mode_env") or ""),
            "set_to": str(rollback.get("set_to") or ""),
            "fallback_likelihood": str(rollback.get("fallback_likelihood") or ""),
        },
        "guards": {
            "candidate_cannot_place_orders": guards.get("candidate_cannot_place_orders") is True,
            "candidate_parameter_learning_requires_evidence_maturity": guards.get("candidate_parameter_learning_requires_evidence_maturity") is True,
            "hard_mask_applied_by_posterior": guards.get("hard_mask_applied_by_posterior") is True,
            "phase_guard_applied_before_candidate": guards.get("phase_guard_applied_before_candidate") is True,
            "raw_metar_text_persisted_in_fact": False,
        },
    }


def _pipeline_source_event_ids(pipeline_output: Mapping[str, Any]) -> list[str]:
    lineage = _as_mapping(pipeline_output.get("source_lineage"))
    values = lineage.get("source_event_ids")
    out: list[str] = []
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for value in values:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    for runtime_key in ("forecast_weak_likelihood", "metar_strong_likelihood"):
        trace = _runtime_output(pipeline_output, runtime_key).get("source_trace")
        if not isinstance(trace, Sequence) or isinstance(trace, (str, bytes, bytearray)):
            continue
        for row in trace:
            row_map = _as_mapping(row)
            if not row_map.get("included"):
                continue
            source_event_id = str(row_map.get("source_event_id") or "").strip()
            if source_event_id and source_event_id not in out:
                out.append(source_event_id)
    return out


def _pipeline_source_observation_ids(pipeline_output: Mapping[str, Any]) -> list[str]:
    lineage = _as_mapping(pipeline_output.get("source_lineage"))
    values = lineage.get("source_observation_ids")
    out: list[str] = []
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for value in values:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    for runtime_key in ("forecast_weak_likelihood", "metar_strong_likelihood"):
        trace = _runtime_output(pipeline_output, runtime_key).get("source_trace")
        if not isinstance(trace, Sequence) or isinstance(trace, (str, bytes, bytearray)):
            continue
        for row in trace:
            row_map = _as_mapping(row)
            if not row_map.get("included"):
                continue
            source_observation_id = str(row_map.get("source_observation_id") or "").strip()
            if source_observation_id and source_observation_id not in out:
                out.append(source_observation_id)
    return out


def _pipeline_source_trace(
    pipeline_output: Mapping[str, Any],
    source_acquisition_trace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(source_acquisition_trace, Mapping) and source_acquisition_trace:
        return dict(source_acquisition_trace)

    observations: list[dict[str, Any]] = []
    hard_ids: list[str] = []
    forecast_ids: list[str] = []
    seen: set[str] = set()
    for runtime_key in ("forecast_weak_likelihood", "metar_strong_likelihood"):
        trace = _runtime_output(pipeline_output, runtime_key).get("source_trace")
        if not isinstance(trace, Sequence) or isinstance(trace, (str, bytes, bytearray)):
            continue
        for row in trace:
            row_map = _as_mapping(row)
            if not row_map.get("included"):
                continue
            source_event_id = str(row_map.get("source_event_id") or "").strip()
            source_observation_id = str(row_map.get("source_observation_id") or "").strip()
            if not source_event_id or source_event_id in seen:
                continue
            seen.add(source_event_id)
            source_class = str(row_map.get("source_class") or SOURCE_CLASSES["unknown"])
            hard_eligible = source_class == SOURCE_CLASSES["hard_observation"]
            if hard_eligible:
                hard_ids.append(source_event_id)
            if source_class == SOURCE_CLASSES["forecast_weak"]:
                forecast_ids.append(source_event_id)
            observations.append(
                {
                    "source_event_id": source_event_id,
                    "source_observation_id": source_observation_id,
                    "source_id": str(row_map.get("source_id") or ""),
                    "source_class": source_class,
                    "role": str(row_map.get("role") or ""),
                    "hard_eligible": hard_eligible,
                    "lineage_only_from_posterior_pipeline": True,
                }
            )

    for source_event_id in _pipeline_source_event_ids(pipeline_output):
        if source_event_id in seen:
            continue
        seen.add(source_event_id)
        observations.append(
            {
                "source_event_id": source_event_id,
                "source_class": SOURCE_CLASSES["unknown"],
                "hard_eligible": False,
                "lineage_only_from_posterior_pipeline": True,
            }
        )

    return {
        "schema": POSTERIOR_PIPELINE_SOURCE_TRACE_SCHEMA,
        "read_only": True,
        "synthetic_from_posterior_pipeline": True,
        "source_observations": observations,
        "source_event_ids": [row["source_event_id"] for row in observations],
        "source_observation_ids": [
            str(row.get("source_observation_id") or "")
            for row in observations
            if str(row.get("source_observation_id") or "").strip()
        ],
        "source_latency_trace": [],
        "hard_observation_candidate_ids": hard_ids,
        "forecast_weak_ids": forecast_ids,
        "diagnostic_current_ids": [],
        "next_boundary": "weather_fact_snapshot_writer_no_apply_plan",
    }


def _trace_hard_candidate_ids(trace: Mapping[str, Any] | None) -> list[str]:
    trace_map = _as_mapping(trace)
    ids = trace_map.get("hard_observation_candidate_ids")
    if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes, bytearray)):
        return [str(item) for item in ids if str(item or "").strip()]
    out: list[str] = []
    observations = trace_map.get("source_observations")
    if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes, bytearray)):
        for obs in observations:
            obs_map = _as_mapping(obs)
            if bool(obs_map.get("hard_eligible")) and str(obs_map.get("source_class") or "") == "hard_observation_candidate":
                source_event_id = str(obs_map.get("source_event_id") or "").strip()
                if source_event_id:
                    out.append(source_event_id)
    return out


def _source_text_is_diagnostic_current(source: Any) -> bool:
    text = str(source or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "wu_twc",
            "wunderground",
            "weather.com",
            "weather_com",
            "twc_current",
            "wu_display",
            "wu_page_api_current",
            "diagnostic_current",
        )
    )


def _source_text_is_hard_observation(source: Any) -> bool:
    text = str(source or "").strip().lower()
    if not text or _source_text_is_diagnostic_current(text):
        return False
    return any(
        token in text
        for token in (
            "awc",
            "metar",
            "aviationweather",
            "noaa",
            "zuuu",
            "official",
            "settlement",
            "hard_source",
        )
    )


def _trace_has_hard_observation_candidate(trace: Mapping[str, Any] | None) -> bool:
    return bool(_trace_hard_candidate_ids(trace))


def _trace_has_diagnostic_current_source(trace: Mapping[str, Any] | None) -> bool:
    trace_map = _as_mapping(trace)
    observations = trace_map.get("source_observations")
    if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes, bytearray)):
        for obs in observations:
            obs_map = _as_mapping(obs)
            if str(obs_map.get("source_class") or "").strip() == SOURCE_CLASSES["diagnostic_current"]:
                return True
            if _source_text_is_diagnostic_current(obs_map.get("source_id")):
                return True
    values = trace_map.get("diagnostic_current_ids")
    return isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)) and bool(values)


def _payload_observed_today_max_hard_eligible(
    payload: Mapping[str, Any] | None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> bool:
    item = _as_mapping(payload)
    features = _as_mapping(item.get("weather_likelihood_features"))
    compact = _as_mapping(item.get("weather_likelihood_features_compact"))
    trace_provided = isinstance(source_acquisition_trace, Mapping) and bool(source_acquisition_trace)
    if _trace_has_hard_observation_candidate(source_acquisition_trace):
        return True
    if _non_empty(item.get("zuuu_metar_raw_awc_today_max_c")) or _non_empty(features.get("zuuu_metar_raw_awc_today_max_c")):
        return True
    if _non_empty(compact.get("zuuu_metar_raw_awc_today_max_c")):
        return True
    source_text = _payload_observed_today_max_source(item)
    if _source_text_is_hard_observation(source_text):
        return True
    if _source_text_is_diagnostic_current(source_text) or _trace_has_diagnostic_current_source(source_acquisition_trace):
        return False
    if _non_empty(item.get("hard_source_observed_value_c")) or _non_empty(features.get("hard_source_observed_value_c")):
        return True
    if trace_provided:
        return False
    return _non_empty(item.get("observed_today_max_c")) or _non_empty(features.get("observed_today_max_c"))


def _payload_current_temperature_hard_eligible(
    payload: Mapping[str, Any] | None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> bool:
    item = _as_mapping(payload)
    features = _as_mapping(item.get("weather_likelihood_features"))
    compact = _as_mapping(item.get("weather_likelihood_features_compact"))
    trace_provided = isinstance(source_acquisition_trace, Mapping) and bool(source_acquisition_trace)
    if _trace_has_hard_observation_candidate(source_acquisition_trace):
        return True
    if (
        _non_empty(item.get("zuuu_metar_raw_awc_latest_temp_c"))
        or _non_empty(features.get("zuuu_metar_raw_awc_latest_temp_c"))
        or _non_empty(compact.get("zuuu_metar_raw_awc_latest_temp_c"))
        or _non_empty(item.get("metar_temperature_c"))
        or _non_empty(features.get("metar_temperature_c"))
    ):
        return True
    source_text = _first_payload_value(item, "current_temperature_source", "observed_current_temp_source", "temperature_source")
    if _source_text_is_hard_observation(source_text):
        return True
    if _source_text_is_diagnostic_current(source_text) or _trace_has_diagnostic_current_source(source_acquisition_trace):
        return False
    if trace_provided:
        return False
    return _non_empty(item.get("current_temperature_c")) or _non_empty(item.get("current_temp_c"))


def _payload_hard_rank(
    payload: Mapping[str, Any] | None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> int:
    item = _as_mapping(payload)
    if _non_empty(item.get("settlement_actual")) or bool(item.get("final_bucket_lock")):
        return 4
    if bool(item.get("hard_source_direct_ssot_write") or item.get("hard_source_direct")):
        return 3
    if bool(item.get("hard_source_posterior_override") or item.get("hard_source_posterior_override_applied")):
        return 2
    if _payload_observed_today_max_hard_eligible(item, source_acquisition_trace):
        return 1
    if _payload_current_temperature_hard_eligible(item, source_acquisition_trace):
        return 1
    return 0


def _first_payload_value(item: Mapping[str, Any], *keys: str) -> Any:
    features = _as_mapping(item.get("weather_likelihood_features"))
    compact = _as_mapping(item.get("weather_likelihood_features_compact"))
    hard_constraints = _as_mapping(item.get("hard_constraints"))
    posterior_constraints = _as_mapping(hard_constraints.get("posterior"))
    metar_constraints = _as_mapping(hard_constraints.get("metar_strong"))
    for key in keys:
        for source in (item, features, compact, hard_constraints, posterior_constraints, metar_constraints):
            value = _as_mapping(source).get(key)
            if _non_empty(value):
                return value
    return None


def _strong_physical_trade_evidence(
    item: Mapping[str, Any],
    hard_constraints: Mapping[str, Any],
    bucket_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project composer-owned physical verdicts; this is not a trade-side model."""
    metar_hard = _as_mapping(hard_constraints.get("metar_strong"))
    evidence = copy.deepcopy(_as_mapping(item.get("strong_physical_evidence")))
    observed_floor_c = _float_or_none(metar_hard.get("observed_today_max_c"))
    observed_floor_source = str(metar_hard.get("observed_today_max_source") or "").strip()
    observed_floor_at = str(metar_hard.get("observed_today_max_at") or "").strip()
    cutoff = _as_mapping(item.get("physical_cutoff_evidence"))
    trajectory = _as_mapping(item.get("temperature_trajectory"))
    joint_cutoff = bool(
        observed_floor_c is not None
        and cutoff.get("true_cooling_or_cutoff_confirmed") is True
        and cutoff.get("magnitude_cooling_confirmed") is True
        and str(trajectory.get("state") or "").strip().lower() == "cooling"
        and cutoff.get("physical_cutoff_confirmed") is True
    )
    upper_exclusions: list[dict[str, Any]] = []
    if joint_cutoff:
        for row in bucket_rows:
            bucket = str(_as_mapping(row).get("bucket") or "").strip()
            bounds = temperature_bucket_bounds(bucket, rounding_rule="rounded_degrees")
            if not bucket or bounds is None or not math.isfinite(float(bounds[0])):
                continue
            if float(bounds[0]) <= float(observed_floor_c) + 1e-9:
                continue
            upper_exclusions.append(
                {
                    "bucket": bucket,
                    "excluded": True,
                    "kind": "remaining_warming_unattainable",
                    "temperature_momentum": True,
                    "temperature_trajectory": True,
                    "cloud_rain_physics": True,
                    "confidence": 1.0,
                    "source": "metar_strong_joint_confirmed_cooling_cutoff",
                }
            )
    ranked = sorted(
        (_as_mapping(row) for row in bucket_rows),
        key=lambda row: float(_prob01(row.get("probability")) or 0.0),
        reverse=True,
    )
    top_bucket = str((ranked[0] if ranked else {}).get("bucket") or "").strip()
    top_bounds = temperature_bucket_bounds(top_bucket, rounding_rule="rounded_degrees")
    top_contains_observed_floor = bool(
        observed_floor_c is not None
        and top_bounds is not None
        and float(top_bounds[0]) <= float(observed_floor_c) < float(top_bounds[1])
    )
    trajectory_state = str(trajectory.get("state") or "").strip().lower()
    warming_trajectory = trajectory_state in {
        "sustained_warming", "emerging_warming", "weak_warming", "pre_peak_integer_plateau"
    }
    environment_state_available = bool(_as_mapping(item.get("state_probs")))
    top_mode = (
        "confirmed_cutoff_observed_bucket"
        if joint_cutoff and top_contains_observed_floor
        else "joint_warming_environment"
        if warming_trajectory and environment_state_available and not cutoff.get("true_cooling_or_cutoff_confirmed")
        else "not_supported"
    )
    evidence.update(
        {
            "schema": "strong_physical_trade_evidence_v1",
            "safe_no_observed_max_c": observed_floor_c,
            "safe_no_observation_source": observed_floor_source,
            "safe_no_observed_at": observed_floor_at,
            "safe_no_amsc_observation": bool(
                observed_floor_c is not None and observed_floor_source == PRIMARY_METAR_SOURCE_ID
            ),
            "safe_no_monotonic_daily_high_floor": observed_floor_c is not None,
            "bucket_exclusions": upper_exclusions,
            "bucket_exclusions_na_reason": "" if upper_exclusions else "joint_cooling_cutoff_not_confirmed",
            "top_support_mode": top_mode,
        }
    )
    if top_mode == "confirmed_cutoff_observed_bucket":
        evidence.setdefault("top_temperature_momentum", True)
        evidence.setdefault("top_temperature_trajectory", True)
        evidence.setdefault("top_cloud_rain_physics", True)
    elif top_mode == "joint_warming_environment":
        evidence.setdefault("top_temperature_momentum", True)
        evidence.setdefault("top_temperature_trajectory", True)
        evidence.setdefault("top_cloud_rain_physics", True)
    return evidence


def _phase_placeholder(value: Any) -> bool:
    return str(value or "").strip() in PHASE_PLACEHOLDER_VALUES


def _phase_reason_placeholder(value: Any) -> bool:
    return str(value or "").strip() in PHASE_REASON_PLACEHOLDER_VALUES


def _station_timezone_from_fact_payload(item: Mapping[str, Any]) -> str:
    value = _first_payload_value(
        item,
        "station_timezone",
        "event_tz",
        "timezone",
        "tz",
        "observation_hour_local_timezone",
    )
    return str(value or DEFAULT_STATION_TIMEZONE).strip() or DEFAULT_STATION_TIMEZONE


def _phase_as_of_from_fact_payload(item: Mapping[str, Any], *, fact_as_of: str) -> str:
    value = _first_payload_value(
        item,
        "posterior_as_of",
        "as_of",
        "latest_at",
        "valid_time",
        "valid_time_local",
        "observation_time",
        "zuuu_metar_raw_awc_latest_at",
        "zuuu_metar_raw_awc_fetched_at",
        "fetched_at",
        "updated_at",
    )
    return str(value or fact_as_of or "").strip()


def _flag_or_present(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _truthy(value)
    return _non_empty(value)


def _filter_hard_mask_reason_for_source_gate(
    reason: Any,
    *,
    observed_today_max_eligible: bool,
    current_temperature_eligible: bool,
    any_hard_constraint_eligible: bool,
) -> str:
    parts = [part.strip() for part in str(reason or "").strip().split("+") if part.strip()]
    if not parts:
        return ""
    filtered: list[str] = []
    for part in parts:
        if part == "observed_today_max_c" and not observed_today_max_eligible:
            continue
        if part == "current_temperature_c" and not current_temperature_eligible:
            continue
        if part == "masked_bucket_count" and not any_hard_constraint_eligible:
            continue
        filtered.append(part)
    return "+".join(dict.fromkeys(filtered))


def _phase_hard_mask_source_gate(
    item: Mapping[str, Any],
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    trace = _as_mapping(source_acquisition_trace) or _as_mapping(item.get("source_acquisition_trace"))
    observed_eligible = _payload_observed_today_max_hard_eligible(item, trace)
    current_eligible = _payload_current_temperature_hard_eligible(item, trace)
    hard_ids = _trace_hard_candidate_ids(trace)
    diagnostic_current = _trace_has_diagnostic_current_source(trace)
    observed_source = _payload_observed_today_max_source(item)
    return {
        "schema": "fact_writer_hard_mask_source_gate_v1",
        "observed_today_max_hard_eligible": bool(observed_eligible),
        "current_temperature_hard_eligible": bool(current_eligible),
        "hard_observation_candidate_ids": hard_ids,
        "hard_observation_candidate_count": len(hard_ids),
        "diagnostic_current_source_present": bool(diagnostic_current),
        "observed_today_max_source": observed_source,
        "source_trace_schema": str(trace.get("schema") or ""),
        "demoted_reason": (
            "diagnostic_current_source_not_hard_observation"
            if diagnostic_current and not (observed_eligible or current_eligible)
            else ""
        ),
        "boundary": "only_AWC_METAR_official_settlement_or_accepted_hard_source_can_emit_hard_mask_observation_signals",
    }


def _phase_hard_mask_signals_from_fact_payload(
    item: Mapping[str, Any],
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    hard_constraints = _as_mapping(item.get("hard_constraints"))
    posterior_constraints = _as_mapping(hard_constraints.get("posterior"))
    phase_weight_constraints = _as_mapping(hard_constraints.get("phase_weight"))
    hard_mask_reason = _first_payload_value(item, "hard_mask_reason") or posterior_constraints.get("hard_mask_reason")
    hard_mask_reason_text = str(hard_mask_reason or "").strip()
    if hard_mask_reason_text in {"", "none", "phase_not_materialized_for_this_writer_path"}:
        hard_mask_reason = ""
    source_gate = _phase_hard_mask_source_gate(item, source_acquisition_trace)
    observed_today_max_eligible = bool(source_gate.get("observed_today_max_hard_eligible"))
    current_temperature_eligible = bool(source_gate.get("current_temperature_hard_eligible"))
    hard_lock_signal = any(
        _flag_or_present(value)
        for value in (
            item.get("final_bucket_lock"),
            item.get("hard_source_direct_ssot_write"),
            item.get("hard_source_direct"),
            item.get("hard_source_posterior_override_applied"),
            _first_payload_value(item, "hard_lock", "hard_source_final_daily_max", "official_final_result"),
        )
    )
    settlement_actual = _first_payload_value(item, "settlement_actual", "settlement_actual_c")
    official_final_result = _first_payload_value(item, "official_final_result", "final_daily_max_bucket_lock")
    any_hard_constraint_eligible = bool(
        hard_lock_signal
        or _non_empty(settlement_actual)
        or _non_empty(official_final_result)
        or observed_today_max_eligible
        or current_temperature_eligible
    )
    hard_mask_reason = _filter_hard_mask_reason_for_source_gate(
        hard_mask_reason,
        observed_today_max_eligible=observed_today_max_eligible,
        current_temperature_eligible=current_temperature_eligible,
        any_hard_constraint_eligible=any_hard_constraint_eligible,
    )
    signals = {
        "hard_lock": hard_lock_signal,
        "hard_source_direct_target_realized": any(
            _flag_or_present(value)
            for value in (
                item.get("hard_source_direct_ssot_write"),
                item.get("hard_source_direct"),
                item.get("hard_source_posterior_override_applied"),
            )
        ),
        "settlement_actual": settlement_actual,
        "official_final_result": official_final_result,
        "observed_today_max_c": (
            _first_payload_value(
                item,
                "observed_today_max_c",
                "today_max_c",
                "daily_max_observed_c",
                "zuuu_metar_raw_awc_today_max_c",
                "hard_source_observed_value_c",
            )
            if observed_today_max_eligible
            else None
        ),
        "current_temperature_c": (
            _first_payload_value(
                item,
                "current_temperature_c",
                "current_temp_c",
                "observed_current_temp_c",
                "metar_temperature_c",
                "temperature_c",
                "zuuu_metar_raw_awc_latest_temp_c",
            )
            if current_temperature_eligible
            else None
        ),
        "masked_bucket_count": (
            _first_payload_value(item, "masked_bucket_count", "masked_zero_count")
            or posterior_constraints.get("masked_zero_count")
        ) if any_hard_constraint_eligible else None,
        "hard_mask_reason": hard_mask_reason,
    }
    if phase_weight_constraints.get("hard_lock_active") is not None:
        signals["hard_lock"] = bool(phase_weight_constraints.get("hard_lock_active"))
    return signals


def _materialize_phase_weighting_fields(
    effective: dict[str, Any],
    *,
    event_date: str,
    fact_as_of: str,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not str(event_date or "").strip():
        return []
    current_phase = str(effective.get("phase") or "").strip()
    current_phase_reason = str(effective.get("phase_reason") or "").strip()
    phase_missing_or_placeholder = _phase_placeholder(current_phase)
    phase_reason_missing_or_placeholder = _phase_reason_placeholder(current_phase_reason)
    as_of = _phase_as_of_from_fact_payload(effective, fact_as_of=fact_as_of)
    if not as_of:
        return []

    operations: list[dict[str, Any]] = []
    source_gate = _phase_hard_mask_source_gate(effective, source_acquisition_trace)
    try:
        snapshot = build_phase_weighting_snapshot(
            event_date=event_date,
            as_of=as_of,
            station_timezone=_station_timezone_from_fact_payload(effective),
            hard_mask_signals=_phase_hard_mask_signals_from_fact_payload(
                effective,
                source_acquisition_trace=source_acquisition_trace or _as_mapping(effective.get("source_acquisition_trace")),
            ),
            phase_override="" if phase_missing_or_placeholder else current_phase,
            include_schedule=False,
        )
    except Exception as exc:
        if not _non_empty(effective.get("phase_weighting_state")):
            effective["phase_weighting_state"] = {
                "schema": "phase_weighting_fact_materialization_v1",
                "state": "not_materialized",
                "state_reason": str(exc),
                "event_date": str(event_date or ""),
                "as_of": str(as_of or ""),
                "read_only": True,
                "write_owner": WRITE_OWNER,
            }
            operations.append({"path": "phase_weighting_state", "action": "phase_weighting_materialization_error"})
        return operations

    fields = {
        "phase": snapshot.get("phase"),
        "beta_weak": snapshot.get("beta_weak"),
        "beta_strong": snapshot.get("beta_strong"),
        "phase_reason": snapshot.get("phase_reason"),
        "hard_mask_reason": snapshot.get("hard_mask_reason"),
    }
    for key, value in fields.items():
        if key == "phase":
            should_fill = phase_missing_or_placeholder
        elif key == "phase_reason":
            should_fill = phase_missing_or_placeholder or phase_reason_missing_or_placeholder
        elif key in {"beta_weak", "beta_strong"}:
            should_fill = phase_missing_or_placeholder or not _non_empty(effective.get(key))
        elif key == "hard_mask_reason":
            existing = str(effective.get(key) or "").strip()
            should_fill = (
                phase_missing_or_placeholder
                or existing == ""
                or (existing == "none" and str(value or "").strip() not in {"", "none"})
            )
        else:
            should_fill = not _non_empty(effective.get(key))
        if should_fill:
            effective[key] = copy.deepcopy(value)
            operations.append({"path": key, "action": "materialized_from_phase_weighting_runtime"})
        elif effective.get(key) != value:
            operations.append({"path": key, "action": "preserved_existing_phase_weighting_field"})

    if not _non_empty(effective.get("phase_weighting_state")):
        effective["phase_weighting_state"] = {
            "schema": "phase_weighting_fact_materialization_v1",
            "state": "materialized",
            "source_runtime_schema": snapshot.get("schema"),
            "production_entry_enabled": False,
            "read_only": True,
            "write_owner": WRITE_OWNER,
            "event_date": snapshot.get("event_date"),
            "station_timezone": snapshot.get("station_timezone"),
            "as_of_local": snapshot.get("as_of_local"),
            "as_of_utc": snapshot.get("as_of_utc"),
            "phase_source": "phase_weighting_runtime" if phase_missing_or_placeholder else "preserved_existing_phase",
            "beta_source": snapshot.get("beta_source"),
            "hard_lock_active": bool(snapshot.get("hard_lock_active")),
            "hard_mask_reason_parts": copy.deepcopy(snapshot.get("hard_mask_reason_parts") or []),
            "hard_mask_source_gate": source_gate,
            "awc_release_window": copy.deepcopy(snapshot.get("awc_release_window") or {}),
            "boundaries": copy.deepcopy(snapshot.get("beta_constraints") or {}),
        }
        operations.append({"path": "phase_weighting_state", "action": "attached_phase_weighting_state"})
    return operations


def _merge_fill_nested(dst: dict[str, Any], src: Mapping[str, Any], *, prefix: str = "") -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for key, value in src.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key in dst and str(key).endswith("date_mismatch_reason"):
            operations.append({"path": path, "action": "preserved_explicit_incoming_clear"})
            continue
        if key not in dst or not _non_empty(dst.get(key)):
            dst[key] = copy.deepcopy(value)
            operations.append({"path": path, "action": "filled_missing"})
            continue
        if isinstance(dst.get(key), dict) and isinstance(value, Mapping):
            operations.extend(_merge_fill_nested(dst[key], value, prefix=path))
        elif dst.get(key) != value:
            operations.append({"path": path, "action": "preserved_existing"})
    return operations


def _phase0_fact_json_contract_defaults(
    *,
    fact_snapshot_id: str,
    posterior_run_id: str,
    source_event_ids: Sequence[str],
    source_observation_ids: Sequence[str],
    fact_as_of: str,
    write_reason: str,
) -> dict[str, Any]:
    clean_source_observation_ids = [
        str(item) for item in source_observation_ids if str(item or "").strip()
    ]
    clean_source_event_ids = [str(item) for item in source_event_ids if str(item or "").strip()]
    return {
        "phase": "unknown",
        "beta_weak": 0.0,
        "beta_strong": 0.0,
        "phase_reason": "phase_not_materialized_for_this_writer_path",
        "hard_mask_reason": "none",
        "posterior_ssot_fact": {
            "schema": POSTERIOR_SSOT_FACT_PLACEHOLDER_SCHEMA,
            "state": "not_materialized",
            "state_reason": "posterior_ssot_fact_not_materialized_for_this_writer_path",
            "fact_snapshot_id": str(fact_snapshot_id or ""),
            "posterior_run_id": str(posterior_run_id or ""),
            "source_event_ids": clean_source_event_ids,
            "source_observation_ids": clean_source_observation_ids,
            "read_only": True,
        },
        "hard_source_acceptance_state": {
            "schema": "hard_source_acceptance_state_placeholder_v1",
            "state": "not_applicable",
            "state_reason": "no_hard_source_signal_state_attached_to_fact_snapshot",
            "fact_snapshot_id": str(fact_snapshot_id or ""),
        },
        "signal_to_order_timing": {
            "schema": "signal_to_order_timing_placeholder_v1",
            "state": "not_applicable",
            "state_reason": "fact_snapshot_write_does_not_submit_orders",
            "fact_snapshot_id": str(fact_snapshot_id or ""),
            "posterior_run_id": str(posterior_run_id or ""),
            "source_event_ids": clean_source_event_ids,
            "source_observation_ids": clean_source_observation_ids,
        },
        "daily_review_state": {
            "schema": "daily_review_state_placeholder_v1",
            "state": "pending",
            "state_reason": "daily_review_not_generated_at_fact_write_time",
            "fact_snapshot_id": str(fact_snapshot_id or ""),
            "fact_as_of": str(fact_as_of or ""),
            "write_reason": str(write_reason or ""),
        },
    }


def _merge_fill_phase0_fact_json_contract(
    effective: dict[str, Any],
    *,
    fact_snapshot_id: str,
    posterior_run_id: str,
    source_event_ids: Sequence[str],
    source_observation_ids: Sequence[str],
    fact_as_of: str,
    write_reason: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    defaults = _phase0_fact_json_contract_defaults(
        fact_snapshot_id=fact_snapshot_id,
        posterior_run_id=posterior_run_id,
        source_event_ids=source_event_ids,
        source_observation_ids=source_observation_ids,
        fact_as_of=fact_as_of,
        write_reason=write_reason,
    )
    for key, value in defaults.items():
        if key not in effective or not _non_empty(effective.get(key)):
            effective[key] = copy.deepcopy(value)
            operations.append({"path": key, "action": "filled_phase0_fact_json_contract"})
        elif effective.get(key) != value:
            operations.append({"path": key, "action": "preserved_existing_phase0_fact_json_contract"})
    effective.setdefault("phase0_fact_json_contract", {})
    contract = effective["phase0_fact_json_contract"]
    if isinstance(contract, dict):
        contract.setdefault("schema", PHASE0_FACT_JSON_CONTRACT_SCHEMA)
        contract.setdefault("required_keys", list(PHASE0_FACT_JSON_CONTRACT_KEYS))
        contract.setdefault(
            "missing_required_keys_after_merge_fill",
            [key for key in PHASE0_FACT_JSON_CONTRACT_KEYS if key not in effective],
        )
        contract.setdefault("historical_backfill", False)
        contract.setdefault("write_owner", WRITE_OWNER)
        contract.setdefault(
            "persisted_coverage_contract",
            {
                "schema": FACT_WRITER_PERSISTED_COVERAGE_CONTRACT_SCHEMA,
                "state": "enabled_for_future_writer_facts",
                "posterior_ssot_coverage_required": True,
                "phase_beta_hard_mask_required": True,
                "selection_must_be_posterior_ssot_fact": True,
                "historical_backfill": False,
            },
        )
        contract.setdefault("next_boundary", "future_fact_snapshot_contract_only_no_history_backfill")
        operations.append({"path": "phase0_fact_json_contract", "action": "attached_contract_status"})
    return operations


def _sync_writer_lineage_state_contract(
    effective: dict[str, Any],
    *,
    lineage_state: Mapping[str, Any] | None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
    strict_source_trace: bool = False,
) -> list[dict[str, Any]]:
    writer_lineage = _as_mapping(lineage_state)
    trace = _as_mapping(source_acquisition_trace)
    if not writer_lineage and not (strict_source_trace and trace):
        return []
    existing = effective.get("lineage_state")
    next_lineage = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
    operations: list[dict[str, Any]] = []

    def sync_path(path: str, current: Any, value: Any, setter) -> None:
        if current != value:
            setter(copy.deepcopy(value))
            operations.append({"path": path, "action": "synced_writer_lineage_metadata"})

    for key in (
        "schema",
        "correlation_id",
        "fact_snapshot_id",
        "source_event_ids",
        "source_observation_ids",
        "posterior_run_id",
        "write_owner",
    ):
        if key not in writer_lineage:
            continue
        value = copy.deepcopy(writer_lineage.get(key))
        should_sync = key in {"source_event_ids", "source_observation_ids"} or _non_empty(value)
        if should_sync and next_lineage.get(key) != value:
            next_lineage[key] = value
            operations.append({"path": f"lineage_state.{key}", "action": "synced_writer_lineage_metadata"})
    if next_lineage != existing and next_lineage:
        effective["lineage_state"] = next_lineage
    if strict_source_trace and trace:
        trace_source_events = _source_event_ids(trace)
        trace_source_observations = _source_observation_ids(trace)
        if "source_observations" in trace:
            value = copy.deepcopy(trace.get("source_observations") or [])
            sync_path("source_observations", effective.get("source_observations"), value, lambda next_value: effective.__setitem__("source_observations", next_value))
        if "source_latency_trace" in trace:
            value = copy.deepcopy(trace.get("source_latency_trace") or [])
            sync_path("source_latency_trace", effective.get("source_latency_trace"), value, lambda next_value: effective.__setitem__("source_latency_trace", next_value))
        if trace:
            sync_path("source_acquisition_trace", effective.get("source_acquisition_trace"), copy.deepcopy(dict(trace)), lambda next_value: effective.__setitem__("source_acquisition_trace", next_value))
        if trace_source_events or "source_event_ids" in effective:
            sync_path("source_event_ids", effective.get("source_event_ids"), trace_source_events, lambda next_value: effective.__setitem__("source_event_ids", next_value))
        if trace_source_observations or "source_observation_ids" in effective:
            sync_path("source_observation_ids", effective.get("source_observation_ids"), trace_source_observations, lambda next_value: effective.__setitem__("source_observation_ids", next_value))
        for key in ("posterior_ssot_fact", "signal_to_order_timing", "hard_source_acceptance_state"):
            nested = effective.get(key)
            if not isinstance(nested, Mapping):
                continue
            next_nested = copy.deepcopy(dict(nested))
            if "source_event_ids" in next_nested:
                if next_nested.get("source_event_ids") != trace_source_events:
                    next_nested["source_event_ids"] = copy.deepcopy(trace_source_events)
                    operations.append({"path": f"{key}.source_event_ids", "action": "synced_writer_lineage_metadata"})
            if "source_observation_ids" in next_nested:
                if next_nested.get("source_observation_ids") != trace_source_observations:
                    next_nested["source_observation_ids"] = copy.deepcopy(trace_source_observations)
                    operations.append({"path": f"{key}.source_observation_ids", "action": "synced_writer_lineage_metadata"})
            nested_lineage = next_nested.get("lineage")
            if isinstance(nested_lineage, Mapping):
                next_nested_lineage = copy.deepcopy(dict(nested_lineage))
                if "source_event_ids" in next_nested_lineage:
                    if next_nested_lineage.get("source_event_ids") != trace_source_events:
                        next_nested_lineage["source_event_ids"] = copy.deepcopy(trace_source_events)
                        operations.append({"path": f"{key}.lineage.source_event_ids", "action": "synced_writer_lineage_metadata"})
                if "source_observation_ids" in next_nested_lineage:
                    if next_nested_lineage.get("source_observation_ids") != trace_source_observations:
                        next_nested_lineage["source_observation_ids"] = copy.deepcopy(trace_source_observations)
                        operations.append({"path": f"{key}.lineage.source_observation_ids", "action": "synced_writer_lineage_metadata"})
                next_nested["lineage"] = next_nested_lineage
            if next_nested != nested:
                effective[key] = next_nested
    return operations


def _posterior_rows_from_existing_payload(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    item = _as_mapping(payload)
    for key in (
        "posterior_temperature_distribution",
        "posterior_temperature_distribution_map",
        "bucket_probabilities",
        "distribution",
        "rows",
    ):
        rows = _posterior_bucket_probability_rows(item.get(key))
        if rows:
            return rows
    return _posterior_bucket_probability_rows(item)


def _posterior_run_id_from_existing_payload(
    payload: Mapping[str, Any] | None,
    *,
    slug: str,
    event_date: str,
    fact_snapshot_id: str,
    source_event_ids: Sequence[str],
) -> str:
    item = _as_mapping(payload)
    rows = _posterior_rows_from_existing_payload(item)
    if not rows:
        return ""
    posterior_hash = sha256_json(
        {
            "posterior_temperature_distribution": rows,
            "phase": item.get("phase"),
            "beta_weak": item.get("beta_weak"),
            "beta_strong": item.get("beta_strong"),
            "hard_mask_reason": item.get("hard_mask_reason"),
        }
    )
    return build_posterior_run_id(
        slug=str(slug or ""),
        event_date=str(event_date or ""),
        fact_snapshot_id=str(fact_snapshot_id or ""),
        source_event_ids=source_event_ids,
        posterior_hash=posterior_hash,
        phase=str(item.get("phase") or ""),
    )


def _posterior_ssot_fact_matches_canonical(
    existing: Mapping[str, Any] | None,
    canonical: Mapping[str, Any] | None,
) -> bool:
    existing_map = _as_mapping(existing)
    canonical_map = _as_mapping(canonical)
    if not existing_map or not canonical_map:
        return False
    if str(existing_map.get("target_bucket") or "").strip() != str(canonical_map.get("target_bucket") or "").strip():
        return False
    existing_prob = _prob01(existing_map.get("target_probability"))
    canonical_prob = _prob01(canonical_map.get("target_probability"))
    if existing_prob is None or canonical_prob is None:
        return existing_prob is None and canonical_prob is None
    if abs(float(existing_prob) - float(canonical_prob)) > 1e-9:
        return False
    canonical_norm = _as_mapping(canonical_map.get("hard_mask_normalization"))
    if canonical_norm.get("applied") and not _as_mapping(existing_map.get("hard_mask_normalization")).get("applied"):
        return False
    return True


def _hard_mask_existing_posterior_ssot_fact(
    existing: Mapping[str, Any] | None,
    *,
    hard_temperature_floor_c: float | None,
) -> tuple[dict[str, Any], bool]:
    """Re-apply the observed hard floor to a posterior SSOT block we did not compose.

    build_posterior_ssot_fact masks only the blocks it builds itself, and it
    builds none unless the payload carries joint_weak_physical_strong evidence.
    The floor is a constraint on the surface we publish, not an output of the
    model, so it has to hold on the pass-through path too.  Masking removes
    impossible mass; it never invents any, so it stays inside fail-closed.
    """
    ssot = _as_mapping(existing)
    if not ssot or hard_temperature_floor_c is None:
        return dict(ssot), False
    rows = _posterior_bucket_probability_rows(ssot.get("bucket_probabilities"))
    if not rows:
        return dict(ssot), False
    if not any(
        _posterior_bucket_below_hard_temperature_floor(
            str(row.get("bucket") or row.get("outcome") or row.get("label") or ""),
            hard_temperature_floor_c,
        )
        and float(_prob01(row.get("probability")) or 0.0) > 0.0
        for row in rows
    ):
        return dict(ssot), False
    masked_rows, normalization = _posterior_ssot_bucket_probability_rows(
        rows,
        hard_temperature_floor_c=hard_temperature_floor_c,
    )
    masked = dict(ssot)
    masked["bucket_probabilities"] = masked_rows
    masked["bucket_count"] = len(masked_rows)
    masked["hard_temperature_floor_c"] = float(hard_temperature_floor_c)
    masked["hard_mask_normalization"] = normalization
    if isinstance(ssot.get("ranked_bucket_probabilities"), list):
        masked["ranked_bucket_probabilities"] = sorted(
            copy.deepcopy(masked_rows),
            key=lambda row: float(_prob01(_as_mapping(row).get("probability")) or 0.0),
            reverse=True,
        )
    target_bucket, target_probability = _top_target_from_probability_rows(
        masked_rows,
        hard_temperature_floor_c=hard_temperature_floor_c,
    )
    if target_bucket and target_probability is not None:
        masked["target_bucket"] = target_bucket
        masked["target_probability"] = float(target_probability)
        masked["target_probability_pct"] = float(target_probability) * 100.0
        target_row = next(
            (
                _as_mapping(row)
                for row in masked_rows
                if str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
                == target_bucket
            ),
            {},
        )
        pre_mask = _prob01(target_row.get("probability_pre_hard_mask"))
        if pre_mask is not None:
            masked["target_probability_pre_hard_mask"] = float(pre_mask)
    return masked, True


def _materialize_posterior_ssot_fact_from_existing_payload(
    effective: dict[str, Any],
    *,
    source_acquisition_trace: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    rows = _posterior_rows_from_existing_payload(effective)
    if not rows:
        return []
    operations: list[dict[str, Any]] = []
    if not _non_empty(effective.get("posterior_temperature_distribution")):
        effective["posterior_temperature_distribution"] = copy.deepcopy(rows)
        operations.append(
            {
                "path": "posterior_temperature_distribution",
                "action": "filled_from_existing_distribution_for_posterior_ssot_fact",
            }
        )
    if not _non_empty(effective.get("posterior_temperature_distribution_map")):
        effective["posterior_temperature_distribution_map"] = {
            str(row["bucket"]): float(row["probability"])
            for row in rows
            if str(row.get("bucket") or "").strip() and _prob01(row.get("probability")) is not None
        }
        operations.append(
            {
                "path": "posterior_temperature_distribution_map",
                "action": "filled_from_existing_distribution_for_posterior_ssot_fact",
            }
        )
    existing_ssot = _as_mapping(effective.get("posterior_ssot_fact"))
    existing_schema = str(existing_ssot.get("schema") or "")
    existing_state = str(existing_ssot.get("state") or "")
    hard_floor_c = _hard_temperature_floor_for_target_selection(
        effective,
        source_acquisition_trace=source_acquisition_trace,
    )
    existing_ssot, existing_hard_masked = _hard_mask_existing_posterior_ssot_fact(
        existing_ssot,
        hard_temperature_floor_c=hard_floor_c,
    )
    if existing_hard_masked:
        effective["posterior_ssot_fact"] = existing_ssot
        operations.append(
            {
                "path": "posterior_ssot_fact.bucket_probabilities",
                "action": "hard_masked_existing_posterior_ssot_fact_below_observed_floor",
            }
        )
    canonical_posterior_ssot_fact = build_posterior_ssot_fact(
        effective,
        source_acquisition_trace=source_acquisition_trace,
    )
    canonical_available = canonical_posterior_ssot_fact.get("state") == "available"
    existing_target_row = {
        "bucket": str(existing_ssot.get("target_bucket") or ""),
        "probability": existing_ssot.get("target_probability"),
    }
    existing_target_valid = _posterior_probability_row_eligible_for_target(
        existing_target_row,
        hard_temperature_floor_c=hard_floor_c,
    )
    if existing_schema == POSTERIOR_SSOT_FACT_SCHEMA and existing_state == "available" and existing_target_valid:
        if canonical_available and not _posterior_ssot_fact_matches_canonical(
            existing_ssot,
            canonical_posterior_ssot_fact,
        ):
            posterior_ssot_fact = canonical_posterior_ssot_fact
            effective["posterior_ssot_fact"] = posterior_ssot_fact
            operations.append(
                {
                    "path": "posterior_ssot_fact",
                    "action": "resynced_existing_posterior_ssot_fact_from_canonical_hard_mask_distribution",
                }
            )
        else:
            posterior_ssot_fact = dict(existing_ssot)
    else:
        posterior_ssot_fact = canonical_posterior_ssot_fact
        if posterior_ssot_fact.get("state") != "available":
            return operations
        posterior_ssot_fact.setdefault(
            "materialization_note",
            "wrapped_existing_distribution_without_recomputing_posterior",
        )
        effective["posterior_ssot_fact"] = posterior_ssot_fact
        operations.append(
            {
                "path": "posterior_ssot_fact",
                "action": (
                    "replaced_invalid_existing_posterior_ssot_fact"
                    if existing_schema == POSTERIOR_SSOT_FACT_SCHEMA and existing_state == "available"
                    else "materialized_from_existing_distribution"
                ),
            }
        )
    ssot_contract_changed = False
    ssot_fact_snapshot_id = str(effective.get("fact_snapshot_id") or "")
    if ssot_fact_snapshot_id and not _non_empty(posterior_ssot_fact.get("fact_snapshot_id")):
        posterior_ssot_fact["fact_snapshot_id"] = ssot_fact_snapshot_id
        ssot_contract_changed = True
        operations.append({"path": "posterior_ssot_fact.fact_snapshot_id", "action": "filled_writer_lineage"})
    ssot_posterior_run_id = str(effective.get("posterior_run_id") or "")
    if ssot_posterior_run_id and not _non_empty(posterior_ssot_fact.get("posterior_run_id")):
        posterior_ssot_fact["posterior_run_id"] = ssot_posterior_run_id
        ssot_contract_changed = True
        operations.append({"path": "posterior_ssot_fact.posterior_run_id", "action": "filled_writer_lineage"})
    for identity_key in POSTERIOR_SSOT_PRODUCTION_IDENTITY_KEYS:
        if identity_key in posterior_ssot_fact:
            continue
        if identity_key not in canonical_posterior_ssot_fact:
            continue
        # An available SSOT must name the model that produced it.  A block we
        # did not compose can arrive without one, and the honest value is the
        # identity the canonical build derived from this same payload -- which
        # says joint_weak_physical_strong_unavailable when there was no
        # evidence, rather than claiming a composer that never ran.
        posterior_ssot_fact[identity_key] = canonical_posterior_ssot_fact[identity_key]
        ssot_contract_changed = True
        operations.append(
            {
                "path": f"posterior_ssot_fact.{identity_key}",
                "action": "filled_writer_production_identity",
            }
        )
    boundaries = dict(_as_mapping(posterior_ssot_fact.get("boundaries")))
    boundary_defaults = {
        "old_additive_evidence_pool_used": False,
        "legacy_bucket_product_posterior_used": False,
        "shadow_posterior_emitted": False,
        "frontend_recompute_allowed": False,
        "decision_recompute_allowed": False,
        "weak_likelihood_is_prediction_layer": True,
        "strong_likelihood_is_observation_layer": True,
        "hard_mask_is_constraint_not_weight": True,
        "write_owner": WRITE_OWNER,
    }
    for key, value in boundary_defaults.items():
        if key not in boundaries:
            boundaries[key] = value
            ssot_contract_changed = True
    if ssot_contract_changed or boundaries != _as_mapping(posterior_ssot_fact.get("boundaries")):
        posterior_ssot_fact["boundaries"] = boundaries
        operations.append({"path": "posterior_ssot_fact.boundaries", "action": "filled_writer_apply_boundaries"})
    if not _as_mapping(posterior_ssot_fact.get("layer_chain")):
        layer_contributions = _as_mapping(effective.get("layer_contributions"))
        prior_layer = _as_mapping(layer_contributions.get("prior"))
        weak_layer = _as_mapping(layer_contributions.get("forecast_weak_likelihood"))
        strong_layer = _as_mapping(layer_contributions.get("metar_strong_likelihood"))
        posterior_ssot_fact["layer_chain"] = {
            "schema": "bayes_layer_chain_summary_v1",
            "formula": "continuous_forecast_error_prior_then_sequential_state_zero_inflated_remaining_warming_mixture_then_bucket_integral",
            "prior_present": bool(prior_layer),
            "forecast_weak_likelihood_present": bool(weak_layer),
            "metar_strong_likelihood_present": bool(strong_layer),
            "layer_contributions_present": bool(layer_contributions),
            "prior_boundary": str(prior_layer.get("boundary") or "historical_climate_date_site_terrain_weather_type_only"),
            "forecast_weak_boundary": str(weak_layer.get("boundary") or "models_taf_wu_twc_forecasts_only_no_hard_facts"),
            "metar_strong_boundary": str(strong_layer.get("boundary") or "metar_temperature_momentum_physics_cloud_rain_state_once_no_double_count"),
        }
        ssot_contract_changed = True
        operations.append({"path": "posterior_ssot_fact.layer_chain", "action": "filled_writer_layer_chain"})
    if ssot_contract_changed:
        effective["posterior_ssot_fact"] = posterior_ssot_fact
    target_bucket = str(posterior_ssot_fact.get("target_bucket") or "")
    target_probability = _prob01(posterior_ssot_fact.get("target_probability"))
    if target_bucket:
        selection = _posterior_ssot_target_selection(
            target_bucket=target_bucket,
            target_probability=target_probability,
            fact_snapshot_id=str(effective.get("fact_snapshot_id") or ""),
            posterior_run_id=str(effective.get("posterior_run_id") or ""),
        )
        for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
            existing_selection = _as_mapping(effective.get(key))
            fast_trade_compacted = (
                _truthy(effective.get("awc_hard_observation_fast_trade_write"))
                or _truthy(effective.get("posterior_target_direct_fast_trade_write"))
                or _truthy(existing_selection.get("fast_trade_compacted"))
            )
            existing_source = " ".join(
                str(existing_selection.get(source_key) or "")
                for source_key in (
                    "ssot_source",
                    "target_bucket_selection_source",
                    "target_probability_field",
                )
            )
            source_mentions_ssot = "posterior_ssot_fact" in existing_source
            selection_matches_ssot = _posterior_ssot_selection_matches(
                existing_selection,
                target_bucket=target_bucket,
                target_probability=target_probability,
            )
            selection_has_writer_boundary = existing_selection.get("selection_runtime_recomputed") is False
            if (
                not existing_selection
                or not source_mentions_ssot
                or not selection_matches_ssot
                or not selection_has_writer_boundary
            ):
                next_selection = copy.deepcopy(selection)
                if fast_trade_compacted:
                    next_selection["fast_trade_compacted"] = True
                effective[key] = next_selection
                operations.append(
                    {
                        "path": key,
                        "action": (
                            "resynced_from_posterior_ssot_fact_contract_boundary"
                            if source_mentions_ssot and selection_matches_ssot
                            else
                            "resynced_from_posterior_ssot_fact"
                            if source_mentions_ssot
                            else "filled_from_posterior_ssot_fact"
                        ),
                    }
                )
            elif fast_trade_compacted and not _truthy(existing_selection.get("fast_trade_compacted")):
                next_selection = dict(existing_selection)
                next_selection["fast_trade_compacted"] = True
                effective[key] = next_selection
                operations.append(
                    {
                        "path": key,
                        "action": "filled_fast_trade_compacted_marker_on_posterior_ssot_selection",
                    }
                )
        operations.extend(
            _sync_top_level_posterior_ssot_target_fields(
                effective,
                target_bucket=target_bucket,
                target_probability=target_probability,
            )
        )
        operations.extend(
            _sync_distribution_rows_from_posterior_ssot_fact(
                effective,
                source="weather_fact_snapshot_writer.merge_fill.posterior_ssot_fact",
            )
        )
    return operations


def merge_fill_fact_payload(
    incoming_payload: Mapping[str, Any] | None,
    *,
    current_payload: Mapping[str, Any] | None = None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
    lineage_state: Mapping[str, Any] | None = None,
    fact_layer_state: Mapping[str, Any] | None = None,
    db_write_guard: Mapping[str, Any] | None = None,
    phase0_contract_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    effective = copy.deepcopy(dict(incoming_payload or {}))
    operations: list[dict[str, Any]] = []

    current = _as_mapping(current_payload)
    incoming_prior_meta = _as_mapping(effective.get("prior_meta"))
    incoming_physical_prior = bool(
        incoming_prior_meta.get("settlement_s_used") is False
        and "physical_X" in str(incoming_prior_meta.get("boundary") or "")
    )
    for key in HARD_FACT_MERGE_FILL_KEYS:
        if key not in current:
            continue
        if incoming_physical_prior and key in {"prior_meta", "prior_na_reason"}:
            operations.append({"path": key, "action": "preserved_incoming_physical_prior_atomic_no_settlement_s_merge"})
            continue
        if key not in effective or not _non_empty(effective.get(key)):
            effective[key] = copy.deepcopy(current.get(key))
            operations.append({"path": key, "action": "filled_from_current_hard_fact"})
        elif key == "prior_distribution":
            operations.append({"path": key, "action": "preserved_incoming_no_overwrite"})
        elif isinstance(effective.get(key), dict) and isinstance(current.get(key), Mapping):
            operations.extend(
                {
                    **op,
                    "action": "filled_from_current_hard_fact" if op["action"] == "filled_missing" else op["action"],
                }
                for op in _merge_fill_nested(effective[key], current[key], prefix=key)
            )
        elif effective.get(key) != current.get(key):
            operations.append({"path": key, "action": "preserved_incoming_no_overwrite"})

    trace = _as_mapping(source_acquisition_trace)
    trace_payloads = {
        "source_observations": trace.get("source_observations") or [],
        "source_acquisition_trace": trace,
        "source_latency_trace": trace.get("source_latency_trace") or [],
        "source_observation_ids": _source_observation_ids(source_acquisition_trace),
        "lineage_state": dict(lineage_state or {}),
        "fact_layer_state": dict(fact_layer_state or {}),
        "db_write_guard": dict(db_write_guard or {}),
    }
    for key, value in trace_payloads.items():
        if key not in effective or not _non_empty(effective.get(key)):
            effective[key] = copy.deepcopy(value)
            operations.append({"path": key, "action": "filled_writer_metadata"})
        elif effective.get(key) != value:
            operations.append({"path": key, "action": "preserved_incoming_metadata"})
    operations.extend(
        _sync_writer_lineage_state_contract(
            effective,
            lineage_state=lineage_state,
        )
    )
    phase0_context = _as_mapping(phase0_contract_context)
    writer_fact_as_of = str(phase0_context.get("fact_as_of") or "").strip()
    if writer_fact_as_of and effective.get("fact_as_of") != writer_fact_as_of:
        effective["fact_as_of"] = writer_fact_as_of
        operations.append({"path": "fact_as_of", "action": "synced_writer_fact_as_of"})
    operations.extend(
        _merge_fill_phase0_fact_json_contract(
            effective,
            fact_snapshot_id=str(
                phase0_context.get("fact_snapshot_id")
                or _as_mapping(lineage_state).get("fact_snapshot_id")
                or effective.get("fact_snapshot_id")
                or ""
            ),
            posterior_run_id=str(
                phase0_context.get("posterior_run_id")
                or _as_mapping(lineage_state).get("posterior_run_id")
                or effective.get("posterior_run_id")
                or ""
            ),
            source_event_ids=phase0_context.get("source_event_ids") or _source_event_ids(source_acquisition_trace),
            source_observation_ids=(
                phase0_context.get("source_observation_ids")
                or _source_observation_ids(source_acquisition_trace)
            ),
            fact_as_of=str(phase0_context.get("fact_as_of") or effective.get("fact_as_of") or ""),
            write_reason=str(phase0_context.get("write_reason") or ""),
        )
    )
    fact_snapshot_id = str(
        phase0_context.get("fact_snapshot_id")
        or _as_mapping(lineage_state).get("fact_snapshot_id")
        or effective.get("fact_snapshot_id")
        or ""
    )
    posterior_run_id = str(
        phase0_context.get("posterior_run_id")
        or _as_mapping(lineage_state).get("posterior_run_id")
        or effective.get("posterior_run_id")
        or ""
    )
    if fact_snapshot_id and not _non_empty(effective.get("fact_snapshot_id")):
        effective["fact_snapshot_id"] = fact_snapshot_id
        operations.append({"path": "fact_snapshot_id", "action": "filled_writer_lineage"})
    if posterior_run_id and not _non_empty(effective.get("posterior_run_id")):
        effective["posterior_run_id"] = posterior_run_id
        operations.append({"path": "posterior_run_id", "action": "filled_writer_lineage"})
    operations.extend(
        _materialize_phase_weighting_fields(
            effective,
            event_date=str(phase0_context.get("event_date") or effective.get("event_date") or ""),
            fact_as_of=str(phase0_context.get("fact_as_of") or effective.get("fact_as_of") or ""),
            source_acquisition_trace=source_acquisition_trace,
        )
    )
    operations.extend(
        _materialize_posterior_ssot_fact_from_existing_payload(
            effective,
            source_acquisition_trace=source_acquisition_trace,
        )
    )
    operations.extend(
        _sync_writer_lineage_state_contract(
            effective,
            lineage_state=lineage_state,
            source_acquisition_trace=source_acquisition_trace,
            strict_source_trace=bool(phase0_context),
        )
    )
    return effective, operations


def _summary_cache_needed(payload: Mapping[str, Any]) -> bool:
    return isinstance(payload.get("distribution"), list) or isinstance(payload.get("rows"), list)


def _plan_action(plan: Mapping[str, Any], action: str) -> Mapping[str, Any]:
    for row in plan.get("plan_actions") or []:
        row_map = _as_mapping(row)
        if str(row_map.get("action") or "") == action:
            return row_map
    return {}


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _round_float(value: float | None, ndigits: int = 12) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _parse_iso_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=None)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _json_loads_map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _repair_payload_path_value(payload: Mapping[str, Any], path: str) -> Any:
    if path == "fact_snapshot_id":
        return _as_mapping(payload).get("fact_snapshot_id")
    if path == "lineage_state.fact_snapshot_id":
        return _as_mapping(_as_mapping(payload).get("lineage_state")).get("fact_snapshot_id")
    return None


def _set_repair_payload_path(payload: dict[str, Any], path: str, value: Any) -> None:
    if path == "fact_snapshot_id":
        payload["fact_snapshot_id"] = value
        return
    if path == "lineage_state.fact_snapshot_id":
        lineage = payload.get("lineage_state")
        if not isinstance(lineage, Mapping):
            lineage = {}
        lineage_out = copy.deepcopy(dict(lineage))
        lineage_out["fact_snapshot_id"] = value
        payload["lineage_state"] = lineage_out


def _event_fact_history_merge_fill_blocked_result(
    *,
    reason: str,
    allow_apply: bool,
    blockers: Sequence[str] | None = None,
    repair_item_count: int = 0,
) -> dict[str, Any]:
    blocker_values = [str(item or "").strip() for item in blockers or [] if str(item or "").strip()]
    if reason and reason not in blocker_values:
        blocker_values.append(reason)
    return {
        "schema": EVENT_FACT_HISTORY_MERGE_FILL_REPAIR_RESULT_SCHEMA,
        "ok": False,
        "applied": False,
        "blocked": True,
        "block_reason": reason,
        "blockers": sorted(set(blocker_values)),
        "allow_apply": bool(allow_apply),
        "write_owner": WRITE_OWNER,
        "repair_item_count": int(repair_item_count or 0),
        "applied_items": [],
        "skipped_items": [],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_source_observations": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql": False,
        },
    }


def apply_event_fact_history_merge_fill_repair_contract(
    conn: sqlite3.Connection | None,
    contract: Mapping[str, Any],
    *,
    allow_apply: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """Apply a targeted merge-fill repair to existing fact rows under the writer owner."""

    contract_map = _as_mapping(contract)
    repair_items = [dict(_as_mapping(item)) for item in _sequence(contract_map.get("repair_items"))]
    if not allow_apply:
        return _event_fact_history_merge_fill_blocked_result(
            reason="allow_apply_false",
            allow_apply=allow_apply,
            repair_item_count=len(repair_items),
        )
    if conn is None:
        return _event_fact_history_merge_fill_blocked_result(
            reason="missing_sqlite_connection",
            allow_apply=allow_apply,
            repair_item_count=len(repair_items),
        )
    if contract_map.get("schema") != EVENT_FACT_HISTORY_MERGE_FILL_REPAIR_CONTRACT_SCHEMA:
        return _event_fact_history_merge_fill_blocked_result(
            reason="invalid_contract_schema",
            allow_apply=allow_apply,
            repair_item_count=len(repair_items),
        )
    if not repair_items:
        return {
            "schema": EVENT_FACT_HISTORY_MERGE_FILL_REPAIR_RESULT_SCHEMA,
            "ok": True,
            "applied": False,
            "blocked": False,
            "allow_apply": True,
            "write_owner": WRITE_OWNER,
            "repair_item_count": 0,
            "applied_count": 0,
            "skipped_count": 0,
            "applied_items": [],
            "skipped_items": [],
            "guards": {
                "writes_archive_db": False,
                "writes_facts_sqlite": False,
                "writes_event_fact_history": False,
                "writes_event_fact_latest": False,
                "writes_event_fact_summary_cache": False,
                "writes_auto_trade_decisions": False,
                "places_orders": False,
                "restarts_launchd": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "executes_sql": False,
            },
        }

    conn.row_factory = sqlite3.Row
    prepared: list[dict[str, Any]] = []
    validation_blockers: list[str] = []
    savepoint = "weather_event_fact_history_merge_fill_repair"
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
        for item in repair_items:
            if str(item.get("target_table") or "").strip() != "event_fact_history":
                validation_blockers.append("target_table_unsupported")
                continue
            target_row_id = _int_or_none(item.get("target_row_id")) or 0
            if target_row_id <= 0:
                validation_blockers.append("target_row_id_invalid")
                continue
            row = conn.execute(
                "SELECT * FROM event_fact_history WHERE id = ? LIMIT 1",
                (target_row_id,),
            ).fetchone()
            if row is None:
                validation_blockers.append("target_row_not_found")
                continue

            row_keys = set(row.keys())
            expected_slug = str(item.get("slug") or "").strip()
            expected_event_date = str(item.get("event_date") or "").strip()[:10]
            if "slug" in row_keys and expected_slug and str(row["slug"] or "").strip() != expected_slug:
                validation_blockers.append("target_row_slug_mismatch")
            if "event_date" in row_keys and expected_event_date and str(row["event_date"] or "").strip()[:10] != expected_event_date:
                validation_blockers.append("target_row_event_date_mismatch")

            payload = _json_loads_map(row["fact_json"] if "fact_json" in row_keys else "")
            if not payload:
                validation_blockers.append("fact_json_parse_error")
                continue

            paths = _as_mapping(item.get("expected_merge_fill")).get("paths")
            if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes, bytearray)):
                paths = ()
            candidate_fact_snapshot_id = str(item.get("candidate_fact_snapshot_id") or "").strip()
            path_values: dict[str, Any] = {}
            for path_row in paths:
                path_map = _as_mapping(path_row)
                path = str(path_map.get("json_path") or "").strip()
                value = path_map.get("value")
                if path:
                    path_values[path] = value
            if candidate_fact_snapshot_id:
                path_values.setdefault("fact_snapshot_id", candidate_fact_snapshot_id)
                path_values.setdefault("lineage_state.fact_snapshot_id", candidate_fact_snapshot_id)
            if not candidate_fact_snapshot_id:
                validation_blockers.append("candidate_fact_snapshot_id_missing")
            unsupported_paths = sorted(path for path in path_values if path not in {"fact_snapshot_id", "lineage_state.fact_snapshot_id"})
            validation_blockers.extend(f"unsupported_merge_fill_path:{path}" for path in unsupported_paths)

            repaired_payload = copy.deepcopy(payload)
            operations: list[dict[str, Any]] = []
            for path, incoming_value in sorted(path_values.items()):
                current_value = _repair_payload_path_value(repaired_payload, path)
                value_text = str(incoming_value or "").strip()
                if current_value in (None, "", [], {}):
                    _set_repair_payload_path(repaired_payload, path, incoming_value)
                    operations.append({"json_path": path, "action": "filled_missing", "value": incoming_value})
                elif str(current_value or "").strip() == value_text:
                    operations.append({"json_path": path, "action": "already_matching", "value": incoming_value})
                else:
                    validation_blockers.append(f"{path.replace('.', '_')}_conflict")

            changed = canonical_json(repaired_payload) != canonical_json(payload)
            prepared.append(
                {
                    "target_row_id": target_row_id,
                    "repair_correlation_id": item.get("repair_correlation_id"),
                    "candidate_fact_snapshot_id": candidate_fact_snapshot_id,
                    "changed": changed,
                    "operations": operations,
                    "fact_json": json.dumps(repaired_payload, ensure_ascii=False, sort_keys=True),
                }
            )

        if validation_blockers:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return _event_fact_history_merge_fill_blocked_result(
                reason="target_validation_blocked",
                allow_apply=allow_apply,
                blockers=validation_blockers,
                repair_item_count=len(repair_items),
            )

        applied_items: list[dict[str, Any]] = []
        skipped_items: list[dict[str, Any]] = []
        for item in prepared:
            if not item["changed"]:
                skipped_items.append(
                    {
                        "target_row_id": item["target_row_id"],
                        "repair_correlation_id": item.get("repair_correlation_id"),
                        "reason": "already_satisfied",
                    }
                )
                continue
            conn.execute(
                "UPDATE event_fact_history SET fact_json = ? WHERE id = ?",
                (item["fact_json"], item["target_row_id"]),
            )
            applied_items.append(
                {
                    "target_row_id": item["target_row_id"],
                    "repair_correlation_id": item.get("repair_correlation_id"),
                    "candidate_fact_snapshot_id": item.get("candidate_fact_snapshot_id"),
                    "operations": item.get("operations") or [],
                }
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            conn.commit()
    except sqlite3.Error as exc:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass
        result = _event_fact_history_merge_fill_blocked_result(
            reason="sqlite_error",
            allow_apply=allow_apply,
            blockers=[f"sqlite_error:{exc.__class__.__name__}"],
            repair_item_count=len(repair_items),
        )
        result["sqlite_error"] = str(exc)
        return result

    wrote = bool(applied_items)
    return {
        "schema": EVENT_FACT_HISTORY_MERGE_FILL_REPAIR_RESULT_SCHEMA,
        "ok": True,
        "applied": wrote,
        "blocked": False,
        "allow_apply": True,
        "write_owner": WRITE_OWNER,
        "repair_item_count": len(repair_items),
        "applied_count": len(applied_items),
        "skipped_count": len(skipped_items),
        "applied_items": applied_items,
        "skipped_items": skipped_items,
        "guards": {
            "writes_archive_db": wrote,
            "writes_facts_sqlite": False,
            "writes_event_fact_history": wrote,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql": wrote,
        },
    }


def _prob01(value: Any) -> float | None:
    prob = _float_or_none(value)
    if prob is None:
        return None
    if float(prob) > 1.0 and float(prob) <= 100.0:
        prob = float(prob) / 100.0
    return max(0.0, min(1.0, float(prob)))


def _feature_ts(features: Mapping[str, Any], key: str) -> datetime | None:
    return _parse_iso_ts(_as_mapping(features).get(key))


def _feature_truthy(value: Any) -> bool:
    return _truthy(value)


def _features_have_awc_hard_observation(features: Mapping[str, Any] | None) -> bool:
    feature_map = _as_mapping(features)
    return bool(
        feature_map.get("zuuu_metar_raw_awc_latest_at")
        or feature_map.get("zuuu_metar_raw_awc_fetched_at")
        or _float_or_none(feature_map.get("zuuu_metar_raw_awc_today_max_c")) is not None
    )


def _awc_today_max_anchor(features: Mapping[str, Any] | None) -> tuple[float | None, str]:
    feature_map = _as_mapping(features)
    if (
        _feature_truthy(feature_map.get("zuuu_metar_raw_awc_date_mismatch"))
        or _feature_truthy(feature_map.get("metar_observation_date_mismatch"))
    ):
        return None, ""
    awc_max = _float_or_none(feature_map.get("zuuu_metar_raw_awc_today_max_c"))
    if awc_max is None:
        return None, ""
    return float(awc_max), "zuuu_metar_raw_awc"


def _candidate_today_max_is_stronger(
    candidate_features: Mapping[str, Any] | None,
    base_features: Mapping[str, Any] | None,
) -> bool:
    candidate = _as_mapping(candidate_features)
    base = _as_mapping(base_features)
    candidate_max = _float_or_none(candidate.get("zuuu_metar_raw_awc_today_max_c"))
    base_max = _float_or_none(base.get("zuuu_metar_raw_awc_today_max_c"))
    if candidate_max is not None and base_max is not None:
        if float(candidate_max) > float(base_max) + 1e-9:
            return True
        if float(base_max) > float(candidate_max) + 1e-9:
            return False
        candidate_at = _feature_ts(candidate, "zuuu_metar_raw_awc_today_max_at")
        base_at = _feature_ts(base, "zuuu_metar_raw_awc_today_max_at")
        if candidate_at is not None and base_at is not None:
            return candidate_at > base_at
        return candidate_at is not None and base_at is None
    return candidate_max is not None and base_max is None


def _payload_awc_features(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    item = _as_mapping(payload)
    features = item.get("weather_likelihood_features")
    if isinstance(features, Mapping) and _features_have_awc_hard_observation(features):
        return dict(features)
    compact = item.get("weather_likelihood_features_compact")
    if isinstance(compact, Mapping) and _features_have_awc_hard_observation(compact):
        provenance = _as_mapping(item.get("fact_provenance"))
        lineage = _as_mapping(item.get("weather_source_lineage"))
        if (
            bool(item.get("history_compacted"))
            or isinstance(provenance.get("awc_hard_observation_refresh"), Mapping)
            or isinstance(provenance.get("awc_hard_observation_preserved_from_latest_fact"), Mapping)
            or isinstance(lineage.get("zuuu_metar_raw_awc"), Mapping)
        ):
            return dict(compact)
    return {}


def _payload_hard_observation_value(payload: Mapping[str, Any] | None) -> float | None:
    item = _as_mapping(payload)
    features = _as_mapping(item.get("weather_likelihood_features"))
    awc_max, _awc_source = _awc_today_max_anchor(features)
    if awc_max is not None:
        return float(awc_max)
    for key in (
        "hard_source_observed_value_c",
        "zuuu_metar_raw_awc_today_max_c",
        "observed_today_max_c",
        "observed_max_c",
    ):
        value = _float_or_none(item.get(key))
        if value is not None:
            return float(value)
    for key in (
        "hard_source_observed_value_c",
        "zuuu_metar_raw_awc_today_max_c",
        "observed_today_max_c",
    ):
        value = _float_or_none(features.get(key))
        if value is not None:
            return float(value)
    for rows_key in ("rows", "distribution"):
        rows = item.get(rows_key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            continue
        values: list[float] = []
        awc_values: list[float] = []
        for row in rows:
            row_map = _as_mapping(row)
            awc_value = _float_or_none(row_map.get("zuuu_metar_raw_awc_today_max_c"))
            if awc_value is not None:
                awc_values.append(float(awc_value))
                continue
            for key in ("hard_source_observed_value_c", "observed_today_max_c"):
                value = _float_or_none(row_map.get(key))
                if value is not None:
                    values.append(float(value))
                    break
        if awc_values:
            return max(awc_values)
        if values:
            return max(values)
    return None


def _payload_observed_today_max_source(payload: Mapping[str, Any] | None) -> str:
    item = _as_mapping(payload)
    features = _as_mapping(item.get("weather_likelihood_features"))
    awc_max, awc_source = _awc_today_max_anchor(features)
    if awc_max is not None and awc_source:
        return awc_source
    for key in ("observed_today_max_source", "hard_source_observed_source"):
        value = str(features.get(key) or item.get(key) or "").strip()
        if value:
            return value
    for rows_key in ("rows", "distribution"):
        rows = item.get(rows_key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            continue
        for row in rows:
            row_map = _as_mapping(row)
            value = str(row_map.get("observed_today_max_source") or row_map.get("hard_source_observed_source") or "").strip()
            if value:
                return value
    return ""


def _source_is_wu_twc_intraday(source: Any) -> bool:
    return str(source or "").strip().startswith("wu_twc_observed_intraday")


def _payload_observation_path_materialization_rank(payload: Mapping[str, Any] | None) -> int:
    item = _as_mapping(payload)
    features = _as_mapping(item.get("weather_likelihood_features")) or _as_mapping(item.get("weather_likelihood_features_compact"))
    current_peak_mu = _float_or_none(features.get("peak_estimator_mu_c"))
    stale_observation_path = False
    if current_peak_mu is not None:
        observed_mus: list[float] = []
        for rows_key in ("distribution", "rows"):
            rows = item.get(rows_key)
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
                continue
            for row in rows:
                mu = _float_or_none(_as_mapping(row).get("observation_path_likelihood_mu_c"))
                if mu is not None:
                    observed_mus.append(float(mu))
        if observed_mus and max(abs(float(mu) - float(current_peak_mu)) for mu in observed_mus) >= 0.75:
            stale_observation_path = True
    if stale_observation_path:
        return 0
    if _truthy(item.get("observation_path_likelihood_posterior_materialized")):
        return 2
    provenance = _as_mapping(item.get("fact_provenance"))
    observation_meta = _as_mapping(provenance.get("observation_path_likelihood"))
    if _truthy(observation_meta.get("posterior_materialized")):
        return 2
    observed_component = False
    for rows_key in ("distribution", "rows"):
        rows = item.get(rows_key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            continue
        for row in rows:
            row_map = _as_mapping(row)
            q = _float_or_none(row_map.get("q_temperature_bucket_posterior"))
            pre = _float_or_none(row_map.get("q_temperature_bucket_posterior_pre_observation_path_likelihood"))
            if q is not None and pre is not None and abs(float(q) - float(pre)) > 1e-9:
                return 2
            component = _float_or_none(_as_mapping(row_map.get("q_temperature_bucket_score_components")).get("observation_path_likelihood"))
            if component is not None and float(component) > 0.0:
                observed_component = True
            if _truthy(row_map.get("observation_path_likelihood_posterior_materialized")):
                return 2
    if _truthy(observation_meta.get("applied")):
        return 1
    if _truthy(item.get("observation_path_likelihood_applied")) or observed_component:
        return 1
    return 0


def _payload_model_snapshot_id(payload: Mapping[str, Any] | None) -> int | None:
    item = _as_mapping(payload)
    candidates: list[Any] = [item.get("model_snapshot_id")]
    provenance = _as_mapping(item.get("fact_provenance"))
    score_composer = _as_mapping(item.get("score_composer"))
    candidates.extend([provenance.get("model_snapshot_id"), score_composer.get("model_snapshot_id")])
    for value in candidates:
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _payload_fast_trade_latest(payload: Mapping[str, Any] | None) -> bool:
    item = _as_mapping(payload)
    if _truthy(item.get("awc_hard_observation_fast_trade_write")):
        return True
    if _truthy(item.get("posterior_target_direct_fast_trade_write")):
        return True
    for key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        value = _as_mapping(item.get(key))
        if _truthy(value.get("fast_trade_compacted")):
            return True
    provenance = _as_mapping(item.get("fact_provenance"))
    return bool(
        isinstance(provenance.get("awc_fast_trade_fact_write"), Mapping)
        or isinstance(provenance.get("posterior_target_direct_fast_trade_write_requested"), Mapping)
    )


def _posterior_bucket_probability_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        raw = (
            value.get("bucket_probabilities")
            or value.get("distribution")
            or value.get("rows")
            or value.get("posterior_temperature_distribution")
        )
        if raw is None and value and all(_prob01(probability) is not None for probability in value.values()):
            raw = value
        if isinstance(raw, Mapping):
            iterator = [
                {"bucket": str(label), "probability": probability}
                for label, probability in raw.items()
            ]
        else:
            iterator = raw
    else:
        iterator = value
    rows: list[dict[str, Any]] = []
    if not isinstance(iterator, Sequence) or isinstance(iterator, (str, bytes, bytearray)):
        return rows
    for raw_row in iterator:
        row = _as_mapping(raw_row)
        label = str(
            row.get("bucket")
            or row.get("outcome")
            or row.get("label")
            or row.get("temperature_bucket")
            or ""
        ).strip()
        probability = _prob01(
            row.get("probability")
            if row.get("probability") is not None
            else row.get("prob")
            if row.get("prob") is not None
            else row.get("posterior")
            if row.get("posterior") is not None
            else row.get("q_temperature_bucket_posterior")
            if row.get("q_temperature_bucket_posterior") is not None
            else row.get("q_bayes_posterior")
            if row.get("q_bayes_posterior") is not None
            else row.get("q_contract_payout_posterior")
            if row.get("q_contract_payout_posterior") is not None
            else row.get("q_round")
        )
        if not label or probability is None:
            continue
        out = {
            "bucket": label,
            "probability": float(probability),
        }
        for key in (
            "prior",
            "forecast_weak_likelihood",
            "metar_strong_likelihood",
            "physical_strong_distribution",
            "prior_stage_probability",
            "forecast_weak_stage_probability",
            "metar_strong_stage_probability",
            "posterior_stage_probability",
            "hard_mask",
            "log_score",
            "bucket_lower_bound_c",
            "bucket_upper_bound_c",
            "bucket_representative_c",
            "bucket_open_lower",
            "bucket_open_upper",
            "posterior_settlement_candidate_bucket_hard_zero",
            "posterior_settlement_candidate_bucket_hard_zero_reasons",
            "q_posterior_observed_constraints_hard_zero",
            "q_bayes_physical_likelihood_hard_zero",
            "impossible_by_observed_max",
            "hard_zero",
            "hard_mask_reason",
            "impossible_reason",
            "observed_today_max_c",
            "observed_today_max_source",
            "zuuu_metar_raw_awc_today_max_c",
            "hard_source_observed_value_c",
        ):
            if key in row:
                out[key] = copy.deepcopy(row.get(key))
        rows.append(out)
    return rows


def _posterior_probability_row_is_hard_zero(row: Mapping[str, Any] | None) -> bool:
    row_map = _as_mapping(row)
    reasons = row_map.get("posterior_settlement_candidate_bucket_hard_zero_reasons")
    if not isinstance(reasons, Sequence) or isinstance(reasons, (str, bytes, bytearray)):
        reasons = []
    reason_text = " ".join(str(reason or "").lower() for reason in reasons)
    return bool(
        _truthy(row_map.get("posterior_settlement_candidate_bucket_hard_zero"))
        or _truthy(row_map.get("q_posterior_observed_constraints_hard_zero"))
        or _truthy(row_map.get("q_bayes_physical_likelihood_hard_zero"))
        or _truthy(row_map.get("impossible_by_observed_max"))
        or _truthy(row_map.get("hard_zero"))
        or "observed_today_max_above_bucket" in reason_text
        or "hard_source_final_bucket_lock" in reason_text
        or "final_bucket_lock" in reason_text
    )


def _posterior_bucket_upper_bound(label: Any) -> float | None:
    text = str(label or "").strip().lower()
    if not text:
        return None
    clean = (
        text.replace("℃", "")
        .replace("℉", "")
        .replace("°c", "")
        .replace("°f", "")
        .strip()
    )
    number = r"(-?\d+(?:\.\d+)?)"
    match = re.search(rf"(?:<=|≤)\s*{number}", clean)
    if match:
        return float(match.group(1))
    match = re.search(rf"{number}\s+or\s+below", clean)
    if match:
        return float(match.group(1))
    match = re.search(rf"{number}\s*(?:-|–|to)\s*{number}", clean)
    if match:
        return float(match.group(2))
    if re.search(r"\bor\s+(?:higher|above|more)\b|(?:>=|≥)", clean):
        return float("inf")
    match = re.fullmatch(rf"{number}", clean)
    if match:
        return float(match.group(1))
    return None


def _posterior_bucket_matches_temperature_value(label: Any, value_c: float | None) -> bool:
    if value_c is None or not math.isfinite(float(value_c)):
        return False
    text = str(label or "").strip().lower()
    if not text:
        return False
    clean = (
        text.replace("℃", "")
        .replace("°c", "")
        .replace("c", "")
        .strip()
    )
    value = float(value_c)
    number = r"(-?\d+(?:\.\d+)?)"
    match = re.search(rf"(?:<=|≤)\s*{number}", clean)
    if match:
        return value <= float(match.group(1)) + 1e-9
    match = re.search(rf"{number}\s+or\s+below", clean)
    if match:
        return value <= float(match.group(1)) + 1e-9
    match = re.search(rf"{number}\s*(?:-|–|to)\s*{number}", clean)
    if match:
        return float(match.group(1)) - 1e-9 <= value <= float(match.group(2)) + 1e-9
    match = re.search(rf"(?:>=|≥)\s*{number}", clean)
    if match:
        return value >= float(match.group(1)) - 1e-9
    match = re.search(rf"{number}\s+or\s+(?:higher|above|more)", clean)
    if match:
        return value >= float(match.group(1)) - 1e-9
    match = re.fullmatch(number, clean)
    if match:
        return math.floor(value) == math.floor(float(match.group(1)))
    return False


def _final_hard_lock_target_bucket(
    payload: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, float | None, str]:
    item = _as_mapping(payload)
    final_lock_keys = (
        "final_bucket_lock",
        "hard_source_final_bucket_lock",
        "observed_today_max_final",
        "hard_source_final_daily_max",
        "hard_source_settlement_final",
        "temperature_observation_final_daily_max",
        "official_final_result",
        "final_daily_max_bucket_lock",
    )

    def has_final_lock_flag(source: Mapping[str, Any] | None) -> bool:
        source_map = _as_mapping(source)
        return any(_truthy(source_map.get(key)) for key in final_lock_keys)

    for raw_row in rows:
        row = _as_mapping(raw_row)
        if not any(_truthy(row.get(key)) for key in ("hard_source_posterior_override_final_bucket_lock", *final_lock_keys)):
            continue
        label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        if label:
            return label, _prob01(row.get("probability") if row.get("probability") is not None else row.get("posterior")), "row_final_bucket_lock"

    final_value = _float_or_none(
        _first_payload_value(
            item,
            "settlement_actual",
            "settlement_actual_c",
            "official_final_result",
            "final_daily_max_c",
        )
    )
    if final_value is None:
        features = _as_mapping(item.get("weather_likelihood_features"))
        compact = _as_mapping(item.get("weather_likelihood_features_compact"))
        hard_constraints = _as_mapping(item.get("hard_constraints"))
        for source in (features, compact, hard_constraints, _as_mapping(hard_constraints.get("metar_strong"))):
            if has_final_lock_flag(source):
                final_value = _float_or_none(
                    source.get("observed_today_max_c")
                    if source.get("observed_today_max_c") is not None
                    else source.get("zuuu_metar_raw_awc_today_max_c")
                )
                if final_value is not None:
                    break
    has_final_lock = any(
        has_final_lock_flag(source)
        for source in (
            item,
            _as_mapping(item.get("weather_likelihood_features")),
            _as_mapping(item.get("weather_likelihood_features_compact")),
            _as_mapping(item.get("hard_constraints")),
            _as_mapping(_as_mapping(item.get("hard_constraints")).get("metar_strong")),
        )
    )
    if final_value is None or not has_final_lock:
        return "", None, ""
    for raw_row in rows:
        row = _as_mapping(raw_row)
        label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        if label and _posterior_bucket_matches_temperature_value(label, final_value):
            return label, _prob01(row.get("probability") if row.get("probability") is not None else row.get("posterior")), "final_observed_temperature"
    return "", None, ""


def _source_text_is_hard_temperature_observation(source: Any) -> bool:
    text = str(source or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "awc",
            "metar",
            "aviationweather",
            "noaa",
            "zuuu",
            "hard_source",
            "settlement",
            "official",
        )
    )


def _hard_temperature_floor_for_target_selection(
    payload: Mapping[str, Any] | None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> float | None:
    item = _as_mapping(payload)
    candidates: list[float] = []

    def add(value: Any) -> None:
        parsed = _float_or_none(value)
        if parsed is not None and math.isfinite(float(parsed)):
            candidates.append(float(parsed))

    def add_from_mapping(value: Any) -> None:
        row = _as_mapping(value)
        if not row:
            return
        for key in (
            "zuuu_metar_raw_awc_today_max_c",
            "zuuu_metar_raw_awc_latest_temp_c",
            "hard_source_observed_value_c",
            "metar_temperature_c",
            "settlement_actual",
        ):
            add(row.get(key))
        source_text = (
            row.get("observed_today_max_source")
            or row.get("hard_source_observed_source")
            or row.get("current_temperature_source")
            or row.get("observed_current_temp_source")
            or row.get("temperature_source")
        )
        if _source_text_is_hard_temperature_observation(source_text):
            add(row.get("observed_today_max_c"))
            add(row.get("current_temperature_c"))
            add(row.get("current_temp_c"))

    add_from_mapping(item)
    add_from_mapping(item.get("weather_likelihood_features"))
    add_from_mapping(item.get("weather_likelihood_features_compact"))
    hard_constraints = _as_mapping(item.get("hard_constraints"))
    add_from_mapping(hard_constraints)
    for child in hard_constraints.values():
        add_from_mapping(child)
    for rows_key in ("posterior_temperature_distribution", "distribution", "rows"):
        rows = item.get(rows_key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            continue
        for row in rows:
            add_from_mapping(row)
    if not candidates and _payload_observed_today_max_hard_eligible(item, source_acquisition_trace):
        add(_payload_hard_observation_value(item))
    if not candidates:
        return None
    return max(candidates)


def _posterior_bucket_below_hard_temperature_floor(label: Any, hard_floor_c: float | None) -> bool:
    if hard_floor_c is None:
        return False
    upper_bound = _posterior_bucket_upper_bound(label)
    if upper_bound is None or not math.isfinite(float(upper_bound)):
        return False
    return math.floor(float(hard_floor_c)) > float(upper_bound) + 1e-9


def _posterior_probability_row_eligible_for_target(
    row: Mapping[str, Any] | None,
    *,
    hard_temperature_floor_c: float | None = None,
) -> bool:
    row_map = _as_mapping(row)
    label = str(row_map.get("bucket") or row_map.get("outcome") or row_map.get("label") or "").strip()
    if not label:
        return False
    if _posterior_probability_row_is_hard_zero(row_map):
        return False
    return not _posterior_bucket_below_hard_temperature_floor(label, hard_temperature_floor_c)


def _top_target_from_probability_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    hard_temperature_floor_c: float | None = None,
) -> tuple[str, float | None]:
    best_label = ""
    best_prob: float | None = None
    for raw_row in rows:
        row = _as_mapping(raw_row)
        label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        if not _posterior_probability_row_eligible_for_target(
            row,
            hard_temperature_floor_c=hard_temperature_floor_c,
        ):
            continue
        probability = _prob01(row.get("probability") if row.get("probability") is not None else row.get("posterior"))
        if label and probability is not None and (best_prob is None or float(probability) > float(best_prob)):
            best_label = label
            best_prob = float(probability)
    return best_label, best_prob


def _posterior_ssot_bucket_probability_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    hard_temperature_floor_c: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared: list[tuple[dict[str, Any], float, bool, bool]] = []
    eligible_total = 0.0
    excluded_count = 0
    for raw_row in rows:
        row = copy.deepcopy(dict(_as_mapping(raw_row)))
        label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        probability = _prob01(row.get("probability") if row.get("probability") is not None else row.get("posterior"))
        if not label or probability is None:
            continue
        below_floor = _posterior_bucket_below_hard_temperature_floor(label, hard_temperature_floor_c)
        hard_zero = bool(_posterior_probability_row_is_hard_zero(row) or below_floor)
        if below_floor:
            row["hard_zero"] = True
            reasons = row.get("posterior_settlement_candidate_bucket_hard_zero_reasons")
            if not isinstance(reasons, list):
                reasons = []
            if "hard_temperature_floor_above_bucket" not in reasons:
                reasons = list(reasons) + ["hard_temperature_floor_above_bucket"]
            row["posterior_settlement_candidate_bucket_hard_zero_reasons"] = reasons
            row.setdefault("hard_mask_reason", "hard_temperature_floor")
        if hard_zero:
            excluded_count += 1
        else:
            eligible_total += float(probability)
        prepared.append((row, float(probability), hard_zero, below_floor))

    if not prepared:
        return [], {
            "schema": "posterior_ssot_hard_mask_normalization_v1",
            "applied": False,
            "reason": "posterior_rows_missing",
        }

    should_normalize = bool(excluded_count > 0 or abs(eligible_total - 1.0) > 1e-9)
    if eligible_total <= 0.0:
        out = []
        for row, probability, hard_zero, _below_floor in prepared:
            row["probability"] = 0.0 if hard_zero else float(probability)
            row["posterior_field"] = "posterior_ssot_fact.bucket_probabilities"
            if hard_zero:
                row["probability_pre_hard_mask"] = float(probability)
            out.append(row)
        return out, {
            "schema": "posterior_ssot_hard_mask_normalization_v1",
            "applied": False,
            "reason": "eligible_probability_sum_nonpositive",
            "excluded_bucket_count": excluded_count,
            "eligible_probability_pre_hard_mask_sum": float(eligible_total),
        }

    out: list[dict[str, Any]] = []
    for row, probability, hard_zero, _below_floor in prepared:
        row["posterior_field"] = "posterior_ssot_fact.bucket_probabilities"
        if should_normalize:
            row["probability_pre_hard_mask"] = float(probability)
            row["probability"] = 0.0 if hard_zero else float(probability) / float(eligible_total)
            row["hard_mask_conditioned_probability"] = row["probability"]
            if hard_zero:
                row["hard_zero"] = True
        else:
            row["probability"] = float(probability)
        out.append(row)
    return out, {
        "schema": "posterior_ssot_hard_mask_normalization_v1",
        "applied": should_normalize,
        "reason": "hard_zero_or_floor_excluded" if excluded_count else "distribution_renormalized",
        "excluded_bucket_count": excluded_count,
        "eligible_bucket_count": len(prepared) - excluded_count,
        "eligible_probability_pre_hard_mask_sum": float(eligible_total),
        "hard_temperature_floor_c": hard_temperature_floor_c,
    }


def _posterior_ssot_final_lock_probability_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_bucket: str,
    reason: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    out: list[dict[str, Any]] = []
    target_key = str(target_bucket or "").strip()
    for raw_row in rows:
        row = copy.deepcopy(dict(_as_mapping(raw_row)))
        label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        if not label:
            continue
        pre_probability = _prob01(row.get("probability") if row.get("probability") is not None else row.get("posterior"))
        if pre_probability is not None:
            row["probability_pre_hard_mask"] = float(pre_probability)
        row["posterior_field"] = "posterior_ssot_fact.bucket_probabilities"
        if label == target_key:
            row["probability"] = 1.0
            row["hard_mask_conditioned_probability"] = 1.0
            row["final_bucket_lock_target"] = True
        else:
            row["probability"] = 0.0
            row["hard_mask_conditioned_probability"] = 0.0
            row["hard_zero"] = True
            reasons = row.get("posterior_settlement_candidate_bucket_hard_zero_reasons")
            if not isinstance(reasons, list):
                reasons = []
            if "final_bucket_lock" not in reasons:
                reasons = list(reasons) + ["final_bucket_lock"]
            row["posterior_settlement_candidate_bucket_hard_zero_reasons"] = reasons
            row.setdefault("hard_mask_reason", "final_bucket_lock")
        out.append(row)
    return out, {
        "schema": "posterior_ssot_hard_mask_normalization_v1",
        "applied": True,
        "reason": "final_bucket_lock",
        "final_lock_reason": str(reason or "final_bucket_lock"),
        "excluded_bucket_count": max(0, len(out) - 1),
        "eligible_bucket_count": 1 if target_key else 0,
        "eligible_probability_pre_hard_mask_sum": 1.0,
        "hard_lock_active": True,
    }


def _top_target_from_rows(payload: Mapping[str, Any] | None) -> tuple[str, float | None]:
    item = _as_mapping(payload)
    rows = item.get("distribution") if isinstance(item.get("distribution"), list) else item.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return "", None
    best_label = ""
    best_prob: float | None = None
    for row in rows:
        row_map = _as_mapping(row)
        label = str(row_map.get("outcome") or row_map.get("label") or row_map.get("bucket") or "").strip()
        if not label:
            continue
        prob = _prob01(
            row_map.get("q_temperature_bucket_posterior")
            if row_map.get("q_temperature_bucket_posterior") is not None
            else row_map.get("q_bayes_posterior")
            if row_map.get("q_bayes_posterior") is not None
            else row_map.get("q_contract_payout_posterior")
            if row_map.get("q_contract_payout_posterior") is not None
            else row_map.get("posterior")
            if row_map.get("posterior") is not None
            else row_map.get("probability")
        )
        if prob is None:
            prob = _prob01(row_map.get("q_bayes_posterior_pct"))
        if prob is not None and (best_prob is None or float(prob) > float(best_prob)):
            best_label = label
            best_prob = float(prob)
    return best_label, best_prob


def _top_target_from_ctp(payload: Mapping[str, Any] | None) -> tuple[str, float | None]:
    item = _as_mapping(payload)
    rows = _posterior_bucket_probability_rows(item.get("posterior_temperature_distribution"))
    if not rows:
        rows = _posterior_bucket_probability_rows(item.get("posterior_temperature_distribution_map"))
    return _top_target_from_probability_rows(rows)


def _payload_target_probability(payload: Mapping[str, Any] | None) -> tuple[str, float | None]:
    item = _as_mapping(payload)
    for key in ("posterior_target_bucket_selection", "target_bucket_selection", "target_realization_capital_plan"):
        value = _as_mapping(item.get(key))
        label = str(value.get("target_bucket") or value.get("bucket") or value.get("outcome") or "").strip()
        prob: float | None = None
        for prob_key in (
            "target_probability",
            "target_posterior",
            "target_bucket_probability",
            "posterior",
            "target_probability_pct",
            "target_bucket_probability_pct",
        ):
            prob = _prob01(value.get(prob_key))
            if prob is not None:
                break
        if label and prob is not None:
            return label, float(prob)
    label, prob = _top_target_from_ctp(item)
    if label and prob is not None:
        return label, prob
    return _top_target_from_rows(item)


def _posterior_ssot_target_selection(
    *,
    target_bucket: str,
    target_probability: float | None,
    fact_snapshot_id: str,
    posterior_run_id: str,
) -> dict[str, Any]:
    probability = _prob01(target_probability)
    selection: dict[str, Any] = {
        "schema": "posterior_target_bucket_selection_v1",
        "target_bucket": str(target_bucket or ""),
        "selected_bucket": str(target_bucket or ""),
        "target_bucket_selection_source": "posterior_ssot_fact.top_bucket",
        "selection_priority": "posterior_ssot_fact_top_bucket",
        "target_bucket_probability_field": "posterior_ssot_fact.target_probability",
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "posterior_field": "posterior_ssot_fact.target_probability",
        "ssot_source": "posterior_ssot_fact",
        "fact_snapshot_id": str(fact_snapshot_id or ""),
        "posterior_run_id": str(posterior_run_id or ""),
        "selection_runtime_recomputed": False,
        "read_only": True,
    }
    if probability is not None:
        selection["target_probability"] = float(probability)
        selection["target_posterior"] = float(probability)
        selection["target_bucket_probability"] = float(probability)
        selection["target_probability_pct"] = float(probability) * 100.0
        selection["target_bucket_probability_pct"] = float(probability) * 100.0
        selection["posterior_highest_probability_pct"] = float(probability) * 100.0
    return selection


def _posterior_ssot_probability_rows_by_bucket(ssot: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    rows = _posterior_bucket_probability_rows(_as_mapping(ssot))
    out: dict[str, dict[str, Any]] = {}
    raw_rows = _as_mapping(ssot).get("bucket_probabilities")
    raw_by_label: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes, bytearray)):
        for raw in raw_rows:
            row = _as_mapping(raw)
            label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
            if label:
                raw_by_label[label] = row
    for row in rows:
        label = str(row.get("bucket") or "").strip()
        if not label:
            continue
        merged = dict(row)
        if label in raw_by_label:
            merged.update(copy.deepcopy(dict(raw_by_label[label])))
            merged["bucket"] = label
            merged["probability"] = float(row["probability"])
        out[label] = merged
    return out


def _sync_distribution_row_from_posterior_ssot(
    row: dict[str, Any],
    ssot_row: Mapping[str, Any],
    *,
    source: str,
) -> bool:
    label = str(row.get("outcome") or row.get("label") or row.get("bucket") or "").strip()
    if not label:
        return False
    probability = _prob01(ssot_row.get("probability"))
    if probability is None:
        return False
    prob = float(probability)
    changed = False
    for key in (
        "probability",
        "posterior",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
        "q_contract_payout_posterior",
        "q_entry",
        "target_bucket_probability",
        "posterior_temperature_distribution_probability",
    ):
        old = _prob01(row.get(key))
        if old is not None and abs(float(old) - prob) > 1e-12:
            pre_key = f"{key}_pre_posterior_ssot_fact_alignment"
            if pre_key not in row:
                row[pre_key] = float(old)
        if old is None or abs(float(old) - prob) > 1e-12:
            changed = True
        row[key] = prob
        row[f"{key}_pct"] = prob * 100.0
    no_prob = max(0.0, min(1.0, 1.0 - prob))
    for key in ("q_bayes_posterior_no", "q_entry_no"):
        old = _prob01(row.get(key))
        if old is not None and abs(float(old) - no_prob) > 1e-12:
            pre_key = f"{key}_pre_posterior_ssot_fact_alignment"
            if pre_key not in row:
                row[pre_key] = float(old)
        if old is None or abs(float(old) - no_prob) > 1e-12:
            changed = True
        row[key] = no_prob
        row[f"{key}_pct"] = no_prob * 100.0
    for key in (
        "probability_pre_hard_mask",
        "hard_mask_conditioned_probability",
        "hard_zero",
        "hard_mask_reason",
        "posterior_settlement_candidate_bucket_hard_zero",
        "posterior_settlement_candidate_bucket_hard_zero_reasons",
        "q_posterior_observed_constraints_hard_zero",
        "q_bayes_physical_likelihood_hard_zero",
        "impossible_by_observed_max",
        "final_bucket_lock_target",
    ):
        if key in ssot_row and row.get(key) != ssot_row.get(key):
            row[key] = copy.deepcopy(ssot_row.get(key))
            changed = True
    if row.get("posterior_field") != "posterior_ssot_fact.bucket_probabilities":
        row["posterior_field"] = "posterior_ssot_fact.bucket_probabilities"
        changed = True
    row["q_probability_source"] = "posterior_ssot_fact"
    row["posterior_ssot_fact_distribution_aligned"] = True
    row.setdefault("posterior_ssot_fact_distribution_alignment_source", source)
    return changed


def _sync_distribution_rows_from_posterior_ssot_fact(
    payload: dict[str, Any],
    *,
    source: str,
    sync_posterior_temperature_distribution_list: bool = False,
) -> list[dict[str, Any]]:
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    if str(ssot.get("schema") or "") != POSTERIOR_SSOT_FACT_SCHEMA or str(ssot.get("state") or "") != "available":
        return []
    by_label = _posterior_ssot_probability_rows_by_bucket(ssot)
    if not by_label:
        return []
    operations: list[dict[str, Any]] = []

    def sync_rows(value: Any, path: str) -> None:
        if not isinstance(value, list):
            return
        touched = 0
        for raw in value:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("outcome") or raw.get("label") or raw.get("bucket") or "").strip()
            ssot_row = by_label.get(label)
            if not ssot_row:
                continue
            if _sync_distribution_row_from_posterior_ssot(raw, ssot_row, source=source):
                touched += 1
        if touched:
            operations.append(
                {
                    "path": path,
                    "action": "aligned_from_posterior_ssot_fact_bucket_probabilities",
                    "row_count": touched,
                }
            )

    sync_rows(payload.get("distribution"), "distribution")
    sync_rows(payload.get("rows"), "rows")

    def sync_probability_rows(value: Any, path: str) -> None:
        if not isinstance(value, list):
            return
        touched = 0
        for raw in value:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("outcome") or raw.get("label") or raw.get("bucket") or "").strip()
            ssot_row = by_label.get(label)
            if not ssot_row:
                continue
            probability = _prob01(ssot_row.get("probability"))
            if probability is None:
                continue
            prob = float(probability)
            old = _prob01(
                raw.get("probability")
                if raw.get("probability") is not None
                else raw.get("posterior")
                if raw.get("posterior") is not None
                else raw.get("q_temperature_bucket_posterior")
            )
            if old is not None and abs(float(old) - prob) <= 1e-12:
                continue
            if old is not None and "probability_pre_posterior_ssot_fact_alignment" not in raw:
                raw["probability_pre_posterior_ssot_fact_alignment"] = float(old)
            raw["probability"] = prob
            raw["probability_pct"] = prob * 100.0
            raw["posterior_field"] = "posterior_ssot_fact.bucket_probabilities"
            touched += 1
        if touched:
            operations.append(
                {
                    "path": path,
                    "action": "aligned_from_posterior_ssot_fact_bucket_probabilities",
                    "row_count": touched,
                }
            )

    posterior_distribution = payload.get("posterior_temperature_distribution")
    if isinstance(posterior_distribution, list):
        if sync_posterior_temperature_distribution_list:
            sync_rows(posterior_distribution, "posterior_temperature_distribution")
        else:
            sync_probability_rows(posterior_distribution, "posterior_temperature_distribution")
    elif isinstance(posterior_distribution, dict):
        bucket_rows = posterior_distribution.get("bucket_probabilities")
        before_count = len(operations)
        sync_rows(bucket_rows, "posterior_temperature_distribution.bucket_probabilities")
        if len(operations) > before_count:
            posterior_distribution["ssot_source"] = "posterior_ssot_fact.bucket_probabilities"
            posterior_distribution["aligned_from_posterior_ssot_fact"] = True
    probability_map = {
        label: float(_prob01(row.get("probability")) or 0.0)
        for label, row in by_label.items()
    }
    for path in ("posterior_temperature_distribution_map",):
        value = payload.get(path)
        if isinstance(value, dict):
            changed = False
            for label, prob in probability_map.items():
                if label in value and _prob01(value.get(label)) != prob:
                    value[f"{label}__pre_posterior_ssot_fact_alignment"] = _prob01(value.get(label))
                    value[label] = prob
                    changed = True
            if changed:
                operations.append(
                    {
                        "path": path,
                        "action": "aligned_from_posterior_ssot_fact_bucket_probabilities",
                        "row_count": sum(1 for label in probability_map if label in value),
                    }
                )
    if operations:
        provenance = dict(_as_mapping(payload.get("fact_provenance")))
        provenance["posterior_ssot_fact_distribution_alignment"] = {
            "applied": True,
            "source": source,
            "probability_field": "posterior_ssot_fact.bucket_probabilities",
            "operation_count": len(operations),
        }
        payload["fact_provenance"] = provenance
    return operations


def _sync_top_level_posterior_ssot_target_fields(
    payload: dict[str, Any],
    *,
    target_bucket: str,
    target_probability: float | None,
) -> list[dict[str, Any]]:
    expected: dict[str, Any] = {
        "ssot_target_bucket": str(target_bucket or ""),
        "target_bucket": str(target_bucket or ""),
        "target_bucket_source": "posterior_ssot_fact.top_bucket",
        "target_bucket_source_semantics": "bayes_posterior_ssot_fact_v1",
        "target_bucket_probability_field": "posterior_ssot_fact.target_probability",
    }
    probability = _prob01(target_probability)
    if probability is not None:
        expected.update(
            {
                "target_bucket_probability": float(probability),
                "target_bucket_probability_pct": float(probability) * 100.0,
                "posterior_highest_probability_pct": float(probability) * 100.0,
            }
        )
    operations: list[dict[str, Any]] = []
    for key, expected_value in expected.items():
        current_value = payload.get(key)
        if key in {"target_bucket_probability_pct", "posterior_highest_probability_pct"}:
            current_pct = _float_or_none(current_value)
            expected_pct = _float_or_none(expected_value)
            values_match = (
                current_pct is not None
                and expected_pct is not None
                and abs(float(current_pct) - float(expected_pct)) <= 1e-9
            )
        elif key == "target_bucket_probability":
            current_prob = _prob01(current_value)
            expected_prob = _prob01(expected_value)
            values_match = (
                current_prob is not None
                and expected_prob is not None
                and abs(float(current_prob) - float(expected_prob)) <= 1e-9
            )
        else:
            values_match = str(current_value or "") == str(expected_value or "")
        if values_match:
            continue
        action = "resynced_from_posterior_ssot_fact" if _non_empty(current_value) else "filled_from_posterior_ssot_fact"
        payload[key] = copy.deepcopy(expected_value)
        operations.append({"path": key, "action": action})
    return operations


def _posterior_ssot_selection_matches(
    selection: Mapping[str, Any] | None,
    *,
    target_bucket: str,
    target_probability: float | None,
) -> bool:
    row = _as_mapping(selection)
    if not row:
        return False
    selected_bucket = str(
        row.get("target_bucket")
        or row.get("selected_bucket")
        or row.get("posterior_highest_bucket_label")
        or ""
    ).strip()
    if selected_bucket != str(target_bucket or "").strip():
        return False
    expected_prob = _prob01(target_probability)
    if expected_prob is None:
        return True
    for key in (
        "target_probability",
        "target_posterior",
        "target_bucket_probability",
        "posterior",
        "target_probability_pct",
        "target_bucket_probability_pct",
        "posterior_highest_probability_pct",
    ):
        actual_prob = _prob01(row.get(key))
        if actual_prob is not None:
            return abs(float(actual_prob) - float(expected_prob)) <= 1e-9
    return False


def _posterior_ssot_selection_contract_issues(
    payload: Mapping[str, Any],
    *,
    selection_key: str,
    target_bucket: str,
    target_probability: float | None,
    fact_snapshot_id: str,
    posterior_run_id: str,
) -> list[str]:
    issues: list[str] = []
    selection = _as_mapping(payload.get(selection_key))
    if not selection:
        return [f"{selection_key}_missing"]
    source_text = " ".join(
        str(selection.get(key) or "")
        for key in (
            "ssot_source",
            "target_bucket_selection_source",
            "target_probability_field",
            "target_bucket_probability_field",
            "posterior_field",
        )
    )
    if "posterior_ssot_fact" not in source_text:
        issues.append(f"{selection_key}_not_from_posterior_ssot_fact")
    if selection.get("selection_runtime_recomputed") is not False:
        issues.append(f"{selection_key}_runtime_recomputed_not_false")
    if not _posterior_ssot_selection_matches(
        selection,
        target_bucket=target_bucket,
        target_probability=target_probability,
    ):
        issues.append(f"{selection_key}_target_mismatch")
    selection_fact_snapshot_id = str(selection.get("fact_snapshot_id") or "")
    if fact_snapshot_id and selection_fact_snapshot_id and selection_fact_snapshot_id != fact_snapshot_id:
        issues.append(f"{selection_key}_fact_snapshot_id_mismatch")
    selection_posterior_run_id = str(selection.get("posterior_run_id") or "")
    if posterior_run_id and selection_posterior_run_id and selection_posterior_run_id != posterior_run_id:
        issues.append(f"{selection_key}_posterior_run_id_mismatch")
    return issues


def _posterior_bucket_labels_for_prior(payload: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    for row in _posterior_rows_from_existing_payload(payload):
        label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _payload_text(payload: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return str(default or "")


def _materialize_prior_layer_fields(
    payload: dict[str, Any],
    *,
    event_date: str = "",
    fact_as_of: str = "",
    prior_history_conn: sqlite3.Connection | None = None,
    prior_history_db_path: Any = None,
) -> None:
    needs_prior_materialization = (
        not _as_mapping(payload.get("prior_distribution"))
        or not _as_mapping(payload.get("prior_meta"))
        or "prior_na_reason" not in payload
    )
    if needs_prior_materialization and (prior_history_conn is not None or prior_history_db_path):
        labels = _posterior_bucket_labels_for_prior(payload)
        if labels:
            try:
                from temperature_prior_runtime import build_temperature_prior_distribution

                prior_output = build_temperature_prior_distribution(
                    buckets=labels,
                    event_date=str(event_date or payload.get("event_date") or ""),
                    as_of=str(
                        fact_as_of
                        or payload.get("fact_as_of")
                        or payload.get("posterior_as_of")
                        or payload.get("as_of")
                        or ""
                    ),
                    conn=prior_history_conn,
                    db_path=prior_history_db_path,
                    city=_payload_text(payload, "city", "city_key"),
                    station_id=_payload_text(payload, "station_id", "source_station_id"),
                    market_var=_payload_text(payload, "market_var", default="temperature_2m_max"),
                    market_unit=_payload_text(payload, "market_unit", default="C"),
                    rounding_rule=_payload_text(payload, "rounding_rule", default="rounded_degrees"),
                    weather_type=_payload_text(payload, "weather_type"),
                    terrain_class=_payload_text(payload, "terrain_class"),
                    site_class=_payload_text(payload, "site_class"),
                    urbanization_class=_payload_text(payload, "urbanization_class"),
                    site_temperature_bias_c=payload.get("site_temperature_bias_c", 0.0),
                    terrain_temperature_bias_c=payload.get("terrain_temperature_bias_c", 0.0),
                    urban_heat_island_c=payload.get("urban_heat_island_c", 0.0),
                )
            except Exception as exc:
                prior_output = {
                    "schema": "temperature_prior_runtime_v1",
                    "state": "unavailable",
                    "ok": False,
                    "prior_distribution": {},
                    "prior_na_reason": "prior_runtime_error",
                    "prior_meta": {
                        "state_reason": "prior_runtime_error",
                        "error": str(exc)[:160],
                    },
                }
            prior_distribution = _as_mapping(prior_output.get("prior_distribution"))
            if prior_distribution and not _as_mapping(payload.get("prior_distribution")):
                payload["prior_distribution"] = copy.deepcopy(dict(prior_distribution))
            prior_continuous_distribution = _as_mapping(prior_output.get("prior_continuous_distribution"))
            if prior_continuous_distribution and not _as_mapping(payload.get("prior_continuous_distribution")):
                payload["prior_continuous_distribution"] = copy.deepcopy(dict(prior_continuous_distribution))
            prior_meta = _as_mapping(prior_output.get("prior_meta"))
            if prior_meta:
                if isinstance(payload.get("prior_meta"), dict):
                    for key, value in prior_meta.items():
                        payload["prior_meta"].setdefault(key, copy.deepcopy(value))
                elif not _as_mapping(payload.get("prior_meta")):
                    payload["prior_meta"] = copy.deepcopy(dict(prior_meta))
            if "prior_na_reason" not in payload:
                payload["prior_na_reason"] = str(
                    prior_output.get("prior_na_reason")
                    or _as_mapping(prior_output.get("prior_meta")).get("prior_na_reason")
                    or ""
                )
            payload.setdefault(
                "prior_runtime",
                {
                    "schema": prior_output.get("schema"),
                    "state": prior_output.get("state"),
                    "ok": bool(prior_output.get("ok")),
                    "read_only": bool(prior_output.get("read_only", True)),
                    "production_entry_enabled": bool(prior_output.get("production_entry_enabled", False)),
                    "guards": copy.deepcopy(prior_output.get("guards") or {}),
                    "next_boundary": prior_output.get("next_boundary"),
                },
            )
    prior_meta = _as_mapping(payload.get("prior_meta"))
    if prior_meta and isinstance(payload.get("prior_meta"), dict):
        if not str(payload["prior_meta"].get("shrinkage_target") or ""):
            payload["prior_meta"]["shrinkage_target"] = "broader_settled_history"
        if "prior_na_reason" not in payload["prior_meta"]:
            payload["prior_meta"]["prior_na_reason"] = ""
    if "prior_na_reason" not in payload and prior_meta:
        payload["prior_na_reason"] = str(_as_mapping(payload.get("prior_meta")).get("prior_na_reason") or "")


def _distribution_sum(values: Mapping[str, Any]) -> float | None:
    total = 0.0
    found = False
    for value in values.values():
        number = _float_or_none(value)
        if number is None:
            return None
        total += max(0.0, float(number))
        found = True
    return total if found else None


def build_prior_layer_contract(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    item = _as_mapping(payload)
    rows = _posterior_rows_from_existing_payload(item)
    if not rows:
        return {
            "schema": PRIOR_LAYER_CONTRACT_SCHEMA,
            "ok": True,
            "state": "not_applicable_no_posterior_distribution",
            "enforced": False,
            "issues": [],
        }

    prior_distribution = _as_mapping(item.get("prior_distribution"))
    prior_meta = _as_mapping(item.get("prior_meta"))
    prior_na_reason_present = "prior_na_reason" in item or "prior_na_reason" in prior_meta
    issues: list[str] = []
    if not prior_distribution:
        issues.append("prior_distribution_missing")
    else:
        total = _distribution_sum(prior_distribution)
        if total is None:
            issues.append("prior_distribution_contains_non_numeric_probability")
        elif total <= 0.0:
            issues.append("prior_distribution_sum_zero")
        elif abs(float(total) - 1.0) > 1e-6:
            issues.append("prior_distribution_not_normalized")
        posterior_labels = {
            str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
            for row in rows
            if str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
        }
        prior_labels = {str(label or "").strip() for label in prior_distribution.keys() if str(label or "").strip()}
        missing_labels = sorted(label for label in posterior_labels if label not in prior_labels)
        if missing_labels:
            issues.append("prior_distribution_missing_posterior_buckets")
    if str(prior_meta.get("schema") or "") != "temperature_prior_meta_v1":
        issues.append("prior_meta_schema_invalid")
    boundary = str(prior_meta.get("boundary") or "")
    if "historical" not in boundary or "no_forecast_no_metar_no_market" not in boundary:
        issues.append("prior_meta_boundary_invalid")
    if "sample_count" not in prior_meta:
        issues.append("prior_meta_sample_count_missing")
    if not str(prior_meta.get("shrinkage_target") or ""):
        issues.append("prior_meta_shrinkage_target_missing")
    if "date_window_days" not in prior_meta:
        issues.append("prior_meta_date_window_days_missing")
    if not prior_na_reason_present:
        issues.append("prior_na_reason_missing")
    return {
        "schema": PRIOR_LAYER_CONTRACT_SCHEMA,
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "enforced": True,
        "issues": issues,
        "prior_distribution_bucket_count": len(prior_distribution),
        "posterior_distribution_bucket_count": len(rows),
        "prior_distribution_sum": _round_float(_distribution_sum(prior_distribution)),
        "prior_meta_schema": str(prior_meta.get("schema") or ""),
        "prior_meta_boundary": boundary,
        "prior_na_reason": str(item.get("prior_na_reason") if "prior_na_reason" in item else prior_meta.get("prior_na_reason") or ""),
        "sample_count": _int_or_none(prior_meta.get("sample_count")),
        "shrinkage_target": str(prior_meta.get("shrinkage_target") or ""),
    }


def build_posterior_ssot_apply_contract(
    payload: Mapping[str, Any] | None,
    *,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the writer-owned posterior SSOT fields before fact apply.

    The contract is intentionally local to the writer so future SQL apply paths
    do not depend on UI or audit scripts to keep impossible target buckets out
    of persisted fact_json.
    """

    item = _as_mapping(payload)
    rows = _posterior_rows_from_existing_payload(item)
    ssot = _as_mapping(item.get("posterior_ssot_fact"))
    ssot_rows = _posterior_bucket_probability_rows(ssot)
    hard_floor_c = _hard_temperature_floor_for_target_selection(
        item,
        source_acquisition_trace=source_acquisition_trace,
    )
    raw_target_bucket, raw_target_probability = _top_target_from_probability_rows(
        rows,
        hard_temperature_floor_c=hard_floor_c,
    )
    target_bucket, target_probability = _top_target_from_probability_rows(ssot_rows)
    ssot_probability_sum = sum(float(_prob01(row.get("probability")) or 0.0) for row in ssot_rows)
    phase = str(item.get("phase") or ssot.get("phase") or "")
    phase_reason = str(item.get("phase_reason") or ssot.get("phase_reason") or "")
    beta_weak = _float_or_none(item.get("beta_weak") if item.get("beta_weak") is not None else ssot.get("beta_weak"))
    beta_strong = _float_or_none(
        item.get("beta_strong") if item.get("beta_strong") is not None else ssot.get("beta_strong")
    )
    hard_mask_reason = str(item.get("hard_mask_reason") or ssot.get("hard_mask_reason") or "")
    fact_snapshot_id = str(item.get("fact_snapshot_id") or _as_mapping(item.get("lineage_state")).get("fact_snapshot_id") or "")
    posterior_run_id = str(
        item.get("posterior_run_id")
        or _as_mapping(item.get("lineage_state")).get("posterior_run_id")
        or _as_mapping(item.get("posterior_runtime")).get("posterior_run_id")
        or ""
    )
    if not rows:
        return {
            "schema": "posterior_ssot_apply_contract_v1",
            "ok": True,
            "state": "not_applicable_no_posterior_distribution",
            "enforced": False,
            "issues": [],
            "hard_temperature_floor_c": hard_floor_c,
            "target_bucket": "",
            "target_probability": None,
        }

    issues: list[str] = []
    ssot_schema = str(ssot.get("schema") or "")
    ssot_state = str(ssot.get("state") or "")
    if ssot_schema != POSTERIOR_SSOT_FACT_SCHEMA:
        issues.append("posterior_ssot_fact_schema_invalid")
    if ssot_state != "available":
        issues.append("posterior_ssot_fact_not_available")
    ssot_fact_snapshot_id = str(ssot.get("fact_snapshot_id") or "")
    if not ssot_fact_snapshot_id:
        issues.append("posterior_ssot_fact_snapshot_id_missing")
    elif fact_snapshot_id and ssot_fact_snapshot_id != fact_snapshot_id:
        issues.append("posterior_ssot_fact_snapshot_id_mismatch")
    ssot_posterior_run_id = str(ssot.get("posterior_run_id") or "")
    if not ssot_posterior_run_id:
        issues.append("posterior_ssot_fact_posterior_run_id_missing")
    elif posterior_run_id and ssot_posterior_run_id != posterior_run_id:
        issues.append("posterior_ssot_fact_posterior_run_id_mismatch")
    ssot_bucket = str(ssot.get("target_bucket") or ssot.get("ssot_target_bucket") or "")
    ssot_probability = _prob01(ssot.get("target_probability"))
    if not ssot_bucket:
        issues.append("posterior_ssot_fact_target_bucket_missing")
    if ssot_probability is None:
        issues.append("posterior_ssot_fact_target_probability_missing")
    if not ssot_rows:
        issues.append("posterior_ssot_fact_bucket_probabilities_missing")
    elif abs(float(ssot_probability_sum) - 1.0) > 1e-6:
        issues.append("posterior_ssot_fact_bucket_probabilities_not_normalized")
    if target_bucket and ssot_bucket and ssot_bucket != target_bucket:
        issues.append("posterior_ssot_fact_target_not_distribution_top")
    if (
        target_probability is not None
        and ssot_probability is not None
        and abs(float(target_probability) - float(ssot_probability)) > 1e-9
    ):
        issues.append("posterior_ssot_fact_target_probability_not_distribution_top")
    if _posterior_bucket_below_hard_temperature_floor(ssot_bucket, hard_floor_c):
        issues.append("posterior_ssot_fact_target_below_hard_temperature_floor")
    if "posterior_ssot_fact" not in str(ssot.get("target_probability_field") or ""):
        issues.append("posterior_ssot_fact_probability_field_invalid")
    boundaries = _as_mapping(ssot.get("boundaries"))
    if boundaries.get("frontend_recompute_allowed") is not False:
        issues.append("posterior_ssot_fact_frontend_recompute_boundary_missing")
    if boundaries.get("decision_recompute_allowed") is not False:
        issues.append("posterior_ssot_fact_decision_recompute_boundary_missing")
    if boundaries.get("hard_mask_is_constraint_not_weight") is not True:
        issues.append("posterior_ssot_fact_hard_mask_boundary_missing")
    if not _as_mapping(ssot.get("layer_chain")):
        issues.append("posterior_ssot_fact_layer_chain_missing")
    if not phase or phase in PHASE_PLACEHOLDER_VALUES:
        issues.append("phase_missing_or_placeholder")
    if not phase_reason or phase_reason == "phase_not_materialized_for_this_writer_path":
        issues.append("phase_reason_missing_or_placeholder")
    if beta_weak is None or not 0.0 <= beta_weak <= 1.0:
        issues.append("beta_weak_missing_or_out_of_range")
    if beta_strong is None or not 0.0 <= beta_strong <= 1.0:
        issues.append("beta_strong_missing_or_out_of_range")
    if not hard_mask_reason:
        issues.append("hard_mask_reason_missing")
    for selection_key in ("posterior_target_bucket_selection", "target_bucket_selection"):
        issues.extend(
            _posterior_ssot_selection_contract_issues(
                item,
                selection_key=selection_key,
                target_bucket=ssot_bucket,
                target_probability=ssot_probability,
                fact_snapshot_id=fact_snapshot_id,
                posterior_run_id=posterior_run_id,
            )
        )
    return {
        "schema": "posterior_ssot_apply_contract_v1",
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "enforced": True,
        "issues": issues,
        "hard_temperature_floor_c": hard_floor_c,
        "distribution_bucket_count": len(rows),
        "distribution_top_bucket": raw_target_bucket,
        "distribution_top_probability": raw_target_probability,
        "posterior_ssot_distribution_bucket_count": len(ssot_rows),
        "posterior_ssot_distribution_top_bucket": target_bucket,
        "posterior_ssot_distribution_top_probability": target_probability,
        "posterior_ssot_distribution_probability_sum": ssot_probability_sum if ssot_rows else None,
        "target_bucket": ssot_bucket,
        "target_probability": ssot_probability,
        "phase": phase,
        "beta_weak": beta_weak,
        "beta_strong": beta_strong,
        "hard_mask_reason": hard_mask_reason,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
    }


def build_posterior_ssot_fact(payload: Mapping[str, Any], *, source_acquisition_trace: Mapping[str, Any] | None = None) -> dict[str, Any]:
    item = _as_mapping(payload)
    trace = _as_mapping(source_acquisition_trace) or _as_mapping(item.get("source_acquisition_trace"))
    posterior_rows = _posterior_bucket_probability_rows(item.get("posterior_temperature_distribution"))
    if not posterior_rows:
        posterior_rows = _posterior_bucket_probability_rows(item.get("posterior_temperature_distribution_map"))
    hard_floor_c = _hard_temperature_floor_for_target_selection(item, trace)
    final_lock_bucket, final_lock_pre_probability, final_lock_reason = _final_hard_lock_target_bucket(
        item,
        posterior_rows,
    )
    raw_production_model_version = str(item.get("production_model_version") or "").strip()
    new_model_evidenced = raw_production_model_version == "joint_weak_physical_strong_composer_v1"
    if final_lock_bucket:
        production_model_version = "joint_weak_physical_strong_official_final_lock_v1"
        production_model_ready = True
    elif new_model_evidenced:
        production_model_version = raw_production_model_version
        production_model_ready = item.get("production_model_ready") is not False
    else:
        production_model_version = "joint_weak_physical_strong_unavailable"
        production_model_ready = False
    production_identity = {
        "production_model_family": "joint_weak_physical_strong",
        "production_model_version": production_model_version,
        "production_model_ready": bool(production_model_ready),
        "production_probability_policy": "unique_joint_weak_physical_strong_only_fail_closed",
        "legacy_model_fallback_allowed": False,
    }
    if not production_model_ready and not final_lock_bucket:
        return {
            "schema": POSTERIOR_SSOT_FACT_SCHEMA,
            "state": "blocked",
            "state_reason": str(item.get("production_block_reason") or "joint_weak_physical_strong_model_unavailable"),
            "read_only": True,
            "fact_snapshot_id": str(item.get("fact_snapshot_id") or ""),
            "posterior_run_id": str(item.get("posterior_run_id") or ""),
            "event_date": str(item.get("event_date") or ""),
            **production_identity,
        }
    if final_lock_bucket:
        ssot_probability_rows, ssot_normalization = _posterior_ssot_final_lock_probability_rows(
            posterior_rows,
            target_bucket=final_lock_bucket,
            reason=final_lock_reason,
        )
    else:
        ssot_probability_rows, ssot_normalization = _posterior_ssot_bucket_probability_rows(
            posterior_rows,
            hard_temperature_floor_c=hard_floor_c,
        )
    target_bucket, target_probability = _top_target_from_probability_rows(
        ssot_probability_rows,
        hard_temperature_floor_c=hard_floor_c,
    )
    layer_contributions = _as_mapping(item.get("layer_contributions"))
    prior_layer = _as_mapping(layer_contributions.get("prior"))
    weak_layer = _as_mapping(layer_contributions.get("forecast_weak_likelihood"))
    strong_layer = _as_mapping(layer_contributions.get("metar_strong_likelihood"))
    forecast_weak_attribution = copy.deepcopy(_as_mapping(item.get("forecast_weak_attribution")))
    if not forecast_weak_attribution:
        forecast_weak_attribution = {
            "schema": FORECAST_WEAK_ATTRIBUTION_TRACE_SCHEMA,
            "state": "not_available",
            "read_only": True,
            "weak_likelihood_consumed": bool(weak_layer),
            "na_reasons": ["forecast_weak_runtime_attribution_not_persisted"],
            "boundaries": {
                "does_not_recompute_or_change_posterior": True,
            },
        }
    temperature_trajectory = _as_mapping(item.get("temperature_trajectory"))
    predicted_peak_time_prior = _as_mapping(item.get("predicted_peak_time_prior"))
    predicted_peak_time_posterior = _as_mapping(item.get("predicted_peak_time_posterior"))
    observed_peak_platform = _as_mapping(item.get("observed_peak_platform"))
    source_lineage = _as_mapping(item.get("source_lineage"))
    source_event_ids = _source_event_ids(trace) or [
        str(value)
        for value in (source_lineage.get("source_event_ids") or [])
        if str(value or "").strip()
    ]
    source_observation_ids = _source_observation_ids(trace) or [
        str(value)
        for value in (source_lineage.get("source_observation_ids") or [])
        if str(value or "").strip()
    ]
    ranked_rows = sorted(
        ssot_probability_rows,
        key=lambda row: float(_prob01(_as_mapping(row).get("probability")) or 0.0),
        reverse=True,
    )
    ranked_target_rows = [
        row
        for row in ranked_rows
        if _posterior_probability_row_eligible_for_target(
            row,
            hard_temperature_floor_c=hard_floor_c,
        )
    ]
    hard_constraints = _as_mapping(item.get("hard_constraints"))
    strong_physical_evidence = _strong_physical_trade_evidence(
        item,
        hard_constraints,
        ssot_probability_rows,
    )
    fact_snapshot_id = str(item.get("fact_snapshot_id") or "")
    posterior_run_id = str(item.get("posterior_run_id") or "")
    if not posterior_rows:
        return {
            "schema": POSTERIOR_SSOT_FACT_SCHEMA,
            "state": "missing",
            "state_reason": "posterior_distribution_missing",
            "read_only": True,
            "fact_snapshot_id": fact_snapshot_id,
            "posterior_run_id": posterior_run_id,
            "event_date": str(item.get("event_date") or ""),
            "source_event_ids": source_event_ids,
            "source_observation_ids": source_observation_ids,
            **production_identity,
        }
    if not target_bucket or target_probability is None:
        return {
            "schema": POSTERIOR_SSOT_FACT_SCHEMA,
            "state": "missing",
            "state_reason": "posterior_distribution_has_no_hard_observation_eligible_target",
            "read_only": True,
            "fact_snapshot_id": fact_snapshot_id,
            "posterior_run_id": posterior_run_id,
            "event_date": str(item.get("event_date") or ""),
            "source_event_ids": source_event_ids,
            "source_observation_ids": source_observation_ids,
            "hard_temperature_floor_c": hard_floor_c,
            "bucket_probabilities": ssot_probability_rows,
            "bucket_count": len(ssot_probability_rows),
            "hard_mask_normalization": ssot_normalization,
            **production_identity,
        }
    target_row = next(
        (
            _as_mapping(row)
            for row in ssot_probability_rows
            if str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
            == str(target_bucket or "").strip()
        ),
        {},
    )
    target_probability_pre_hard_mask = _prob01(target_row.get("probability_pre_hard_mask"))
    if final_lock_bucket and target_probability_pre_hard_mask is None:
        target_probability_pre_hard_mask = final_lock_pre_probability
    return {
        "schema": POSTERIOR_SSOT_FACT_SCHEMA,
        "state": "available",
        "state_reason": "",
        "read_only": True,
        "ssot_kind": "posterior_temperature_distribution",
        "source": "hierarchical_posterior_fact_payload",
        "posterior_composer_schema": str(item.get("posterior_composer_schema") or ""),
        "posterior_activation_mode": str(item.get("posterior_activation_mode") or ""),
        **production_identity,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "event_date": str(item.get("event_date") or ""),
        "posterior_as_of": str(item.get("posterior_as_of") or ""),
        "target_bucket": target_bucket,
        "target_probability": target_probability,
        "target_probability_pct": (float(target_probability) * 100.0 if target_probability is not None else None),
        "target_probability_pre_hard_mask": target_probability_pre_hard_mask,
        "target_probability_pre_hard_mask_pct": (
            float(target_probability_pre_hard_mask) * 100.0
            if target_probability_pre_hard_mask is not None
            else None
        ),
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "bucket_probabilities": ssot_probability_rows,
        "ranked_bucket_probabilities": ranked_target_rows[:12],
        "bucket_count": len(ssot_probability_rows),
        "hard_mask_normalization": ssot_normalization,
        "final_bucket_lock": bool(final_lock_bucket),
        "final_bucket_lock_reason": final_lock_reason,
        "phase": item.get("phase"),
        "beta_weak": item.get("beta_weak"),
        "beta_strong": item.get("beta_strong"),
        "phase_reason": item.get("phase_reason"),
        "hard_mask_reason": item.get("hard_mask_reason"),
        "hard_constraints_present": bool(hard_constraints),
        "hard_constraints": copy.deepcopy(dict(hard_constraints)),
        "strong_physical_evidence": strong_physical_evidence,
        "layer_chain": {
            "schema": "bayes_layer_chain_summary_v1",
            "formula": "hard_condition((1-lambda_obs)*normalize(physical_X_prior*joint_weak^beta_weak)+lambda_obs*physical_strong_X_distribution)_then_bucket_integral",
            "prior_present": bool(prior_layer),
            "forecast_weak_likelihood_present": bool(weak_layer),
            "metar_strong_likelihood_present": bool(strong_layer),
            "layer_contributions_present": bool(layer_contributions),
            "prior_boundary": str(prior_layer.get("boundary") or "historical_physical_X_climate_reference_only_no_settlement_S"),
            "forecast_weak_boundary": str(weak_layer.get("boundary") or "models_taf_wu_twc_forecasts_only_no_hard_facts"),
            "metar_strong_boundary": str(strong_layer.get("boundary") or "metar_temperature_momentum_physics_cloud_rain_state_once_no_double_count"),
        },
        "forecast_weak_attribution": forecast_weak_attribution,
        "observation_chain": {
            "schema": "bayes_observation_chain_summary_v1",
            "metar_strong_likelihood_present": bool(strong_layer),
            "environment_state_probs_present": bool(item.get("state_probs")),
            "remaining_warming_by_state_present": bool(item.get("remaining_warming_by_state")),
            "temperature_trajectory_present": bool(temperature_trajectory),
            "temperature_trajectory_state": temperature_trajectory.get("state"),
            "temperature_trajectory_gate_state": temperature_trajectory.get("gate_state"),
            "temperature_trajectory_gate_reason": temperature_trajectory.get("gate_reason"),
            "temperature_trajectory_effective_rate_c_per_hour": temperature_trajectory.get("effective_rate_c_per_hour"),
            "temperature_trajectory_adjusted_rate_c_per_hour": temperature_trajectory.get("adjusted_rate_c_per_hour"),
            "sequential_state_filter_present": bool(item.get("sequential_state_filter")),
            "canonical_metar_snapshot_present": bool(item.get("canonical_metar_snapshot")),
            "predicted_peak_time_prior_present": bool(predicted_peak_time_prior),
            "predicted_peak_time_prior": copy.deepcopy(dict(predicted_peak_time_prior)),
            "predicted_peak_time_posterior_present": bool(predicted_peak_time_posterior),
            "predicted_peak_time_posterior": copy.deepcopy(dict(predicted_peak_time_posterior)),
            "observed_peak_platform_present": bool(observed_peak_platform),
            "observed_peak_platform": copy.deepcopy(dict(observed_peak_platform)),
            "predicted_peak_window_cannot_finalize_daily_peak": True,
            "remaining_warming_mixture_present": bool(item.get("remaining_warming_mixture")),
            "observation_information_gate_present": bool(item.get("observation_information_gate")),
            "dedupe_policy": "metar_cloud_visibility_dewpoint_precip_temperature_trajectory_counted_once_in_strong_likelihood",
        },
        "lineage": {
            "schema": "bayes_posterior_ssot_fact_lineage_v1",
            "source_event_ids": source_event_ids,
            "source_observation_ids": source_observation_ids,
            "hard_observation_candidate_ids": [
                str(value)
                for value in (trace.get("hard_observation_candidate_ids") or [])
                if str(value or "").strip()
            ],
            "forecast_weak_ids": [
                str(value)
                for value in (trace.get("forecast_weak_ids") or [])
                if str(value or "").strip()
            ],
            "source_trace_schema": str(trace.get("schema") or ""),
        },
        "boundaries": {
            "old_additive_evidence_pool_used": False,
            "legacy_bucket_product_posterior_used": False,
            "live_primary_composer": str(item.get("posterior_composer_schema") or ""),
            "posterior_activation_mode": str(item.get("posterior_activation_mode") or ""),
            "shadow_posterior_emitted": False,
            "frontend_recompute_allowed": False,
            "decision_recompute_allowed": False,
            "weak_likelihood_is_prediction_layer": True,
            "strong_likelihood_is_observation_layer": True,
            "forecast_weak_attribution_present": forecast_weak_attribution.get("state") == "available",
            "hard_mask_is_constraint_not_weight": True,
            "write_owner": WRITE_OWNER,
        },
        "normalization": copy.deepcopy(dict(_as_mapping(item.get("normalization")))),
        "na_reasons": copy.deepcopy(item.get("na_reasons") or []),
    }


def _fast_trade_latest_should_hold(
    *,
    existing_payload: Mapping[str, Any],
    incoming_payload: Mapping[str, Any],
    existing_awc_features: Mapping[str, Any],
    incoming_awc_features: Mapping[str, Any],
) -> bool:
    if not _payload_fast_trade_latest(existing_payload):
        return False
    if _payload_fast_trade_latest(incoming_payload):
        return False
    if _payload_hard_rank(existing_payload) < 1:
        return False
    incoming_awc_max_stronger = (
        _candidate_today_max_is_stronger(incoming_awc_features, existing_awc_features)
        if existing_awc_features and incoming_awc_features
        else False
    )
    if incoming_awc_max_stronger:
        return False
    existing_awc_latest = _feature_ts(existing_awc_features, "zuuu_metar_raw_awc_latest_at") if existing_awc_features else None
    incoming_awc_latest = _feature_ts(incoming_awc_features, "zuuu_metar_raw_awc_latest_at") if incoming_awc_features else None
    if existing_awc_latest is not None and incoming_awc_latest is not None and incoming_awc_latest > existing_awc_latest:
        return False
    existing_label, existing_prob = _payload_target_probability(existing_payload)
    incoming_label, incoming_prob = _payload_target_probability(incoming_payload)
    if not existing_label or not incoming_label or existing_prob is None or incoming_prob is None:
        return False
    if existing_label != incoming_label:
        return False
    return abs(float(existing_prob) - float(incoming_prob)) <= 1e-9


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


SOURCE_OBSERVATION_PERSIST_COLUMNS: tuple[str, ...] = (
    "source_event_id",
    "source_observation_id",
    "source_id",
    "source_class",
    "role",
    "station_id",
    "event_date",
    "valid_time",
    "valid_time_local",
    "fetched_at",
    "parsed_at",
    "response_completed_at",
    "response_duration_ms",
    "temperature_c",
    "dewpoint_c",
    "observed_today_max_c",
    "forecast_peak_c",
    "raw_ref",
    "raw_sha256",
    "parser_version",
    "hard_eligible",
    "demotion_reason",
    "quality_flags_json",
    "observation_json",
    "created_at",
)

SOURCE_OBSERVATION_IMMUTABLE_COLUMNS: tuple[str, ...] = (
    "source_event_id",
    "source_observation_id",
    "source_id",
    "source_class",
    "role",
    "station_id",
    "event_date",
    "valid_time",
    "valid_time_local",
    "fetched_at",
    "response_completed_at",
    "response_duration_ms",
    "temperature_c",
    "dewpoint_c",
    "observed_today_max_c",
    "forecast_peak_c",
    "raw_ref",
    "raw_sha256",
    "parser_version",
    "hard_eligible",
    "demotion_reason",
    "quality_flags_json",
)

# ``parsed_at`` is a local adapter-processing timestamp, not an upstream
# observation or transport fact. Rebuilding a posterior source overlay can
# legitimately parse the same immutable source event again at a later local
# time. The table keeps the original value but must not fail-close a posterior
# commit solely because this ephemeral field changed. ``source_observation_id``
# identifies the normalized physical report; a ``source_event_id`` additionally
# carries transport receipt time, so a later retrieval of the identical report
# legitimately has a different event id and different transport timestamps.
# The table keeps the first durable receipt for a physical observation; it is
# not a transport-log table.
SOURCE_OBSERVATION_PHYSICAL_IMMUTABLE_COLUMNS: tuple[str, ...] = tuple(
    column
    for column in SOURCE_OBSERVATION_IMMUTABLE_COLUMNS
    if column
    not in {
        "source_event_id",
        "fetched_at",
        "parsed_at",
        "response_completed_at",
        "response_duration_ms",
    }
)


def _source_observation_candidates_for_persistence(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(raw: Any) -> None:
        row = dict(_as_mapping(raw))
        if not row:
            return
        source_event_id = str(row.get("source_event_id") or "").strip()
        source_observation_id = str(row.get("source_observation_id") or "").strip()
        if not source_event_id:
            return
        key = (source_event_id, source_observation_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append(row)

    for raw in payload.get("source_observations") or []:
        add(raw)
    trace = _as_mapping(payload.get("source_acquisition_trace"))
    for raw in trace.get("source_observations") or []:
        add(raw)
    return candidates


def _source_observation_persist_values(raw: Mapping[str, Any], *, created_at: str) -> dict[str, Any]:
    payload_value = raw.get("payload")
    quality_flags = raw.get("quality_flags")
    if not isinstance(quality_flags, Sequence) or isinstance(quality_flags, (str, bytes, bytearray)):
        quality_flags = []
    values: dict[str, Any] = {
        "source_event_id": str(raw.get("source_event_id") or "").strip(),
        "source_observation_id": str(raw.get("source_observation_id") or "").strip(),
        "source_id": str(raw.get("source_id") or "").strip(),
        "source_class": str(raw.get("source_class") or "").strip(),
        "role": str(raw.get("role") or raw.get("source_role") or "").strip(),
        "station_id": str(raw.get("station_id") or raw.get("station") or raw.get("icao") or "").strip(),
        "event_date": str(raw.get("event_date") or "").strip(),
        "valid_time": str(raw.get("valid_time") or raw.get("valid_time_utc") or "").strip(),
        "valid_time_local": str(raw.get("valid_time_local") or "").strip(),
        "fetched_at": str(raw.get("fetched_at") or "").strip(),
        "parsed_at": str(raw.get("parsed_at") or "").strip(),
        "response_completed_at": str(raw.get("response_completed_at") or "").strip(),
        "response_duration_ms": _float_or_none(raw.get("response_duration_ms")),
        "temperature_c": _float_or_none(raw.get("temperature_c") if raw.get("temperature_c") is not None else raw.get("temp_c")),
        "dewpoint_c": _float_or_none(raw.get("dewpoint_c") if raw.get("dewpoint_c") is not None else raw.get("dew_point_c")),
        "observed_today_max_c": _float_or_none(raw.get("observed_today_max_c") if raw.get("observed_today_max_c") is not None else raw.get("today_max_c")),
        "forecast_peak_c": _float_or_none(raw.get("forecast_peak_c") if raw.get("forecast_peak_c") is not None else raw.get("taf_forecast_peak_c")),
        "raw_ref": str(raw.get("raw_ref") or "").strip(),
        "raw_sha256": str(raw.get("raw_sha256") or "").strip(),
        "parser_version": str(raw.get("parser_version") or "").strip(),
        "hard_eligible": 1 if bool(raw.get("hard_eligible")) else 0,
        "demotion_reason": str(raw.get("demotion_reason") or "").strip(),
        "quality_flags_json": json.dumps(list(quality_flags), ensure_ascii=False, separators=(",", ":"), default=str),
        "observation_json": json.dumps(dict(raw), ensure_ascii=False, separators=(",", ":"), default=str),
        "created_at": str(created_at or utc_now_iso()),
    }
    if isinstance(payload_value, Mapping) and not values["raw_ref"]:
        values["raw_ref"] = str(payload_value.get("raw_ref") or payload_value.get("raw_text") or "").strip()
    return values


def _source_observation_values_equivalent(left: Any, right: Any) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    left_float = _float_or_none(left)
    right_float = _float_or_none(right)
    if left_float is not None and right_float is not None:
        return abs(left_float - right_float) <= 1e-12
    return str(left or "").strip() == str(right or "").strip()


def _existing_source_observation_rows(
    conn: sqlite3.Connection,
    *,
    table_columns: set[str],
    source_event_id: str,
    source_observation_id: str,
) -> list[dict[str, Any]]:
    selected = [column for column in SOURCE_OBSERVATION_IMMUTABLE_COLUMNS if column in table_columns]
    if not selected:
        return []
    where = ["source_event_id=?"]
    params: list[Any] = [source_event_id]
    if source_observation_id and "source_observation_id" in table_columns:
        where.append("source_observation_id=?")
        params.append(source_observation_id)
    selected_sql = ",".join(f'"{column}"' for column in selected)
    rows = conn.execute(
        f"SELECT {selected_sql} FROM source_observations WHERE {' OR '.join(where)}",
        tuple(params),
    ).fetchall()
    return [dict(zip(selected, row)) for row in rows]


def _source_observation_replay_drift(
    existing_rows: Sequence[Mapping[str, Any]],
    incoming: Mapping[str, Any],
    *,
    table_columns: set[str],
    comparison_columns: Sequence[str] = SOURCE_OBSERVATION_IMMUTABLE_COLUMNS,
) -> list[dict[str, Any]]:
    """Detect contradictory immutable replay; omitted compact fields never weaken a stored row."""

    conflicts: list[dict[str, Any]] = []
    for existing in existing_rows:
        differing_fields: list[str] = []
        for column in comparison_columns:
            if column not in table_columns:
                continue
            incoming_value = incoming.get(column)
            existing_value = existing.get(column)
            if incoming_value in (None, ""):
                continue
            if not _source_observation_values_equivalent(existing_value, incoming_value):
                differing_fields.append(column)
        if differing_fields:
            conflicts.append(
                {
                    "existing_source_event_id": str(existing.get("source_event_id") or ""),
                    "existing_source_observation_id": str(existing.get("source_observation_id") or ""),
                    "differing_immutable_fields": sorted(differing_fields),
                }
            )
    return conflicts


def _source_observation_existing_conflicts(
    existing_rows: Sequence[Mapping[str, Any]],
    incoming: Mapping[str, Any],
    *,
    table_columns: set[str],
) -> tuple[list[dict[str, Any]], bool]:
    """Return immutable conflicts and whether a stable physical replay was seen.

    An exact source-event replay must match every persisted immutable field.
    A different source event sharing ``source_observation_id`` is a later
    transport receipt of the same physical report: compare only physical
    fields and retain the original first-seen row if they match.
    """

    source_event_id = str(incoming.get("source_event_id") or "").strip()
    source_observation_id = str(incoming.get("source_observation_id") or "").strip()
    event_matches = [
        row
        for row in existing_rows
        if str(row.get("source_event_id") or "").strip() == source_event_id
    ]
    observation_matches = [
        row
        for row in existing_rows
        if source_observation_id
        and str(row.get("source_observation_id") or "").strip() == source_observation_id
    ]
    conflicts: list[dict[str, Any]] = []
    if event_matches:
        conflicts.extend(
            _source_observation_replay_drift(
                event_matches,
                incoming,
                table_columns=table_columns,
            )
        )
    physical_replay = False
    for row in observation_matches:
        if row in event_matches:
            continue
        row_conflicts = _source_observation_replay_drift(
            [row],
            incoming,
            table_columns=table_columns,
            comparison_columns=SOURCE_OBSERVATION_PHYSICAL_IMMUTABLE_COLUMNS,
        )
        if row_conflicts:
            conflicts.extend(row_conflicts)
        else:
            physical_replay = True
    return conflicts, physical_replay


def _persist_source_observations_for_fact_payload(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    created_at: str,
) -> dict[str, Any]:
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_observations'"
        ).fetchone() is not None
    except sqlite3.Error:
        exists = False
    if not exists:
        return {
            "schema": "source_observation_persistence_v1",
            "ok": True,
            "state": "table_missing",
            "attempted": 0,
            "inserted": 0,
            "ignored": 0,
            "missing_columns": [],
        }
    table_columns = set(_table_columns(conn, "source_observations"))
    rows = _source_observation_candidates_for_persistence(payload)
    required_existing = {"source_event_id", "source_observation_id"}
    if not required_existing.issubset(table_columns):
        return {
            "schema": "source_observation_persistence_v1",
            "ok": True,
            "state": "schema_incomplete_no_write",
            "attempted": 0,
            "inserted": 0,
            "ignored": 0,
            "missing_columns": sorted(required_existing - table_columns),
            "source_observation_id_column_present": "source_observation_id" in table_columns,
            "write_policy": "skip_until_source_observation_id_schema_present",
        }
    writable_columns = [col for col in SOURCE_OBSERVATION_PERSIST_COLUMNS if col in table_columns]
    missing_columns = [col for col in SOURCE_OBSERVATION_PERSIST_COLUMNS if col not in table_columns]
    inserted = 0
    attempted = 0
    transport_replays_ignored = 0
    replay_drift: list[dict[str, Any]] = []
    if rows and writable_columns:
        quoted_columns = ",".join(f'"{col}"' for col in writable_columns)
        placeholders = ",".join("?" for _ in writable_columns)
        sql = f'INSERT OR IGNORE INTO source_observations({quoted_columns}) VALUES ({placeholders})'
        for raw in rows:
            values = _source_observation_persist_values(raw, created_at=created_at)
            if not values.get("source_event_id"):
                continue
            attempted += 1
            existing = _existing_source_observation_rows(
                conn,
                table_columns=table_columns,
                source_event_id=values["source_event_id"],
                source_observation_id=str(values.get("source_observation_id") or ""),
            )
            conflicts, physical_replay = _source_observation_existing_conflicts(
                existing,
                values,
                table_columns=table_columns,
            )
            if conflicts:
                replay_drift.extend(conflicts)
                continue
            if existing or physical_replay:
                if physical_replay:
                    transport_replays_ignored += 1
                continue
            cur = conn.execute(sql, tuple(values.get(col) for col in writable_columns))
            if int(cur.rowcount or 0) > 0:
                inserted += 1
                continue
            # A concurrent append can win between the pre-read and INSERT OR
            # IGNORE. Re-read retained immutable identity before accepting the
            # idempotent result, so a collision never becomes a silent skip.
            existing_after = _existing_source_observation_rows(
                conn,
                table_columns=table_columns,
                source_event_id=values["source_event_id"],
                source_observation_id=str(values.get("source_observation_id") or ""),
            )
            conflicts_after, physical_replay_after = _source_observation_existing_conflicts(
                existing_after,
                values,
                table_columns=table_columns,
            )
            if conflicts_after:
                replay_drift.extend(conflicts_after)
            elif physical_replay_after:
                transport_replays_ignored += 1
    if replay_drift:
        return {
            "schema": "source_observation_persistence_v1",
            "ok": False,
            "state": "immutable_replay_drift",
            "attempted": attempted,
            "inserted": inserted,
            "ignored": max(0, attempted - inserted),
            "transport_replays_ignored": transport_replays_ignored,
            "replay_drift": replay_drift,
            "missing_columns": missing_columns,
            "source_observation_id_column_present": "source_observation_id" in table_columns,
            "write_policy": "insert_or_ignore_no_overwrite_but_immutable_drift_fails_closed",
        }
    return {
        "schema": "source_observation_persistence_v1",
        "ok": True,
        "state": "available",
        "attempted": attempted,
        "inserted": inserted,
        "ignored": max(0, attempted - inserted),
        "transport_replays_ignored": transport_replays_ignored,
        "missing_columns": missing_columns,
        "source_observation_id_column_present": "source_observation_id" in table_columns,
        "write_policy": "first_physical_observation_retained_transport_replays_ignored_immutable_drift_fails_closed",
    }


def persist_source_observations_for_payload(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    created_at: str,
) -> dict[str, Any]:
    """Append immutable source observations without materializing a fact.

    This is the narrow public boundary used by the AMSC probability fast
    lane.  The caller owns the transaction.  Keeping this operation separate
    prevents the large legacy fact projection from sitting in front of
    CanonicalObservationEvent -> posterior SSOT while retaining the exact
    replay-drift and first-physical-receipt rules used by the fact writer.
    """

    return _persist_source_observations_for_fact_payload(
        conn,
        payload,
        created_at=created_at,
    )


def _best_edge_from_payload_rows(rows: Any) -> tuple[str, str, float | None, float | None]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return "", "", None, None
    best: tuple[str, str, float | None, float | None] = ("", "", None, None)
    for row in rows:
        row_map = _as_mapping(row)
        if not row_map:
            continue
        current_best_edge = best[2]
        outcome = str(row_map.get("outcome") or row_map.get("label") or row_map.get("bucket") or "")
        if not outcome:
            continue
        yes_score = _float_or_none(row_map.get("edge_score_yes"))
        if yes_score is None:
            yes_score = _float_or_none(row_map.get("edge_score"))
        no_score = _float_or_none(row_map.get("edge_score_no"))
        if yes_score is not None and (current_best_edge is None or yes_score > current_best_edge):
            best = (
                "YES",
                outcome,
                yes_score,
                _float_or_none(row_map.get("signal_edge_min"))
                if _float_or_none(row_map.get("signal_edge_min")) is not None
                else _float_or_none(row_map.get("signal_edge_entry")),
            )
            current_best_edge = yes_score
        if no_score is not None and (current_best_edge is None or no_score > current_best_edge):
            best = (
                "NO",
                outcome,
                no_score,
                _float_or_none(row_map.get("signal_edge_min_no"))
                if _float_or_none(row_map.get("signal_edge_min_no")) is not None
                else _float_or_none(row_map.get("signal_edge_entry_no")),
            )
            current_best_edge = no_score
    return best


def _summary_cache_payload_from_effective_payload(
    payload: Mapping[str, Any],
    *,
    fact_id: int,
    slug: str,
    event_date: str,
    run_date: str,
    fact_as_of: str,
) -> dict[str, Any]:
    summary = copy.deepcopy(dict(payload))
    summary["slug"] = slug
    summary["event_date"] = event_date
    summary["run_date"] = run_date
    summary["fact_as_of"] = fact_as_of
    summary["fact_id"] = int(fact_id)
    summary["summary_cache_source"] = WRITE_OWNER
    summary["summary_cache_schema"] = "weather_fact_snapshot_writer_summary_cache_v1"
    summary["summary_cache_version"] = SUMMARY_CACHE_COMPAT_VERSION
    if bool(payload.get("awc_hard_observation_fast_trade_write") or payload.get("posterior_target_direct_fast_trade_write")):
        summary["fast_trade_summary_cache"] = True
    return summary


def build_fact_summary_cache_repair_plan(
    *,
    fact_id: int,
    slug: str,
    event_date: str,
    run_date: str = "",
    fact_as_of: str = "",
    payload: Mapping[str, Any] | None = None,
    write_reason: str = "summary_cache_repair",
    enable_apply_contract: bool = False,
) -> dict[str, Any]:
    effective_payload = copy.deepcopy(dict(payload or {}))
    effective_payload.setdefault("slug", slug)
    effective_payload.setdefault("event_date", event_date)
    effective_payload.setdefault("run_date", run_date or event_date)
    effective_payload.setdefault("fact_as_of", fact_as_of)
    payload_hash = sha256_json(effective_payload)
    return {
        "schema": SUMMARY_CACHE_REPAIR_PLAN_SCHEMA,
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "apply_supported": bool(enable_apply_contract),
        "execution_mode": (
            "explicit_apply_contract_ready_summary_cache_repair_plan"
            if enable_apply_contract
            else "no_apply_summary_cache_repair_plan"
        ),
        "write_owner": WRITE_OWNER,
        "target_tables": ["event_fact_summary_cache"],
        "fact_id": int(fact_id or 0),
        "slug": str(slug or ""),
        "event_date": str(event_date or ""),
        "run_date": str(run_date or event_date or ""),
        "fact_as_of": str(fact_as_of or ""),
        "write_reason": str(write_reason or ""),
        "payload_hash_after": payload_hash,
        "effective_payload": effective_payload,
        "plan_actions": [
            {
                "action": "upsert_event_fact_summary_cache",
                "target_table": "event_fact_summary_cache",
                "would_write": True,
                "write_owner": WRITE_OWNER,
                "apply_status": "ready_for_explicit_apply" if enable_apply_contract else "blocked_no_apply",
            }
        ],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql": False,
        },
    }


def apply_fact_summary_cache_repair_plan(
    conn: sqlite3.Connection | None,
    plan: Mapping[str, Any],
    *,
    allow_apply: bool = False,
    commit: bool = False,
    created_at: str = "",
) -> dict[str, Any]:
    plan_map = _as_mapping(plan)
    if not allow_apply:
        return _blocked_apply_result(plan_map, reason="allow_apply_false", allow_apply=allow_apply)
    if conn is None:
        return _blocked_apply_result(plan_map, reason="missing_sqlite_connection", allow_apply=allow_apply)
    if plan_map.get("schema") != SUMMARY_CACHE_REPAIR_PLAN_SCHEMA:
        return _blocked_apply_result(plan_map, reason="invalid_plan_schema", allow_apply=allow_apply)
    if not bool(plan_map.get("apply_supported")):
        return _blocked_apply_result(plan_map, reason="plan_apply_contract_not_enabled", allow_apply=allow_apply)
    payload = plan_map.get("effective_payload")
    if not isinstance(payload, Mapping):
        return _blocked_apply_result(plan_map, reason="effective_payload_missing", allow_apply=allow_apply)
    expected_hash = str(plan_map.get("payload_hash_after") or "")
    if expected_hash and sha256_json(payload) != expected_hash:
        return _blocked_apply_result(plan_map, reason="effective_payload_hash_mismatch", allow_apply=allow_apply)
    fact_id = int(plan_map.get("fact_id") or 0)
    if fact_id <= 0:
        return _blocked_apply_result(plan_map, reason="fact_id_missing", allow_apply=allow_apply)
    slug = str(plan_map.get("slug") or payload.get("slug") or "")
    event_date = _normalize_date_key(plan_map.get("event_date") or payload.get("event_date") or "")
    run_date = _canonical_fact_run_date(
        slug=slug,
        event_date=event_date,
        run_date=plan_map.get("run_date") or payload.get("run_date") or event_date,
    )
    fact_as_of = str(plan_map.get("fact_as_of") or payload.get("fact_as_of") or utc_now_iso())
    created_at = str(created_at or utc_now_iso())
    summary_payload = _summary_cache_payload_from_effective_payload(
        payload,
        fact_id=fact_id,
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
    )
    savepoint = "weather_fact_summary_cache_repair_apply"
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
        conn.execute(
            """
            INSERT OR REPLACE INTO event_fact_summary_cache(
                fact_id, slug, event_date, run_date, fact_as_of, summary_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_id,
                slug,
                event_date,
                run_date,
                fact_as_of,
                json.dumps(summary_payload, ensure_ascii=False, separators=(",", ":"), default=str),
                created_at,
            ),
        )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            conn.commit()
        _touch_ui_snapshot_dirty_stamp(f"event_fact_summary_cache:{slug}:{event_date}:{fact_id}")
        return {
            "schema": APPLY_RESULT_SCHEMA_VERSION,
            "ok": True,
            "applied": True,
            "blocked": False,
            "allow_apply": True,
            "write_owner": WRITE_OWNER,
            "fact_id": fact_id,
            "latest_repointed": False,
            "summary_cache_written": True,
            "actions_applied": [{"action": "upsert_event_fact_summary_cache", "fact_id": fact_id}],
            "guards": {
                "writes_archive_db": True,
                "writes_facts_sqlite": False,
                "writes_event_fact_history": False,
                "writes_event_fact_latest": False,
                "writes_event_fact_summary_cache": True,
                "writes_auto_trade_decisions": False,
                "places_orders": False,
                "restarts_launchd": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "executes_sql": True,
            },
            "commit": bool(commit),
            "created_at": created_at,
        }
    except sqlite3.Error as exc:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass
        result = _blocked_apply_result(plan_map, reason="sqlite_error", allow_apply=allow_apply)
        result["error"] = str(exc)
        return result


def _compact_distribution_row_for_history(raw_row: Any) -> dict[str, Any]:
    row = _as_mapping(raw_row)
    if not row:
        return {}
    keep_keys = (
        "outcome",
        "label",
        "bucket",
        "q_temperature_bucket_posterior",
        "q_bayes_posterior",
        "q_contract_payout_posterior",
        "q_entry",
        "q_round",
        "edge_score_yes",
        "edge_score_no",
        "target_bucket_probability",
        "hard_source_observed_value_c",
        "observed_today_max_c",
        "zuuu_metar_raw_awc_today_max_c",
    )
    return {key: copy.deepcopy(row.get(key)) for key in keep_keys if _non_empty(row.get(key))}


def _compact_fact_payload_for_history_storage(
    payload: Mapping[str, Any],
    *,
    fact_as_of: str = "",
    compacted_at: str = "",
) -> dict[str, Any]:
    item = _as_mapping(payload)
    keep_keys = (
        "slug",
        "event_date",
        "run_date",
        "title",
        "city",
        "market_unit",
        "station_id",
        "market_var",
        "target_bucket",
        "ssot_target_bucket",
        "fact_snapshot_id",
        "posterior_run_id",
        "correlation_id",
        "source_event_ids",
        "phase",
        "beta_weak",
        "beta_strong",
        "phase_reason",
        "hard_mask_reason",
        "posterior_ssot_fact",
        "posterior_target_bucket_selection",
        "target_bucket_selection",
        "model_top_label",
        "single_temperature_top_label",
        "atomic_temperature_top_label",
        "contract_payout_top_label",
        "hard_source_direct",
        "hard_source_direct_ssot_write",
        "hard_source_observed_value_c",
        "hard_source_observed_source",
        "observed_today_max_c",
        "observed_today_max_source",
        "weather_likelihood_features_compact",
        "fact_provenance",
    )
    compact = {key: copy.deepcopy(item.get(key)) for key in keep_keys if _non_empty(item.get(key))}
    distribution = item.get("distribution")
    if isinstance(distribution, Sequence) and not isinstance(distribution, (str, bytes, bytearray)):
        rows = [_compact_distribution_row_for_history(row) for row in distribution]
        rows = [row for row in rows if row]
        if rows:
            compact["distribution"] = rows
    if fact_as_of and not compact.get("fact_as_of"):
        compact["fact_as_of"] = fact_as_of
    compact["history_compacted"] = True
    compact["history_compaction_version"] = HISTORY_FACT_COMPACTION_VERSION
    compact["history_compacted_at"] = compacted_at or utc_now_iso()
    return compact


def compact_replaced_event_fact_history_row(
    conn: sqlite3.Connection | None,
    fact_id: int,
    *,
    fact_as_of: str = "",
    fact_json: str = "",
) -> bool:
    old_fact_id = int(fact_id or 0)
    if conn is None or old_fact_id <= 0:
        return False
    try:
        if conn.execute("SELECT 1 FROM event_fact_latest WHERE fact_id = ? LIMIT 1", (old_fact_id,)).fetchone():
            return False
    except sqlite3.Error:
        return False
    raw = str(fact_json or "")
    old_as_of = str(fact_as_of or "")
    if not raw:
        try:
            row = conn.execute(
                "SELECT fact_as_of, fact_json FROM event_fact_history WHERE id = ?",
                (old_fact_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if not row:
            return False
        old_as_of = old_as_of or str(row[0] or "")
        raw = str(row[1] or "")
    payload = _json_loads_map(raw)
    if not payload or bool(payload.get("history_compacted")):
        return False
    compact = _compact_fact_payload_for_history_storage(payload, fact_as_of=old_as_of)
    compact_json = json.dumps(compact, ensure_ascii=False)
    if len(compact_json) >= len(raw):
        return False
    try:
        cur = conn.execute(
            """
            UPDATE event_fact_history
            SET fact_json = ?
            WHERE id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM event_fact_latest
                  WHERE event_fact_latest.fact_id = event_fact_history.id
              )
            """,
            (compact_json, old_fact_id),
        )
    except sqlite3.Error:
        return False
    return int(cur.rowcount or 0) > 0


def build_fact_latest_repoint_plan(
    conn: sqlite3.Connection | None,
    *,
    slug: str,
    event_date: str,
    fact_id: int,
    source_acquisition_trace: Mapping[str, Any] | None = None,
    enable_apply_contract: bool = False,
    include_payload: bool = False,
) -> dict[str, Any]:
    slug = str(slug or "")
    event_date = str(event_date or "")
    fact_id_int = int(fact_id or 0)
    incoming_fact_as_of = ""
    incoming_payload: dict[str, Any] = {}
    incoming_model_snapshot_id: int | None = None
    incoming_fact_snapshot_id = ""
    incoming_correlation_id = ""
    existing_fact_id = 0
    existing_fact_as_of = ""
    existing_fact_json = ""
    existing_payload: dict[str, Any] = {}
    existing_model_snapshot_id: int | None = None
    read_error = ""
    if conn is not None and fact_id_int > 0:
        try:
            incoming_row = conn.execute(
                """
                SELECT fact_as_of, fact_json, model_snapshot_id, fact_snapshot_id, correlation_id
                FROM event_fact_history
                WHERE id = ?
                """,
                (fact_id_int,),
            ).fetchone()
        except sqlite3.Error as exc:
            incoming_row = None
            read_error = str(exc)
        if incoming_row:
            incoming_fact_as_of = str(incoming_row[0] or "")
            incoming_payload = _json_loads_map(incoming_row[1])
            incoming_model_snapshot_id = _int_or_none(incoming_row[2]) if len(incoming_row) > 2 else None
            incoming_fact_snapshot_id = str(incoming_row[3] or "") if len(incoming_row) > 3 else ""
            incoming_correlation_id = str(incoming_row[4] or "") if len(incoming_row) > 4 else ""
            if not incoming_fact_snapshot_id:
                incoming_fact_snapshot_id = str(incoming_payload.get("fact_snapshot_id") or "")
            if not incoming_correlation_id:
                incoming_correlation_id = str(_as_mapping(incoming_payload.get("lineage_state")).get("correlation_id") or "")
        try:
            existing_row = conn.execute(
                """
                SELECT l.fact_id, h.fact_as_of, h.fact_json, h.model_snapshot_id
                FROM event_fact_latest l
                JOIN event_fact_history h ON h.id = l.fact_id
                WHERE l.slug = ? AND l.event_date = ?
                """,
                (slug, event_date),
            ).fetchone()
        except sqlite3.Error as exc:
            existing_row = None
            read_error = read_error or str(exc)
        if existing_row:
            existing_fact_id = int(existing_row[0] or 0)
            existing_fact_as_of = str(existing_row[1] or "")
            existing_fact_json = str(existing_row[2] or "")
            existing_payload = _json_loads_map(existing_fact_json)
            existing_model_snapshot_id = _int_or_none(existing_row[3]) if len(existing_row) > 3 else None
    policy = evaluate_event_fact_latest_repoint_policy(
        existing_fact_as_of=existing_fact_as_of,
        current_payload=existing_payload,
        incoming_fact_as_of=incoming_fact_as_of,
        incoming_payload=incoming_payload,
        existing_model_snapshot_id=existing_model_snapshot_id,
        incoming_model_snapshot_id=incoming_model_snapshot_id,
        source_acquisition_trace=source_acquisition_trace,
    )
    missing_incoming = fact_id_int <= 0 or not incoming_payload
    latest_repoint_allowed = bool(policy.get("allowed")) and not missing_incoming and not read_error
    block_reason = str(policy.get("block_reason") or "")
    if missing_incoming:
        block_reason = "incoming_fact_missing"
    if read_error:
        block_reason = "sqlite_read_error"
    plan: dict[str, Any] = {
        "schema": LATEST_REPOINT_PLAN_SCHEMA,
        "ok": not bool(read_error) and not missing_incoming,
        "read_only": True,
        "dry_run": True,
        "apply_supported": bool(enable_apply_contract),
        "execution_mode": (
            "explicit_apply_contract_ready_fact_latest_repoint_plan"
            if enable_apply_contract
            else "no_apply_fact_latest_repoint_plan"
        ),
        "write_owner": WRITE_OWNER,
        "target_tables": ["event_fact_latest", "event_fact_history"],
        "slug": slug,
        "event_date": event_date,
        "fact_id": fact_id_int,
        "fact_snapshot_id": incoming_fact_snapshot_id,
        "correlation_id": incoming_correlation_id,
        "incoming_fact_as_of": incoming_fact_as_of,
        "existing_fact_id": existing_fact_id,
        "existing_fact_as_of": existing_fact_as_of,
        "existing_fact_json": existing_fact_json if include_payload else "",
        "incoming_payload_hash": sha256_json(incoming_payload) if incoming_payload else "",
        "existing_payload_hash": sha256_json(existing_payload) if existing_payload else "",
        "latest_repoint_allowed": latest_repoint_allowed,
        "latest_repoint_block_reason": "" if latest_repoint_allowed else block_reason,
        "latest_repoint_policy": policy,
        "source_acquisition_trace": copy.deepcopy(dict(source_acquisition_trace or {})),
        "plan_actions": [
            {
                "action": "repoint_event_fact_latest",
                "target_table": "event_fact_latest",
                "would_write": latest_repoint_allowed,
                "write_owner": WRITE_OWNER,
                "apply_status": (
                    "ready_for_explicit_apply"
                    if enable_apply_contract and latest_repoint_allowed
                    else "blocked_no_apply"
                    if latest_repoint_allowed
                    else "blocked_policy"
                ),
                "block_reason": "" if latest_repoint_allowed else block_reason,
            },
            {
                "action": "compact_replaced_event_fact_history",
                "target_table": "event_fact_history",
                "would_write": bool(latest_repoint_allowed and existing_fact_id and existing_fact_id != fact_id_int),
                "write_owner": WRITE_OWNER,
                "apply_status": (
                    "ready_for_explicit_apply"
                    if enable_apply_contract and latest_repoint_allowed and existing_fact_id and existing_fact_id != fact_id_int
                    else "not_applicable"
                ),
            },
        ],
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql": False,
        },
    }
    if include_payload:
        plan["incoming_payload"] = incoming_payload
        plan["existing_payload"] = existing_payload
    if read_error:
        plan["read_error"] = read_error
    return plan


def apply_fact_latest_repoint_plan(
    conn: sqlite3.Connection | None,
    plan: Mapping[str, Any],
    *,
    allow_apply: bool = False,
    commit: bool = False,
    updated_at: str = "",
) -> dict[str, Any]:
    plan_map = _as_mapping(plan)
    if not allow_apply:
        return _blocked_apply_result(plan_map, reason="allow_apply_false", allow_apply=allow_apply)
    if conn is None:
        return _blocked_apply_result(plan_map, reason="missing_sqlite_connection", allow_apply=allow_apply)
    if plan_map.get("schema") != LATEST_REPOINT_PLAN_SCHEMA:
        return _blocked_apply_result(plan_map, reason="invalid_plan_schema", allow_apply=allow_apply)
    if not bool(plan_map.get("apply_supported")):
        return _blocked_apply_result(plan_map, reason="plan_apply_contract_not_enabled", allow_apply=allow_apply)
    if not bool(plan_map.get("latest_repoint_allowed")):
        return {
            "schema": APPLY_RESULT_SCHEMA_VERSION,
            "ok": True,
            "applied": False,
            "blocked": False,
            "block_reason": str(plan_map.get("latest_repoint_block_reason") or "latest_repoint_policy_blocked"),
            "allow_apply": True,
            "write_owner": WRITE_OWNER,
            "fact_id": int(plan_map.get("fact_id") or 0),
            "latest_repointed": False,
            "summary_cache_written": False,
            "actions_applied": [],
            "guards": {
                "writes_archive_db": False,
                "writes_facts_sqlite": False,
                "writes_event_fact_history": False,
                "writes_event_fact_latest": False,
                "writes_event_fact_summary_cache": False,
                "writes_auto_trade_decisions": False,
                "places_orders": False,
                "restarts_launchd": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "executes_sql": False,
            },
        }
    slug = str(plan_map.get("slug") or "")
    event_date = str(plan_map.get("event_date") or "")
    fact_id = int(plan_map.get("fact_id") or 0)
    if not slug or not event_date or fact_id <= 0:
        return _blocked_apply_result(plan_map, reason="slug_event_date_or_fact_id_missing", allow_apply=allow_apply)
    updated_at = str(updated_at or utc_now_iso())
    actions_applied: list[dict[str, Any]] = []
    savepoint = "weather_fact_latest_repoint_apply"
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
        latest_values = {
            "slug": slug,
            "event_date": event_date,
            "fact_id": fact_id,
            "updated_at": updated_at,
            "fact_snapshot_id": str(plan_map.get("fact_snapshot_id") or ""),
            "correlation_id": str(plan_map.get("correlation_id") or ""),
        }
        latest_columns = [
            col
            for col in _table_columns(conn, "event_fact_latest")
            if col in latest_values
        ]
        latest_column_sql = ",".join(latest_columns)
        latest_placeholders = ",".join("?" for _ in latest_columns)
        latest_update_columns = [col for col in latest_columns if col not in {"slug", "event_date"}]
        latest_update_sql = ", ".join(f"{col}=excluded.{col}" for col in latest_update_columns)
        conn.execute(
            f"""
            INSERT INTO event_fact_latest({latest_column_sql})
            VALUES({latest_placeholders})
            ON CONFLICT(slug, event_date) DO UPDATE SET
                {latest_update_sql}
            """,
            tuple(latest_values[col] for col in latest_columns),
        )
        actions_applied.append({"action": "repoint_event_fact_latest", "fact_id": fact_id})
        compacted = False
        existing_fact_id = int(plan_map.get("existing_fact_id") or 0)
        if existing_fact_id and existing_fact_id != fact_id:
            compacted = compact_replaced_event_fact_history_row(
                conn,
                existing_fact_id,
                fact_as_of=str(plan_map.get("existing_fact_as_of") or ""),
                fact_json=str(plan_map.get("existing_fact_json") or ""),
            )
            if compacted:
                actions_applied.append({"action": "compact_replaced_event_fact_history", "fact_id": existing_fact_id})
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            conn.commit()
        _touch_ui_snapshot_dirty_stamp(f"event_fact_latest:{slug}:{event_date}:{fact_id}")
        return {
            "schema": APPLY_RESULT_SCHEMA_VERSION,
            "ok": True,
            "applied": True,
            "blocked": False,
            "allow_apply": True,
            "write_owner": WRITE_OWNER,
            "fact_id": fact_id,
            "latest_repointed": True,
            "history_compacted": compacted,
            "summary_cache_written": False,
            "actions_applied": actions_applied,
            "guards": {
                "writes_archive_db": True,
                "writes_facts_sqlite": False,
                "writes_event_fact_history": bool(compacted),
                "writes_event_fact_latest": True,
                "writes_event_fact_summary_cache": False,
                "writes_auto_trade_decisions": False,
                "places_orders": False,
                "restarts_launchd": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "executes_sql": True,
            },
            "commit": bool(commit),
            "updated_at": updated_at,
        }
    except sqlite3.Error as exc:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass
        result = _blocked_apply_result(plan_map, reason="sqlite_error", allow_apply=allow_apply)
        result["error"] = str(exc)
        result["actions_applied"] = actions_applied
        return result


def evaluate_event_fact_latest_repoint_policy(
    *,
    existing_fact_as_of: str = "",
    current_payload: Mapping[str, Any] | None,
    incoming_fact_as_of: str = "",
    incoming_payload: Mapping[str, Any] | None,
    existing_model_snapshot_id: int | None = None,
    incoming_model_snapshot_id: int | None = None,
    source_acquisition_trace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    existing_payload = _as_mapping(current_payload)
    incoming_payload_map = _as_mapping(incoming_payload)
    existing_dt = _parse_iso_ts(existing_fact_as_of)
    incoming_dt = _parse_iso_ts(incoming_fact_as_of)
    existing_rank = _payload_hard_rank(existing_payload)
    incoming_rank = max(
        _payload_hard_rank(incoming_payload_map, source_acquisition_trace=source_acquisition_trace),
        1 if _trace_hard_candidate_ids(source_acquisition_trace) else 0,
    )
    existing_observation_path_rank = _payload_observation_path_materialization_rank(existing_payload)
    incoming_observation_path_rank = _payload_observation_path_materialization_rank(incoming_payload_map)
    existing_hard_source_direct_ssot = bool(existing_payload.get("hard_source_direct_ssot_write"))
    incoming_hard_source_direct_ssot = bool(incoming_payload_map.get("hard_source_direct_ssot_write"))
    existing_value = _payload_hard_observation_value(existing_payload) if existing_rank > 0 else None
    incoming_value = _payload_hard_observation_value(incoming_payload_map) if incoming_rank > 0 else None
    existing_source = _payload_observed_today_max_source(existing_payload)
    incoming_source = _payload_observed_today_max_source(incoming_payload_map)
    existing_model_id = (
        _int_or_none(existing_model_snapshot_id)
        if existing_model_snapshot_id is not None
        else _payload_model_snapshot_id(existing_payload)
    )
    incoming_model_id = (
        _int_or_none(incoming_model_snapshot_id)
        if incoming_model_snapshot_id is not None
        else _payload_model_snapshot_id(incoming_payload_map)
    )
    incoming_newer_model_snapshot = (
        existing_model_id is not None
        and incoming_model_id is not None
        and int(incoming_model_id) > int(existing_model_id)
    )
    incoming_hard_observation_not_regressed = (
        existing_value is None
        or (incoming_value is not None and float(incoming_value) + 1e-9 >= float(existing_value))
    )
    same_wu_twc_intraday = _source_is_wu_twc_intraday(existing_source) and _source_is_wu_twc_intraday(incoming_source)
    existing_awc_features = _payload_awc_features(existing_payload)
    incoming_awc_features = _payload_awc_features(incoming_payload_map)

    reason = ""
    allowed = True
    if _fast_trade_latest_should_hold(
        existing_payload=existing_payload,
        incoming_payload=incoming_payload_map,
        existing_awc_features=existing_awc_features,
        incoming_awc_features=incoming_awc_features,
    ):
        allowed = False
        reason = "existing_fast_trade_latest_same_target_hold"
    elif existing_awc_features and incoming_awc_features:
        existing_awc_latest = _feature_ts(existing_awc_features, "zuuu_metar_raw_awc_latest_at")
        incoming_awc_latest = _feature_ts(incoming_awc_features, "zuuu_metar_raw_awc_latest_at")
        incoming_awc_max_stronger = _candidate_today_max_is_stronger(incoming_awc_features, existing_awc_features)
        if (
            existing_awc_latest is not None
            and incoming_awc_latest is not None
            and incoming_awc_latest < existing_awc_latest
            and not incoming_awc_max_stronger
        ):
            allowed = False
            reason = "incoming_stale_awc_latest_at"

    if allowed and existing_value is not None:
        if incoming_value is None and existing_rank >= 2:
            allowed = False
            reason = "incoming_without_hard_observation_cannot_repoint_over_hard_fact"
        elif incoming_value is not None and float(incoming_value) + 1e-9 < float(existing_value):
            if same_wu_twc_intraday and (existing_dt is None or incoming_dt is None or incoming_dt >= existing_dt):
                allowed = True
            elif existing_rank >= incoming_rank or existing_rank >= 2:
                allowed = False
                reason = "incoming_hard_observation_regresses_existing_value"

    if allowed and existing_dt is not None and incoming_dt is not None and incoming_dt < existing_dt:
        if incoming_value is not None and existing_value is not None and float(incoming_value) > float(existing_value) + 1e-9:
            allowed = True
        elif incoming_rank > existing_rank and incoming_value is not None:
            allowed = True
        elif (
            incoming_newer_model_snapshot
            and incoming_rank >= existing_rank
            and incoming_observation_path_rank >= existing_observation_path_rank
            and incoming_hard_observation_not_regressed
        ):
            allowed = True
        else:
            allowed = False
            reason = "incoming_fact_as_of_older_than_existing_latest"

    if allowed and existing_dt is not None and incoming_dt is None:
        allowed = False
        reason = "incoming_fact_as_of_unparseable_while_existing_is_parseable"

    if (
        allowed
        and incoming_hard_source_direct_ssot
        and not existing_hard_source_direct_ssot
        and incoming_rank >= existing_rank
        and incoming_hard_observation_not_regressed
    ):
        allowed = True

    if (
        allowed
        and existing_rank >= incoming_rank
        and existing_observation_path_rank > incoming_observation_path_rank
        and not (
            incoming_value is not None
            and existing_value is not None
            and float(incoming_value) > float(existing_value) + 1e-9
        )
    ):
        allowed = False
        reason = "incoming_observation_path_not_materialized_over_existing_materialized_fact"

    if allowed and existing_rank >= 2 and incoming_rank < existing_rank:
        allowed = False
        reason = "incoming_weaker_than_existing_hard_fact"
    if allowed and existing_rank >= 1 and incoming_rank == 0:
        allowed = False
        reason = "incoming_without_hard_observation_cannot_repoint_over_hard_fact"

    if not allowed and existing_rank >= 2 and incoming_rank < existing_rank:
        reason = "incoming_weaker_than_existing_hard_fact"

    return {
        "allowed": bool(allowed),
        "block_reason": "" if allowed else reason,
        "existing_rank": int(existing_rank),
        "incoming_rank": int(incoming_rank),
        "existing_observation_path_rank": int(existing_observation_path_rank),
        "incoming_observation_path_rank": int(incoming_observation_path_rank),
        "existing_hard_observation_value_c": existing_value,
        "incoming_hard_observation_value_c": incoming_value,
        "existing_model_snapshot_id": existing_model_id,
        "incoming_model_snapshot_id": incoming_model_id,
    }


def event_fact_latest_update_allowed(
    *,
    existing_fact_as_of: str,
    existing_payload: Mapping[str, Any],
    incoming_fact_as_of: str,
    incoming_payload: Mapping[str, Any],
    existing_model_snapshot_id: int | None = None,
    incoming_model_snapshot_id: int | None = None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
) -> bool:
    return bool(
        evaluate_event_fact_latest_repoint_policy(
            existing_fact_as_of=existing_fact_as_of,
            current_payload=existing_payload,
            incoming_fact_as_of=incoming_fact_as_of,
            incoming_payload=incoming_payload,
            existing_model_snapshot_id=existing_model_snapshot_id,
            incoming_model_snapshot_id=incoming_model_snapshot_id,
            source_acquisition_trace=source_acquisition_trace,
        ).get("allowed")
    )


def _latest_repoint_policy(
    *,
    current_fact_as_of: str = "",
    current_payload: Mapping[str, Any] | None,
    incoming_fact_as_of: str = "",
    incoming_payload: Mapping[str, Any] | None,
    source_acquisition_trace: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    policy = evaluate_event_fact_latest_repoint_policy(
        existing_fact_as_of=current_fact_as_of,
        current_payload=current_payload,
        incoming_fact_as_of=incoming_fact_as_of,
        incoming_payload=incoming_payload,
        source_acquisition_trace=source_acquisition_trace,
    )
    return bool(policy.get("allowed")), str(policy.get("block_reason") or "")


def build_fact_snapshot_write_plan(
    *,
    slug: str,
    event_date: str,
    run_date: str = "",
    fact_as_of: str = "",
    incoming_payload: Mapping[str, Any] | None = None,
    current_fact_id: int | None = None,
    current_fact_as_of: str = "",
    current_payload: Mapping[str, Any] | None = None,
    source_acquisition_trace: Mapping[str, Any] | None = None,
    composer_version: str = PARSER_VERSION,
    write_reason: str = "",
    posterior_run_id: str = "",
    fact_identity_salt: Any = "",
    include_payload: bool = False,
    enable_apply_contract: bool = False,
    write_latest: bool = True,
    prior_history_conn: sqlite3.Connection | None = None,
    prior_history_db_path: Any = None,
) -> dict[str, Any]:
    fact_as_of = str(fact_as_of or utc_now_iso())
    event_date_key = _normalize_date_key(event_date)
    run_date_key = _canonical_fact_run_date(slug=slug, event_date=event_date_key, run_date=run_date)
    source_event_ids = _source_event_ids(source_acquisition_trace)
    source_observation_ids = _source_observation_ids(source_acquisition_trace)
    fact_snapshot_id = build_fact_snapshot_id(
        slug=str(slug or ""),
        event_date=str(event_date or ""),
        fact_as_of=fact_as_of,
        source_event_ids=source_event_ids,
        identity_salt=fact_identity_salt,
    )
    posterior_id_payload = copy.deepcopy(dict(incoming_payload or {}))
    _materialize_phase_weighting_fields(
        posterior_id_payload,
        event_date=str(event_date or ""),
        fact_as_of=fact_as_of,
        source_acquisition_trace=source_acquisition_trace,
    )
    effective_posterior_run_id = str(
        posterior_run_id
        or _as_mapping(posterior_id_payload).get("posterior_run_id")
        or _as_mapping(_as_mapping(posterior_id_payload).get("lineage_state")).get("posterior_run_id")
        or _as_mapping(_as_mapping(posterior_id_payload).get("posterior_ssot_fact")).get("posterior_run_id")
        or _posterior_run_id_from_existing_payload(
            posterior_id_payload,
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            fact_snapshot_id=fact_snapshot_id,
            source_event_ids=source_event_ids,
        )
    )
    correlation_id = str(_as_mapping(source_acquisition_trace).get("correlation_id") or "")
    if not correlation_id:
        correlation_id = build_correlation_id("fact_snapshot", slug, event_date, source_event_ids)
    latest_repoint_allowed, latest_repoint_block_reason = _latest_repoint_policy(
        current_fact_as_of=current_fact_as_of,
        current_payload=current_payload,
        incoming_fact_as_of=fact_as_of,
        incoming_payload=incoming_payload,
        source_acquisition_trace=source_acquisition_trace,
    )
    db_write_guard = {
        "schema": "fact_writer_db_write_guard_v1",
        "write_owner": WRITE_OWNER,
        "read_only": True,
        "dry_run": True,
        "apply_supported": bool(enable_apply_contract),
        "executes_sql": False,
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "historical_backfill": False,
        "places_orders": False,
        "restarts_launchd": False,
    }
    lineage_state = {
        "schema": "fact_writer_lineage_state_v1",
        "correlation_id": correlation_id,
        "fact_snapshot_id": fact_snapshot_id,
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
        "posterior_run_id": effective_posterior_run_id,
        "write_owner": WRITE_OWNER,
    }
    fact_layer_state = {
        "schema": "fact_layer_state_v1",
        "state": "planned_no_apply",
        "write_owner": WRITE_OWNER,
        "write_reason": str(write_reason or ""),
        "latest_repoint_allowed": latest_repoint_allowed,
        "latest_repoint_block_reason": latest_repoint_block_reason,
        "current_fact_id": current_fact_id,
        "current_fact_as_of": str(current_fact_as_of or ""),
    }
    effective_payload, merge_fill_operations = merge_fill_fact_payload(
        incoming_payload,
        current_payload=current_payload,
        source_acquisition_trace=source_acquisition_trace,
        lineage_state=lineage_state,
        fact_layer_state=fact_layer_state,
        db_write_guard=db_write_guard,
        phase0_contract_context={
            "fact_snapshot_id": fact_snapshot_id,
            "posterior_run_id": effective_posterior_run_id,
            "event_date": str(event_date or ""),
            "source_event_ids": source_event_ids,
            "source_observation_ids": source_observation_ids,
            "fact_as_of": fact_as_of,
            "write_reason": str(write_reason or ""),
        },
    )
    effective_payload["event_date"] = event_date_key or str(event_date or "")
    effective_payload["run_date"] = run_date_key
    _materialize_prior_layer_fields(
        effective_payload,
        event_date=str(event_date or ""),
        fact_as_of=fact_as_of,
        prior_history_conn=prior_history_conn,
        prior_history_db_path=prior_history_db_path,
    )
    prior_layer_contract = build_prior_layer_contract(effective_payload)
    posterior_ssot_apply_contract = build_posterior_ssot_apply_contract(
        effective_payload,
        source_acquisition_trace=source_acquisition_trace,
    )
    summary_cache_needed = _summary_cache_needed(effective_payload)
    latest_would_write = bool(latest_repoint_allowed and write_latest)
    latest_block_reason = latest_repoint_block_reason
    if not write_latest:
        latest_block_reason = "latest_repoint_disabled_by_caller"
    plan_actions = [
        {
            "action": "append_event_fact_history",
            "target_table": "event_fact_history",
            "would_write": True,
            "write_owner": WRITE_OWNER,
            "apply_status": "ready_for_explicit_apply" if enable_apply_contract else "blocked_no_apply",
        },
        {
            "action": "repoint_event_fact_latest",
            "target_table": "event_fact_latest",
            "would_write": latest_would_write,
            "write_owner": WRITE_OWNER,
            "apply_status": (
                "ready_for_explicit_apply"
                if enable_apply_contract and latest_would_write
                else "blocked_no_apply"
                if latest_would_write
                else "blocked_policy"
                if latest_repoint_block_reason
                else "not_applicable"
            ),
            "block_reason": latest_block_reason,
        },
        {
            "action": "upsert_event_fact_summary_cache",
            "target_table": "event_fact_summary_cache",
            "would_write": bool(summary_cache_needed),
            "write_owner": WRITE_OWNER,
            "apply_status": (
                "ready_for_explicit_apply"
                if enable_apply_contract and summary_cache_needed
                else "blocked_no_apply"
                if summary_cache_needed
                else "not_applicable"
            ),
        },
    ]
    payload_hash_before = sha256_json(dict(incoming_payload or {}))
    payload_hash_after = sha256_json(effective_payload)
    plan = {
        "schema": SCHEMA_VERSION,
        "ok": bool(prior_layer_contract.get("ok") and posterior_ssot_apply_contract.get("ok")),
        "read_only": True,
        "dry_run": True,
        "apply_supported": bool(enable_apply_contract),
        "execution_mode": (
            "explicit_apply_contract_ready_fact_snapshot_write_plan"
            if enable_apply_contract
            else "no_apply_fact_snapshot_write_plan"
        ),
        "write_owner": WRITE_OWNER,
        "parser_version": str(composer_version or PARSER_VERSION),
        "slug": str(slug or ""),
        "event_date": event_date_key or str(event_date or ""),
        "run_date": run_date_key,
        "fact_as_of": fact_as_of,
        "correlation_id": correlation_id,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": effective_posterior_run_id,
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
        "target_tables": ["event_fact_history", "event_fact_latest", "event_fact_summary_cache"],
        "write_latest": bool(write_latest),
        "plan_actions": plan_actions,
        "merge_fill_operations": merge_fill_operations,
        "payload_hash_before": payload_hash_before,
        "payload_hash_after": payload_hash_after,
        "payload_changed_by_merge_fill": payload_hash_before != payload_hash_after,
        "fact_layer_state": fact_layer_state,
        "db_write_guard": db_write_guard,
        "lineage_state": lineage_state,
        "phase0_fact_json_contract_schema": PHASE0_FACT_JSON_CONTRACT_SCHEMA,
        "phase0_fact_json_contract_keys": list(PHASE0_FACT_JSON_CONTRACT_KEYS),
        "prior_layer_contract": prior_layer_contract,
        "posterior_ssot_apply_contract": posterior_ssot_apply_contract,
        "source_acquisition_trace_summary": {
            "schema": _as_mapping(source_acquisition_trace).get("schema") or "",
            "source_count": len(_as_mapping(source_acquisition_trace).get("source_observations") or []),
            "hard_observation_candidate_count": len(_trace_hard_candidate_ids(source_acquisition_trace)),
            "source_observation_id_count": len(source_observation_ids),
        },
        "next_actions": [
            "review_fact_snapshot_write_plan",
            (
                "call_apply_fact_snapshot_write_plan_with_allow_apply_true_in_controlled_window"
                if enable_apply_contract
                else "require_explicit_apply_path_before_sql_execution"
            ),
            "post_apply_phase0_schema_probe_schema_only_after_schema_apply",
            "post_forward_write_phase0_schema_probe_full_strict_must_pass_after_future_fact_json_contract",
            "future_fact_json_contract_keys_are_merge_filled_without_history_backfill",
        ],
    }
    if include_payload:
        plan["effective_payload"] = effective_payload
    return plan


def _blocked_apply_result(
    plan: Mapping[str, Any] | None,
    *,
    reason: str,
    allow_apply: bool,
) -> dict[str, Any]:
    plan_map = _as_mapping(plan)
    return {
        "schema": APPLY_RESULT_SCHEMA_VERSION,
        "ok": False,
        "applied": False,
        "blocked": True,
        "block_reason": reason,
        "allow_apply": bool(allow_apply),
        "write_owner": WRITE_OWNER,
        "fact_snapshot_id": str(plan_map.get("fact_snapshot_id") or ""),
        "prior_layer_contract": copy.deepcopy(
            dict(_as_mapping(plan_map.get("prior_layer_contract")))
        ),
        "posterior_ssot_apply_contract": copy.deepcopy(
            dict(_as_mapping(plan_map.get("posterior_ssot_apply_contract")))
        ),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_event_fact_history": False,
            "writes_event_fact_latest": False,
            "writes_event_fact_summary_cache": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "executes_sql": False,
        },
        "actions_applied": [],
    }


def apply_fact_snapshot_write_plan(
    conn: sqlite3.Connection | None,
    plan: Mapping[str, Any],
    *,
    allow_apply: bool = False,
    commit: bool = False,
    created_at: str = "",
) -> dict[str, Any]:
    """Apply one writer-generated plan into fact tables.

    The function is intentionally opt-in. A caller must build the plan with
    enable_apply_contract=True, include the effective payload, and pass
    allow_apply=True. This keeps production migration explicit while making
    the eventual write path testable under the same owner module.
    """

    plan_map = _as_mapping(plan)
    if not allow_apply:
        return _blocked_apply_result(plan_map, reason="allow_apply_false", allow_apply=allow_apply)
    if conn is None:
        return _blocked_apply_result(plan_map, reason="missing_sqlite_connection", allow_apply=allow_apply)
    if plan_map.get("schema") != SCHEMA_VERSION:
        return _blocked_apply_result(plan_map, reason="invalid_plan_schema", allow_apply=allow_apply)
    if not bool(plan_map.get("apply_supported")):
        return _blocked_apply_result(plan_map, reason="plan_apply_contract_not_enabled", allow_apply=allow_apply)
    payload = plan_map.get("effective_payload")
    if not isinstance(payload, Mapping):
        return _blocked_apply_result(plan_map, reason="effective_payload_missing", allow_apply=allow_apply)
    expected_hash = str(plan_map.get("payload_hash_after") or "")
    if expected_hash and sha256_json(payload) != expected_hash:
        return _blocked_apply_result(plan_map, reason="effective_payload_hash_mismatch", allow_apply=allow_apply)
    payload_map = dict(payload)
    _materialize_prior_layer_fields(payload_map)
    prior_contract = build_prior_layer_contract(payload_map)
    if not bool(prior_contract.get("ok")):
        plan_with_contract = dict(plan_map)
        plan_with_contract["prior_layer_contract"] = prior_contract
        return _blocked_apply_result(
            plan_with_contract,
            reason="prior_layer_contract_failed",
            allow_apply=allow_apply,
        )
    posterior_contract = build_posterior_ssot_apply_contract(
        payload_map,
        source_acquisition_trace=_as_mapping(payload_map).get("source_acquisition_trace"),
    )
    if not bool(posterior_contract.get("ok")):
        plan_with_contract = dict(plan_map)
        plan_with_contract["posterior_ssot_apply_contract"] = posterior_contract
        return _blocked_apply_result(
            plan_with_contract,
            reason="posterior_ssot_apply_contract_failed",
            allow_apply=allow_apply,
        )

    append_action = _plan_action(plan_map, "append_event_fact_history")
    latest_action = _plan_action(plan_map, "repoint_event_fact_latest")
    summary_action = _plan_action(plan_map, "upsert_event_fact_summary_cache")
    if not bool(append_action.get("would_write")):
        return _blocked_apply_result(plan_map, reason="append_event_fact_history_not_planned", allow_apply=allow_apply)

    slug = str(plan_map.get("slug") or payload.get("slug") or "")
    event_date = _normalize_date_key(plan_map.get("event_date") or payload.get("event_date") or "")
    run_date = _canonical_fact_run_date(
        slug=slug,
        event_date=event_date,
        run_date=plan_map.get("run_date") or payload.get("run_date") or event_date,
    )
    payload = payload_map
    payload["event_date"] = event_date
    payload["run_date"] = run_date
    fact_as_of = str(plan_map.get("fact_as_of") or payload.get("fact_as_of") or utc_now_iso())
    composer_version = str(payload.get("composer_version") or plan_map.get("parser_version") or PARSER_VERSION)
    if not slug or not event_date:
        return _blocked_apply_result(plan_map, reason="slug_or_event_date_missing", allow_apply=allow_apply)

    created_at = str(created_at or utc_now_iso())
    rows = payload.get("distribution") if isinstance(payload.get("distribution"), list) else payload.get("rows")
    best_side, best_outcome, best_edge_score, best_signal_edge = _best_edge_from_payload_rows(rows)
    payload_json = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), default=str)
    actions_applied: list[dict[str, Any]] = []
    savepoint = "weather_fact_snapshot_writer_apply"
    try:
        conn.execute(f"SAVEPOINT {savepoint}")
        source_observation_persistence = _persist_source_observations_for_fact_payload(
            conn,
            payload,
            created_at=created_at,
        )
        if not bool(source_observation_persistence.get("ok")):
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            result = _blocked_apply_result(
                plan_map,
                reason="source_observation_persistence_failed",
                allow_apply=allow_apply,
            )
            result["source_observation_persistence"] = source_observation_persistence
            return result
        if int(source_observation_persistence.get("attempted") or 0) > 0:
            actions_applied.append(
                {
                    "action": "insert_source_observations",
                    "attempted": int(source_observation_persistence.get("attempted") or 0),
                    "inserted": int(source_observation_persistence.get("inserted") or 0),
                    "ignored": int(source_observation_persistence.get("ignored") or 0),
                    "write_policy": "insert_or_ignore_no_overwrite",
                }
            )
        source_event_id_values = plan_map.get("source_event_ids")
        if not isinstance(source_event_id_values, list):
            source_event_id_values = []
        history_values = {
            "run_date": run_date,
            "slug": slug,
            "event_date": event_date,
            "fact_as_of": fact_as_of,
            "correlation_id": str(plan_map.get("correlation_id") or ""),
            "source_event_id": str((source_event_id_values or [""])[0] or ""),
            "fact_snapshot_id": str(plan_map.get("fact_snapshot_id") or ""),
            "posterior_run_id": str(plan_map.get("posterior_run_id") or ""),
            "model_snapshot_id": _int_or_none(payload.get("model_snapshot_id")),
            "market_snapshot_id": _int_or_none(payload.get("market_snapshot_id")),
            "composer_version": composer_version,
            "market_updated_at": payload.get("market_updated_at") or None,
            "model_updated_at": payload.get("model_updated_at") or None,
            "market_stale_sec": _int_or_none(payload.get("market_stale_sec")),
            "model_cache_age_sec": _int_or_none(payload.get("model_cache_age_sec")),
            "best_side": best_side or None,
            "best_outcome": best_outcome or None,
            "best_edge_score": best_edge_score,
            "best_signal_edge_entry": best_signal_edge,
            "fact_json": payload_json,
            "created_at": created_at,
        }
        history_columns = [col for col in _table_columns(conn, "event_fact_history") if col != "id" and col in history_values]
        required_history_columns = {"run_date", "slug", "event_date", "fact_as_of", "fact_json", "created_at"}
        if not required_history_columns.issubset(set(history_columns)):
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return _blocked_apply_result(plan_map, reason="event_fact_history_required_columns_missing", allow_apply=allow_apply)
        placeholders = ",".join("?" for _ in history_columns)
        column_sql = ",".join(history_columns)
        cur = conn.execute(
            f"INSERT INTO event_fact_history({column_sql}) VALUES ({placeholders})",
            tuple(history_values[col] for col in history_columns),
        )
        fact_id = int(cur.lastrowid or 0)
        actions_applied.append({"action": "append_event_fact_history", "fact_id": fact_id})
        post_apply_verification = build_post_apply_persisted_posterior_ssot_verification(
            conn,
            fact_id=fact_id,
            expected_payload_hash=expected_hash,
            expected_source_event_ids=plan_map.get("source_event_ids") or [],
            expected_source_observation_ids=plan_map.get("source_observation_ids") or [],
        )
        if not bool(post_apply_verification.get("ok")):
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            result = _blocked_apply_result(
                plan_map,
                reason="post_apply_persisted_posterior_ssot_verification_failed",
                allow_apply=allow_apply,
            )
            result["post_apply_persisted_posterior_ssot_verification"] = post_apply_verification
            result["actions_applied"] = actions_applied
            return result

        latest_repointed = False
        if bool(latest_action.get("would_write")):
            latest_values = {
                "slug": slug,
                "event_date": event_date,
                "fact_id": fact_id,
                "updated_at": created_at,
                "fact_snapshot_id": str(plan_map.get("fact_snapshot_id") or ""),
                "correlation_id": str(plan_map.get("correlation_id") or ""),
            }
            latest_columns = [
                col
                for col in _table_columns(conn, "event_fact_latest")
                if col in latest_values
            ]
            latest_placeholders = ",".join("?" for _ in latest_columns)
            latest_column_sql = ",".join(latest_columns)
            conn.execute(
                f"INSERT OR REPLACE INTO event_fact_latest({latest_column_sql}) VALUES ({latest_placeholders})",
                tuple(latest_values[col] for col in latest_columns),
            )
            latest_repointed = True
            actions_applied.append({"action": "repoint_event_fact_latest", "fact_id": fact_id})
            current_fact_id = _int_or_none(_as_mapping(plan_map.get("fact_layer_state")).get("current_fact_id"))
            if current_fact_id and int(current_fact_id) != int(fact_id):
                if compact_replaced_event_fact_history_row(
                    conn,
                    int(current_fact_id),
                    fact_as_of=str(_as_mapping(plan_map.get("fact_layer_state")).get("current_fact_as_of") or ""),
                ):
                    actions_applied.append({"action": "compact_replaced_event_fact_history", "fact_id": int(current_fact_id)})

        summary_cache_written = False
        if bool(summary_action.get("would_write")):
            summary_payload = _summary_cache_payload_from_effective_payload(
                payload,
                fact_id=fact_id,
                slug=slug,
                event_date=event_date,
                run_date=run_date,
                fact_as_of=fact_as_of,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO event_fact_summary_cache(
                    fact_id, slug, event_date, run_date, fact_as_of, summary_json, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    slug,
                    event_date,
                    run_date,
                    fact_as_of,
                    json.dumps(summary_payload, ensure_ascii=False, separators=(",", ":"), default=str),
                    created_at,
                ),
            )
            summary_cache_written = True
            actions_applied.append({"action": "upsert_event_fact_summary_cache", "fact_id": fact_id})

        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        committed_at = ""
        if commit:
            conn.commit()
            # This is deliberately captured only after SQLite confirms the
            # fact transaction. Callers append timing in a separate immutable
            # ledger rather than rewriting this just-created fact JSON.
            committed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if latest_repointed or summary_cache_written:
            _touch_ui_snapshot_dirty_stamp(f"event_fact_write:{slug}:{event_date}:{fact_id}")
        return {
            "schema": APPLY_RESULT_SCHEMA_VERSION,
            "ok": True,
            "applied": True,
            "blocked": False,
            "allow_apply": True,
            "write_owner": WRITE_OWNER,
            "fact_snapshot_id": str(plan_map.get("fact_snapshot_id") or ""),
            "posterior_run_id": str(plan_map.get("posterior_run_id") or ""),
            "prior_layer_contract": prior_contract,
            "posterior_ssot_apply_contract": posterior_contract,
            "post_apply_persisted_posterior_ssot_verification": post_apply_verification,
            "source_observation_persistence": source_observation_persistence,
            "fact_id": fact_id,
            "latest_repointed": latest_repointed,
            "summary_cache_written": summary_cache_written,
            "actions_applied": actions_applied,
            "guards": {
                "writes_archive_db": True,
                "writes_facts_sqlite": False,
                "writes_event_fact_history": True,
                "writes_source_observations": bool(
                    int(source_observation_persistence.get("attempted") or 0) > 0
                ),
                "writes_event_fact_latest": latest_repointed,
                "writes_event_fact_summary_cache": summary_cache_written,
                "writes_auto_trade_decisions": False,
                "places_orders": False,
                "restarts_launchd": False,
                "historical_backfill": False,
                "changes_parameters": False,
                "executes_sql": True,
            },
            "commit": bool(commit),
            "committed_at": committed_at,
            "created_at": created_at,
        }
    except sqlite3.Error as exc:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass
        result = _blocked_apply_result(plan_map, reason="sqlite_error", allow_apply=allow_apply)
        result["error"] = str(exc)
        result["actions_applied"] = actions_applied
    return result


def compact_fact_snapshot_writer_apply_result_for_lineage(result: Mapping[str, Any] | None) -> dict[str, Any]:
    result_map = _as_mapping(result)
    prior_contract = _as_mapping(result_map.get("prior_layer_contract"))
    posterior_contract = _as_mapping(result_map.get("posterior_ssot_apply_contract"))
    post_apply_verification = _as_mapping(result_map.get("post_apply_persisted_posterior_ssot_verification"))
    source_observation_persistence = _as_mapping(result_map.get("source_observation_persistence"))
    persisted_prior_contract = _as_mapping(post_apply_verification.get("prior_layer_contract"))
    persisted_contract = _as_mapping(post_apply_verification.get("posterior_ssot_apply_contract"))
    source_lineage_contract = _as_mapping(post_apply_verification.get("source_lineage_contract"))
    contract_for_prior = persisted_prior_contract if persisted_prior_contract else prior_contract
    contract_for_target = persisted_contract if persisted_contract else posterior_contract
    post_apply_present = bool(post_apply_verification)

    def _issues(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item) for item in value if str(item)]

    applied = bool(result_map.get("applied"))
    blocked = bool(result_map.get("blocked"))
    post_apply_ok = bool(post_apply_verification.get("ok")) if post_apply_present else False
    if applied and post_apply_ok:
        verification_state = "persisted_verified"
    elif applied and not post_apply_present:
        verification_state = "persisted_verification_missing"
    elif blocked:
        verification_state = "blocked_before_persist"
    else:
        verification_state = "not_applied"

    return {
        "schema": APPLY_RESULT_SUMMARY_SCHEMA_VERSION,
        "source_schema": str(result_map.get("schema") or ""),
        "ok": bool(result_map.get("ok")),
        "applied": applied,
        "blocked": blocked,
        "block_reason": str(result_map.get("block_reason") or ""),
        "error": str(result_map.get("error") or ""),
        "allow_apply": bool(result_map.get("allow_apply")),
        "write_owner": str(result_map.get("write_owner") or ""),
        "fact_id": _int_or_none(result_map.get("fact_id")),
        "fact_snapshot_id": str(
            result_map.get("fact_snapshot_id")
            or post_apply_verification.get("fact_snapshot_id")
            or contract_for_target.get("fact_snapshot_id")
            or ""
        ),
        "posterior_run_id": str(
            result_map.get("posterior_run_id")
            or post_apply_verification.get("posterior_run_id")
            or contract_for_target.get("posterior_run_id")
            or ""
        ),
        "latest_repointed": bool(result_map.get("latest_repointed")),
        "summary_cache_written": bool(result_map.get("summary_cache_written")),
        "lineage_verification_state": verification_state,
        "prior_layer_contract_present": bool(prior_contract),
        "prior_layer_contract_ok": bool(prior_contract.get("ok")),
        "prior_layer_contract_state": str(prior_contract.get("state") or ""),
        "prior_layer_contract_issues": _issues(prior_contract.get("issues")),
        "prior_distribution_bucket_count": _int_or_none(contract_for_prior.get("prior_distribution_bucket_count")),
        "prior_distribution_sum": _float_or_none(contract_for_prior.get("prior_distribution_sum")),
        "prior_meta_boundary": str(contract_for_prior.get("prior_meta_boundary") or ""),
        "posterior_ssot_apply_contract_present": bool(posterior_contract),
        "posterior_ssot_apply_contract_ok": bool(posterior_contract.get("ok")),
        "posterior_ssot_apply_contract_state": str(posterior_contract.get("state") or ""),
        "posterior_ssot_apply_contract_issues": _issues(posterior_contract.get("issues")),
        "post_apply_persisted_verification_present": post_apply_present,
        "post_apply_persisted_verification_ok": post_apply_ok,
        "post_apply_persisted_verification_schema": str(post_apply_verification.get("schema") or ""),
        "post_apply_persisted_verification_issues": _issues(post_apply_verification.get("issues")),
        "post_apply_persisted_verification_fact_id": _int_or_none(post_apply_verification.get("fact_id")),
        "source_observation_persistence_present": bool(source_observation_persistence),
        "source_observation_persistence_state": str(source_observation_persistence.get("state") or ""),
        "source_observation_persistence_attempted": _int_or_none(source_observation_persistence.get("attempted")),
        "source_observation_persistence_inserted": _int_or_none(source_observation_persistence.get("inserted")),
        "source_observation_id_column_present": bool(
            source_observation_persistence.get("source_observation_id_column_present")
        ),
        "source_lineage_contract_present": bool(source_lineage_contract),
        "source_lineage_contract_ok": bool(source_lineage_contract.get("ok")) if source_lineage_contract else False,
        "source_observation_id_replay_anchor_ready": bool(
            source_lineage_contract.get("source_observation_id_replay_anchor_ready")
        ),
        "persisted_source_event_ids": _unique_texts(source_lineage_contract.get("persisted_source_event_ids") or []),
        "persisted_source_observation_ids": _unique_texts(
            source_lineage_contract.get("persisted_source_observation_ids") or []
        ),
        "target_bucket": str(
            post_apply_verification.get("target_bucket")
            or contract_for_target.get("target_bucket")
            or ""
        ),
        "target_probability": _prob01(
            post_apply_verification.get("target_probability")
            if post_apply_verification.get("target_probability") is not None
            else contract_for_target.get("target_probability")
        ),
        "hard_temperature_floor_c": _int_or_none(contract_for_target.get("hard_temperature_floor_c")),
        "phase": str(contract_for_target.get("phase") or ""),
        "beta_weak": _prob01(contract_for_target.get("beta_weak")),
        "beta_strong": _prob01(contract_for_target.get("beta_strong")),
        "hard_mask_reason": str(contract_for_target.get("hard_mask_reason") or ""),
        "guards": dict(_as_mapping(result_map.get("guards"))),
    }


def build_post_apply_persisted_posterior_ssot_verification(
    conn: sqlite3.Connection,
    *,
    fact_id: int,
    expected_payload_hash: str = "",
    expected_source_event_ids: Sequence[Any] | None = None,
    expected_source_observation_ids: Sequence[Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    row = conn.execute(
        "SELECT fact_json FROM event_fact_history WHERE id = ?",
        (int(fact_id),),
    ).fetchone()
    if row is None:
        return {
            "schema": "post_apply_persisted_posterior_ssot_verification_v1",
            "ok": False,
            "fact_id": int(fact_id),
            "issues": ["persisted_fact_row_missing"],
            "prior_layer_contract": {},
            "posterior_ssot_apply_contract": {},
        }
    try:
        raw_json = row["fact_json"] if isinstance(row, sqlite3.Row) else row[0]
    except Exception:
        raw_json = ""
    try:
        payload = json.loads(str(raw_json or ""))
    except Exception:
        return {
            "schema": "post_apply_persisted_posterior_ssot_verification_v1",
            "ok": False,
            "fact_id": int(fact_id),
            "issues": ["persisted_fact_json_invalid"],
            "prior_layer_contract": {},
            "posterior_ssot_apply_contract": {},
        }
    if not isinstance(payload, Mapping):
        return {
            "schema": "post_apply_persisted_posterior_ssot_verification_v1",
            "ok": False,
            "fact_id": int(fact_id),
            "issues": ["persisted_fact_json_not_object"],
            "prior_layer_contract": {},
            "posterior_ssot_apply_contract": {},
        }
    persisted_hash = sha256_json(payload)
    if expected_payload_hash and persisted_hash != str(expected_payload_hash):
        issues.append("persisted_fact_json_hash_mismatch")
    prior_contract = build_prior_layer_contract(payload)
    if not bool(prior_contract.get("ok")):
        issues.append("prior_layer_contract_failed")
    contract = build_posterior_ssot_apply_contract(
        payload,
        source_acquisition_trace=_as_mapping(payload).get("source_acquisition_trace"),
    )
    if not bool(contract.get("ok")):
        issues.append("posterior_ssot_apply_contract_failed")
    persisted_source_event_ids = _payload_source_event_ids(payload)
    persisted_source_observation_ids = _payload_source_observation_ids(payload)
    expected_source_event_ids_clean = _unique_texts(list(expected_source_event_ids or []))
    expected_source_observation_ids_clean = _unique_texts(list(expected_source_observation_ids or []))
    missing_expected_source_event_ids = [
        source_event_id
        for source_event_id in expected_source_event_ids_clean
        if source_event_id not in persisted_source_event_ids
    ]
    extra_persisted_source_event_ids = [
        source_event_id
        for source_event_id in persisted_source_event_ids
        if source_event_id not in expected_source_event_ids_clean
    ]
    if expected_source_event_ids_clean and missing_expected_source_event_ids:
        issues.append("persisted_source_event_ids_mismatch")
    if expected_source_observation_ids_clean and persisted_source_observation_ids != expected_source_observation_ids_clean:
        issues.append("persisted_source_observation_ids_mismatch")
    return {
        "schema": "post_apply_persisted_posterior_ssot_verification_v1",
        "ok": not issues,
        "fact_id": int(fact_id),
        "issues": issues,
        "persisted_payload_hash": persisted_hash,
        "expected_payload_hash": str(expected_payload_hash or ""),
        "prior_layer_contract": prior_contract,
        "posterior_ssot_apply_contract": contract,
        "source_lineage_contract": {
            "schema": "post_apply_source_lineage_contract_v1",
            "ok": (
                "persisted_source_event_ids_mismatch" not in issues
                and "persisted_source_observation_ids_mismatch" not in issues
            ),
            "persisted_source_event_ids": persisted_source_event_ids,
            "expected_source_event_ids": expected_source_event_ids_clean,
            "source_event_ids_contain_expected": not missing_expected_source_event_ids,
            "missing_expected_source_event_ids": missing_expected_source_event_ids,
            "extra_persisted_source_event_ids": extra_persisted_source_event_ids,
            "persisted_source_observation_ids": persisted_source_observation_ids,
            "expected_source_observation_ids": expected_source_observation_ids_clean,
            "source_observation_id_replay_anchor_ready": bool(persisted_source_observation_ids),
        },
        "fact_snapshot_id": str(payload.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(payload.get("posterior_run_id") or ""),
        "target_bucket": str(_as_mapping(payload.get("posterior_ssot_fact")).get("target_bucket") or ""),
        "target_probability": _prob01(_as_mapping(payload.get("posterior_ssot_fact")).get("target_probability")),
    }


def build_hierarchical_posterior_fact_payload(
    *,
    pipeline_output: Mapping[str, Any],
    fact_snapshot_id: str = "",
    posterior_run_id: str = "",
    source_acquisition_trace: Mapping[str, Any] | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    pipeline = _as_mapping(pipeline_output)
    posterior_runtime = _runtime_output(pipeline, "hierarchical_temperature_posterior")
    weak_runtime = _runtime_output(pipeline, "forecast_weak_likelihood")
    strong_runtime = _runtime_output(pipeline, "metar_strong_likelihood")
    phase_runtime = _runtime_output(pipeline, "phase_weighting")
    layer_contributions = _as_mapping(pipeline.get("layer_contributions") or posterior_runtime.get("layer_contributions"))
    strong_constraints = _as_mapping(strong_runtime.get("hard_constraints"))
    observation_features = _as_mapping(strong_runtime.get("observation_features"))
    temperature_trajectory = _as_mapping(strong_runtime.get("temperature_trajectory"))
    recoil_ssot_fields = _metar_recoil_ssot_payload(strong_runtime)
    daily_high_candidate_shadow = _daily_high_observation_candidate_shadow_payload(strong_runtime)
    daily_high_candidate_live_rollout = _daily_high_candidate_live_rollout_payload(pipeline, strong_runtime)
    forecast_group_pooling = copy.deepcopy(
        _as_mapping(pipeline.get("forecast_group_pooling") or weak_runtime.get("forecast_group_pooling"))
    )
    forecast_weak_attribution = _forecast_weak_attribution_trace(weak_runtime)
    likelihood_information_quality = copy.deepcopy(_as_mapping(pipeline.get("likelihood_information_quality")))
    trace = _pipeline_source_trace(pipeline, source_acquisition_trace)

    posterior_distribution = (
        pipeline.get("posterior_temperature_distribution")
        or posterior_runtime.get("posterior_temperature_distribution")
        or []
    )
    posterior_distribution_map = (
        pipeline.get("posterior_temperature_distribution_map")
        or posterior_runtime.get("posterior_temperature_distribution_map")
        or {}
    )
    posterior_continuous_distribution = (
        pipeline.get("posterior_continuous_distribution")
        or posterior_runtime.get("posterior_continuous_distribution")
        or {}
    )
    posterior_bucket_cdf = (
        pipeline.get("posterior_bucket_cdf")
        or posterior_runtime.get("posterior_bucket_cdf")
        or {}
    )
    posterior_quantiles_c = (
        pipeline.get("posterior_quantiles_c")
        or posterior_runtime.get("posterior_quantiles_c")
        or {}
    )
    posterior_quantile_grid = (
        pipeline.get("posterior_quantile_grid")
        or posterior_runtime.get("posterior_quantile_grid")
        or []
    )
    sequential_state_filter = copy.deepcopy(
        _as_mapping(pipeline.get("sequential_state_filter") or posterior_runtime.get("sequential_state_filter"))
    )
    canonical_metar_snapshot = copy.deepcopy(
        _as_mapping(pipeline.get("canonical_metar_snapshot") or posterior_runtime.get("canonical_metar_snapshot"))
    )
    predicted_peak_time_prior = copy.deepcopy(
        _as_mapping(pipeline.get("predicted_peak_time_prior") or posterior_runtime.get("predicted_peak_time_prior"))
    )
    predicted_peak_time_posterior = copy.deepcopy(
        _as_mapping(
            pipeline.get("predicted_peak_time_posterior")
            or posterior_runtime.get("predicted_peak_time_posterior")
        )
    )
    observed_peak_platform = copy.deepcopy(
        _as_mapping(pipeline.get("observed_peak_platform") or posterior_runtime.get("observed_peak_platform"))
    )
    remaining_warming_mixture = copy.deepcopy(
        _as_mapping(pipeline.get("remaining_warming_mixture") or posterior_runtime.get("remaining_warming_mixture"))
    )
    v31_multimodel_posterior = copy.deepcopy(
        _as_mapping(
            pipeline.get("v31_multimodel_posterior")
            or posterior_runtime.get("v31_multimodel_posterior")
            or strong_runtime.get("v31_multimodel_posterior")
        )
    )
    observation_information_gate = copy.deepcopy(
        _as_mapping(pipeline.get("observation_information_gate") or posterior_runtime.get("observation_information_gate"))
    )
    forecast_error_components = copy.deepcopy(
        pipeline.get("forecast_error_components") or posterior_runtime.get("forecast_error_components") or []
    )
    weather_likelihood_features = {
        **dict(observation_features),
        "metar_temperature_c": strong_constraints.get("current_temperature_c"),
        "observed_today_max_c": strong_constraints.get("observed_today_max_c"),
        "temperature_trajectory_state": temperature_trajectory.get("state"),
        "temperature_trajectory_gate_state": temperature_trajectory.get("gate_state"),
        "temperature_trajectory_gate_reason": temperature_trajectory.get("gate_reason"),
        "temperature_trajectory_effective_rate_c_per_hour": temperature_trajectory.get("effective_rate_c_per_hour"),
        "temperature_trajectory_adjusted_rate_c_per_hour": temperature_trajectory.get("adjusted_rate_c_per_hour"),
        "phase": pipeline.get("phase") or posterior_runtime.get("phase"),
        "beta_weak": pipeline.get("beta_weak") or posterior_runtime.get("beta_weak"),
        "beta_strong": pipeline.get("beta_strong") or posterior_runtime.get("beta_strong"),
        "beta_weak_base": pipeline.get("beta_weak_base"),
        "beta_strong_base": pipeline.get("beta_strong_base"),
        "hard_mask_reason": pipeline.get("hard_mask_reason") or posterior_runtime.get("hard_mask_reason"),
        "production_model_version": pipeline.get("production_model_version") or posterior_runtime.get("production_model_version"),
        "production_model_ready": pipeline.get("production_model_ready"),
        "production_block_reason": pipeline.get("production_block_reason") or posterior_runtime.get("production_block_reason"),
        "remaining_warming_source": pipeline.get("remaining_warming_source") or posterior_runtime.get("remaining_warming_source"),
        "v31_model_state": v31_multimodel_posterior.get("state"),
        "v31_model_valid_time_utc": v31_multimodel_posterior.get("synoptic_valid_time_utc"),
    }
    for key, value in recoil_ssot_fields.items():
        if value not in (None, "", [], {}):
            weather_likelihood_features[key] = copy.deepcopy(value)
    payload: dict[str, Any] = {
        "fact_payload_schema": POSTERIOR_FACT_PAYLOAD_SCHEMA,
        "posterior_runtime_schema": pipeline.get("schema") or "",
        "posterior_composer_schema": posterior_runtime.get("schema") or "",
        "posterior_activation_mode": str(
            pipeline.get("posterior_activation_mode")
            or posterior_runtime.get("activation_mode")
            or "live_no_shadow"
        ),
        "fact_snapshot_id": str(fact_snapshot_id or pipeline.get("fact_snapshot_id") or posterior_runtime.get("fact_snapshot_id") or ""),
        "posterior_run_id": str(posterior_run_id or pipeline.get("posterior_run_id") or posterior_runtime.get("posterior_run_id") or ""),
        "event_date": str(pipeline.get("event_date") or ""),
        "posterior_as_of": str(pipeline.get("as_of") or posterior_runtime.get("as_of") or ""),
        "phase": pipeline.get("phase") or posterior_runtime.get("phase"),
        "beta_weak": pipeline.get("beta_weak") or posterior_runtime.get("beta_weak"),
        "beta_strong": pipeline.get("beta_strong") or posterior_runtime.get("beta_strong"),
        "beta_weak_base": pipeline.get("beta_weak_base"),
        "beta_strong_base": pipeline.get("beta_strong_base"),
        "phase_reason": pipeline.get("phase_reason") or posterior_runtime.get("phase_reason"),
        "hard_mask_reason": pipeline.get("hard_mask_reason") or posterior_runtime.get("hard_mask_reason"),
        "posterior_temperature_distribution": copy.deepcopy(posterior_distribution),
        "posterior_temperature_distribution_map": copy.deepcopy(posterior_distribution_map),
        "posterior_continuous_distribution": copy.deepcopy(posterior_continuous_distribution),
        "posterior_bucket_cdf": copy.deepcopy(posterior_bucket_cdf),
        "posterior_quantiles_c": copy.deepcopy(posterior_quantiles_c),
        "posterior_quantile_grid": copy.deepcopy(posterior_quantile_grid),
        "sequential_state_filter": sequential_state_filter,
        "canonical_metar_snapshot": canonical_metar_snapshot,
        "predicted_peak_time_prior": predicted_peak_time_prior,
        "predicted_peak_time_posterior": predicted_peak_time_posterior,
        "observed_peak_platform": observed_peak_platform,
        "remaining_warming_mixture": remaining_warming_mixture,
        "production_model_version": pipeline.get("production_model_version") or posterior_runtime.get("production_model_version"),
        "production_model_ready": pipeline.get("production_model_ready"),
        "production_block_reason": pipeline.get("production_block_reason") or posterior_runtime.get("production_block_reason"),
        "remaining_warming_source": pipeline.get("remaining_warming_source") or posterior_runtime.get("remaining_warming_source"),
        "v31_multimodel_posterior": v31_multimodel_posterior,
        "observation_information_gate": observation_information_gate,
        "forecast_error_components": forecast_error_components,
        "distribution": copy.deepcopy(posterior_distribution),
        "prior_distribution": copy.deepcopy(_as_mapping(_as_mapping(layer_contributions.get("prior")).get("distribution"))),
        "forecast_weak_likelihood": copy.deepcopy(
            weak_runtime.get("forecast_weak_likelihood_map")
            or _as_mapping(_as_mapping(layer_contributions.get("forecast_weak_likelihood")).get("distribution"))
        ),
        "forecast_group_pooling": forecast_group_pooling,
        "forecast_weak_attribution": forecast_weak_attribution,
        "weak_likelihood_source_set": copy.deepcopy(_as_mapping(weak_runtime.get("weak_source_set"))),
        "weak_likelihood_surface_selection": copy.deepcopy(
            _as_mapping(weak_runtime.get("weak_source_surface_selection"))
        ),
        "forecast_multimodality_trace": copy.deepcopy(
            _as_mapping(weak_runtime.get("forecast_multimodality_trace"))
        ),
        "joint_weak_artifact_sha256": str(weak_runtime.get("artifact_sha256") or ""),
        "joint_weak_forecast_evidence_snapshot_id": str(
            weak_runtime.get("forecast_evidence_snapshot_id") or ""
        ),
        "likelihood_information_quality": likelihood_information_quality,
        "metar_strong_likelihood": copy.deepcopy(
            strong_runtime.get("metar_strong_likelihood_map")
            or _as_mapping(_as_mapping(layer_contributions.get("metar_strong_likelihood")).get("distribution"))
        ),
        "state_probs": copy.deepcopy(strong_runtime.get("state_probs") or {}),
        "remaining_warming_by_state": copy.deepcopy(strong_runtime.get("remaining_warming_by_state") or []),
        "recoil_state_ssot": copy.deepcopy(recoil_ssot_fields.get("recoil_state_ssot") or {}),
        "physical_cutoff_evidence": copy.deepcopy(recoil_ssot_fields.get("physical_cutoff_evidence") or {}),
        "temperature_trajectory": copy.deepcopy(dict(temperature_trajectory)),
        "daily_high_observation_candidate": daily_high_candidate_shadow,
        "daily_high_candidate_live_rollout": daily_high_candidate_live_rollout,
        "hard_constraints": copy.deepcopy(pipeline.get("hard_constraints") or posterior_runtime.get("hard_constraints") or {}),
        "layer_contributions": copy.deepcopy(layer_contributions),
        "source_lineage": copy.deepcopy(pipeline.get("source_lineage") or {}),
        "source_acquisition_trace": trace,
        "source_event_ids": list(dict.fromkeys([
            *_source_event_ids(trace),
            *([str(v31_multimodel_posterior.get("source_event_id"))] if v31_multimodel_posterior.get("source_event_id") else []),
        ])),
        "source_observation_ids": list(dict.fromkeys([
            *_source_observation_ids(trace),
            *([str(v31_multimodel_posterior.get("source_observation_id"))] if v31_multimodel_posterior.get("source_observation_id") else []),
        ])),
        "source_latency_trace": copy.deepcopy(trace.get("source_latency_trace") or []),
        "weather_likelihood_features": weather_likelihood_features,
        "na_reasons": copy.deepcopy(pipeline.get("na_reasons") or posterior_runtime.get("na_reasons") or []),
        "normalization": copy.deepcopy(pipeline.get("normalization") or posterior_runtime.get("normalization") or {}),
        "posterior_boundary": copy.deepcopy(pipeline.get("boundary") or {}),
        "runtime_schemas": {
            "phase_weighting": phase_runtime.get("schema") or "",
            "forecast_weak_likelihood": weak_runtime.get("schema") or "",
            "metar_strong_likelihood": strong_runtime.get("schema") or "",
            "hierarchical_temperature_posterior": posterior_runtime.get("schema") or "",
            "sequential_remaining_warming_bayes": str(posterior_runtime.get("schema") or ""),
            "sequential_state_filter": str(sequential_state_filter.get("schema") or ""),
            "predicted_peak_time_prior": str(predicted_peak_time_prior.get("schema") or ""),
            "predicted_peak_time_posterior": str(predicted_peak_time_posterior.get("schema") or ""),
            "observed_peak_platform": str(observed_peak_platform.get("schema") or ""),
            "remaining_warming_mixture": str(remaining_warming_mixture.get("schema") or ""),
            "v31_multimodel_posterior": str(v31_multimodel_posterior.get("schema") or ""),
            "observation_information_gate": str(observation_information_gate.get("schema") or ""),
            "daily_high_observation_candidate": str(daily_high_candidate_shadow.get("schema") or ""),
            "daily_high_candidate_live_rollout": str(daily_high_candidate_live_rollout.get("schema") or ""),
            "forecast_group_pooling": str(forecast_group_pooling.get("schema") or ""),
            "likelihood_information_quality": str(likelihood_information_quality.get("schema") or ""),
        },
        "runtime_policy_versions": {
            "metar_strong_likelihood": strong_runtime.get("runtime_policy_version") or "",
            "daily_high_observation_candidate": str(daily_high_candidate_shadow.get("policy_version") or ""),
            "daily_high_candidate_live_rollout": str(daily_high_candidate_live_rollout.get("candidate_policy_version") or ""),
            "likelihood_information_quality": str(likelihood_information_quality.get("policy_version") or ""),
        },
    }
    posterior_ssot_fact = build_posterior_ssot_fact(payload, source_acquisition_trace=trace)
    payload["posterior_ssot_fact"] = posterior_ssot_fact
    if posterior_ssot_fact.get("state") == "available" and posterior_ssot_fact.get("target_bucket"):
        target_bucket = str(posterior_ssot_fact.get("target_bucket") or "")
        target_probability = _prob01(posterior_ssot_fact.get("target_probability"))
        selection = _posterior_ssot_target_selection(
            target_bucket=target_bucket,
            target_probability=target_probability,
            fact_snapshot_id=str(payload.get("fact_snapshot_id") or ""),
            posterior_run_id=str(payload.get("posterior_run_id") or ""),
        )
        payload.setdefault("posterior_target_bucket_selection", copy.deepcopy(selection))
        payload.setdefault("target_bucket_selection", copy.deepcopy(selection))
        _sync_top_level_posterior_ssot_target_fields(
            payload,
            target_bucket=target_bucket,
            target_probability=target_probability,
        )
        _sync_distribution_rows_from_posterior_ssot_fact(
            payload,
            source="weather_fact_snapshot_writer.hierarchical_posterior_payload",
            sync_posterior_temperature_distribution_list=True,
        )
    if _non_empty(strong_constraints.get("observed_today_max_c")):
        payload["observed_today_max_c"] = strong_constraints.get("observed_today_max_c")
        hard_ids = trace.get("hard_observation_candidate_ids") if isinstance(trace.get("hard_observation_candidate_ids"), Sequence) else []
        payload["observed_today_max_source"] = str(hard_ids[0]) if hard_ids else "metar_strong_likelihood_runtime"
    for key, value in dict(extra_payload or {}).items():
        if key == "extra_payload_preserved":
            continue
        if key in {"weather_likelihood_features", "weather_likelihood_features_compact"} and isinstance(value, Mapping):
            existing = dict(_as_mapping(payload.get(key)))
            changed = False
            for feature_key, feature_value in value.items():
                if feature_value in (None, "", [], {}):
                    continue
                if existing.get(feature_key) in (None, "", [], {}):
                    existing[feature_key] = copy.deepcopy(feature_value)
                    changed = True
            if changed:
                payload[key] = existing
            continue
        if key not in payload:
            payload[key] = copy.deepcopy(value)
        elif payload.get(key) != value:
            preserved = payload.setdefault("extra_payload_preserved", {})
            if not isinstance(preserved, dict) or key in preserved:
                continue
            candidate = _preserved_conflict_value(key, value)
            if (
                _json_size_bytes(preserved) + _json_size_bytes(candidate)
                > EXTRA_PAYLOAD_PRESERVE_MAX_TOTAL_BYTES
            ):
                candidate = {
                    "schema": "extra_payload_conflict_digest_v1",
                    "path": str(key or ""),
                    "omitted": True,
                    "reason": "preserved_payload_total_limit",
                    "original_size_bytes": _json_size_bytes(value),
                }
            preserved[key] = candidate
    return payload


def build_hierarchical_posterior_fact_snapshot_write_plan(
    *,
    slug: str,
    event_date: str,
    pipeline_output: Mapping[str, Any],
    source_acquisition_trace: Mapping[str, Any] | None = None,
    run_date: str = "",
    fact_as_of: str = "",
    current_fact_id: int | None = None,
    current_fact_as_of: str = "",
    current_payload: Mapping[str, Any] | None = None,
    write_reason: str = "hierarchical_posterior_pipeline_no_apply",
    posterior_run_id: str = "",
    extra_payload: Mapping[str, Any] | None = None,
    include_payload: bool = False,
    write_latest: bool = True,
    enable_apply_contract: bool = False,
) -> dict[str, Any]:
    fact_as_of = str(fact_as_of or utc_now_iso())
    pipeline = _as_mapping(pipeline_output)
    trace = _pipeline_source_trace(pipeline, source_acquisition_trace)
    source_event_ids = _source_event_ids(trace)
    source_observation_ids = _source_observation_ids(trace)
    posterior_hash = sha256_json(
        {
            "posterior_temperature_distribution": pipeline.get("posterior_temperature_distribution"),
            "posterior_temperature_distribution_map": pipeline.get("posterior_temperature_distribution_map"),
            "phase": pipeline.get("phase"),
            "beta_weak": pipeline.get("beta_weak"),
            "beta_strong": pipeline.get("beta_strong"),
            "hard_mask_reason": pipeline.get("hard_mask_reason"),
        }
    )
    runtime_outputs = _as_mapping(pipeline.get("runtime_outputs"))
    metar_runtime = _as_mapping(runtime_outputs.get("metar_strong_likelihood"))
    candidate_live_rollout = _as_mapping(pipeline.get("daily_high_candidate_live_rollout"))
    if not candidate_live_rollout:
        candidate_live_rollout = _as_mapping(metar_runtime.get("daily_high_candidate_live_rollout"))
    fact_identity_salt = {
        "writer_path": "hierarchical_posterior_fact_snapshot",
        "posterior_hash": posterior_hash,
        "runtime_policy_versions": {
            "metar_strong_likelihood": str(metar_runtime.get("runtime_policy_version") or ""),
            "daily_high_candidate_live_rollout": {
                "mode": str(candidate_live_rollout.get("mode") or ""),
                "state": str(candidate_live_rollout.get("state") or ""),
                "candidate_policy_version": str(candidate_live_rollout.get("candidate_policy_version") or ""),
            },
        },
    }
    fact_snapshot_id = build_fact_snapshot_id(
        slug=str(slug or ""),
        event_date=str(event_date or ""),
        fact_as_of=fact_as_of,
        source_event_ids=source_event_ids,
        identity_salt=fact_identity_salt,
    )
    posterior_runtime = _runtime_output(pipeline, "hierarchical_temperature_posterior")
    effective_posterior_run_id = str(
        posterior_run_id
        or pipeline.get("posterior_run_id")
        or posterior_runtime.get("posterior_run_id")
        or build_posterior_run_id(
            slug=str(slug or ""),
            event_date=str(event_date or ""),
            fact_snapshot_id=fact_snapshot_id,
            source_event_ids=source_event_ids,
            posterior_hash=posterior_hash,
            phase=str(pipeline.get("phase") or posterior_runtime.get("phase") or ""),
        )
    )
    incoming_payload = build_hierarchical_posterior_fact_payload(
        pipeline_output=pipeline,
        fact_snapshot_id=fact_snapshot_id,
        posterior_run_id=effective_posterior_run_id,
        source_acquisition_trace=trace,
        extra_payload=extra_payload,
    )
    plan = build_fact_snapshot_write_plan(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
        incoming_payload=incoming_payload,
        current_fact_id=current_fact_id,
        current_fact_as_of=current_fact_as_of,
        current_payload=current_payload,
        source_acquisition_trace=trace,
        composer_version=f"{PARSER_VERSION}+hierarchical_posterior_pipeline_runtime_v1",
        write_reason=write_reason,
        posterior_run_id=effective_posterior_run_id,
        fact_identity_salt=fact_identity_salt,
        include_payload=include_payload,
        enable_apply_contract=bool(enable_apply_contract),
        write_latest=bool(write_latest),
    )
    plan["posterior_fact_snapshot_adapter"] = {
        "schema": "hierarchical_posterior_fact_snapshot_adapter_v1",
        "read_only": True,
        "dry_run": True,
        "pipeline_schema": pipeline.get("schema") or "",
        "pipeline_ok": bool(pipeline.get("ok")),
        "pipeline_read_only": bool(pipeline.get("read_only")),
        "pipeline_production_entry_enabled": bool(pipeline.get("production_entry_enabled")),
        "posterior_run_id": effective_posterior_run_id,
        "fact_snapshot_id": fact_snapshot_id,
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
        "posterior_hash": posterior_hash,
        "old_additive_evidence_pool_not_used": bool(_as_mapping(pipeline.get("boundary")).get("old_additive_evidence_pool_not_used")),
        "next_boundary": "review_fact_snapshot_write_plan_before_any_apply_path",
    }
    return plan
