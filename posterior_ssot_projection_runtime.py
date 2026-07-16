"""Read-only posterior SSOT projection helper.

This module is the shared bridge for source-only facts that carry fresh hard
observations but do not carry market bucket labels. It does not write archive
tables, fetch weather sources, place orders, or backfill history.
"""

from __future__ import annotations

import copy
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hierarchical_posterior_pipeline_runtime import build_hierarchical_posterior_pipeline
from metar_strong_likelihood_runtime import RUNTIME_POLICY_VERSION as CURRENT_METAR_STRONG_RUNTIME_POLICY_VERSION
from physical_temperature_prior_context_runtime import build_ncei_operational_climate_reference_prior_context
from physical_x_bucket_contract_runtime import build_physical_x_bucket_contract
from sequential_remaining_warming_bayes_runtime import SCHEMA_VERSION as CURRENT_POSTERIOR_COMPOSER_SCHEMA
from weather_ensemble_bias_correction import bucket_label_for_value
from weather_fact_snapshot_writer import (
    PARSER_VERSION,
    build_prior_layer_contract,
    build_fact_snapshot_write_plan,
    build_hierarchical_posterior_fact_snapshot_write_plan,
)


SCHEMA_VERSION = "posterior_ssot_projection_runtime_v1"
NCEI_PRIOR_ROOT = Path(__file__).resolve().parent / "data" / "research" / "ncei_zuuu_joint_model" / "2021_2025"


DERIVED_TEMPERATURE_TRAJECTORY_INPUT_KEYS = {
    "temperature_trajectory_state",
    "temperature_trajectory_gate_state",
    "temperature_trajectory_gate_reason",
    "temperature_trajectory_stable_rate_c_per_hour",
    "temperature_trajectory_recent_rate_c_per_hour",
    "temperature_trajectory_short_rate_c_per_hour",
    "temperature_trajectory_effective_rate_c_per_hour",
    "temperature_trajectory_adjusted_rate_c_per_hour",
    "temperature_trajectory_response_scale",
    "temperature_trajectory_confidence",
    "temperature_trajectory_realization_state",
    "temperature_momentum_effective_c_per_hour",
    "temperature_momentum_stable_c_per_hour",
}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or ""))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _parse_iso_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _max_iso_text(*values: Any) -> str:
    best_text = ""
    best_dt: datetime | None = None
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        parsed = _parse_iso_ts(text)
        if parsed is None:
            if not best_text:
                best_text = text
            continue
        parsed_utc = parsed.astimezone(timezone.utc)
        if best_dt is None or parsed_utc > best_dt:
            best_dt = parsed_utc
            best_text = text
    return best_text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _prob01(value: Any) -> float | None:
    parsed = _float_or_none(value)
    if parsed is None:
        return None
    if 1.0 < parsed <= 100.0:
        parsed = parsed / 100.0
    if parsed < 0.0 or parsed > 1.0:
        return None
    return float(parsed)


def _probability_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        bucket_rows = value.get("bucket_probabilities")
        if isinstance(bucket_rows, Sequence) and not isinstance(bucket_rows, (str, bytes, bytearray)):
            return _probability_rows(bucket_rows)
        rows: list[dict[str, Any]] = []
        for bucket, probability in value.items():
            if "__pre_" in str(bucket):
                continue
            prob = _prob01(probability)
            label = str(bucket or "").strip()
            if label and prob is not None:
                rows.append({"bucket": label, "probability": float(prob)})
        return rows
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    rows = []
    for raw in value:
        row = _as_mapping(raw)
        label = _text(row.get("bucket"), row.get("outcome"), row.get("label"))
        probability = None
        for key in (
            "probability",
            "posterior",
            "q_temperature_bucket_posterior",
            "q_bayes_posterior",
            "target_bucket_probability",
        ):
            probability = _prob01(row.get(key))
            if probability is not None:
                break
        if label and probability is not None:
            rows.append({"bucket": label, "probability": float(probability)})
    return rows


def _append_label(out: list[str], raw: Any) -> None:
    label = str(raw or "").strip()
    if label and label not in out:
        out.append(label)


def _bucket_sort_key(label: str) -> tuple[int, float, str]:
    import re

    match = re.search(r"-?\d+(?:\.\d+)?", str(label or ""))
    if not match:
        return (1, 0.0, str(label or ""))
    try:
        numeric = float(match.group(0))
    except ValueError:
        numeric = 0.0
    return (0, numeric, str(label or ""))


def _sorted_bucket_labels(labels: Sequence[str]) -> list[str]:
    return sorted([str(label) for label in labels], key=_bucket_sort_key)


