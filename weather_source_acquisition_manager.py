from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from external_forecast_snapshot_runtime import default_provider_family
from lineage_identity_runtime import build_correlation_id
from source_observation_runtime import (
    SCHEMA_VERSION as SOURCE_OBSERVATION_SCHEMA_VERSION,
    PRIMARY_METAR_SOURCE_ID,
    PRIMARY_TAF_SOURCE_ID,
    REFERENCE_METAR_SOURCE_IDS,
    REFERENCE_TAF_SOURCE_IDS,
    SOURCE_CLASSES,
    SourceObservation,
    build_source_observation,
    source_defaults,
)


SCHEMA_VERSION = "weather_source_acquisition_trace_v1"
PARSER_VERSION = "weather_source_acquisition_manager_v1"


SOURCE_ID_ALIASES: dict[str, str] = {
    "awc_metar_raw": "zuuu_metar_raw_awc",
    "aviationweather_metar": "zuuu_metar_raw_awc",
    "amsc_metar": "zuuu_metar_raw_amsc",
    "amsc_zuuu_metar": "zuuu_metar_raw_amsc",
    "zuuu_taf_awc_tx_peak": "zuuu_taf_forecast_peak",
    "amsc_taf": "zuuu_taf_forecast_amsc",
    "amsc_zuuu_taf": "zuuu_taf_forecast_amsc",
    "wu_display": "wu_display_current",
    "wu_display_html": "wu_display_current",
    "wu_page_api_current_icao_metric": "wu_page_api_current",
    "weather_com_current": "twc_current_icao_metric",
    "weather_com_current_icao_metric": "twc_current_icao_metric",
    "twc_current": "twc_current_icao_metric",
    "weather_com_forecast": "weather_com_forecast_peak",
    "twc_forecast": "twc_forecast_peak",
    "twc_hourly_forecast": "twc_forecast_hourly_peak",
    "taf_peak": "zuuu_taf_forecast_peak",
    "model_ensemble": "current_model_ensemble",
    "gfs_seamless": "gfs_model_peak",
    "gfs": "gfs_model_peak",
    "icon_seamless": "icon_model_peak",
    "icon": "icon_model_peak",
    "ecmwf_ifs025": "ecmwf_model_peak",
    "ecmwf": "ecmwf_model_peak",
    "meteoblue": "meteoblue_model_peak",
    "orderbook": "market_realtime_orderbook",
    "market_orderbook": "market_realtime_orderbook",
    "settlement": "settlement_actual",
}

SOURCE_CLASS_CATALOG_DEFAULTS: dict[str, dict[str, Any]] = {
    SOURCE_CLASSES["hard_observation"]: {
        "lane": "hard_observation",
        "timeout_ms": 3000,
        "cache_ttl_sec": 60,
        "raw_payload_location": "source_acquisition_trace.source_observations[].raw_ref",
        "parser": "observation_from_source_payload",
    },
    SOURCE_CLASSES["diagnostic_current"]: {
        "lane": "external_current_diagnostic",
        "timeout_ms": 3000,
        "cache_ttl_sec": 30,
        "raw_payload_location": "source_acquisition_trace.source_observations[].raw_ref",
        "parser": "observation_from_source_payload",
    },
    SOURCE_CLASSES["forecast_weak"]: {
        "lane": "forecast_weak_likelihood",
        "timeout_ms": 5000,
        "cache_ttl_sec": 3600,
        "raw_payload_location": "source_acquisition_trace.source_observations[].raw_ref",
        "parser": "source_payloads_from_fact_payload_or_refresh_result",
    },
    SOURCE_CLASSES["market"]: {
        "lane": "market_execution_context",
        "timeout_ms": 1000,
        "cache_ttl_sec": 3,
        "raw_payload_location": "archive_db.event_market_ticks_or_market_tokens",
        "parser": "source_payloads_from_refresh_result",
    },
    SOURCE_CLASSES["settlement"]: {
        "lane": "settlement",
        "timeout_ms": 5000,
        "cache_ttl_sec": 86400,
        "raw_payload_location": "event_runs.settlement_actual_or_settlement_source",
        "parser": "source_payloads_from_fact_payload",
    },
    SOURCE_CLASSES["unknown"]: {
        "lane": "unregistered_source",
        "timeout_ms": 0,
        "cache_ttl_sec": 0,
        "raw_payload_location": "unknown",
        "parser": "observation_from_source_payload",
    },
}

SOURCE_ID_CATALOG_OVERRIDES: dict[str, dict[str, Any]] = {
    "zuuu_metar_raw_awc": {
        "lane": "hard_source_reference_awc_raw_metar",
        "cache_ttl_sec": 15,
        "raw_payload_location": "AviationWeather raw METAR response/cache; reference only when AMSC is available",
        "parser": "build_trace_from_refresh_result(source_family=awc_reference)",
    },
    "zuuu_metar_raw_amsc": {
        "lane": "hard_source_primary_amsc_raw_metar",
        "cache_ttl_sec": 1800,
        "raw_payload_location": "AMSC PacketRetrieval messageRetrievalSort METAR/SPECI content",
        "parser": "fetch_zuuu_metar_taf.py --observations-json",
    },
    "awc_cache_metars_csv_gz": {
        "lane": "hard_source_reference_awc_cache",
        "cache_ttl_sec": 15,
        "raw_payload_location": "https://aviationweather.gov/data/cache/metars.cache.csv.gz; reference only when AMSC is available",
        "parser": "build_trace_from_peak_window_sample_or_awc_signal_candidate",
    },
    "twc_current_icao_metric": {
        "lane": "external_current_diagnostic",
        "cache_ttl_sec": 30,
        "raw_payload_location": "weather.com v3 current endpoint raw/cache",
    },
    "zuuu_taf_forecast_amsc": {
        "lane": "forecast_weak_primary_amsc_taf",
        "cache_ttl_sec": 1800,
        "raw_payload_location": "AMSC PacketRetrieval messageRetrievalSort TAF content",
        "parser": "fetch_zuuu_metar_taf.py --observations-json",
    },
    "zuuu_taf_forecast_peak": {
        "lane": "forecast_weak_reference_awc_taf",
        "cache_ttl_sec": 3600,
        "raw_payload_location": "AviationWeather TAF TX/TN derived peak; reference only when AMSC TAF is unavailable",
        "parser": "fetch_zuuu_taf_forecast_peak_metrics",
    },
    "wu_display_current": {
        "lane": "external_current_diagnostic",
        "cache_ttl_sec": 30,
        "raw_payload_location": "wunderground display/html or browser-derived payload",
    },
    "wu_page_api_current": {
        "lane": "external_current_diagnostic",
        "cache_ttl_sec": 30,
        "raw_payload_location": "wunderground page API current payload",
    },
    "market_realtime_orderbook": {
        "lane": "pre_signal_execution_context",
        "cache_ttl_sec": 3,
        "raw_payload_location": "archive_db.event_market_ticks",
    },
}

