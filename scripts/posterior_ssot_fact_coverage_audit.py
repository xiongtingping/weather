#!/usr/bin/env python3
"""Read-only coverage audit for posterior_ssot_fact inside fact snapshots.

This validates the backend Bayes SSOT fact itself, before UI, review, or
execution consumers interpret it. It never recomputes posterior probabilities,
does not write archive.db, and does not fetch external weather sources.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "data" / "archive.db"
POSTERIOR_SSOT_FACT_SCHEMA = "bayes_posterior_ssot_fact_v1"
POSTERIOR_SSOT_FACT_PLACEHOLDER_SCHEMA = "bayes_posterior_ssot_fact_placeholder_v1"
SUMMARY_SCHEMA = "posterior_ssot_fact_coverage_audit_v1_summary"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _prob01(value: Any) -> Optional[float]:
    parsed = _to_float(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed = parsed / 100.0
    if parsed < 0.0 or parsed > 1.0:
        return None
    return float(parsed)


def _bucket_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.replace("°", "").replace("c", "").replace(" ", "")


def _source_text_is_hard_temperature_observation(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in ("awc", "metar", "zuuu", "hard_source", "settlement", "official"))


def _hard_temperature_floor_c(payload: Mapping[str, Any]) -> Optional[float]:
    candidates: list[float] = []

    def add(raw: Any) -> None:
        value = _to_float(raw)
        if value is not None:
            candidates.append(float(value))

    def add_from_mapping(raw: Any) -> None:
        row = _as_mapping(raw)
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
        source_text = _first_text(
            row.get("observed_today_max_source"),
            row.get("hard_source_observed_source"),
            row.get("current_temperature_source"),
            row.get("observed_current_temp_source"),
            row.get("temperature_source"),
        )
        if _source_text_is_hard_temperature_observation(source_text):
            add(row.get("observed_today_max_c"))
            add(row.get("current_temperature_c"))
            add(row.get("current_temp_c"))

    add_from_mapping(payload)
    add_from_mapping(payload.get("weather_likelihood_features"))
    add_from_mapping(payload.get("weather_likelihood_features_compact"))
    hard_constraints = _as_mapping(payload.get("hard_constraints"))
    add_from_mapping(hard_constraints)
    for child in hard_constraints.values():
        add_from_mapping(child)
    for rows_key in ("posterior_temperature_distribution", "distribution", "rows"):
        rows = payload.get(rows_key)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            continue
        for raw_row in rows:
            add_from_mapping(raw_row)
    if not candidates:
        return None
    return max(candidates)


def _bucket_upper_bound_c(label: Any) -> Optional[float]:
    text = str(label or "").strip().lower()
    if not text:
        return None
    if any(token in text for token in ("or higher", "以上", ">=")):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def _bucket_below_hard_floor(label: Any, hard_floor_c: Optional[float]) -> bool:
    if hard_floor_c is None:
        return False
    upper_bound = _bucket_upper_bound_c(label)
    if upper_bound is None:
        return False
    return math.floor(float(hard_floor_c)) > float(upper_bound) + 1e-9


def _probability_from_row(row: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "probability",
        "posterior",
        "q_temperature_bucket_posterior",
        "target_probability",
        "target_bucket_probability",
    ):
        parsed = _prob01(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _distribution_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        payload.get("posterior_temperature_distribution"),
        payload.get("distribution"),
        payload.get("bucket_probabilities"),
    )
    for raw_rows in candidates:
        if not isinstance(raw_rows, list) or not raw_rows:
            continue
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            row = _as_mapping(raw)
            if not row:
                continue
            bucket = _first_text(row.get("outcome"), row.get("bucket"), row.get("label"))
            prob = _probability_from_row(row)
            if bucket and prob is not None:
                rows.append({"bucket": bucket, "probability": prob})
        if rows:
            return rows
    raw_map = payload.get("posterior_temperature_distribution_map")
    if isinstance(raw_map, Mapping):
        rows = []
        for bucket, probability in raw_map.items():
            prob = _prob01(probability)
            if str(bucket or "").strip() and prob is not None:
                rows.append({"bucket": str(bucket), "probability": prob})
        if rows:
            return rows
    return []


def _posterior_surface_probability_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def append_row(raw: Any, *, path: str) -> None:
        row = _as_mapping(raw)
        if not row:
            return
        bucket = _first_text(row.get("outcome"), row.get("bucket"), row.get("label"))
        prob = _probability_from_row(row)
        if not bucket or prob is None:
            return
        rows.append(
            {
                "path": path,
                "bucket": bucket,
                "probability": float(prob),
                "posterior_field": _first_text(
                    row.get("posterior_field"),
                    row.get("target_bucket_probability_field"),
                    row.get("target_probability_field"),
                ),
            }
        )

    def append_rows(raw_rows: Any, *, path: str) -> None:
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes, bytearray)):
            return
        for index, raw in enumerate(raw_rows):
            append_row(raw, path=f"{path}[{index}]")

    posterior_distribution = payload.get("posterior_temperature_distribution")
    if isinstance(posterior_distribution, Mapping):
        append_rows(
            posterior_distribution.get("bucket_probabilities"),
            path="posterior_temperature_distribution.bucket_probabilities",
        )
    else:
        append_rows(posterior_distribution, path="posterior_temperature_distribution")
    for rows_key in ("distribution", "rows", "bucket_probabilities"):
        append_rows(payload.get(rows_key), path=rows_key)
    raw_map = payload.get("posterior_temperature_distribution_map")
    if isinstance(raw_map, Mapping):
        for bucket, probability in raw_map.items():
            bucket_text = str(bucket or "")
            if "__pre_" in bucket_text:
                continue
            prob = _prob01(probability)
            if bucket_text.strip() and prob is not None:
                rows.append(
                    {
                        "path": f"posterior_temperature_distribution_map.{bucket_text}",
                        "bucket": bucket_text,
                        "probability": float(prob),
                        "posterior_field": "posterior_temperature_distribution_map",
                    }
                )
    return rows


def _hard_constraint_probability_violations(
    payload: Mapping[str, Any],
    *,
    hard_temperature_floor_c: Optional[float],
    tolerance: float = 1e-9,
) -> list[dict[str, Any]]:
    if hard_temperature_floor_c is None:
        return []
    violations: list[dict[str, Any]] = []
    for row in _posterior_surface_probability_rows(payload):
        bucket = row.get("bucket")
        probability = _prob01(row.get("probability"))
        if probability is None or float(probability) <= float(tolerance):
            continue
        if not _bucket_below_hard_floor(bucket, hard_temperature_floor_c):
            continue
        violations.append(
            {
                "path": row.get("path"),
                "bucket": bucket,
                "probability": float(probability),
                "hard_temperature_floor_c": float(hard_temperature_floor_c),
                "posterior_field": row.get("posterior_field") or "",
            }
        )
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    for index, row in enumerate(_distribution_rows(ssot)):
        bucket = row.get("bucket")
        probability = _prob01(row.get("probability"))
        if probability is None or float(probability) <= float(tolerance):
            continue
        if not _bucket_below_hard_floor(bucket, hard_temperature_floor_c):
            continue
        violations.append(
            {
                "path": f"posterior_ssot_fact.bucket_probabilities[{index}]",
                "bucket": bucket,
                "probability": float(probability),
                "hard_temperature_floor_c": float(hard_temperature_floor_c),
                "posterior_field": "posterior_ssot_fact.bucket_probabilities",
            }
        )
    return violations[:20]


def _posterior_ssot_probability_map(ssot: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    rows = ssot.get("bucket_probabilities")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return out
    for raw in rows:
        row = _as_mapping(raw)
        bucket = _first_text(row.get("outcome"), row.get("bucket"), row.get("label"))
        prob = _probability_from_row(row)
        if bucket and prob is not None:
            out[_bucket_key(bucket)] = float(prob)
    return out


def _posterior_surface_alignment(
    payload: Mapping[str, Any],
    ssot: Mapping[str, Any],
    *,
    tolerance: float = 1e-9,
) -> tuple[list[str], list[dict[str, Any]]]:
    ssot_probs = _posterior_ssot_probability_map(ssot)
    if not ssot_probs:
        return [], []
    mismatches: list[dict[str, Any]] = []
    for row in _posterior_surface_probability_rows(payload):
        key = _bucket_key(row.get("bucket"))
        if key not in ssot_probs:
            continue
        surface_prob = _prob01(row.get("probability"))
        ssot_prob = ssot_probs[key]
        if surface_prob is None:
            continue
        if abs(float(surface_prob) - float(ssot_prob)) <= float(tolerance):
            continue
        mismatches.append(
            {
                "path": row.get("path"),
                "bucket": row.get("bucket"),
                "surface_probability": float(surface_prob),
                "posterior_ssot_probability": float(ssot_prob),
                "delta": float(surface_prob) - float(ssot_prob),
                "posterior_field": row.get("posterior_field") or "",
            }
        )
    issues: list[str] = []
    surface_mismatches: list[dict[str, Any]] = []
    if mismatches:
        issues.append("posterior_surface_probability_mismatch_posterior_ssot_fact")
    return issues, mismatches[:20]


def _top_distribution_bucket(
    rows: Sequence[Mapping[str, Any]],
    *,
    hard_temperature_floor_c: Optional[float] = None,
) -> tuple[str, Optional[float]]:
    top_bucket = ""
    top_probability: Optional[float] = None
    for row in rows:
        prob = _prob01(row.get("probability"))
        bucket = _first_text(row.get("bucket"))
        if not bucket or prob is None:
            continue
        if _bucket_below_hard_floor(bucket, hard_temperature_floor_c):
            continue
        if top_probability is None or prob > top_probability:
            top_bucket = bucket
            top_probability = prob
    return top_bucket, top_probability


def _selection_issues(
    *,
    name: str,
    selection: Mapping[str, Any],
    ssot_bucket: str,
    ssot_probability: Optional[float],
    fact_snapshot_id: str,
    posterior_run_id: str,
) -> list[str]:
    issues: list[str] = []
    if not selection:
        return [f"{name}_missing"]
    source = _first_text(
        selection.get("ssot_source"),
        selection.get("target_bucket_selection_source"),
        selection.get("target_bucket_source_semantics"),
    )
    if "posterior_ssot_fact" not in source:
        issues.append(f"{name}_not_from_posterior_ssot_fact")
    if selection.get("selection_runtime_recomputed") is not False:
        issues.append(f"{name}_runtime_recomputed_not_false")
    bucket = _first_text(
        selection.get("target_bucket"),
        selection.get("selected_bucket"),
        selection.get("ssot_target_bucket"),
    )
    if ssot_bucket and bucket and _bucket_key(bucket) != _bucket_key(ssot_bucket):
        issues.append(f"{name}_target_bucket_mismatch")
    probability = None
    for key in ("target_probability", "target_posterior", "target_bucket_probability"):
        probability = _prob01(selection.get(key))
        if probability is not None:
            break
    if (
        ssot_probability is not None
        and probability is not None
        and abs(float(probability) - float(ssot_probability)) > 1e-9
    ):
        issues.append(f"{name}_target_probability_mismatch")
    field = _first_text(
        selection.get("target_probability_field"),
        selection.get("target_bucket_probability_field"),
        selection.get("posterior_field"),
    )
    if field and "posterior_ssot_fact" not in field:
        issues.append(f"{name}_probability_field_not_posterior_ssot_fact")
    selection_fact_snapshot_id = _first_text(selection.get("fact_snapshot_id"))
    if fact_snapshot_id and selection_fact_snapshot_id and selection_fact_snapshot_id != fact_snapshot_id:
        issues.append(f"{name}_fact_snapshot_id_mismatch")
    selection_posterior_run_id = _first_text(selection.get("posterior_run_id"))
    if posterior_run_id and selection_posterior_run_id and selection_posterior_run_id != posterior_run_id:
        issues.append(f"{name}_posterior_run_id_mismatch")
    return issues


def _top_level_target_issues(
    *,
    payload: Mapping[str, Any],
    ssot_bucket: str,
    ssot_probability: Optional[float],
) -> list[str]:
    issues: list[str] = []
    for key in ("target_bucket", "ssot_target_bucket"):
        value = _first_text(payload.get(key))
        if value and ssot_bucket and _bucket_key(value) != _bucket_key(ssot_bucket):
            issues.append(f"{key}_mismatch_posterior_ssot_fact")
    source = _first_text(payload.get("target_bucket_source"), payload.get("target_bucket_source_semantics"))
    if source and "posterior_ssot_fact" not in source:
        issues.append("target_bucket_source_not_posterior_ssot_fact")
    field = _first_text(payload.get("target_bucket_probability_field"))
    if field and "posterior_ssot_fact" not in field:
        issues.append("target_bucket_probability_field_not_posterior_ssot_fact")
    probability = _prob01(payload.get("target_bucket_probability"))
    if (
        ssot_probability is not None
        and probability is not None
        and abs(float(probability) - float(ssot_probability)) > 1e-9
    ):
        issues.append("target_bucket_probability_mismatch_posterior_ssot_fact")
    for key in ("target_bucket_probability_pct", "posterior_highest_probability_pct"):
        pct_value = _to_float(payload.get(key))
        probability_from_pct = float(pct_value) / 100.0 if pct_value is not None else None
        if (
            ssot_probability is not None
            and probability_from_pct is not None
            and abs(float(probability_from_pct) - float(ssot_probability)) > 1e-9
        ):
            issues.append(f"{key}_mismatch_posterior_ssot_fact")
    return issues


def audit_fact_payload(payload: Mapping[str, Any], *, meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
    meta_map = _as_mapping(meta)
    lineage = _as_mapping(payload.get("lineage_state"))
    posterior_runtime = _as_mapping(payload.get("posterior_runtime"))
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    rows = _distribution_rows(payload)
    ssot_rows = _distribution_rows(ssot) if ssot else []
    fact_snapshot_id = _first_text(payload.get("fact_snapshot_id"), lineage.get("fact_snapshot_id"))
    posterior_run_id = _first_text(
        payload.get("posterior_run_id"),
        lineage.get("posterior_run_id"),
        posterior_runtime.get("posterior_run_id"),
    )
    phase = _first_text(payload.get("phase"), ssot.get("phase"))
    phase_reason = _first_text(payload.get("phase_reason"), ssot.get("phase_reason"))
    hard_mask_reason = _first_text(payload.get("hard_mask_reason"), ssot.get("hard_mask_reason"))
    hard_temperature_floor_c = _hard_temperature_floor_c(payload)
    hard_constraint_violations = _hard_constraint_probability_violations(
        payload,
        hard_temperature_floor_c=hard_temperature_floor_c,
    )
    top_bucket, top_probability = _top_distribution_bucket(
        rows,
        hard_temperature_floor_c=hard_temperature_floor_c,
    )
    ssot_top_bucket, ssot_top_probability = _top_distribution_bucket(ssot_rows)
    ssot_probability_sum = sum(float(row.get("probability") or 0.0) for row in ssot_rows)
    beta_weak = _to_float(_first_present(payload.get("beta_weak"), ssot.get("beta_weak")))
    beta_strong = _to_float(_first_present(payload.get("beta_strong"), ssot.get("beta_strong")))
    ssot_schema = _first_text(ssot.get("schema")) if ssot else ""
    ssot_state = _first_text(ssot.get("state")) if ssot else ""
    ssot_not_materialized = bool(
        ssot
        and (
            ssot_schema == POSTERIOR_SSOT_FACT_PLACEHOLDER_SCHEMA
            or ssot_state in {"not_materialized", "pending_model_snapshot"}
        )
    )

    issues: list[str] = []
    surface_mismatches: list[dict[str, Any]] = []
    if not ssot:
        issues.append("posterior_ssot_fact_missing")
    else:
        if ssot_schema not in {POSTERIOR_SSOT_FACT_SCHEMA, POSTERIOR_SSOT_FACT_PLACEHOLDER_SCHEMA}:
            issues.append("posterior_ssot_fact_schema_invalid")
        if ssot_state != "available":
            issues.append("posterior_ssot_fact_not_available")
            if ssot_not_materialized:
                issues.append("posterior_ssot_fact_not_materialized")
        ssot_fact_snapshot_id = _first_text(ssot.get("fact_snapshot_id"))
        ssot_posterior_run_id = _first_text(ssot.get("posterior_run_id"))
        if not ssot_fact_snapshot_id:
            issues.append("posterior_ssot_fact_snapshot_id_missing")
        elif fact_snapshot_id and ssot_fact_snapshot_id != fact_snapshot_id:
            issues.append("posterior_ssot_fact_snapshot_id_mismatch")
        if ssot_state == "available" and not ssot_posterior_run_id:
            issues.append("posterior_ssot_fact_posterior_run_id_missing")
        elif ssot_state == "available" and posterior_run_id and ssot_posterior_run_id != posterior_run_id:
            issues.append("posterior_ssot_fact_posterior_run_id_mismatch")
        ssot_bucket = _first_text(ssot.get("target_bucket"), ssot.get("ssot_target_bucket"))
        ssot_probability = _prob01(ssot.get("target_probability"))
        if ssot_state == "available":
            if _first_text(ssot.get("production_model_family")) != "v3.1":
                issues.append("posterior_ssot_fact_production_model_family_not_v31")
            if not _first_text(ssot.get("production_model_version")).lower().startswith("v3.1"):
                issues.append("posterior_ssot_fact_production_model_version_not_v31")
            if _first_text(ssot.get("production_probability_policy")) != "v3.1_only_fail_closed":
                issues.append("posterior_ssot_fact_production_probability_policy_invalid")
            if ssot.get("legacy_model_fallback_allowed") is not False:
                issues.append("posterior_ssot_fact_legacy_model_fallback_not_false")
            if not ssot_bucket:
                issues.append("posterior_ssot_fact_target_bucket_missing")
            if ssot_probability is None:
                issues.append("posterior_ssot_fact_target_probability_missing")
            field = _first_text(ssot.get("target_probability_field"))
            if "posterior_ssot_fact" not in field:
                issues.append("posterior_ssot_fact_probability_field_invalid")
            if not ssot_rows:
                issues.append("posterior_ssot_fact_bucket_probabilities_missing")
            elif abs(float(ssot_probability_sum) - 1.0) > 1e-6:
                issues.append("posterior_ssot_fact_bucket_probabilities_not_normalized")
            if ssot_top_bucket and ssot_bucket and _bucket_key(ssot_top_bucket) != _bucket_key(ssot_bucket):
                issues.append("posterior_ssot_fact_target_not_distribution_top")
            if (
                ssot_top_probability is not None
                and ssot_probability is not None
                and abs(float(ssot_top_probability) - float(ssot_probability)) > 1e-9
            ):
                issues.append("posterior_ssot_fact_target_probability_not_distribution_top")
            if _bucket_below_hard_floor(ssot_bucket, hard_temperature_floor_c):
                issues.append("posterior_ssot_fact_target_below_hard_temperature_floor")
            boundaries = _as_mapping(ssot.get("boundaries"))
            if boundaries.get("frontend_recompute_allowed") is not False:
                issues.append("posterior_ssot_fact_frontend_recompute_boundary_missing")
            if boundaries.get("decision_recompute_allowed") is not False:
                issues.append("posterior_ssot_fact_decision_recompute_boundary_missing")
            if boundaries.get("hard_mask_is_constraint_not_weight") is not True:
                issues.append("posterior_ssot_fact_hard_mask_boundary_missing")
            if not _as_mapping(ssot.get("layer_chain")):
                issues.append("posterior_ssot_fact_layer_chain_missing")
            for selection_name in ("posterior_target_bucket_selection", "target_bucket_selection"):
                issues.extend(
                    _selection_issues(
                        name=selection_name,
                        selection=_as_mapping(payload.get(selection_name)),
                        ssot_bucket=ssot_bucket,
                        ssot_probability=ssot_probability,
                        fact_snapshot_id=fact_snapshot_id,
                        posterior_run_id=posterior_run_id,
                    )
                )
            issues.extend(
                _top_level_target_issues(
                    payload=payload,
                    ssot_bucket=ssot_bucket,
                    ssot_probability=ssot_probability,
                )
            )
            surface_issues, surface_mismatches = _posterior_surface_alignment(payload, ssot)
            issues.extend(surface_issues)

    if not rows:
        issues.append("posterior_temperature_distribution_missing")
    if not fact_snapshot_id:
        issues.append("fact_snapshot_id_missing")
    if not posterior_run_id:
        issues.append("posterior_run_id_missing")
    if not phase:
        issues.append("phase_missing")
    elif phase == "unknown":
        issues.append("phase_unknown")
    if not phase_reason:
        issues.append("phase_reason_missing")
    elif phase_reason == "phase_not_materialized_for_this_writer_path":
        issues.append("phase_reason_not_materialized")
    if beta_weak is None:
        issues.append("beta_weak_missing")
    if beta_strong is None:
        issues.append("beta_strong_missing")
    if not hard_mask_reason:
        issues.append("hard_mask_reason_missing")
    if beta_weak is not None and not 0.0 <= beta_weak <= 1.0:
        issues.append("beta_weak_out_of_range")
    if beta_strong is not None and not 0.0 <= beta_strong <= 1.0:
        issues.append("beta_strong_out_of_range")
    if hard_constraint_violations:
        issues.append("d0_hard_constraint_violation")

    return {
        "ok": not issues,
        "fact_id": meta_map.get("id"),
        "slug": _first_text(meta_map.get("slug"), payload.get("slug")),
        "event_date": _first_text(meta_map.get("event_date"), payload.get("event_date")),
        "fact_as_of": _first_text(meta_map.get("fact_as_of"), payload.get("fact_as_of")),
        "composer_version": _first_text(meta_map.get("composer_version"), payload.get("composer_version")),
        "posterior_ssot_fact_present": bool(ssot),
        "posterior_ssot_fact_schema": ssot_schema,
        "posterior_ssot_fact_state": ssot_state,
        "posterior_ssot_fact_state_reason": _first_text(ssot.get("state_reason")) if ssot else "",
        "posterior_ssot_fact_available": ssot_state == "available" if ssot else False,
        "posterior_ssot_fact_not_materialized": ssot_not_materialized,
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": posterior_run_id,
        "phase": phase,
        "phase_reason": phase_reason,
        "beta_weak": beta_weak,
        "beta_strong": beta_strong,
        "hard_mask_reason": hard_mask_reason,
        "hard_temperature_floor_c": hard_temperature_floor_c,
        "hard_constraint_violation_count": len(hard_constraint_violations),
        "hard_constraint_violations": hard_constraint_violations,
        "target_bucket": _first_text(ssot.get("target_bucket")),
        "target_probability": _prob01(ssot.get("target_probability")),
        "distribution_top_bucket": top_bucket,
        "distribution_top_probability": top_probability,
        "distribution_bucket_count": len(rows),
        "posterior_ssot_distribution_top_bucket": ssot_top_bucket,
        "posterior_ssot_distribution_top_probability": ssot_top_probability,
        "posterior_ssot_distribution_probability_sum": ssot_probability_sum if ssot_rows else None,
        "posterior_ssot_distribution_bucket_count": len(ssot_rows),
        "posterior_surface_probability_mismatches": surface_mismatches,
        "issues": issues,
    }


def _forward_projected_payload_audit(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    *,
    db_path: Path,
) -> dict[str, Any]:
    from posterior_ssot_projection_runtime import build_fact_projection_no_apply

    slug = _first_text(payload.get("slug"), row["slug"])
    event_date = _first_text(payload.get("event_date"), row["event_date"])
    run_date = _first_text(payload.get("run_date"), row["run_date"], event_date)
    fact_as_of = _first_text(payload.get("fact_as_of"), row["fact_as_of"])
    projection = build_fact_projection_no_apply(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        payload=payload,
        fact_as_of=fact_as_of,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        composer_version=str(row["composer_version"] or ""),
        write_reason="posterior_ssot_forward_projection_coverage_audit",
        db_path=db_path,
    )
    plan = _as_mapping(projection.get("plan"))
    projected_payload = plan.get("effective_payload") if isinstance(plan.get("effective_payload"), Mapping) else {}
    if not projected_payload:
        projected_payload = projection.get("projected_payload") if isinstance(projection.get("projected_payload"), Mapping) else {}
    if not projected_payload:
        projected = {
            "ok": False,
            "fact_id": row["id"],
            "slug": row["slug"],
            "event_date": row["event_date"],
            "fact_as_of": row["fact_as_of"],
            "composer_version": row["composer_version"],
            "issues": ["writer_projection_payload_missing"],
        }
    else:
        projected = audit_fact_payload(
            projected_payload,
            meta={
                "id": row["id"],
                "slug": row["slug"],
                "event_date": row["event_date"],
                "fact_as_of": row["fact_as_of"],
                "composer_version": row["composer_version"],
            },
        )
    projected["coverage_mode"] = "forward_writer_projection_no_apply"
    projected["persisted_fact_id"] = row["id"]
    projected["projection_plan_ok"] = bool(plan.get("ok"))
    projected["projection_execution_mode"] = str(plan.get("execution_mode") or "")
    projected["projection_write_latest"] = bool(plan.get("write_latest"))
    projected["projection_apply_supported"] = bool(plan.get("apply_supported"))
    projected["projection_payload_changed_by_merge_fill"] = bool(plan.get("payload_changed_by_merge_fill"))
    projected["projection_fact_snapshot_id"] = str(plan.get("fact_snapshot_id") or "")
    projected["projection_posterior_run_id"] = str(plan.get("posterior_run_id") or "")
    projected["projection_mode"] = str(projection.get("projection_mode") or "")
    projected["projection_context"] = dict(_as_mapping(projection.get("context")))
    projected["projection_issues"] = list(projection.get("issues") or [])
    projected["projection_plan_actions"] = [
        {
            "action": str(action.get("action") or ""),
            "target_table": str(action.get("target_table") or ""),
            "would_write": bool(action.get("would_write")),
            "apply_status": str(action.get("apply_status") or ""),
            "block_reason": str(action.get("block_reason") or ""),
        }
        for action in list(plan.get("plan_actions") or [])
        if isinstance(action, Mapping)
    ]
    return projected


def _since_text(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(hours)))).isoformat(
        timespec="seconds"
    )


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _load_fact_rows(
    conn: sqlite3.Connection,
    *,
    fact_id: str = "",
    slug: str = "",
    event_date: str = "",
    since_hours: float = 72.0,
    limit: int = 100,
    latest_only: bool = False,
    active_only: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if fact_id:
        clauses.append("h.id = ?")
        params.append(int(fact_id))
    if slug:
        clauses.append("h.slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("h.event_date = ?")
        params.append(event_date)
    if not fact_id and since_hours > 0:
        clauses.append("h.fact_as_of >= ?")
        params.append(_since_text(float(since_hours)))
    if active_only:
        clauses.append("h.event_date >= ?")
        params.append(_today_et())
    latest_join = "JOIN event_fact_latest l ON l.fact_id = h.id" if latest_only else ""
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of,
               h.composer_version, h.fact_json, h.created_at
        FROM event_fact_history h
        {latest_join}
        {where}
        ORDER BY h.id DESC
        LIMIT ?
        """,
        [*params, max(1, int(limit))],
    ).fetchall()