def _bucket_labels_from_market_rows(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    labels: list[str] = []
    for raw in value:
        row = _as_mapping(raw)
        _append_label(labels, _text(row.get("bucket"), row.get("outcome"), row.get("label"), row.get("groupItemTitle")))
    return _sorted_bucket_labels(labels)


def bucket_labels_from_payload(payload: Mapping[str, Any] | None) -> list[str]:
    item = _as_mapping(payload)
    labels: list[str] = []
    market_context = _as_mapping(item.get("market_bucket_context"))
    market_context_labels = market_context.get("labels")
    if isinstance(market_context_labels, Sequence) and not isinstance(
        market_context_labels, (str, bytes, bytearray)
    ):
        for label in market_context_labels:
            _append_label(labels, label)
        if labels:
            return _sorted_bucket_labels(labels)
    for key in (
        "posterior_temperature_distribution",
        "posterior_temperature_distribution_map",
        "bucket_probabilities",
        "distribution",
        "rows",
    ):
        for row in _probability_rows(item.get(key)):
            label = str(row.get("bucket") or "").strip()
            if label and label not in labels:
                labels.append(label)
        if labels:
            return labels
    for key in ("forecast_weak_likelihood", "metar_strong_likelihood", "prior_distribution"):
        value = item.get(key)
        if isinstance(value, Mapping) and value:
            for label in value.keys():
                label_text = str(label or "").strip()
                if label_text and label_text not in labels:
                    labels.append(label_text)
            if labels:
                return labels
    return labels


def _source_trace(payload: Mapping[str, Any]) -> dict[str, Any]:
    trace = dict(_as_mapping(payload.get("source_acquisition_trace")))
    observations = trace.get("source_observations")
    if not isinstance(observations, list) and isinstance(payload.get("source_observations"), list):
        trace["source_observations"] = copy.deepcopy(payload.get("source_observations"))
    if "source_latency_trace" not in trace and isinstance(payload.get("source_latency_trace"), list):
        trace["source_latency_trace"] = copy.deepcopy(payload.get("source_latency_trace"))
    if "source_observation_ids" not in trace and isinstance(payload.get("source_observation_ids"), list):
        trace["source_observation_ids"] = copy.deepcopy(payload.get("source_observation_ids"))
    trace.setdefault("schema", "weather_source_acquisition_trace_v1")
    trace.setdefault("read_only", True)
    return trace


def _source_observations(payload: Mapping[str, Any]) -> list[Any]:
    trace = _source_trace(payload)
    observations = trace.get("source_observations")
    if isinstance(observations, list):
        return copy.deepcopy(observations)
    rows = payload.get("source_observations")
    return copy.deepcopy(rows) if isinstance(rows, list) else []


POSTERIOR_INPUT_SOURCE_CLASSES = {
    "hard_observation_candidate",
    "forecast_weak_source",
    "settlement_source",
}

STALE_OBSERVATION_INPUT_KEYS_WHEN_SOURCE_HISTORY_PRESENT = {
    "current_temp_c",
    "current_temperature_c",
    "metar_temperature_c",
    "observed_today_max_c",
    "today_max_c",
    "settlement_actual",
    "settlement_actual_c",
    "official_final_result",
    "final_daily_max_c",
    "final_bucket_lock",
    "hard_source_final_daily_max",
    "hard_source_settlement_final",
    "observed_today_max_final",
    "observed_today_max_is_final",
    "observed_today_max_is_official",
    "observed_today_max_trusted_final",
    "temperature_observation_final_daily_max",
    "no_more_warming_possible",
    "dewpoint_c",
    "metar_dewpoint_c",
    "visibility_m",
    "cloud_text",
    "clouds",
    "weather_text",
    "weather",
    "pressure_hpa",
    "metar_pressure_hpa",
    "wind_speed_mps",
    "momentum_rate_c_per_hour",
    "temperature_rate_c_per_hour",
    "temperature_trajectory_realization_state",
    "temperature_trajectory_stable_rate_c_per_hour",
    "expected_peak_hour_local",
    "expected_peak_at_local",
    "expected_peak_hour_source",
    "expected_peak_window_start_hour_local",
    "expected_peak_window_end_hour_local",
    "expected_peak_window_source",
    "expected_peak_window_basis",
    "peak_hour_local",
    "peak_window_start_hour_local",
    "peak_window_end_hour_local",
    "taf_forecast_peak_at",
    "taf_forecast_peak_local",
}


def _source_observation_posterior_input_allowed(row: Mapping[str, Any]) -> bool:
    payload = _as_mapping(row.get("payload"))
    for key in (
        "ui_display_derived_from_fact_features",
        "display_only",
        "not_posterior_input",
        "lineage_only_from_posterior_pipeline",
    ):
        if _truthy(row.get(key)) or _truthy(payload.get(key)):
            return False
    source_class = _text(row.get("source_class"), payload.get("source_class"))
    if source_class in POSTERIOR_INPUT_SOURCE_CLASSES:
        return True
    source_id = _text(row.get("source_id"), payload.get("source_id"), payload.get("source"))
    return source_id in {
        "zuuu_metar_raw_amsc",
        "zuuu_metar_raw_awc",
        "awc_cache_metars_csv_gz",
        "zuuu_taf_forecast_amsc",
        "zuuu_taf_forecast_peak",
        "settlement_actual",
    }


def _source_observation_key(row: Mapping[str, Any]) -> str:
    source_observation_id = _text(row.get("source_observation_id"))
    if source_observation_id:
        return f"source_observation_id:{source_observation_id}"
    return _source_observation_signature_key(row)


def _source_observation_signature_key(row: Mapping[str, Any]) -> str:
    key_payload = {
        "source_id": _text(row.get("source_id")),
        "source_class": _text(row.get("source_class")),
        "event_date": _text(row.get("event_date")),
        "valid_time": _text(row.get("valid_time"), row.get("valid_time_local")),
        "temperature_c": row.get("temperature_c"),
        "observed_today_max_c": row.get("observed_today_max_c"),
        "forecast_peak_c": row.get("forecast_peak_c"),
        "raw_ref": _text(row.get("raw_ref")),
    }
    return "source_observation_signature:" + json.dumps(key_payload, ensure_ascii=False, sort_keys=True, default=str)


def _source_observation_compact_value_key(row: Mapping[str, Any]) -> str:
    value = row.get("temperature_c")
    value_name = "temperature_c"
    if value in (None, ""):
        value = row.get("forecast_peak_c")
        value_name = "forecast_peak_c"
    if value in (None, ""):
        value = row.get("observed_today_max_c")
        value_name = "observed_today_max_c"
    if value in (None, ""):
        return ""
    parsed_value = _float_or_none(value)
    if parsed_value is not None:
        value = round(float(parsed_value), 6)
    key_payload = {
        "source_id": _text(row.get("source_id")),
        "source_class": _text(row.get("source_class")),
        "valid_time": _text(row.get("valid_time"), row.get("valid_time_local")),
        value_name: value,
    }
    return "source_observation_compact_value:" + json.dumps(key_payload, ensure_ascii=False, sort_keys=True, default=str)


def _dedupe_source_observations(rows: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        row = _as_mapping(raw)
        if not row:
            continue
        if not _source_observation_posterior_input_allowed(row):
            continue
        keys = {
            _source_observation_key(row),
            _source_observation_signature_key(row),
            _source_observation_compact_value_key(row),
        }
        if any(key in seen for key in keys):
            continue
        seen.update(key for key in keys if key)
        out.append(copy.deepcopy(dict(row)))
    return out


def _source_ids_from_observations(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        source_id = _text(row.get("source_id"))
        if source_id and source_id not in out:
            out.append(source_id)
    return out


def _source_event_ids_from_observations(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        source_event_id = _text(row.get("source_event_id"))
        if source_event_id and source_event_id not in out:
            out.append(source_event_id)
    return out


def _source_observation_ids_from_observations(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        source_observation_id = _text(row.get("source_observation_id"))
        if source_observation_id and source_observation_id not in out:
            out.append(source_observation_id)
    return out


def _source_latency_row_from_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_event_id": _text(row.get("source_event_id")),
        "source_observation_id": _text(row.get("source_observation_id")),
        "source_id": _text(row.get("source_id")),
        "source_class": _text(row.get("source_class")),
        "role": _text(row.get("role")),
        "station_id": _text(row.get("station_id")),
        "event_date": _text(row.get("event_date")),
        "valid_time": _text(row.get("valid_time")),
        "valid_time_local": _text(row.get("valid_time_local")),
        "fetched_at": _text(row.get("fetched_at")),
        "response_completed_at": _text(row.get("response_completed_at")),
        "response_duration_ms": row.get("response_duration_ms"),
        "temperature_c": row.get("temperature_c"),
        "observed_today_max_c": row.get("observed_today_max_c"),
        "forecast_peak_c": row.get("forecast_peak_c"),
        "raw_ref": _text(row.get("raw_ref")),
        "hard_eligible": bool(row.get("hard_eligible")),
        "demotion_reason": _text(row.get("demotion_reason")),
    }


def _source_trace_with_observations(
    base_trace: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    source_observation_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    trace = copy.deepcopy(dict(base_trace or {}))
    rows = [copy.deepcopy(dict(row)) for row in observations]
    trace["schema"] = _text(trace.get("schema"), "weather_source_acquisition_trace_v1")
    trace["read_only"] = True
    trace["source_observations"] = rows
    trace["source_event_ids"] = _source_event_ids_from_observations(rows)
    trace["source_observation_ids"] = _source_observation_ids_from_observations(rows)
    trace["hard_observation_candidate_ids"] = [
        _text(row.get("source_event_id"))
        for row in rows
        if _text(row.get("source_class")) == "hard_observation_candidate" and _text(row.get("source_event_id"))
    ]
    trace["forecast_weak_ids"] = [
        _text(row.get("source_event_id"))
        for row in rows
        if _text(row.get("source_class")) == "forecast_weak_source" and _text(row.get("source_event_id"))
    ]
    current_latency_keys = {
        key
        for row in rows
        for key in (_text(row.get("source_observation_id")), _text(row.get("source_event_id")))
        if key
    }
    existing_latency = trace.get("source_latency_trace")
    latency_rows: list[dict[str, Any]] = []
    seen_latency: set[str] = set()
    if isinstance(existing_latency, Sequence) and not isinstance(existing_latency, (str, bytes, bytearray)):
        for raw in existing_latency:
            row = _as_mapping(raw)
            key = _text(row.get("source_observation_id"), row.get("source_event_id"))
            if row and key and key in current_latency_keys and key not in seen_latency:
                seen_latency.add(key)
                latency_rows.append(copy.deepcopy(dict(row)))
    for row in rows:
        key = _text(row.get("source_observation_id"), row.get("source_event_id"))
        if key and key not in seen_latency:
            seen_latency.add(key)
            latency_rows.append(_source_latency_row_from_observation(row))
    trace["source_latency_trace"] = latency_rows
    if source_observation_context:
        trace["source_observation_context"] = copy.deepcopy(dict(source_observation_context))
    return trace


def _source_observations_from_history_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _dedupe_source_observations(_source_observations(payload))


def _source_observations_from_fact_payload_features(
    payload: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    fact_as_of: str,
) -> list[dict[str, Any]]:
    try:
        from weather_source_acquisition_manager import (
            build_source_acquisition_trace,
            source_payloads_from_fact_payload,
        )
    except Exception:
        return []
    source_payloads = source_payloads_from_fact_payload(
        payload,
        station_id=station_id,
        event_date=event_date,
        fact_as_of=fact_as_of,
    )
    if not source_payloads:
        return []
    trace = build_source_acquisition_trace(
        sources=source_payloads,
        station_id=station_id,
        fetched_at=fact_as_of,
        parser_version="posterior_ssot_projection_fact_feature_source_adapter_v1",
        include_payload=True,
    )
    observations = trace.get("source_observations")
    return _dedupe_source_observations(observations if isinstance(observations, list) else [])


def _float_sequence_from_json(value: Any) -> list[float]:
    raw = value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except Exception:
            raw = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    out: list[float] = []
    for item in raw:
        parsed = _float_or_none(item)
        if parsed is not None:
            out.append(float(parsed))
    return out


def _model_member_cache_source_observations_from_conn(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    station_id: str,
    fact_as_of: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not _table_exists(conn, "model_member_cache"):
        return [], {
            "schema": "model_member_cache_projection_context_v1",
            "state": "skipped_table_missing",
            "source_observation_count": 0,
            "boundary": "read_only_model_member_cache_forecast_weak_likelihood_adapter",
        }
    try:
        rows = conn.execute(
            """
            SELECT id, model, loc_key, event_date, market_var, market_unit, members_json,
                   created_at, official_updated_at, official_updated_source, members_signature
            FROM model_member_cache
            WHERE event_date = ?
              AND (market_var IS NULL OR market_var = '' OR market_var = 'temperature_2m_max')
              AND (market_unit IS NULL OR market_unit = '' OR market_unit = 'C')
              AND (
                    ? = ''
                    OR julianday(COALESCE(NULLIF(official_updated_at, ''), created_at))
                       <= julianday(?)
                  )
            ORDER BY datetime(COALESCE(official_updated_at, created_at)) DESC, id DESC
            LIMIT 120
            """,
            (event_date, str(fact_as_of or ""), str(fact_as_of or "")),
        ).fetchall()
    except sqlite3.Error as exc:
        return [], {
            "schema": "model_member_cache_projection_context_v1",
            "state": "sqlite_error",
            "error": str(exc),
            "source_observation_count": 0,
            "boundary": "read_only_model_member_cache_forecast_weak_likelihood_adapter",
        }

    selected: dict[str, sqlite3.Row] = {}
    selected_members: dict[str, list[float]] = {}
    for row in rows:
        model = _text(row["model"])
        if not model or model in selected:
            continue
        members = _float_sequence_from_json(row["members_json"])
        if not members:
            continue
        selected[model] = row
        selected_members[model] = members

    if not selected:
        return [], {
            "schema": "model_member_cache_projection_context_v1",
            "state": "not_found",
            "source_observation_count": 0,
            "boundary": "read_only_model_member_cache_forecast_weak_likelihood_adapter",
        }

    synthetic_payload = {
        "model_members_decimal_raw": selected_members,
        "model_member_cache_snapshot": {
            "schema": "model_member_cache_projection_snapshot_v1",
            "source": "archive_db.model_member_cache",
            "models": {},
        },
    }
    model_entries = synthetic_payload["model_member_cache_snapshot"]["models"]
    for model, row in selected.items():
        members = selected_members[model]
        model_entries[model] = {
            "mean_c": float(sum(members) / len(members)),
            "member_count": len(members),
            "created_at": _text(row["created_at"], fact_as_of),
            "official_updated_at": _text(row["official_updated_at"], row["created_at"], fact_as_of),
            "official_updated_source": _text(row["official_updated_source"], "model_member_cache"),
            "members_signature": _text(row["members_signature"]),
            "cache_source": "archive_db.model_member_cache",
            "model_member_cache_id": int(row["id"] or 0),
            "loc_key": _text(row["loc_key"]),
        }

    observations = _source_observations_from_fact_payload_features(
        synthetic_payload,
        station_id=station_id,
        event_date=event_date,
        fact_as_of=fact_as_of,
    )
    return observations, {
        "schema": "model_member_cache_projection_context_v1",
        "state": "merged" if observations else "not_found",
        "source_observation_count": len(observations),
        "source_ids": _source_ids_from_observations(observations),
        "model_count": len(selected),
        "model_member_cache_ids": [int(row["id"] or 0) for row in selected.values()],
        "model_member_cache_created_at_max": _max_iso_text(*[row["created_at"] for row in selected.values()]),
        "model_member_cache_official_updated_at_max": _max_iso_text(
            *[row["official_updated_at"] for row in selected.values()]
        ),
        "boundary": "read_only_model_member_cache_forecast_weak_likelihood_adapter",
    }


def _model_member_cache_source_observations(
    *,
    event_date: str,
    station_id: str,
    fact_as_of: str,
    db_path: Any = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not event_date:
        return [], {
            "schema": "model_member_cache_projection_context_v1",
            "state": "skipped_missing_event_date",
            "source_observation_count": 0,
            "boundary": "read_only_model_member_cache_forecast_weak_likelihood_adapter",
        }
    if conn is not None:
        return _model_member_cache_source_observations_from_conn(
            conn,
            event_date=event_date,
            station_id=station_id,
            fact_as_of=fact_as_of,
        )
    ro_conn = _open_ro(db_path)
    if ro_conn is None:
        return [], {
            "schema": "model_member_cache_projection_context_v1",
            "state": "skipped_db_unavailable",
            "source_observation_count": 0,
            "boundary": "read_only_model_member_cache_forecast_weak_likelihood_adapter",
        }
    try:
        return _model_member_cache_source_observations_from_conn(
            ro_conn,
            event_date=event_date,
            station_id=station_id,
            fact_as_of=fact_as_of,
        )
    finally:
        ro_conn.close()


def _payload_containers(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    for key in ("weather_likelihood_features", "weather_likelihood_features_compact"):
        value = _as_mapping(payload.get(key))
        if value:
            containers.append(value)
    rows = payload.get("rows")
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
        for row in rows:
            value = _as_mapping(row)
            if value:
                containers.append(value)
    containers.append(payload)
    return containers


def _first_container_value(
    containers: Sequence[Mapping[str, Any]],
    *keys: str,
) -> Any:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _soft_forecast_metric_from_event_run_payload(
    payload: Mapping[str, Any],
    channel: str,
) -> dict[str, Any]:
    containers = _payload_containers(payload)
    if channel == "wu":
        metric = _as_mapping(payload.get("wu_forecast_peak"))
        value_c = _float_or_none(
            _first_container_value(containers, "wu_forecast_peak_c")
        )
        if value_c is None:
            value_c = _float_or_none(metric.get("value_c"))
        if value_c is None:
            return {}
        return {
            "channel": "wu",
            "value_c": value_c,
            "at": _text(
                _first_container_value(containers, "wu_forecast_peak_at"),
                metric.get("peak_at"),
                metric.get("forecast_valid_local"),
            ),
            "source": _text(
                _first_container_value(containers, "wu_forecast_peak_source"),
                metric.get("source"),
                "wunderground_forecast_daily_peak",
            ),
            "source_url": _text(
                _first_container_value(containers, "wu_forecast_peak_source_url"),
                metric.get("forecast_source_url"),
                metric.get("source_url"),
                payload.get("resolution_source"),
            ),
        }
    if channel == "weather_com":
        metric = _as_mapping(payload.get("weather_com_forecast_peak"))
        value_c = _float_or_none(
            _first_container_value(
                containers,
                "weather_com_forecast_peak_c",
                "twc_forecast_peak_c",
            )
        )
        if value_c is None:
            value_c = _float_or_none(metric.get("value_c"))
        if value_c is None:
            return {}
        return {
            "channel": "weather_com",
            "value_c": value_c,
            "at": _text(
                _first_container_value(
                    containers,
                    "weather_com_forecast_peak_at",
                    "weather_com_forecast_peak_valid_local",
                    "twc_forecast_peak_at",
                    "twc_forecast_peak_valid_local",
                ),
                metric.get("forecast_valid_local"),
                metric.get("peak_at"),
            ),
            "source": _text(
                _first_container_value(
                    containers,
                    "weather_com_forecast_peak_source",
                    "twc_forecast_peak_source",
                ),
                metric.get("source"),
                "weather_com_forecast_daily_peak",
            ),
            "source_url": _text(
                _first_container_value(
                    containers,
                    "weather_com_forecast_peak_source_url",
                    "twc_forecast_peak_source_url",
                ),
                metric.get("source_url"),
            ),
        }
    return {}


def _soft_forecast_features_from_event_run_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    features: dict[str, Any] = {}
    wu = _soft_forecast_metric_from_event_run_payload(payload, "wu")
    if wu:
        features.update(
            {
                "wu_forecast_peak_c": wu["value_c"],
                "wu_forecast_peak_at": wu.get("at"),
                "wu_forecast_peak_source": wu.get("source"),
                "wu_forecast_peak_source_url": wu.get("source_url"),
                "wu_forecast_peak_soft": True,
                "wu_forecast_peak_usable_for_settlement": False,
            }
        )
    weather_com = _soft_forecast_metric_from_event_run_payload(payload, "weather_com")
    if weather_com:
        features.update(
            {
                "weather_com_forecast_peak_c": weather_com["value_c"],
                "weather_com_forecast_peak_at": weather_com.get("at"),
                "weather_com_forecast_peak_valid_local": weather_com.get("at"),
                "weather_com_forecast_peak_source": weather_com.get("source"),
                "weather_com_forecast_peak_source_url": weather_com.get("source_url"),
                "weather_com_forecast_peak_soft": True,
                "weather_com_forecast_peak_usable_for_settlement": False,
                "twc_provider": "Weather.com/TWC",
                "twc_provider_role": "weather_api_provider_wrapper_not_sensor_origin",
            }
        )
    return {key: value for key, value in features.items() if value not in (None, "", [], {})}


def _soft_forecast_event_run_source_observations_from_conn(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    station_id: str,
    fact_as_of: str,
    limit: int = 80,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context_base = {
        "schema": "soft_forecast_event_runs_projection_context_v1",
        "source": "archive_db.event_runs",
        "source_observation_count": 0,
        "boundary": "read_only_same_event_soft_forecast_event_runs_adapter_no_fetch_no_write",
    }
    if not slug or not event_date:
        return [], {**context_base, "state": "skipped_missing_slug_or_event_date"}
    if not _table_exists(conn, "event_runs"):
        return [], {**context_base, "state": "skipped_table_missing"}
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, payload_json
            FROM event_runs
            WHERE slug = ?
              AND event_date = ?
              AND payload_json IS NOT NULL
              AND payload_json != ''
              AND (? = '' OR julianday(created_at) <= julianday(?))
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (slug, event_date, str(fact_as_of or ""), str(fact_as_of or ""), max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error as exc:
        return [], {**context_base, "state": "sqlite_error", "error": str(exc)}

    selected_features: dict[str, Any] = {}
    selected_event_run_ids: list[int] = []
    selected_created_at: list[str] = []
    selected_channels: set[str] = set()
    scanned = 0
    for row in rows:
        scanned += 1
        payload = _parse_json(row["payload_json"])
        if not payload:
            continue
        features = _soft_forecast_features_from_event_run_payload(payload)
        if not features:
            continue
        channel_features: dict[str, tuple[str, ...]] = {
            "wu": (
                "wu_forecast_peak_c",
                "wu_forecast_peak_at",
                "wu_forecast_peak_source",
                "wu_forecast_peak_source_url",
                "wu_forecast_peak_soft",
                "wu_forecast_peak_usable_for_settlement",
            ),
            "weather_com": (
                "weather_com_forecast_peak_c",
                "weather_com_forecast_peak_at",
                "weather_com_forecast_peak_valid_local",
                "weather_com_forecast_peak_source",
                "weather_com_forecast_peak_source_url",
                "weather_com_forecast_peak_soft",
                "weather_com_forecast_peak_usable_for_settlement",
                "twc_provider",
                "twc_provider_role",
            ),
        }
        row_used = False
        for channel, keys in channel_features.items():
            if channel in selected_channels:
                continue
            if channel == "wu" and _float_or_none(features.get("wu_forecast_peak_c")) is None:
                continue
            if channel == "weather_com" and _float_or_none(features.get("weather_com_forecast_peak_c")) is None:
                continue
            for key in keys:
                if key in features:
                    selected_features[key] = copy.deepcopy(features[key])
            selected_channels.add(channel)
            row_used = True
        if row_used:
            selected_event_run_ids.append(int(row["id"] or 0))
            selected_created_at.append(_text(row["created_at"]))
        if selected_channels == {"wu", "weather_com"}:
            break

    if not selected_features:
        return [], {
            **context_base,
            "state": "not_found",
            "event_run_count_scanned": scanned,
        }

    synthetic_payload = {"weather_likelihood_features": selected_features}
    observations = _source_observations_from_fact_payload_features(
        synthetic_payload,
        station_id=station_id,
        event_date=event_date,
        fact_as_of=fact_as_of,
    )
    return observations, {
        **context_base,
        "state": "merged" if observations else "not_found",
        "source_observation_count": len(observations),
        "source_ids": _source_ids_from_observations(observations),
        "channels": sorted(selected_channels),
        "event_run_ids": selected_event_run_ids,
        "event_run_count_scanned": scanned,
        "event_run_created_at_max": _max_iso_text(*selected_created_at),
    }


def _soft_forecast_event_run_source_observations(
    *,
    slug: str,
    event_date: str,
    station_id: str,
    fact_as_of: str,
    db_path: Any = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if conn is not None:
        return _soft_forecast_event_run_source_observations_from_conn(
            conn,
            slug=slug,
            event_date=event_date,
            station_id=station_id,
            fact_as_of=fact_as_of,
        )
    ro_conn = _open_ro(db_path)
    if ro_conn is None:
        return [], {
            "schema": "soft_forecast_event_runs_projection_context_v1",
            "state": "skipped_db_unavailable",
            "source_observation_count": 0,
            "boundary": "read_only_same_event_soft_forecast_event_runs_adapter_no_fetch_no_write",
        }
    try:
        return _soft_forecast_event_run_source_observations_from_conn(
            ro_conn,
            slug=slug,
            event_date=event_date,
            station_id=station_id,
            fact_as_of=fact_as_of,
        )
    finally:
        ro_conn.close()


def _query_newer_source_observations(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    current_fact_id: int,
    current_fact_as_of: str,
    as_of_cutoff: str = "",
    limit: int = 80,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not slug or not event_date:
        return [], {"source_history_merge_state": "skipped_missing_slug_or_event_date"}
    clauses = ["slug = ?", "event_date = ?"]
    params: list[Any] = [slug, event_date]
    if current_fact_id > 0 and current_fact_as_of:
        clauses.append("(id > ? OR fact_as_of > ?)")
        params.extend([int(current_fact_id), str(current_fact_as_of)])
    elif current_fact_id > 0:
        clauses.append("id > ?")
        params.append(int(current_fact_id))
    elif current_fact_as_of:
        clauses.append("fact_as_of > ?")
        params.append(str(current_fact_as_of))
    else:
        return [], {"source_history_merge_state": "skipped_missing_current_fact_anchor"}
    cutoff = str(as_of_cutoff or "").strip()
    if cutoff:
        clauses.append("julianday(fact_as_of) <= julianday(?)")
        params.append(cutoff)
    rows = conn.execute(
        f"""
        SELECT id, fact_as_of, composer_version, fact_json
        FROM event_fact_history
        WHERE {' AND '.join(clauses)}
        ORDER BY id ASC
        LIMIT ?
        """,
        tuple([*params, max(1, int(limit))]),
    ).fetchall()
    observations: list[dict[str, Any]] = []
    fact_ids: list[int] = []
    fact_as_of_values: list[str] = []
    composer_versions: list[str] = []
    for row in rows:
        payload = _parse_json(row["fact_json"])
        if not payload:
            continue
        payload_observations = _source_observations_from_history_payload(payload)
        if not payload_observations:
            continue
        fact_ids.append(int(row["id"] or 0))
        fact_as_of_values.append(str(row["fact_as_of"] or ""))
        composer_versions.append(str(row["composer_version"] or ""))
        observations.extend(payload_observations)
    deduped = _dedupe_source_observations(observations)
    context = {
        "schema": "posterior_projection_source_history_context_v1",
        "source_history_merge_state": "merged" if deduped else "not_found",
        "source_history_fact_ids": fact_ids,
        "source_history_fact_count": len(fact_ids),
        "source_history_observation_count_raw": len(observations),
        "source_history_merged_observation_count": len(deduped),
        "source_history_source_ids": _source_ids_from_observations(deduped),
        "source_history_fact_as_of_max": max(fact_as_of_values) if fact_as_of_values else "",
        "source_history_composer_versions": sorted(set(composer_versions)),
        "current_fact_id": int(current_fact_id or 0),
        "current_fact_as_of": str(current_fact_as_of or ""),
        "as_of_cutoff": cutoff,
        "boundary": "same_slug_event_date_newer_source_only_history_read_only_no_db_write",
    }
    return deduped, context


def _newer_source_observations_from_history(
    *,
    slug: str,
    event_date: str,
    current_fact_id: int,
    current_fact_as_of: str,
    as_of_cutoff: str = "",
    db_path: Any = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if conn is not None:
        return _query_newer_source_observations(
            conn,
            slug=slug,
            event_date=event_date,
            current_fact_id=current_fact_id,
            current_fact_as_of=current_fact_as_of,
            as_of_cutoff=as_of_cutoff,
        )
    ro_conn = _open_ro(db_path)
    if ro_conn is None:
        return [], {"source_history_merge_state": "skipped_db_unavailable"}
    try:
        return _query_newer_source_observations(
            ro_conn,
            slug=slug,
            event_date=event_date,
            current_fact_id=current_fact_id,
            current_fact_as_of=current_fact_as_of,
            as_of_cutoff=as_of_cutoff,
        )
    finally:
        ro_conn.close()


def _merge_projection_source_observations(
    payload: Mapping[str, Any],
    history_observations: Sequence[Mapping[str, Any]],
    context_observations: Sequence[Mapping[str, Any]] | None = None,
    *,
    station_id: str,
    event_date: str,
    fact_as_of: str,
) -> list[dict[str, Any]]:
    base_observations = _dedupe_source_observations(_source_observations(payload))
    feature_observations = _source_observations_from_fact_payload_features(
        payload,
        station_id=station_id,
        event_date=event_date,
        fact_as_of=fact_as_of,
    )
    combined = _dedupe_source_observations(
        [
            *base_observations,
            *feature_observations,
            *list(history_observations or []),
            *list(context_observations or []),
        ]
    )
    adapter_version = "posterior_ssot_projection_fact_feature_source_adapter_v1"
    direct_source_ids = {
        _text(row.get("source_id"))
        for row in combined
        if _text(row.get("source_id"))
        and _text(row.get("parser_version")) != adapter_version
    }
    return [
        row
        for row in combined
        if not (
            _text(row.get("parser_version")) == adapter_version
            and _text(row.get("source_id")) in direct_source_ids
        )
    ]


def _observation_inputs_for_projection(
    payload: Mapping[str, Any],
    *,
    source_history_merged: bool,
    runtime_policy_recompute: bool = False,
    source_observations: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    inputs = _observation_inputs(payload)
    if runtime_policy_recompute:
        for key in DERIVED_TEMPERATURE_TRAJECTORY_INPUT_KEYS:
            inputs.pop(key, None)
    if source_history_merged:
        for key in STALE_OBSERVATION_INPUT_KEYS_WHEN_SOURCE_HISTORY_PRESENT:
            inputs.pop(key, None)
        for window in ("30m", "45m", "1h", "2h", "3h", "6h", "12h"):
            inputs.pop(f"temperature_momentum_{window}_c_per_hour", None)
            inputs.pop(f"temperature_rise_rate_{window}_c_per_hour", None)
    overlay = (
        _source_observation_feature_overlay(
            source_observations or [],
            event_date=_text(payload.get("event_date")),
        )
        if source_history_merged
        else {}
    )
    if overlay:
        for key, value in overlay.items():
            if value in (None, "", [], {}):
                continue
            inputs[key] = copy.deepcopy(value)
    return inputs


def _persisted_metar_strong_runtime_policy_version(payload: Mapping[str, Any]) -> str:
    versions = _as_mapping(payload.get("runtime_policy_versions"))
    value = _text(
        versions.get("metar_strong_likelihood"),
        payload.get("metar_strong_likelihood_runtime_policy_version"),
    )
    if value:
        return value
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    ssot_versions = _as_mapping(ssot.get("runtime_policy_versions"))
    value = _text(
        ssot_versions.get("metar_strong_likelihood"),
        ssot.get("metar_strong_likelihood_runtime_policy_version"),
    )
    if value:
        return value
    layer_chain = _as_mapping(ssot.get("layer_chain"))
    layer_versions = _as_mapping(layer_chain.get("runtime_policy_versions"))
    return _text(layer_versions.get("metar_strong_likelihood"))


def _persisted_posterior_composer_schema(payload: Mapping[str, Any]) -> str:
    runtime_schemas = _as_mapping(payload.get("runtime_schemas"))
    value = _text(
        payload.get("posterior_composer_schema"),
        runtime_schemas.get("sequential_remaining_warming_bayes"),
        runtime_schemas.get("hierarchical_temperature_posterior"),
    )
    if value:
        return value
    ssot = _as_mapping(payload.get("posterior_ssot_fact"))
    return _text(ssot.get("posterior_composer_schema"))


def _payload_has_metar_strong_runtime_surface(payload: Mapping[str, Any]) -> bool:
    runtime_schemas = _as_mapping(payload.get("runtime_schemas"))
    return bool(
        payload.get("metar_strong_likelihood")
        or payload.get("temperature_trajectory")
        or runtime_schemas.get("metar_strong_likelihood")
    )


def _runtime_policy_projection_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    current = str(CURRENT_METAR_STRONG_RUNTIME_POLICY_VERSION or "")
    persisted = _persisted_metar_strong_runtime_policy_version(payload)
    composer_current = str(CURRENT_POSTERIOR_COMPOSER_SCHEMA or "")
    composer_persisted = _persisted_posterior_composer_schema(payload)
    has_runtime_surface = _payload_has_metar_strong_runtime_surface(payload)
    metar_policy_changed = bool(has_runtime_surface and current and persisted != current)
    composer_changed = bool(has_runtime_surface and composer_current and composer_persisted != composer_current)
    requires = bool(metar_policy_changed or composer_changed)
    return {
        "schema": "posterior_projection_runtime_policy_context_v1",
        "requires_contextual_projection": requires,
        "reason": (
            "metar_strong_runtime_policy_changed"
            if metar_policy_changed
            else "posterior_composer_changed"
            if composer_changed
            else "metar_strong_runtime_surface_missing"
            if not has_runtime_surface
            else "runtime_policy_current"
        ),
        "metar_strong_runtime_policy_version_current": current,
        "metar_strong_runtime_policy_version_persisted": persisted,
        "posterior_composer_schema_current": composer_current,
        "posterior_composer_schema_persisted": composer_persisted,
        "posterior_composer_changed": composer_changed,
        "metar_strong_runtime_policy_changed": metar_policy_changed,
        "metar_strong_runtime_surface_present": has_runtime_surface,
        "boundary": "runtime_or_composer_change_recomputes_posterior_from_source_observations_not_previous_posterior",
    }


def _observation_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    features = dict(_as_mapping(payload.get("weather_likelihood_features")))
    compact = _as_mapping(payload.get("weather_likelihood_features_compact"))
    for key, value in compact.items():
        features.setdefault(key, copy.deepcopy(value))
    for target, fields in {
        "current_temp_c": ("current_temp_c", "metar_temperature_c", "current_temperature_c"),
        "current_temperature_c": ("current_temperature_c", "metar_temperature_c", "current_temp_c"),
        "observed_today_max_c": ("observed_today_max_c", "today_max_c"),
        "dewpoint_c": ("dewpoint_c", "metar_dewpoint_c"),
        "visibility_m": ("visibility_m",),
        "cloud_text": ("cloud_text", "clouds"),
        "weather_text": ("weather_text", "weather"),
        "pressure_hpa": ("pressure_hpa", "metar_pressure_hpa"),
        "wind_speed_mps": ("wind_speed_mps",),
    }.items():
        if features.get(target) not in (None, ""):
            continue
        for field in fields:
            if payload.get(field) not in (None, ""):
                features[target] = payload.get(field)
                break
    for key in (
        "settlement_actual",
        "settlement_actual_c",
        "official_final_result",
        "final_daily_max_c",
        "final_bucket_lock",
        "hard_source_final_daily_max",
        "observed_today_max_final",
        "temperature_observation_final_daily_max",
    ):
        if payload.get(key) not in (None, ""):
            features.setdefault(key, payload.get(key))
    return features


def _query_context_row(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    current_fact_id: int,
    current_fact_as_of: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not slug or not event_date:
        return {}, {}
    clauses = ["slug = ?", "event_date = ?"]
    params: list[Any] = [slug, event_date]
    if current_fact_id > 0:
        clauses.append("id < ?")
        params.append(int(current_fact_id))
    elif current_fact_as_of:
        clauses.append("fact_as_of <= ?")
        params.append(str(current_fact_as_of))
    rows = conn.execute(
        f"""
        SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        FROM event_fact_history
        WHERE {' AND '.join(clauses)}
        ORDER BY id DESC
        LIMIT 50
        """,
        tuple(params),
    ).fetchall()
    for row in rows:
        payload = _parse_json(row["fact_json"])
        if not payload:
            continue
        labels = bucket_labels_from_payload(payload)
        ssot = _as_mapping(payload.get("posterior_ssot_fact"))
        if labels and (
            str(ssot.get("state") or "") == "available"
            or bool(payload.get("distribution"))
            or bool(payload.get("posterior_temperature_distribution"))
        ):
            return payload, {
                "context_fact_id": int(row["id"] or 0),
                "context_fact_as_of": str(row["fact_as_of"] or ""),
                "context_composer_version": str(row["composer_version"] or ""),
                "bucket_count": len(labels),
            }
    return {}, {}


def _open_ro(db_path: Any) -> sqlite3.Connection | None:
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _context_payload(
    *,
    slug: str,
    event_date: str,
    current_fact_id: int,
    current_fact_as_of: str,
    db_path: Any = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    context_payload: dict[str, Any] = {}
    context_meta: dict[str, Any] = {}
    if conn is not None:
        context_payload, context_meta = _query_context_row(
            conn,
            slug=slug,
            event_date=event_date,
            current_fact_id=current_fact_id,
            current_fact_as_of=current_fact_as_of,
        )
        if context_payload:
            return context_payload, context_meta
        return _market_bucket_context_payload(slug=slug, event_date=event_date, conn=conn, db_path=db_path)
    ro_conn = _open_ro(db_path)
    if ro_conn is not None:
        try:
            context_payload, context_meta = _query_context_row(
                ro_conn,
                slug=slug,
                event_date=event_date,
                current_fact_id=current_fact_id,
                current_fact_as_of=current_fact_as_of,
            )
            if context_payload:
                return context_payload, context_meta
            return _market_bucket_context_payload(slug=slug, event_date=event_date, conn=ro_conn, db_path=db_path)
        finally:
            ro_conn.close()
    return _market_bucket_context_payload(slug=slug, event_date=event_date, conn=None, db_path=db_path)


def _market_event_cache_paths(db_path: Any = None) -> list[Path]:
    paths: list[Path] = []
    if db_path:
        db = Path(db_path)
        paths.extend([db.parent / "weather_events_cache.json", db.parent / "weather_events_cache.good.json"])
    return [path for path in paths if path.exists()]


def _market_bucket_context_from_event_cache(
    *,
    slug: str,
    event_date: str,
    db_path: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    for path in _market_event_cache_paths(db_path):
        cache = _parse_json(path.read_text(encoding="utf-8"))
        events = cache.get("events")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
            continue
        for raw_event in events:
            event = _as_mapping(raw_event)
            if str(event.get("slug") or "").strip() != slug:
                continue
            markets = event.get("markets")
            if not isinstance(markets, Sequence) or isinstance(markets, (str, bytes, bytearray)):
                continue
            labels = _bucket_labels_from_market_rows(markets)
            if not labels:
                continue
            event_url = f"https://polymarket.com/event/{slug}" if slug else ""
            payload = {
                "slug": slug,
                "event_date": event_date,
                "run_date": event_date,
                "title": _text(event.get("title"), slug),
                "event_url": event_url,
                "resolution_source": _text(event.get("resolutionSource"), event.get("resolution_source")),
                "event_end_utc": _text(event.get("endDate"), event.get("end_date")),
                "station_id": "ZUUU",
                "station_timezone": "Asia/Shanghai",
                "market_var": "temperature_2m_max",
                "market_unit": "C",
                "rounding_rule": "rounded_degrees",
                "market_bucket_context": {
                    "schema": "market_bucket_context_v1",
                    "source": "weather_events_cache_static_market_labels",
                    "source_file": str(path),
                    "event_id": _text(event.get("id")),
                    "labels": labels,
                    "label_count": len(labels),
                    "prices_used_as_probabilities": False,
                    "boundary": "static_market_bucket_labels_only_no_price_likelihood",
                },
            }
            return payload, {
                "bucket_context_source": "weather_events_cache_static_market_labels",
                "bucket_context_file": str(path),
                "bucket_count": len(labels),
            }
    return {}, {}


def _market_bucket_context_from_snapshots(
    *,
    slug: str,
    event_date: str,
    conn: sqlite3.Connection | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if conn is None or not _table_exists(conn, "market_snapshots"):
        return {}, {}
    try:
        rows = conn.execute(
            """
            SELECT id, run_date, event_date, slug, payload_json, meta_json, created_at
            FROM market_snapshots
            WHERE slug = ? AND event_date = ?
            ORDER BY id DESC
            LIMIT 80
            """,
            (slug, event_date),
        ).fetchall()
    except sqlite3.Error:
        return {}, {}
    best_payload: dict[str, Any] = {}
    best_meta: dict[str, Any] = {}
    best_labels: list[str] = []
    best_row: sqlite3.Row | None = None
    for row in rows:
        payload = _parse_json(row["payload_json"])
        meta = _parse_json(row["meta_json"])
        labels = _bucket_labels_from_market_rows(payload.get("rows"))
        if len(labels) > len(best_labels):
            best_payload = payload
            best_meta = meta
            best_labels = labels
            best_row = row
    if not best_labels or best_row is None:
        return {}, {}
    payload = {
        "slug": slug,
        "event_date": event_date,
        "run_date": _text(best_row["run_date"], event_date),
        "title": _text(best_meta.get("title"), best_payload.get("title"), slug),
        "event_url": _text(best_meta.get("event_url"), best_payload.get("event_url")),
        "station_id": "ZUUU",
        "station_timezone": "Asia/Shanghai",
        "market_var": "temperature_2m_max",
        "market_unit": "C",
        "rounding_rule": "rounded_degrees",
        "market_bucket_context": {
            "schema": "market_bucket_context_v1",
            "source": "market_snapshots_static_bucket_labels",
            "source_snapshot_id": int(best_row["id"] or 0),
            "source_created_at": str(best_row["created_at"] or ""),
            "labels": best_labels,
            "label_count": len(best_labels),
            "prices_used_as_probabilities": False,
            "boundary": "market_snapshot_outcome_labels_only_no_price_likelihood",
        },
    }
    return payload, {
        "bucket_context_source": "market_snapshots_static_bucket_labels",
        "bucket_context_snapshot_id": int(best_row["id"] or 0),
        "bucket_count": len(best_labels),
    }


def _market_bucket_context_payload(
    *,
    slug: str,
    event_date: str,
    conn: sqlite3.Connection | None = None,
    db_path: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [
        _market_bucket_context_from_event_cache(slug=slug, event_date=event_date, db_path=db_path),
        _market_bucket_context_from_snapshots(slug=slug, event_date=event_date, conn=conn),
    ]
    candidates = [
        (payload, meta)
        for payload, meta in candidates
        if payload and int(_as_mapping(payload.get("market_bucket_context")).get("label_count") or 0) > 0
    ]
    if not candidates:
        return {}, {}
    payload, meta = max(
        candidates,
        key=lambda item: int(_as_mapping(item[0].get("market_bucket_context")).get("label_count") or 0),
    )
    return dict(payload), dict(meta)


def _prior_layer_contract_for_labels(
    *,
    labels: Sequence[str],
    prior: Mapping[str, Any],
    prior_meta: Mapping[str, Any],
    prior_na_reason: str = "",
) -> Mapping[str, Any]:
    return build_prior_layer_contract(
        {
            "prior_distribution": prior,
            "prior_meta": dict(prior_meta),
            "prior_na_reason": str(prior_na_reason or prior_meta.get("prior_na_reason") or ""),
            "posterior_temperature_distribution": [
                {"bucket": str(label), "probability": 1.0 / max(1, len(labels))}
                for label in labels
            ],
        }
    )


def _prior_coverage_value(meta: Mapping[str, Any], key: str) -> int:
    try:
        value = int(float(meta.get(key) or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _prior_coverage_tuple(meta: Mapping[str, Any]) -> tuple[int, int, int, int]:
    raw_candidate_count = _prior_coverage_value(meta, "raw_candidate_count")
    sample_count = _prior_coverage_value(meta, "sample_count")
    return (
        raw_candidate_count or sample_count,
        sample_count,
        _prior_coverage_value(meta, "effective_sample_count"),
        _prior_coverage_value(meta, "date_window_sample_count"),
    )


def _runtime_prior_has_stronger_coverage(
    runtime_meta: Mapping[str, Any],
    source_meta: Mapping[str, Any],
) -> bool:
    runtime_coverage = _prior_coverage_tuple(runtime_meta)
    source_coverage = _prior_coverage_tuple(source_meta)
    if runtime_coverage[0] > source_coverage[0]:
        return True
    if runtime_coverage[0] == source_coverage[0] and runtime_coverage[2] > source_coverage[2]:
        return True
    if runtime_coverage[0] == source_coverage[0] and runtime_coverage[3] > source_coverage[3]:
        return True
    return False


def _runtime_prior_coverage_not_weaker(
    runtime_meta: Mapping[str, Any],
    source_meta: Mapping[str, Any],
) -> bool:
    runtime_coverage = _prior_coverage_tuple(runtime_meta)
    source_coverage = _prior_coverage_tuple(source_meta)
    return all(runtime >= source for runtime, source in zip(runtime_coverage, source_coverage))


def _prior_distribution(
    *,
    labels: Sequence[str],
    payload: Mapping[str, Any],
    context_payload: Mapping[str, Any],
    event_date: str,
    fact_as_of: str,
    db_path: Any = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, float], dict[str, Any], dict[str, Any], str]:
    grid_contract = build_physical_x_bucket_contract()
    if grid_contract.get("ok") is not True:
        return {}, {"state": "blocked", "prior_na_reason": "physical_x_grid_contract_unavailable"}, {}, "physical_prior_unavailable"
    prior_output = build_ncei_operational_climate_reference_prior_context(
        event_date=event_date,
        decision_as_of=fact_as_of,
        temperature_grid_c=grid_contract.get("temperature_grid_c") or [],
        daily_labels_path=NCEI_PRIOR_ROOT / "derived" / "daily_labels.csv",
        download_manifest_path=NCEI_PRIOR_ROOT / "download_manifest.json",
        station_id=_text(payload.get("station_id"), context_payload.get("station_id"), "ZUUU"),
    )
    if prior_output.get("ok") is not True:
        return (
            {},
            {
                "schema": "temperature_prior_meta_v1",
                "state": str(prior_output.get("state") or "blocked"),
                "prior_na_reason": "+".join(prior_output.get("block_reasons") or ["ncei_physical_prior_unavailable"]),
                "boundary": "historical_physical_X_climate_prior_only_no_forecast_no_metar_no_market_no_settlement_S",
            },
            {},
            "ncei_physical_x_climate_reference_prior_unavailable",
        )
    continuous = dict(_as_mapping(prior_output.get("continuous_distribution")))
    bucket_mass = {str(label): 0.0 for label in labels}
    rounding_rule = _text(payload.get("rounding_rule"), context_payload.get("rounding_rule"), "rounded_degrees")
    for row in continuous.get("grid") or []:
        if not isinstance(row, Mapping):
            continue
        temperature = _float_or_none(row.get("temp_c"))
        mass = _float_or_none(row.get("mass"))
        if temperature is None or mass is None or mass < 0.0:
            continue
        label = bucket_label_for_value(labels, temperature, rounding_rule)
        if label:
            bucket_mass[label] += mass
    total = sum(bucket_mass.values())
    if total <= 0.0:
        return {}, {"state": "blocked", "prior_na_reason": "ncei_prior_market_bucket_projection_empty"}, {}, "physical_prior_unavailable"
    prior = {label: value / total for label, value in bucket_mass.items()}
    sample_count = int(prior_output.get("reference_sample_count") or 0)
    prior_meta = {
        "schema": "temperature_prior_meta_v1",
        "state": "available",
        "source": "approved_ncei_ghcnh_hourly_climate_reference_prior",
        "boundary": "historical_physical_X_climate_prior_only_no_forecast_no_metar_no_market_no_settlement_S",
        "sample_count": sample_count,
        "raw_candidate_count": sample_count,
        "effective_sample_count": sample_count,
        "date_window_sample_count": sample_count,
        "date_window_matched_sample_count": sample_count,
        "broad_matched_sample_count": sample_count,
        "date_window_weight": 1.0,
        "min_samples": 30,
        "sample_scale": 1.0,
        "shrinkage_target": "approved_ncei_operational_climate_reference_baseline",
        "history_days": 1826,
        "date_window_days": int(prior_output.get("seasonal_window_days") or 15),
        "smoothing": None,
        "station_id": str(prior_output.get("station_id") or "ZUUU"),
        "rounding_rule": rounding_rule,
        "artifact_sha256": str(prior_output.get("artifact_sha256") or ""),
        "prior_context_id": str(prior_output.get("prior_context_id") or ""),
        "reference_measurement_caveat": str(prior_output.get("reference_measurement_caveat") or ""),
        "amsc_remains_final_x_authority": True,
        "settlement_s_used": False,
        "prior_na_reason": "",
    }
    return prior, prior_meta, continuous, "ncei_physical_x_climate_reference_prior"


def _source_observation_dt(row: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        parsed = _parse_iso_ts(row.get(key))
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    return None


def _latest_source_observation(
    observations: Sequence[Mapping[str, Any]],
    *,
    source_ids: set[str],
) -> Mapping[str, Any]:
    best: Mapping[str, Any] = {}
    best_key: tuple[datetime, datetime, int] | None = None
    priority = {"zuuu_metar_raw_awc": 1, "zuuu_metar_raw_amsc": 2}
    for raw in observations:
        row = _as_mapping(raw)
        source_id = _text(row.get("source_id"))
        if source_id not in source_ids:
            continue
        valid_dt = _source_observation_dt(row, "valid_time", "valid_time_utc", "valid_time_local")
        fetched_dt = _source_observation_dt(row, "fetched_at", "response_completed_at", "parsed_at")
        if valid_dt is None:
            continue
        key = (
            valid_dt,
            fetched_dt or datetime.min.replace(tzinfo=timezone.utc),
            priority.get(source_id, 0),
        )
        if best_key is None or key > best_key:
            best_key = key
            best = row
    return best


def _source_observation_event_date_matches(row: Mapping[str, Any], *, event_date: str) -> bool:
    expected = _text(event_date)
    if not expected:
        return True
    actual = _text(row.get("event_date"))
    return not actual or actual == expected


def _source_observation_feature_overlay(
    observations: Sequence[Mapping[str, Any]],
    *,
    event_date: str = "",
) -> dict[str, Any]:
    rows = [dict(_as_mapping(row)) for row in observations if isinstance(row, Mapping)]
    if event_date:
        rows = [row for row in rows if _source_observation_event_date_matches(row, event_date=event_date)]
    hard_rows = [
        row
        for row in rows
        if _text(row.get("source_id")) in {"zuuu_metar_raw_amsc", "zuuu_metar_raw_awc"}
        and _text(row.get("source_class")) in {"hard_observation_candidate", "settlement_source", ""}
    ]
    latest_hard = _latest_source_observation(
        hard_rows,
        source_ids={"zuuu_metar_raw_amsc", "zuuu_metar_raw_awc"},
    )
    overlay: dict[str, Any] = {}
    if latest_hard:
        source_id = _text(latest_hard.get("source_id"))
        latest_temp = _float_or_none(latest_hard.get("temperature_c"))
        latest_dewpoint = _float_or_none(latest_hard.get("dewpoint_c"))
        today_max = None
        today_max_at = ""
        for row in hard_rows:
            value = _float_or_none(row.get("observed_today_max_c"))
            if value is None:
                value = _float_or_none(row.get("temperature_c"))
            if value is None:
                continue
            valid_time = _text(row.get("observed_today_max_at"), row.get("valid_time"))
            if today_max is None or value > today_max or (value == today_max and valid_time > today_max_at):
                today_max = value
                today_max_at = valid_time
        overlay.update(
            {
                "observed_temp_c": latest_temp,
                "observed_current_temp_c": latest_temp,
                "observed_today_max_c": today_max,
                "observed_today_max_at": today_max_at,
                "observed_today_max_source": source_id,
                "observed_temp_source": source_id,
                "observation_path_primary_source": source_id,
                f"{source_id}_latest_temp_c": latest_temp,
                f"{source_id}_latest_dewpoint_c": latest_dewpoint,
                f"{source_id}_latest_at": _text(latest_hard.get("valid_time")),
                f"{source_id}_latest_local": _text(latest_hard.get("valid_time_local")),
                f"{source_id}_fetched_at": _text(latest_hard.get("fetched_at")),
                f"{source_id}_latest_raw": _text(latest_hard.get("raw_ref")),
                f"{source_id}_source_observation_id": _text(latest_hard.get("source_observation_id")),
                f"{source_id}_today_max_c": today_max,
                f"{source_id}_today_max_at": today_max_at,
            }
        )
    taf_rows = [
        row
        for row in rows
        if _text(row.get("source_id")) == "zuuu_taf_forecast_amsc"
        and _float_or_none(row.get("forecast_peak_c")) is not None
    ]
    latest_taf = _latest_source_observation(taf_rows, source_ids={"zuuu_taf_forecast_amsc"})
    if latest_taf:
        overlay.update(
            {
                "taf_forecast_peak_c": _float_or_none(latest_taf.get("forecast_peak_c")),
                "taf_forecast_peak_at": _text(latest_taf.get("valid_time")),
                "taf_forecast_peak_local": _text(latest_taf.get("valid_time_local")),
                "taf_forecast_peak_raw": _text(latest_taf.get("raw_ref")),
                "taf_forecast_peak_source": _text(latest_taf.get("source_id")),
                "taf_forecast_peak_source_observation_id": _text(latest_taf.get("source_observation_id")),
            }
        )
    for source_id, prefix in (
        ("wu_forecast_high", "wu_forecast"),
        ("weather_com_forecast_peak", "weather_com_forecast"),
    ):
        candidates = [
            row
            for row in rows
            if _text(row.get("source_id")) == source_id
            and _float_or_none(row.get("forecast_peak_c")) is not None
        ]
        latest = _latest_source_observation(candidates, source_ids={source_id})
        if not latest and candidates:
            latest = sorted(
                candidates,
                key=lambda row: (
                    _source_observation_dt(row, "fetched_at", "response_completed_at", "parsed_at")
                    or datetime.min.replace(tzinfo=timezone.utc),
                    _source_observation_dt(row, "valid_time", "valid_time_local")
                    or datetime.min.replace(tzinfo=timezone.utc),
                ),
            )[-1]
        if latest:
            overlay.update(
                {
                    f"{prefix}_peak_c": _float_or_none(latest.get("forecast_peak_c")),
                    f"{prefix}_peak_at": _text(latest.get("valid_time"), latest.get("valid_time_local")),
                    f"{prefix}_peak_source": _text(latest.get("raw_ref"), latest.get("source_id")),
                    f"{prefix}_peak_source_observation_id": _text(latest.get("source_observation_id")),
                }
            )
    return {key: value for key, value in overlay.items() if value not in (None, "", [], {})}


def _source_observations_as_of_max(observations: Sequence[Mapping[str, Any]]) -> str:
    values: list[str] = []
    for raw in observations:
        row = _as_mapping(raw)
        row_values: list[str] = []
        for key in ("fetched_at", "response_completed_at", "parsed_at"):
            value = _text(row.get(key))
            if value:
                row_values.append(value)
        if not row_values and _text(row.get("source_class")) in {"hard_observation_candidate", "settlement_source"}:
            value = _text(row.get("valid_time"))
            if value:
                row_values.append(value)
        values.extend(row_values)
    return _max_iso_text(*values)


def _extra_payload(
    *,
    payload: Mapping[str, Any],
    context_payload: Mapping[str, Any],
    prior_distribution: Mapping[str, Any],
    prior_continuous_distribution: Mapping[str, Any],
    prior_meta: Mapping[str, Any],
    prior_source: str,
    bucket_context_source: str,
    source_observations: Sequence[Mapping[str, Any]] | None = None,
    source_observation_context: Mapping[str, Any] | None = None,
    runtime_policy_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    extra = copy.deepcopy(dict(payload))
    # A previous writer payload may already contain conflict diagnostics.  They
    # are audit metadata, not source facts, and carrying them forward would
    # recursively nest the full previous payload on every materialization.
    extra.pop("extra_payload_preserved", None)
    for key in (
        "source_observations",
        "source_acquisition_trace",
        "source_latency_trace",
        "source_observation_ids",
        "source_event_ids",
        "source_lineage",
    ):
        extra.pop(key, None)
    for key in (
        "market_meta",
        "title",
        "event_url",
        "resolution_source",
        "event_end_utc",
        "market_unit",
        "market_var",
        "rounding_rule",
        "city",
        "city_key",
        "station_id",
        "station_timezone",
        "weather_type",
        "terrain_class",
        "site_class",
        "urbanization_class",
        "site_temperature_bias_c",
        "terrain_temperature_bias_c",
        "urban_heat_island_c",
    ):
        if extra.get(key) in (None, "") and context_payload.get(key) not in (None, ""):
            extra[key] = copy.deepcopy(context_payload.get(key))
    extra["prior_distribution"] = copy.deepcopy(dict(prior_distribution))
    extra["prior_continuous_distribution"] = copy.deepcopy(dict(prior_continuous_distribution))
    extra["prior_meta"] = copy.deepcopy(dict(prior_meta))
    extra["prior_na_reason"] = str(prior_meta.get("prior_na_reason") or "")
    feature_overlay = _source_observation_feature_overlay(
        source_observations or [],
        event_date=_text(payload.get("event_date")),
    )
    if feature_overlay:
        for key in ("weather_likelihood_features", "weather_likelihood_features_compact"):
            features = dict(_as_mapping(extra.get(key)))
            features.update(copy.deepcopy(feature_overlay))
            extra[key] = features
    extra["posterior_projection_context"] = {
        "schema": SCHEMA_VERSION,
        "projection_context_kind": "market_bucket_context_plus_current_source_observations",
        "bucket_context_source": str(bucket_context_source or "previous_same_event_posterior_fact"),
        "prior_source": prior_source,
        "does_not_inherit_prior_from_previous_posterior_distribution": True,
        "does_not_inherit_previous_observations": True,
    }
    if source_observation_context:
        context = dict(source_observation_context)
        source_history_merged = int(context.get("source_history_merged_observation_count") or 0) > 0
        model_cache_context = _as_mapping(context.get("model_member_cache_context"))
        model_cache_merged = int(model_cache_context.get("source_observation_count") or 0) > 0
        soft_forecast_context = _as_mapping(context.get("soft_forecast_event_runs_context"))
        soft_forecast_merged = int(soft_forecast_context.get("source_observation_count") or 0) > 0
        extra["posterior_projection_context"]["source_observation_context"] = context
        extra["posterior_projection_context"]["source_history_observations_merged"] = source_history_merged
        extra["posterior_projection_context"]["model_member_cache_observations_merged"] = model_cache_merged
        extra["posterior_projection_context"]["soft_forecast_event_runs_observations_merged"] = soft_forecast_merged
        merged_kinds: list[str] = []
        if source_history_merged:
            merged_kinds.append("newer_same_event_source_history")
        if model_cache_merged:
            merged_kinds.append("model_member_cache")
        if soft_forecast_merged:
            merged_kinds.append("soft_forecast_event_runs")
        context_kind = (
            "current_payload_plus_" + "_plus_".join(merged_kinds)
            if merged_kinds
            else "current_payload_only"
        )
        extra["posterior_projection_context"]["source_observation_context_kind"] = context_kind
    if runtime_policy_context:
        policy_context = dict(runtime_policy_context)
        extra["posterior_projection_context"]["runtime_policy_context"] = policy_context
        if bool(policy_context.get("requires_contextual_projection")):
            extra["posterior_projection_context"]["projection_recompute_reason"] = str(
                policy_context.get("reason") or "runtime_policy_changed"
            )
    return extra


def build_fact_projection_no_apply(
    *,
    slug: str,
    event_date: str,
    run_date: str = "",
    fact_as_of: str = "",
    payload: Mapping[str, Any],
    current_fact_id: int = 0,
    current_fact_as_of: str = "",
    composer_version: str = "",
    db_path: Any = None,
    conn: sqlite3.Connection | None = None,
    write_reason: str = "posterior_ssot_projection_no_apply",
    as_of_cutoff: str = "",
    enable_apply_contract: bool = False,
    write_latest: bool = False,
) -> dict[str, Any]:
    """Build a shared no-apply projection for audits and rematerialization plans."""

    payload_map = dict(payload)
    effective_slug = _text(slug, payload_map.get("slug"))
    effective_event_date = _text(event_date, payload_map.get("event_date"))
    effective_run_date = _text(run_date, payload_map.get("run_date"), effective_event_date)
    requested_as_of_cutoff = _text(as_of_cutoff)
    effective_fact_as_of = _text(
        requested_as_of_cutoff,
        fact_as_of,
        payload_map.get("fact_as_of"),
        current_fact_as_of,
    )
    base_trace = _source_trace(payload_map)
    history_observations, source_observation_context = _newer_source_observations_from_history(
        slug=effective_slug,
        event_date=effective_event_date,
        current_fact_id=int(current_fact_id or 0),
        current_fact_as_of=str(current_fact_as_of or effective_fact_as_of or ""),
        as_of_cutoff=requested_as_of_cutoff,
        db_path=db_path,
        conn=conn,
    )
    source_history_fact_as_of_max = str(source_observation_context.get("source_history_fact_as_of_max") or "")
    if not requested_as_of_cutoff:
        effective_fact_as_of = _max_iso_text(effective_fact_as_of, source_history_fact_as_of_max)
    source_history_merged = int(source_observation_context.get("source_history_merged_observation_count") or 0) > 0
    model_cache_observations, model_cache_context = _model_member_cache_source_observations(
        event_date=effective_event_date,
        station_id=_text(payload_map.get("station_id"), "ZUUU"),
        fact_as_of=effective_fact_as_of,
        db_path=db_path,
        conn=conn,
    )
    soft_forecast_observations, soft_forecast_context = _soft_forecast_event_run_source_observations(
        slug=effective_slug,
        event_date=effective_event_date,
        station_id=_text(payload_map.get("station_id"), "ZUUU"),
        fact_as_of=effective_fact_as_of,
        db_path=db_path,
        conn=conn,
    )
    source_observation_context = {
        **dict(source_observation_context),
        "model_member_cache_context": dict(model_cache_context),
        "soft_forecast_event_runs_context": dict(soft_forecast_context),
    }
    projection_source_observations = _merge_projection_source_observations(
        payload_map,
        history_observations,
        [*model_cache_observations, *soft_forecast_observations],
        station_id=_text(payload_map.get("station_id"), "ZUUU"),
        event_date=effective_event_date,
        fact_as_of=effective_fact_as_of,
    )
    if not requested_as_of_cutoff:
        effective_fact_as_of = _max_iso_text(
            effective_fact_as_of,
            _source_observations_as_of_max(projection_source_observations),
        )
    trace = _source_trace_with_observations(
        base_trace,
        projection_source_observations,
        source_observation_context=source_observation_context,
    )
    writer_plan = build_fact_snapshot_write_plan(
        slug=effective_slug,
        event_date=effective_event_date,
        run_date=effective_run_date,
        fact_as_of=effective_fact_as_of,
        incoming_payload=payload_map,
        current_fact_id=int(current_fact_id or 0),
        current_fact_as_of=str(current_fact_as_of or ""),
        current_payload=payload_map,
        source_acquisition_trace=trace,
        composer_version=str(composer_version or PARSER_VERSION),
        write_reason=write_reason,
        include_payload=True,
        enable_apply_contract=bool(enable_apply_contract),
        write_latest=bool(write_latest),
        prior_history_conn=conn,
        prior_history_db_path=db_path,
    )
    writer_payload = writer_plan.get("effective_payload") if isinstance(writer_plan.get("effective_payload"), Mapping) else {}
    writer_labels = bucket_labels_from_payload(writer_payload)
    writer_ssot = _as_mapping(writer_payload.get("posterior_ssot_fact"))
    writer_complete = bool(
        writer_labels
        and _as_mapping(writer_payload.get("prior_distribution"))
        and str(writer_ssot.get("state") or "") == "available"
    )
    runtime_policy_context = _runtime_policy_projection_context(writer_payload)
    runtime_policy_recompute = bool(runtime_policy_context.get("requires_contextual_projection"))
    model_cache_merged = bool(model_cache_observations)
    soft_forecast_merged = bool(soft_forecast_observations)
    prior_coverage_recompute = False
    if (
        writer_complete
        and not source_history_merged
        and not model_cache_merged
        and not soft_forecast_merged
        and not runtime_policy_recompute
        and writer_labels
    ):
        _prior_probe, _prior_probe_meta, _prior_probe_continuous, prior_probe_source = _prior_distribution(
            labels=writer_labels,
            payload=payload_map,
            context_payload=writer_payload,
            event_date=effective_event_date,
            fact_as_of=effective_fact_as_of,
            db_path=db_path,
            conn=conn,
        )
        prior_coverage_recompute = prior_probe_source in {
            "temperature_prior_runtime_coverage_refresh",
            "temperature_prior_runtime_continuous_upgrade",
        }
    if (
        writer_complete
        and not source_history_merged
        and not model_cache_merged
        and not soft_forecast_merged
        and not runtime_policy_recompute
        and not prior_coverage_recompute
    ):
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "read_only": True,
            "projection_mode": "writer_forward_projection",
            "projected_payload": dict(writer_payload),
            "plan": writer_plan,
            "context": {
                "runtime_policy_context": runtime_policy_context,
                "model_member_cache_context": dict(model_cache_context),
                "soft_forecast_event_runs_context": dict(soft_forecast_context),
            },
            "issues": [],
        }

    if writer_labels:
        context_payload = dict(writer_payload)
        context_meta = {
            "context_fact_id": int(current_fact_id or 0),
            "context_fact_as_of": str(current_fact_as_of or effective_fact_as_of or ""),
            "context_composer_version": str(composer_version or ""),
            "bucket_count": len(writer_labels),
            "bucket_context_source": "current_payload_or_writer_projection",
        }
        labels = writer_labels
    else:
        context_payload, context_meta = _context_payload(
            slug=effective_slug,
            event_date=effective_event_date,
            current_fact_id=int(current_fact_id or 0),
            current_fact_as_of=str(current_fact_as_of or effective_fact_as_of or ""),
            db_path=db_path,
            conn=conn,
        )
        labels = bucket_labels_from_payload(context_payload)
    if not labels:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "projection_mode": "blocked_missing_bucket_context",
            "projected_payload": dict(writer_payload or payload_map),
            "plan": writer_plan,
            "context": context_meta,
            "issues": ["bucket_context_missing"],
        }

    prior, prior_meta, prior_continuous, prior_source = _prior_distribution(
        labels=labels,
        payload=payload_map,
        context_payload=context_payload,
        event_date=effective_event_date,
        fact_as_of=effective_fact_as_of,
        db_path=db_path,
        conn=conn,
    )
    if not prior:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "read_only": True,
            "projection_mode": "blocked_missing_prior_distribution",
            "projected_payload": dict(writer_payload or payload_map),
            "plan": writer_plan,
            "context": context_meta,
            "issues": ["prior_distribution_missing"],
        }

    pipeline = build_hierarchical_posterior_pipeline(
        buckets=labels,
        prior_distribution=prior,
        prior_continuous_distribution=prior_continuous,
        event_date=effective_event_date,
        as_of=effective_fact_as_of,
        station_timezone=_text(payload_map.get("station_timezone"), context_payload.get("station_timezone"), "Asia/Shanghai"),
        source_observations=projection_source_observations,
        observation_inputs=_observation_inputs_for_projection(
            payload_map,
            source_history_merged=source_history_merged,
            runtime_policy_recompute=runtime_policy_recompute,
            source_observations=projection_source_observations,
        ),
        rounding_rule=_text(payload_map.get("rounding_rule"), context_payload.get("rounding_rule"), "rounded_degrees"),
    )
    contextual_plan = build_hierarchical_posterior_fact_snapshot_write_plan(
        slug=effective_slug,
        event_date=effective_event_date,
        run_date=effective_run_date,
        fact_as_of=effective_fact_as_of,
        pipeline_output=pipeline,
        source_acquisition_trace=trace,
        current_fact_id=int(current_fact_id or 0),
        current_fact_as_of=str(current_fact_as_of or ""),
        current_payload=payload_map,
        write_reason=write_reason,
        extra_payload=_extra_payload(
            payload=payload_map,
            context_payload=context_payload,
            prior_distribution=prior,
            prior_continuous_distribution=prior_continuous,
            prior_meta=prior_meta,
            prior_source=prior_source,
            bucket_context_source=str(context_meta.get("bucket_context_source") or "previous_same_event_posterior_fact"),
            source_observations=projection_source_observations,
            source_observation_context=source_observation_context,
            runtime_policy_context=runtime_policy_context,
        ),
        include_payload=True,
        write_latest=bool(write_latest),
        enable_apply_contract=bool(enable_apply_contract),
    )
    projected_payload = (
        contextual_plan.get("effective_payload")
        if isinstance(contextual_plan.get("effective_payload"), Mapping)
        else {}
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(projected_payload),
        "read_only": True,
        "projection_mode": "contextual_hierarchical_posterior_projection",
        "projected_payload": dict(projected_payload),
        "plan": contextual_plan,
        "context": {
            **context_meta,
            "bucket_labels": list(labels),
            "prior_source": prior_source,
            "pipeline_ok": bool(pipeline.get("ok")),
            "pipeline_na_reasons": list(pipeline.get("na_reasons") or []),
            "source_observation_context": dict(source_observation_context),
            "source_history_observations_merged": source_history_merged,
            "model_member_cache_observations_merged": model_cache_merged,
            "soft_forecast_event_runs_observations_merged": soft_forecast_merged,
            "source_observation_count": len(projection_source_observations),
            "runtime_policy_context": runtime_policy_context,
            "runtime_policy_recompute": runtime_policy_recompute,
        },
        "issues": [] if projected_payload else ["contextual_projection_payload_missing"],
    }