FINAL_OBSERVATION_FLAG_FIELDS: tuple[str, ...] = (
    "final_bucket_lock",
    "final_daily_max_bucket_lock",
    "observed_today_max_final",
    "observed_today_max_is_final",
    "observed_today_max_is_official",
    "observed_today_max_trusted_final",
    "hard_source_final_daily_max",
    "hard_source_settlement_final",
    "temperature_observation_final_daily_max",
    "official_final_result",
    "no_more_warming_possible",
)

SOURCE_OBSERVATION_PAYLOAD_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "report_type",
    "raw_sha256",
    "raw_object_ref",
    "forecast_snapshot_id",
    "response_sha256",
    "provider_family",
    "provider_available_at",
    "as_of_eligible_at",
    "lineage_quality",
    "first_seen_at",
    "arrival_time_quality",
    "verified_metar_valid_time",
    "raw_text",
    "forecast_revision_kind",
    "taf_modifier",
    "source_revision_id",
    "issue_time_utc",
    "issue_time_local",
    "forecast_peak_valid_time",
    "weather_text",
    "metar_current_no_significant_weather",
    "cloud_text",
    "clouds",
    "cloud_cover_pct",
    "low_cloud_cover_pct",
    "visibility_m",
    "visibility",
    "wind_speed_mps",
    "wind_dir_deg",
    "wind_gust_mps",
    "wind_variable",
    "wind_variable_from_deg",
    "wind_variable_to_deg",
    "pressure_hpa",
    "metar_trend_text",
    "metar_trend_cloud_text",
    "metar_trend_weather_text",
    "metar_trend_nosig",
    "dewpoint_trend_c_per_hour",
    "momentum_rate_c_per_hour",
    "temperature_rate_c_per_hour",
    "hard_source_final_lock_reason",
    "settlement_actual",
    "settlement_actual_c",
    "final_daily_max_c",
    *FINAL_OBSERVATION_FLAG_FIELDS,
)


def _source_default_keys() -> tuple[str, ...]:
    from source_observation_runtime import DEFAULT_SOURCE_REGISTRY

    return tuple(DEFAULT_SOURCE_REGISTRY.keys())


def source_acquisition_catalog() -> dict[str, Any]:
    alias_by_target: dict[str, list[str]] = {}
    for alias, target in SOURCE_ID_ALIASES.items():
        alias_by_target.setdefault(str(target), []).append(str(alias))
    rows: list[dict[str, Any]] = []
    for source_id in sorted(_source_default_keys()):
        defaults = source_defaults(source_id)
        source_class = str(defaults.get("source_class") or SOURCE_CLASSES["unknown"])
        class_defaults = dict(SOURCE_CLASS_CATALOG_DEFAULTS.get(source_class) or SOURCE_CLASS_CATALOG_DEFAULTS[SOURCE_CLASSES["unknown"]])
        override = dict(SOURCE_ID_CATALOG_OVERRIDES.get(source_id) or {})
        catalog = {**class_defaults, **override}
        rows.append(
            {
                "source_id": source_id,
                "aliases": sorted(alias_by_target.get(source_id, [])),
                "owner": "weather_source_acquisition_manager",
                "source_class": source_class,
                "role": str(defaults.get("role") or ""),
                "lane": str(catalog.get("lane") or ""),
                "timeout_ms": int(catalog.get("timeout_ms") or 0),
                "cache_ttl_sec": int(catalog.get("cache_ttl_sec") or 0),
                "raw_payload_location": str(catalog.get("raw_payload_location") or ""),
                "parser": str(catalog.get("parser") or PARSER_VERSION),
                "hard_eligible": bool(defaults.get("hard_eligible")),
                "demotion_reason": str(defaults.get("demotion_reason") or ""),
                "source_priority": int(defaults.get("source_priority") or catalog.get("source_priority") or 0),
                "source_role_tier": str(defaults.get("source_role_tier") or ""),
                "primary_for": str(defaults.get("primary_for") or ""),
                "reference_for": str(defaults.get("reference_for") or ""),
                "primary_source_id": (
                    PRIMARY_METAR_SOURCE_ID
                    if source_id in {PRIMARY_METAR_SOURCE_ID, *REFERENCE_METAR_SOURCE_IDS}
                    else PRIMARY_TAF_SOURCE_ID
                    if source_id in {PRIMARY_TAF_SOURCE_ID, *REFERENCE_TAF_SOURCE_IDS}
                    else ""
                ),
                "required_latency_fields": [
                    "source_observation_id",
                    "source_event_id",
                    "valid_time",
                    "fetched_at",
                    "as_of_eligible_at",
                    "provider_family",
                    "response_duration_ms",
                    "temperature_c_or_observed_today_max_c_or_forecast_peak_c",
                    "raw_ref",
                ],
                "write_policy": "normalized_trace_only_no_db_write",
            }
        )
    by_class: dict[str, int] = {}
    for row in rows:
        source_class = str(row.get("source_class") or "")
        by_class[source_class] = int(by_class.get(source_class, 0)) + 1
    return {
        "schema": "weather_source_acquisition_catalog_v1",
        "read_only": True,
        "source_count": len(rows),
        "alias_count": len(SOURCE_ID_ALIASES),
        "source_class_counts": dict(sorted(by_class.items())),
        "sources": rows,
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "historical_backfill": False,
            "restarts_launchd": False,
        },
        "next_boundary": "wire_fetchers_to_catalog_entries_without_changing_write_policy",
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_source_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return SOURCE_ID_ALIASES.get(text, text)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            out = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(out):
            return float(out)
    return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "ok", "success"}