def audit(
    db_path: Path | str = DEFAULT_DB,
    *,
    fact_id: str = "",
    slug: str = "",
    event_date: str = "",
    since_hours: float = 72.0,
    limit: int = 100,
    max_items_output: int = 20,
    project_forward: bool = False,
    latest_only: bool = False,
    active_only: bool = False,
) -> dict[str, Any]:
    db = Path(db_path)
    blockers: list[str] = []
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    issue_counts: Counter[str] = Counter()
    with _connect_ro(db) as conn:
        if not _table_exists(conn, "event_fact_history"):
            return {
                "ok": False,
                "schema": "posterior_ssot_fact_coverage_audit_v1",
                "read_only": True,
                "db": str(db),
                "blockers": ["event_fact_history_missing"],
                "warnings": [],
                "guards": _guards(),
                "coverage": {"checked_fact_count": 0},
                "items": [],
            }
        if latest_only and not _table_exists(conn, "event_fact_latest"):
            return {
                "ok": False,
                "schema": "posterior_ssot_fact_coverage_audit_v1",
                "read_only": True,
                "db": str(db),
                "blockers": ["event_fact_latest_missing_for_latest_only_scope"],
                "warnings": [],
                "guards": _guards(),
                "coverage": {"checked_fact_count": 0},
                "items": [],
            }
        rows = _load_fact_rows(
            conn,
            fact_id=fact_id,
            slug=slug,
            event_date=event_date,
            since_hours=float(since_hours),
            limit=int(limit),
            latest_only=bool(latest_only),
            active_only=bool(active_only),
        )
    if not rows:
        warnings.append("no_fact_rows_in_scope")
    for row in rows:
        payload_raw = _safe_json(row["fact_json"])
        if not isinstance(payload_raw, Mapping):
            item = {
                "ok": False,
                "fact_id": row["id"],
                "slug": row["slug"],
                "event_date": row["event_date"],
                "fact_as_of": row["fact_as_of"],
                "issues": ["fact_json_invalid"],
            }
        else:
            persisted_item = audit_fact_payload(payload_raw, meta=dict(row))
            if project_forward:
                item = _forward_projected_payload_audit(row, payload_raw, db_path=db)
                item["persisted_ok"] = bool(persisted_item.get("ok"))
                item["persisted_issues"] = list(persisted_item.get("issues") or [])
                item["persisted_target_bucket"] = persisted_item.get("target_bucket")
                item["persisted_target_probability"] = persisted_item.get("target_probability")
            else:
                item = persisted_item
        for issue in item.get("issues") or []:
            issue_counts[str(issue)] += 1
        items.append(item)
    if issue_counts:
        blockers.append(
            "posterior_ssot_forward_projection_incomplete"
            if project_forward
            else "posterior_ssot_fact_coverage_incomplete"
        )
    coverage = {
        "checked_fact_count": len(items),
        "ok_fact_count": sum(1 for item in items if item.get("ok")),
        "issue_fact_count": sum(1 for item in items if not item.get("ok")),
        "posterior_ssot_fact_present_count": sum(1 for item in items if item.get("posterior_ssot_fact_present")),
        "posterior_ssot_fact_available_count": sum(1 for item in items if item.get("posterior_ssot_fact_available")),
        "posterior_ssot_fact_not_materialized_count": sum(
            1 for item in items if item.get("posterior_ssot_fact_not_materialized")
        ),
        "posterior_ssot_fact_missing_count": sum(1 for item in items if not item.get("posterior_ssot_fact_present")),
        "phase_beta_complete_count": sum(
            1
            for item in items
            if item.get("phase")
            and item.get("phase") != "unknown"
            and item.get("phase_reason")
            and item.get("phase_reason") != "phase_not_materialized_for_this_writer_path"
            and item.get("beta_weak") is not None
            and item.get("beta_strong") is not None
            and item.get("hard_mask_reason")
        ),
        "hard_constraint_violation_count": sum(
            int(item.get("hard_constraint_violation_count") or 0) for item in items
        ),
        "hard_constraint_violation_fact_count": sum(
            1 for item in items if int(item.get("hard_constraint_violation_count") or 0) > 0
        ),
    }
    compact_items = items[: max(0, int(max_items_output))]
    return {
        "ok": not blockers,
        "schema": "posterior_ssot_fact_coverage_audit_v1",
        "read_only": True,
        "db": str(db),
        "scope": {
            "fact_id": str(fact_id or ""),
            "slug": str(slug or ""),
            "event_date": str(event_date or ""),
            "since_hours": float(since_hours),
            "limit": int(limit),
            "project_forward": bool(project_forward),
            "latest_only": bool(latest_only),
            "active_only": bool(active_only),
        },
        "coverage_mode": (
            "forward_writer_projection_no_apply"
            if project_forward
            else "persisted_fact_json"
        ),
        "coverage": coverage,
        "issue_counts": dict(sorted(issue_counts.items())),
        "blockers": blockers,
        "warnings": warnings,
        "guards": _guards(),
        "items": compact_items,
        "items_truncated": len(items) > len(compact_items),
        "next_boundary": (
            "forward_posterior_ssot_projection_ok_wait_for_next_fact_or_confirm_controlled_apply_if_needed"
            if project_forward and not blockers
            else "fix_forward_writer_projection_before_review_persistence"
            if project_forward
            else "materialize_posterior_ssot_fact_via_weather_fact_snapshot_writer_before_review_persistence"
        ),
    }


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "places_orders": False,
        "fetches_external_weather_sources": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
    }


