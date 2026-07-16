from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from lineage_identity_runtime import build_source_event_id, build_source_observation_id


SCHEMA_VERSION = "source_observation_v1"


SOURCE_CLASSES: dict[str, str] = {
    "hard_observation": "hard_observation_candidate",
    "diagnostic_current": "diagnostic_current_source",
    "forecast_weak": "forecast_weak_source",
    "market": "market_source",
    "settlement": "settlement_source",
    "unknown": "unknown_source",
}

PRIMARY_METAR_SOURCE_ID = "zuuu_metar_raw_amsc"
REFERENCE_METAR_SOURCE_IDS: tuple[str, ...] = ("zuuu_metar_raw_awc", "awc_cache_metars_csv_gz")
PRIMARY_TAF_SOURCE_ID = "zuuu_taf_forecast_amsc"
REFERENCE_TAF_SOURCE_IDS: tuple[str, ...] = ("zuuu_taf_forecast_peak",)

DEFAULT_SOURCE_REGISTRY: dict[str, dict[str, Any]] = {
    "zuuu_metar_raw_awc": {
        "source_class": SOURCE_CLASSES["hard_observation"],
        "role": "metar_hard_observation_reference",
        "hard_eligible": True,
        "demotion_reason": "",
        "source_priority": 50,
        "source_role_tier": "reference",
        "reference_for": "metar_hard_observation",
    },
    "zuuu_metar_raw_amsc": {
        "source_class": SOURCE_CLASSES["hard_observation"],
        "role": "metar_hard_observation_primary",
        "hard_eligible": True,
        "demotion_reason": "",
        "source_priority": 100,
        "source_role_tier": "primary",
        "primary_for": "metar_hard_observation",
    },
    "awc_cache_metars_csv_gz": {
        "source_class": SOURCE_CLASSES["hard_observation"],
        "role": "metar_hard_observation_reference_cache",
        "hard_eligible": True,
        "demotion_reason": "",
        "source_priority": 40,
        "source_role_tier": "reference",
        "reference_for": "metar_hard_observation",
    },
    "zuuu_taf_forecast_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "taf_forecast_weak_likelihood_reference",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
        "source_priority": 50,
        "source_role_tier": "reference",
        "reference_for": "taf_forecast_weak_likelihood",
    },
    "zuuu_taf_forecast_amsc": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "taf_forecast_weak_likelihood_primary",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
        "source_priority": 100,
        "source_role_tier": "primary",
        "primary_for": "taf_forecast_weak_likelihood",
    },
    "current_model_ensemble": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "model_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    # Canonical logical weak-source identities. Provider/transport identity is
    # preserved inside payload.provider_source_id; Bayesian source-set logic
    # must not reuse provider-specific aliases after canonicalization.
    "taf": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "taf_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "wu": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "wu_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "twc": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "twc_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    # Official NWP provider identifiers are the canonical weak-source
    # identities.  They deliberately do not reuse the legacy *_model_peak
    # sources, whose transport and feature distributions were retired.
    "ecmwf": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "official_nwp_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "icon": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "official_nwp_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "gfs": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "official_nwp_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "jma": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "official_nwp_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "wu_forecast_high": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "settlement_ecosystem_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "wu_display_current": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "wu_twc_intraday_auxiliary_not_hard_source",
    },
    "wu_display_html": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "wu_twc_intraday_auxiliary_not_hard_source",
    },
    "wu_page_api_current": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "wu_twc_intraday_auxiliary_not_hard_source",
    },
    "wu_page_api_current_icao_metric": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "wu_twc_intraday_auxiliary_not_hard_source",
    },
    "wu_page_api_current_day_history_metric": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_day_history_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "wu_twc_intraday_auxiliary_not_hard_source",
    },
    "wu_twc_observed_intraday": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "wu_twc_intraday_auxiliary_not_hard_source",
    },
    "twc_current_icao_metric": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "twc_current_blended_not_hard_source",
    },
    "twc_realtime_display": {
        "source_class": SOURCE_CLASSES["diagnostic_current"],
        "role": "external_current_latency_diagnostic",
        "hard_eligible": False,
        "demotion_reason": "twc_current_blended_not_hard_source",
    },
    "weather_com_forecast_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "twc_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "twc_forecast_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "twc_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "twc_forecast_hourly_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "twc_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "ecmwf_model_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "model_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "gfs_model_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "model_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "icon_model_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "model_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "meteoblue_model_peak": {
        "source_class": SOURCE_CLASSES["forecast_weak"],
        "role": "model_forecast_weak_likelihood",
        "hard_eligible": False,
        "demotion_reason": "forecast_source_not_hard_observation",
    },
    "market_realtime_orderbook": {
        "source_class": SOURCE_CLASSES["market"],
        "role": "market_execution_context",
        "hard_eligible": False,
        "demotion_reason": "market_price_not_weather_evidence",
    },
    "settlement_actual": {
        "source_class": SOURCE_CLASSES["settlement"],
        "role": "settlement_actual",
        "hard_eligible": True,
        "demotion_reason": "",
    },
}