def _source_name(payload: Mapping[str, Any]) -> str:
    return normalize_source_id(
        _first_text(
            payload.get("source_id"),
            payload.get("source"),
            payload.get("source_name"),
            payload.get("provider_source"),
        )
    )


def _transport(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(payload.get("transport") or payload.get("request") or payload.get("response_meta"))


def _raw_ref(payload: Mapping[str, Any], transport: Mapping[str, Any]) -> str:
    return _first_text(
        payload.get("raw_ref"),
        payload.get("source_url"),
        payload.get("url"),
        transport.get("url"),
        payload.get("raw_path"),
        payload.get("cache_path"),
    )


def _valid_time(payload: Mapping[str, Any]) -> tuple[str, str]:
    valid_utc = _first_text(
        payload.get("valid_time_utc"),
        payload.get("valid_time"),
        payload.get("validTimeUtc"),
        payload.get("observation_time"),
        payload.get("observed_at"),
        payload.get("peak_at"),
        payload.get("forecast_valid_utc"),
    )
    valid_local = _first_text(
        payload.get("valid_time_local"),
        payload.get("validTimeLocal"),
        payload.get("valid_time_local_text"),
        payload.get("forecast_valid_local"),
        payload.get("peak_local"),
    )
    return valid_utc, valid_local


def _fetched_at(payload: Mapping[str, Any], transport: Mapping[str, Any], fallback: str = "") -> str:
    return _first_text(
        payload.get("fetched_at"),
        payload.get("request_completed_at"),
        payload.get("response_completed_at"),
        payload.get("sample_completed_at"),
        payload.get("created_at"),
        transport.get("request_completed_at"),
        transport.get("response_date"),
        fallback,
    )


def _response_duration_ms(payload: Mapping[str, Any], transport: Mapping[str, Any]) -> float | None:
    return _first_float(
        payload.get("response_duration_ms"),
        payload.get("duration_ms"),
        payload.get("fetch_duration_ms"),
        transport.get("duration_ms"),
        transport.get("response_duration_ms"),
    )


def _temperature_c(payload: Mapping[str, Any]) -> float | None:
    return _first_float(
        payload.get("temperature_c"),
        payload.get("temp_c"),
        payload.get("temperature"),
        payload.get("temp"),
        payload.get("observed_temp_c"),
        payload.get("metar_temperature_c"),
        payload.get("zuuu_metar_raw_amsc_latest_temp_c"),
        payload.get("zuuu_metar_raw_awc_latest_temp_c"),
    )


def _dewpoint_c(payload: Mapping[str, Any]) -> float | None:
    return _first_float(
        payload.get("dewpoint_c"),
        payload.get("dewPt"),
        payload.get("temperatureDewPoint"),
        payload.get("zuuu_metar_raw_amsc_latest_dewpoint_c"),
        payload.get("zuuu_metar_raw_awc_latest_dewpoint_c"),
    )


def _observed_today_max_c(payload: Mapping[str, Any]) -> float | None:
    return _first_float(
        payload.get("observed_today_max_c"),
        payload.get("today_max_c"),
        payload.get("max_temperature_c"),
        payload.get("settlement_actual"),
        payload.get("settlement_actual_c"),
        payload.get("official_final_result"),
        payload.get("final_daily_max_c"),
        payload.get("zuuu_metar_raw_amsc_today_max_c"),
        payload.get("zuuu_metar_raw_awc_today_max_c"),
        payload.get("wu_twc_observed_intraday_today_max_c"),
    )


def _forecast_peak_c(payload: Mapping[str, Any]) -> float | None:
    return _first_float(
        payload.get("forecast_peak_c"),
        payload.get("value_c"),
        payload.get("peak_c"),
        payload.get("wu_forecast_peak_c"),
        payload.get("twc_forecast_peak_c"),
        payload.get("weather_com_forecast_peak_c"),
        payload.get("taf_forecast_peak_c"),
        payload.get("ecmwf_model_peak_c"),
        payload.get("gfs_model_peak_c"),
        payload.get("icon_model_peak_c"),
        payload.get("meteoblue_model_peak_c"),
    )


def _final_observation_fields_from_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        *FINAL_OBSERVATION_FLAG_FIELDS,
        "hard_source_final_lock_reason",
        "settlement_actual",
        "settlement_actual_c",
        "final_daily_max_c",
    ):
        if key in payload and payload.get(key) not in (None, ""):
            out[key] = payload.get(key)
    return out


def _final_observation_fields_for_source(payload: Mapping[str, Any], *, source_id: str) -> dict[str, Any]:
    fields = _final_observation_fields_from_mapping(payload)
    if not fields:
        return {}
    source_text = _first_text(
        payload.get("hard_source_final_daily_max_source"),
        payload.get("observed_today_max_source"),
        payload.get("source"),
        payload.get("source_id"),
    ).lower()
    source_key = str(source_id or "").strip().lower()
    if source_key == "zuuu_metar_raw_awc":
        return fields if not source_text or "awc" in source_text or "aviationweather" in source_text else {}
    if source_key == "zuuu_metar_raw_amsc":
        return fields if not source_text or "amsc" in source_text else {}
    return fields