def compact_audit(report: Mapping[str, Any]) -> dict[str, Any]:
    coverage = _as_mapping(report.get("coverage"))
    scope = _as_mapping(report.get("scope"))
    return {
        "schema": SUMMARY_SCHEMA,
        "source_schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "db": report.get("db"),
        "scope": dict(scope),
        "coverage_mode": report.get("coverage_mode"),
        "coverage": {
            "checked_fact_count": int(coverage.get("checked_fact_count") or 0),
            "ok_fact_count": int(coverage.get("ok_fact_count") or 0),
            "issue_fact_count": int(coverage.get("issue_fact_count") or 0),
            "posterior_ssot_fact_present_count": int(
                coverage.get("posterior_ssot_fact_present_count") or 0
            ),
            "posterior_ssot_fact_available_count": int(
                coverage.get("posterior_ssot_fact_available_count") or 0
            ),
            "posterior_ssot_fact_missing_count": int(
                coverage.get("posterior_ssot_fact_missing_count") or 0
            ),
            "phase_beta_complete_count": int(coverage.get("phase_beta_complete_count") or 0),
            "hard_constraint_violation_count": int(coverage.get("hard_constraint_violation_count") or 0),
            "hard_constraint_violation_fact_count": int(
                coverage.get("hard_constraint_violation_fact_count") or 0
            ),
        },
        "issue_counts": dict(_as_mapping(report.get("issue_counts"))),
        "blockers": list(report.get("blockers") or []),
        "warnings": list(report.get("warnings") or []),
        "guards": dict(_as_mapping(report.get("guards"))),
        "items_output_count": len(report.get("items") or []),
        "items_truncated": bool(report.get("items_truncated")),
        "next_boundary": report.get("next_boundary"),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--fact-id", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--since-hours", type=float, default=72.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-items-output", type=int, default=20)
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument(
        "--project-forward",
        action="store_true",
        help="Audit no-apply weather_fact_snapshot_writer projection instead of only persisted fact_json.",
    )
    args = parser.parse_args(argv)
    result = audit(
        args.db,
        fact_id=args.fact_id,
        slug=args.slug,
        event_date=args.event_date,
        since_hours=float(args.since_hours),
        limit=int(args.limit),
        max_items_output=int(args.max_items_output),
        project_forward=bool(args.project_forward),
        latest_only=bool(args.latest_only),
        active_only=bool(args.active_only),
    )
    payload = compact_audit(result) if bool(args.summary) else result
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