def raw_payload_sha256(raw_payload: Any) -> str:
    if raw_payload is None:
        return ""
    if isinstance(raw_payload, (bytes, bytearray)):
        raw = bytes(raw_payload)
    elif isinstance(raw_payload, str):
        raw = raw_payload.encode("utf-8")
    else:
        raw = json.dumps(raw_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def source_defaults(source_id: str) -> dict[str, Any]:
    key = str(source_id or "").strip()
    defaults = DEFAULT_SOURCE_REGISTRY.get(key)
    if defaults:
        return dict(defaults)
    return {
        "source_class": SOURCE_CLASSES["unknown"],
        "role": "unregistered_source",
        "hard_eligible": False,
        "demotion_reason": "source_not_registered",
    }


@dataclass(frozen=True)
class SourceObservation:
    source_event_id: str
    source_observation_id: str
    source_id: str
    source_class: str
    role: str
    station_id: str = ""
    event_date: str = ""
    valid_time: str = ""
    valid_time_local: str = ""
    fetched_at: str = ""
    parsed_at: str = ""
    response_completed_at: str = ""
    response_duration_ms: float | None = None
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    observed_today_max_c: float | None = None
    forecast_peak_c: float | None = None
    raw_ref: str = ""
    raw_sha256: str = ""
    parser_version: str = SCHEMA_VERSION
    hard_eligible: bool = False
    demotion_reason: str = ""
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "source_event_id": self.source_event_id,
            "source_observation_id": self.source_observation_id,
            "source_id": self.source_id,
            "source_class": self.source_class,
            "role": self.role,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "valid_time": self.valid_time,
            "valid_time_local": self.valid_time_local,
            "fetched_at": self.fetched_at,
            "parsed_at": self.parsed_at,
            "response_completed_at": self.response_completed_at,
            "response_duration_ms": self.response_duration_ms,
            "temperature_c": self.temperature_c,
            "dewpoint_c": self.dewpoint_c,
            "observed_today_max_c": self.observed_today_max_c,
            "forecast_peak_c": self.forecast_peak_c,
            "raw_ref": self.raw_ref,
            "raw_sha256": self.raw_sha256,
            "parser_version": self.parser_version,
            "hard_eligible": self.hard_eligible,
            "demotion_reason": self.demotion_reason,
            "quality_flags": list(self.quality_flags),
        }
        if include_payload:
            result["payload"] = dict(self.payload or {})
        return result


def build_source_observation(
    *,
    source_id: str,
    station_id: str = "",
    event_date: str = "",
    valid_time: str = "",
    valid_time_local: str = "",
    fetched_at: str = "",
    parsed_at: str = "",
    response_completed_at: str = "",
    response_duration_ms: float | None = None,
    temperature_c: float | None = None,
    dewpoint_c: float | None = None,
    observed_today_max_c: float | None = None,
    forecast_peak_c: float | None = None,
    raw_ref: str = "",
    raw_payload: Any = None,
    raw_sha256_override: str = "",
    parser_version: str = SCHEMA_VERSION,
    quality_flags: tuple[str, ...] | list[str] = (),
    payload: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> SourceObservation:
    defaults = source_defaults(source_id)
    if overrides:
        defaults.update(dict(overrides))
    raw_hash = str(raw_sha256_override or "").strip() or raw_payload_sha256(raw_payload)
    source_event_id = build_source_event_id(
        source_id=source_id,
        station_id=station_id,
        event_date=event_date,
        valid_time=valid_time,
        fetched_at=fetched_at or response_completed_at,
        raw_sha256=raw_hash,
    )
    source_observation_id = build_source_observation_id(
        source_id=source_id,
        station_id=station_id,
        event_date=event_date,
        valid_time=valid_time,
        valid_time_local=valid_time_local,
        fetched_at=fetched_at or response_completed_at,
        temperature_c=temperature_c,
        dewpoint_c=dewpoint_c,
        observed_today_max_c=observed_today_max_c,
        forecast_peak_c=forecast_peak_c,
        raw_ref=raw_ref,
        raw_sha256=raw_hash,
    )
    return SourceObservation(
        source_event_id=source_event_id,
        source_observation_id=source_observation_id,
        source_id=str(source_id or "").strip(),
        source_class=str(defaults.get("source_class") or SOURCE_CLASSES["unknown"]),
        role=str(defaults.get("role") or "unregistered_source"),
        station_id=str(station_id or "").strip(),
        event_date=str(event_date or "").strip(),
        valid_time=str(valid_time or "").strip(),
        valid_time_local=str(valid_time_local or "").strip(),
        fetched_at=str(fetched_at or "").strip(),
        parsed_at=str(parsed_at or "").strip(),
        response_completed_at=str(response_completed_at or "").strip(),
        response_duration_ms=response_duration_ms,
        temperature_c=temperature_c,
        dewpoint_c=dewpoint_c,
        observed_today_max_c=observed_today_max_c,
        forecast_peak_c=forecast_peak_c,
        raw_ref=str(raw_ref or "").strip(),
        raw_sha256=raw_hash,
        parser_version=str(parser_version or SCHEMA_VERSION),
        hard_eligible=bool(defaults.get("hard_eligible")),
        demotion_reason=str(defaults.get("demotion_reason") or ""),
        quality_flags=tuple(str(item) for item in quality_flags if str(item or "").strip()),
        payload=dict(payload or {}),
    )