def _quality_flags(
    payload: Mapping[str, Any],
    *,
    source_id: str,
    valid_time: str,
    raw_ref: str,
    temperature_c: float | None,
    observed_today_max_c: float | None,
    forecast_peak_c: float | None,
) -> tuple[str, ...]:
    flags: list[str] = []
    if "ok" in payload and not _boolish(payload.get("ok")):
        flags.append("source_not_ok")
    if not source_id:
        flags.append("source_id_missing")
    if not valid_time:
        flags.append("valid_time_missing")
    if not raw_ref:
        flags.append("raw_ref_missing")
    if temperature_c is None and observed_today_max_c is None and forecast_peak_c is None:
        defaults = source_defaults(source_id)
        source_class = str(defaults.get("source_class") or "")
        if source_class not in {SOURCE_CLASSES["market"]}:
            flags.append("weather_value_missing")
    return tuple(flags)


def observation_from_source_payload(
    payload: Mapping[str, Any],
    *,
    station_id: str = "",
    event_date: str = "",
    fetched_at_fallback: str = "",
    parser_version: str = PARSER_VERSION,
) -> SourceObservation:
    source_id = _source_name(payload)
    transport = _transport(payload)
    valid_time, valid_time_local = _valid_time(payload)
    fetched_at = _fetched_at(payload, transport, fallback=fetched_at_fallback)
    raw_ref = _raw_ref(payload, transport)
    temperature_c = _temperature_c(payload)
    observed_today_max_c = _observed_today_max_c(payload)
    forecast_peak_c = _forecast_peak_c(payload)
    flags = _quality_flags(
        payload,
        source_id=source_id,
        valid_time=valid_time,
        raw_ref=raw_ref,
        temperature_c=temperature_c,
        observed_today_max_c=observed_today_max_c,
        forecast_peak_c=forecast_peak_c,
    )
    embedded_payload: dict[str, Any] = {
        "source_payload_schema": "weather_source_payload_v1",
        "provider": payload.get("provider") or "",
        "ok": payload.get("ok"),
        "error": payload.get("error") or transport.get("error") or "",
        "transport": dict(transport),
        "provider_family": _first_text(payload.get("provider_family"), default_provider_family(source_id)),
        "provider_available_at": _first_text(payload.get("provider_available_at")),
        "as_of_eligible_at": _first_text(payload.get("as_of_eligible_at"), fetched_at),
    }
    for key in SOURCE_OBSERVATION_PAYLOAD_PASSTHROUGH_FIELDS:
        if key in payload and payload.get(key) not in (None, ""):
            embedded_payload[key] = payload.get(key)

    return build_source_observation(
        source_id=source_id,
        station_id=_first_text(payload.get("station_id"), station_id),
        event_date=_first_text(payload.get("event_date"), event_date),
        valid_time=valid_time,
        valid_time_local=valid_time_local,
        fetched_at=fetched_at,
        parsed_at=utc_now_iso(),
        response_completed_at=_first_text(payload.get("response_completed_at"), transport.get("request_completed_at")),
        response_duration_ms=_response_duration_ms(payload, transport),
        temperature_c=temperature_c,
        dewpoint_c=_dewpoint_c(payload),
        observed_today_max_c=observed_today_max_c,
        forecast_peak_c=forecast_peak_c,
        raw_ref=raw_ref,
        raw_payload=dict(payload),
        raw_sha256_override=_first_text(payload.get("raw_sha256")),
        parser_version=parser_version,
        quality_flags=flags,
        payload=embedded_payload,
    )


def source_latency_row(observation: SourceObservation) -> dict[str, Any]:
    payload = observation.payload if isinstance(observation.payload, Mapping) else {}
    return {
        "source_event_id": observation.source_event_id,
        "source_observation_id": observation.source_observation_id,
        "source_id": observation.source_id,
        "provider_family": _first_text(payload.get("provider_family"), default_provider_family(observation.source_id)),
        "source_class": observation.source_class,
        "role": observation.role,
        "station_id": observation.station_id,
        "event_date": observation.event_date,
        "valid_time": observation.valid_time,
        "valid_time_local": observation.valid_time_local,
        "fetched_at": observation.fetched_at,
        "provider_available_at": _first_text(payload.get("provider_available_at")),
        "as_of_eligible_at": _first_text(payload.get("as_of_eligible_at"), observation.fetched_at),
        "response_completed_at": observation.response_completed_at,
        "response_duration_ms": observation.response_duration_ms,
        "temperature_c": observation.temperature_c,
        "observed_today_max_c": observation.observed_today_max_c,
        "forecast_peak_c": observation.forecast_peak_c,
        "raw_ref": observation.raw_ref,
        "hard_eligible": observation.hard_eligible,
        "demotion_reason": observation.demotion_reason,
        "quality_flags": list(observation.quality_flags),
    }


def _features_from_fact_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    features = payload.get("weather_likelihood_features")
    if isinstance(features, Mapping):
        return features
    features = payload.get("weather_likelihood_features_compact")
    if isinstance(features, Mapping):
        return features
    return {}


def _mean_float(values: Any) -> float | None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return None
    cleaned: list[float] = []
    for value in values:
        parsed = _first_float(value)
        if parsed is not None:
            cleaned.append(float(parsed))
    if not cleaned:
        return None
    return float(sum(cleaned) / len(cleaned))


def _model_snapshot_entry(payload: Mapping[str, Any], model: str) -> Mapping[str, Any]:
    snapshot = payload.get("model_member_cache_snapshot")
    if not isinstance(snapshot, Mapping):
        return {}
    models = snapshot.get("models")
    if not isinstance(models, Mapping):
        return {}
    entry = models.get(model)
    return entry if isinstance(entry, Mapping) else {}


def _model_source_payloads_from_fact_payload(
    payload: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    fact_as_of: str,
) -> list[dict[str, Any]]:
    raw_members = payload.get("model_members_decimal_raw")
    if not isinstance(raw_members, Mapping):
        raw_members = {}
    qm_members = payload.get("model_members")
    if not isinstance(qm_members, Mapping):
        qm_members = {}
    snapshot = payload.get("model_member_cache_snapshot")
    snapshot_models = snapshot.get("models") if isinstance(snapshot, Mapping) else {}
    if not isinstance(snapshot_models, Mapping):
        snapshot_models = {}
    model_names = sorted(
        {
            *(str(key) for key in raw_members.keys()),
            *(str(key) for key in qm_members.keys()),
            *(str(key) for key in snapshot_models.keys()),
        }
    )
    sources: list[dict[str, Any]] = []
    for model in model_names:
        if not model:
            continue
        entry = _model_snapshot_entry(payload, model)
        members = raw_members.get(model)
        peak_c = _mean_float(members)
        if peak_c is None:
            peak_c = _first_float(entry.get("mean_c"), entry.get("dir_value"), entry.get("peak_c"))
        if peak_c is None:
            peak_c = _mean_float(qm_members.get(model))
        if peak_c is None:
            continue
        source_id = normalize_source_id(model)
        created_at = _first_text(entry.get("official_updated_at"), entry.get("created_at"), fact_as_of)
        source_ref = _first_text(
            entry.get("official_updated_source"),
            entry.get("source"),
            entry.get("cache_source"),
            f"model_member_cache:{model}",
        )
        raw_member_count = len(members) if isinstance(members, Sequence) and not isinstance(members, (str, bytes, bytearray)) else entry.get("member_count")
        sources.append(
            {
                "source_id": source_id,
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": created_at,
                "fetched_at": _first_text(entry.get("created_at"), fact_as_of),
                "response_completed_at": _first_text(entry.get("created_at"), fact_as_of),
                "forecast_peak_c": peak_c,
                "raw_ref": source_ref,
                "raw_payload": {
                    "model": model,
                    "forecast_peak_c": peak_c,
                    "member_count": raw_member_count,
                    "members_signature": entry.get("members_signature"),
                    "input_members_source": "model_members_decimal_raw" if model in raw_members else "model_member_cache_snapshot",
                },
            }
        )
    return sources


def source_payloads_from_fact_payload(
    payload: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    fact_as_of: str,
    hard_observation: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    features = _features_from_fact_payload(payload)
    sources: list[dict[str, Any]] = []

    def add(source: Mapping[str, Any]) -> None:
        source_id = normalize_source_id(source.get("source_id") or source.get("source"))
        if not source_id:
            return
        row = dict(source)
        row["source_id"] = source_id
        sources.append(row)

    observation = hard_observation if isinstance(hard_observation, Mapping) else {}
    if observation.get("ok"):
        source_id = normalize_source_id(observation.get("source_id"))
        final_fields = _final_observation_fields_for_source(observation, source_id=source_id)
        add(
            {
                "source_id": source_id,
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": observation.get("observed_at"),
                "fetched_at": observation.get("fetched_at") or fact_as_of,
                "response_completed_at": observation.get("fetched_at") or fact_as_of,
                "response_duration_ms": observation.get("fetch_duration_ms"),
                "temperature_c": _first_float(observation.get("temperature_c"), observation.get("value_c")),
                "dewpoint_c": observation.get("dewpoint_c"),
                "observed_today_max_c": _first_float(observation.get("observed_today_max_c"), observation.get("value_c")),
                "raw_ref": observation.get("raw"),
                **final_fields,
                "raw_payload": {
                    "source_id": source_id,
                    "observed_at": observation.get("observed_at"),
                    "fetched_at": observation.get("fetched_at"),
                    "value_c": observation.get("value_c"),
                    "temperature_c": observation.get("temperature_c"),
                    "dewpoint_c": observation.get("dewpoint_c"),
                    "observed_today_max_c": observation.get("observed_today_max_c"),
                    "raw": observation.get("raw"),
                    **final_fields,
                },
            }
        )

    if _first_float(features.get("zuuu_metar_raw_awc_latest_temp_c"), features.get("zuuu_metar_raw_awc_today_max_c")) is not None:
        awc_final_fields = _final_observation_fields_for_source(features, source_id="zuuu_metar_raw_awc")
        add(
            {
                "source_id": "zuuu_metar_raw_awc",
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": _first_text(
                    features.get("zuuu_metar_raw_awc_today_max_at"),
                    features.get("zuuu_metar_raw_awc_latest_at"),
                    features.get("observed_today_max_at"),
                    features.get("zuuu_metar_raw_awc_latest_local"),
                ),
                "valid_time_local": features.get("zuuu_metar_raw_awc_latest_local"),
                "fetched_at": _first_text(
                    features.get("zuuu_metar_raw_awc_fetched_at"),
                    features.get("awc_fetched_at"),
                    fact_as_of,
                ),
                "response_completed_at": _first_text(
                    features.get("zuuu_metar_raw_awc_fetched_at"),
                    features.get("awc_fetched_at"),
                    fact_as_of,
                ),
                "response_duration_ms": features.get("zuuu_metar_raw_awc_fetch_duration_ms"),
                "temperature_c": features.get("zuuu_metar_raw_awc_latest_temp_c"),
                "dewpoint_c": features.get("zuuu_metar_raw_awc_latest_dewpoint_c"),
                "observed_today_max_c": _first_float(
                    features.get("zuuu_metar_raw_awc_today_max_c"),
                    features.get("observed_today_max_c"),
                    features.get("zuuu_metar_raw_awc_latest_temp_c"),
                ),
                "raw_ref": _first_text(
                    features.get("zuuu_metar_raw_awc_today_max_raw"),
                    features.get("zuuu_metar_raw_awc_latest_raw"),
                    features.get("zuuu_metar_raw_awc_source_url"),
                    features.get("zuuu_metar_raw_awc_url"),
                    "awc_zuuu_metar",
                ),
                **awc_final_fields,
                "raw_payload": {
                    "source": "awc_metar_raw",
                    "latest_raw": features.get("zuuu_metar_raw_awc_latest_raw"),
                    "today_max_raw": features.get("zuuu_metar_raw_awc_today_max_raw"),
                    "latest_temp_c": features.get("zuuu_metar_raw_awc_latest_temp_c"),
                    "latest_dewpoint_c": features.get("zuuu_metar_raw_awc_latest_dewpoint_c"),
                    "today_max_c": features.get("zuuu_metar_raw_awc_today_max_c"),
                    "observed_today_max_c": features.get("observed_today_max_c"),
                    "source_role": "awc_hard_source_settlement_anchor",
                    **awc_final_fields,
                },
            }
        )

    if _first_float(features.get("zuuu_metar_raw_amsc_latest_temp_c"), features.get("zuuu_metar_raw_amsc_today_max_c")) is not None:
        amsc_final_fields = _final_observation_fields_for_source(features, source_id="zuuu_metar_raw_amsc")
        add(
            {
                "source_id": "zuuu_metar_raw_amsc",
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": _first_text(
                    features.get("zuuu_metar_raw_amsc_today_max_at"),
                    features.get("zuuu_metar_raw_amsc_latest_at"),
                    features.get("zuuu_metar_raw_amsc_latest_local"),
                ),
                "valid_time_local": features.get("zuuu_metar_raw_amsc_latest_local"),
                "fetched_at": fact_as_of,
                "response_completed_at": fact_as_of,
                "response_duration_ms": features.get("zuuu_metar_raw_amsc_fetch_duration_ms"),
                "temperature_c": features.get("zuuu_metar_raw_amsc_latest_temp_c"),
                "dewpoint_c": features.get("zuuu_metar_raw_amsc_latest_dewpoint_c"),
                "observed_today_max_c": _first_float(
                    features.get("zuuu_metar_raw_amsc_today_max_c"),
                    features.get("zuuu_metar_raw_amsc_latest_temp_c"),
                ),
                "raw_ref": _first_text(
                    features.get("zuuu_metar_raw_amsc_source_url"),
                    features.get("zuuu_metar_raw_amsc_latest_raw"),
                    "amsc_zuuu_metar_taf",
                ),
                **amsc_final_fields,
                "raw_payload": {
                    "source": "amsc_packet_retrieval",
                    "latest_raw": features.get("zuuu_metar_raw_amsc_latest_raw"),
                    "latest_temp_c": features.get("zuuu_metar_raw_amsc_latest_temp_c"),
                    "latest_dewpoint_c": features.get("zuuu_metar_raw_amsc_latest_dewpoint_c"),
                    "today_max_c": features.get("zuuu_metar_raw_amsc_today_max_c"),
                    "source_role": "primary_half_hour_hard_source_settlement_anchor",
                    **amsc_final_fields,
                },
            }
        )

    if _first_float(features.get("wu_twc_observed_intraday_today_max_c")) is not None or _first_float(
        features.get("wu_twc_observed_intraday_current_temp_c")
    ) is not None:
        add(
            {
                "source_id": "wu_twc_observed_intraday",
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": _first_text(
                    features.get("wu_twc_observed_intraday_today_max_at"),
                    features.get("wu_twc_observed_intraday_current_at"),
                ),
                "fetched_at": features.get("wu_twc_observed_intraday_fetched_at") or fact_as_of,
                "response_completed_at": features.get("wu_twc_observed_intraday_fetched_at") or fact_as_of,
                "response_duration_ms": features.get("wu_twc_observed_intraday_fetch_duration_ms"),
                "temperature_c": features.get("wu_twc_observed_intraday_current_temp_c"),
                "observed_today_max_c": features.get("wu_twc_observed_intraday_today_max_c"),
                "raw_ref": _first_text(
                    features.get("wu_twc_observed_intraday_source_url"),
                    features.get("wu_twc_observed_intraday_url"),
                ),
                "raw_payload": {
                    "today_max_c": features.get("wu_twc_observed_intraday_today_max_c"),
                    "current_temp_c": features.get("wu_twc_observed_intraday_current_temp_c"),
                    "demoted_reason": features.get("wu_twc_observed_intraday_hard_source_demoted_reason"),
                },
            }
        )

    taf_peak_c = _first_float(features.get("taf_forecast_peak_c"), payload.get("taf_forecast_peak_c"))
    if taf_peak_c is not None:
        taf_source_id = normalize_source_id(features.get("taf_forecast_peak_source"))
        if taf_source_id not in {"zuuu_taf_forecast_amsc", "zuuu_taf_forecast_peak"}:
            taf_source_id = "zuuu_taf_forecast_peak"
        add(
            {
                "source_id": taf_source_id,
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": _first_text(
                    features.get("taf_forecast_peak_at"),
                    features.get("taf_forecast_peak_valid_start_at"),
                    payload.get("taf_forecast_peak_at"),
                ),
                "fetched_at": fact_as_of,
                "response_completed_at": fact_as_of,
                "forecast_peak_c": taf_peak_c,
                "raw_ref": _first_text(
                    features.get("taf_forecast_peak_source_url"),
                    features.get("taf_forecast_peak_raw"),
                    payload.get("taf_forecast_peak_source_url"),
                    payload.get("taf_forecast_peak_raw"),
                ),
                "raw_payload": {
                    "forecast_peak_c": taf_peak_c,
                    "raw_text": _first_text(features.get("taf_forecast_peak_raw"), payload.get("taf_forecast_peak_raw")),
                    "valid_start_at": features.get("taf_forecast_peak_valid_start_at"),
                    "valid_end_at": features.get("taf_forecast_peak_valid_end_at"),
                    "source": features.get("taf_forecast_peak_source"),
                },
            }
        )

    for model_source in _model_source_payloads_from_fact_payload(
        payload,
        station_id=station_id,
        event_date=event_date,
        fact_as_of=fact_as_of,
    ):
        add(model_source)

    model_ensemble_peak_c = _first_float(
        features.get("forecast_peak_temp_c"),
        features.get("forecast_peak_c"),
        features.get("model_peak_c"),
        features.get("expected_peak_temp_c"),
    )
    if model_ensemble_peak_c is not None:
        add(
            {
                "source_id": "current_model_ensemble",
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": _first_text(
                    features.get("forecast_valid_time"),
                    features.get("model_valid_time"),
                    features.get("valid_time"),
                ),
                "fetched_at": fact_as_of,
                "response_completed_at": fact_as_of,
                "forecast_peak_c": model_ensemble_peak_c,
                "raw_ref": _first_text(
                    features.get("forecast_source"),
                    features.get("model_source"),
                    features.get("model_cache_source"),
                    "current_model_ensemble",
                ),
                "raw_payload": {
                    "forecast_peak_c": model_ensemble_peak_c,
                    "input_feature": "forecast_peak_temp_c",
                },
            }
        )

    forecast_sources = (
        (
            "wu_forecast_high",
            features.get("wu_forecast_peak_c"),
            features.get("wu_forecast_peak_at"),
            features.get("wu_forecast_peak_source"),
        ),
        (
            "weather_com_forecast_peak",
            features.get("weather_com_forecast_peak_c"),
            _first_text(
                features.get("weather_com_forecast_peak_at"),
                features.get("weather_com_forecast_peak_valid_local"),
            ),
            features.get("weather_com_forecast_peak_source_url"),
        ),
        (
            "twc_forecast_peak",
            features.get("twc_forecast_peak_c"),
            _first_text(features.get("twc_forecast_peak_at"), features.get("twc_forecast_peak_valid_local")),
            features.get("twc_forecast_peak_source_url"),
        ),
    )
    for source_id, peak_c, valid_time, raw_ref in forecast_sources:
        if _first_float(peak_c) is None:
            continue
        add(
            {
                "source_id": source_id,
                "station_id": station_id,
                "event_date": event_date,
                "valid_time": valid_time,
                "fetched_at": fact_as_of,
                "response_completed_at": fact_as_of,
                "forecast_peak_c": peak_c,
                "raw_ref": raw_ref,
                "raw_payload": {
                    "forecast_peak_c": peak_c,
                    "valid_time": valid_time,
                    "raw_ref": raw_ref,
                },
            }
        )
    return sources


def _features_from_refresh_result(result: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in (
        "source_features",
        "weather_likelihood_features",
        "weather_likelihood_features_compact",
        "features",
    ):
        features = result.get(key)
        if isinstance(features, Mapping):
            return features
    return {}


def _refresh_result_fetched_at(result: Mapping[str, Any], *, fallback: str = "") -> str:
    return _first_text(
        result.get("fact_as_of"),
        result.get("awc_fetched_at"),
        result.get("fetched_at"),
        result.get("refreshed_at"),
        result.get("request_completed_at"),
        fallback,
    )


def source_payloads_from_refresh_result(
    result: Mapping[str, Any],
    *,
    source_family: str,
    station_id: str = "",
    event_date: str = "",
    fetched_at: str = "",
) -> list[dict[str, Any]]:
    family = str(source_family or "").strip().lower()
    event_key = _first_text(result.get("event_date"), event_date)
    station_key = _first_text(result.get("station_id"), station_id)
    fetched = _refresh_result_fetched_at(result, fallback=fetched_at)
    features = _features_from_refresh_result(result)
    sources: list[dict[str, Any]] = []

    def add(source: Mapping[str, Any]) -> None:
        source_id = normalize_source_id(source.get("source_id") or source.get("source"))
        if not source_id:
            return
        row = dict(source)
        row["source_id"] = source_id
        row.setdefault("station_id", station_key)
        row.setdefault("event_date", event_key)
        row.setdefault("fetched_at", fetched)
        row.setdefault("response_completed_at", fetched)
        row.setdefault(
            "ok",
            result.get("ok") if result.get("ok") is not None else features.get("ok"),
        )
        row.setdefault("error", result.get("error") or features.get("error") or "")
        sources.append(row)

    if family in {"awc", "metar", "awc_metar", "hard_source_awc"}:
        awc_final_fields = {
            **_final_observation_fields_for_source(features, source_id="zuuu_metar_raw_awc"),
            **_final_observation_fields_for_source(result, source_id="zuuu_metar_raw_awc"),
        }
        add(
            {
                "source_id": _first_text(result.get("source_id"), features.get("source"), "zuuu_metar_raw_awc"),
                "provider": _first_text(features.get("zuuu_metar_observed_source"), "NOAA AviationWeather Raw METAR"),
                "valid_time": _first_text(
                    result.get("awc_today_max_at"),
                    result.get("awc_latest_at"),
                    features.get("zuuu_metar_raw_awc_today_max_at"),
                    features.get("zuuu_metar_raw_awc_latest_at"),
                ),
                "temperature_c": _first_float(
                    result.get("awc_latest_temp_c"),
                    features.get("zuuu_metar_raw_awc_latest_temp_c"),
                ),
                "dewpoint_c": _first_float(
                    result.get("awc_latest_dewpoint_c"),
                    features.get("zuuu_metar_raw_awc_latest_dewpoint_c"),
                ),
                "observed_today_max_c": _first_float(
                    result.get("awc_today_max_c"),
                    features.get("zuuu_metar_raw_awc_today_max_c"),
                ),
                "response_duration_ms": _first_float(
                    result.get("fetch_duration_ms"),
                    result.get("awc_fetch_duration_ms"),
                    features.get("zuuu_metar_raw_awc_fetch_duration_ms"),
                ),
                "raw_ref": _first_text(
                    result.get("awc_source_url"),
                    result.get("source_url"),
                    features.get("zuuu_metar_raw_awc_url"),
                    features.get("zuuu_metar_raw_awc_source_url"),
                    "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
                ),
                **awc_final_fields,
                "raw_payload": {
                    "refresh_status": result.get("status") or "",
                    "refresh_reason": result.get("reason") or "",
                    "awc_latest_raw": _first_text(
                        result.get("awc_latest_raw"),
                        features.get("zuuu_metar_raw_awc_latest_raw"),
                    ),
                    "awc_fetch_hours": result.get("awc_fetch_hours") or features.get("zuuu_metar_raw_awc_fetch_hours"),
                    "awc_fetch_hours_reason": result.get("awc_fetch_hours_reason")
                    or features.get("zuuu_metar_raw_awc_fetch_hours_reason"),
                    **awc_final_fields,
                },
            }
        )

    elif family in {"wu_twc", "weather_current", "external_current"}:
        synthetic_payload = {"weather_likelihood_features": dict(features)}
        fact_sources = source_payloads_from_fact_payload(
            synthetic_payload,
            station_id=station_key,
            event_date=event_key,
            fact_as_of=fetched,
        )
        for source in fact_sources:
            add(source)
        if not sources:
            add(
                {
                    "source_id": "wu_twc_observed_intraday",
                    "provider": "Weather.com/TWC + Wunderground observed intraday",
                    "valid_time": _first_text(
                        result.get("wu_twc_today_max_at"),
                        result.get("wu_twc_current_at"),
                    ),
                    "temperature_c": _first_float(result.get("wu_twc_current_temp_c")),
                    "observed_today_max_c": _first_float(result.get("wu_twc_today_max_c")),
                    "response_duration_ms": _first_float(result.get("fetch_duration_ms")),
                    "raw_ref": _first_text(
                        result.get("wu_twc_source_url"),
                        result.get("source_url"),
                        "wu_twc_observed_intraday_refresh",
                    ),
                    "raw_payload": {
                        "refresh_status": result.get("status") or "",
                        "refresh_reason": result.get("reason") or "",
                    },
                }
            )

    else:
        raw_sources = result.get("sources")
        if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes, bytearray)):
            for source in raw_sources:
                if isinstance(source, Mapping):
                    add(source)

    return sources


def build_source_acquisition_trace(
    *,
    sources: Sequence[Mapping[str, Any]],
    station_id: str = "",
    event_date: str = "",
    fetched_at: str = "",
    parser_version: str = PARSER_VERSION,
    include_payload: bool = False,
) -> dict[str, Any]:
    trace_built_at = utc_now_iso()
    observations = [
        observation_from_source_payload(
            source,
            station_id=station_id,
            event_date=event_date,
            fetched_at_fallback=fetched_at or trace_built_at,
            parser_version=parser_version,
        )
        for source in sources
        if isinstance(source, Mapping)
    ]
    source_event_ids = [obs.source_event_id for obs in observations]
    source_observation_ids = [obs.source_observation_id for obs in observations]
    by_class: dict[str, int] = {}
    for obs in observations:
        by_class[obs.source_class] = int(by_class.get(obs.source_class, 0)) + 1
    hard_candidates = [
        obs.source_event_id
        for obs in observations
        if obs.source_class == SOURCE_CLASSES["hard_observation"] and obs.hard_eligible
    ]
    return {
        "schema": SCHEMA_VERSION,
        "source_observation_schema": SOURCE_OBSERVATION_SCHEMA_VERSION,
        "read_only": True,
        "trace_built_at": trace_built_at,
        "correlation_id": build_correlation_id(
            "source_acquisition",
            station_id,
            event_date,
            source_event_ids,
        ),
        "station_id": str(station_id or "").strip(),
        "event_date": str(event_date or "").strip(),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_auto_trade_decisions": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "historical_backfill": False,
            "restarts_launchd": False,
        },
        "source_observations": [obs.as_dict(include_payload=include_payload) for obs in observations],
        "source_latency_trace": [source_latency_row(obs) for obs in observations],
        "source_event_ids": source_event_ids,
        "source_observation_ids": source_observation_ids,
        "source_class_counts": by_class,
        "hard_observation_candidate_ids": hard_candidates,
        "diagnostic_current_ids": [
            obs.source_event_id
            for obs in observations
            if obs.source_class == SOURCE_CLASSES["diagnostic_current"]
        ],
        "forecast_weak_ids": [
            obs.source_event_id
            for obs in observations
            if obs.source_class == SOURCE_CLASSES["forecast_weak"]
        ],
        "quality_summary": {
            "source_count": len(observations),
            "missing_raw_ref_count": sum(1 for obs in observations if "raw_ref_missing" in obs.quality_flags),
            "missing_valid_time_count": sum(1 for obs in observations if "valid_time_missing" in obs.quality_flags),
            "source_not_ok_count": sum(1 for obs in observations if "source_not_ok" in obs.quality_flags),
        },
        "next_boundary": "fact_snapshot_writer_dry_run_or_no_apply",
    }


def build_trace_from_peak_window_sample(
    sample: Mapping[str, Any],
    *,
    include_payload: bool = False,
) -> dict[str, Any]:
    sources = sample.get("sources")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
        sources = []
    return build_source_acquisition_trace(
        sources=[source for source in sources if isinstance(source, Mapping)],
        station_id=str(sample.get("station_id") or ""),
        event_date=str(sample.get("event_date") or ""),
        fetched_at=str(sample.get("sample_completed_at") or ""),
        include_payload=include_payload,
    )


def build_trace_from_refresh_result(
    result: Mapping[str, Any],
    *,
    source_family: str,
    station_id: str = "",
    event_date: str = "",
    fetched_at: str = "",
    include_payload: bool = False,
) -> dict[str, Any]:
    result_map = result if isinstance(result, Mapping) else {}
    fetched = _refresh_result_fetched_at(result_map, fallback=fetched_at)
    station_key = _first_text(result_map.get("station_id"), station_id)
    event_key = _first_text(result_map.get("event_date"), event_date)
    sources = source_payloads_from_refresh_result(
        result_map,
        source_family=source_family,
        station_id=station_key,
        event_date=event_key,
        fetched_at=fetched,
    )
    trace = build_source_acquisition_trace(
        sources=sources,
        station_id=station_key,
        event_date=event_key,
        fetched_at=fetched,
        include_payload=include_payload,
    )
    trace["refresh_source_family"] = str(source_family or "").strip()
    trace["refresh_status"] = str(result_map.get("status") or "")
    trace["refresh_ok"] = result_map.get("ok")
    trace["refresh_reason"] = str(result_map.get("reason") or "")
    trace["refresh_error"] = str(result_map.get("error") or "")
    trace["next_boundary"] = "refresh_result_trace_read_only_no_trade_decision"
    return trace
