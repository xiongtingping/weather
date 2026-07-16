#!/usr/bin/env python3
"""Read-only contract audit for source acquisition and source observations.

The audit proves the canonical source manager can normalize production source
families into source observations with source class, latency, valid time, raw
provenance, and parser lineage. It also checks named production ingress files
for manager-compatible adapters. It never fetches external sources or writes DB.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lineage_identity_runtime import validate_lineage_id  # noqa: E402
from metar_strong_likelihood_runtime import METAR_PLATFORM_END_DROP_C  # noqa: E402
from source_observation_runtime import SOURCE_CLASSES  # noqa: E402
from weather_source_acquisition_manager import (  # noqa: E402
    PARSER_VERSION,
    build_source_acquisition_trace,
    build_trace_from_refresh_result,
    source_acquisition_catalog,
)


SCHEMA_VERSION = "source_acquisition_contract_audit_v1"
SUMMARY_SCHEMA_VERSION = f"{SCHEMA_VERSION}_summary"

REQUIRED_SOURCE_IDS: tuple[str, ...] = (
    "zuuu_metar_raw_awc",
    "awc_cache_metars_csv_gz",
    "zuuu_metar_raw_amsc",
    "zuuu_taf_forecast_peak",
    "zuuu_taf_forecast_amsc",
    "current_model_ensemble",
    "ecmwf_model_peak",
    "gfs_model_peak",
    "icon_model_peak",
    "meteoblue_model_peak",
    "wu_display_current",
    "wu_page_api_current",
    "wu_twc_observed_intraday",
    "twc_current_icao_metric",
    "weather_com_forecast_peak",
    "twc_forecast_peak",
    "twc_forecast_hourly_peak",
    "wu_forecast_high",
    "market_realtime_orderbook",
    "settlement_actual",
)

EXPECTED_ALIASES: dict[str, str] = {
    "amsc_metar": "zuuu_metar_raw_amsc",
    "amsc_taf": "zuuu_taf_forecast_amsc",
    "awc_metar_raw": "zuuu_metar_raw_awc",
    "weather_com_current": "twc_current_icao_metric",
    "wu_display_html": "wu_display_current",
    "wu_page_api_current_icao_metric": "wu_page_api_current",
    "taf_peak": "zuuu_taf_forecast_peak",
    "gfs": "gfs_model_peak",
    "icon": "icon_model_peak",
    "ecmwf": "ecmwf_model_peak",
    "orderbook": "market_realtime_orderbook",
}

PRODUCTION_INGRESS_CONTRACTS: dict[str, tuple[str, ...]] = {
    "scripts/fetch_zuuu_metar_taf.py": (
        "build_source_acquisition_trace",
        "build_fact_snapshot_write_plan",
    ),
    "scripts/hard_source_direct_poller.py": (
        "build_trace_from_refresh_result",
        "_poll_once_source_acquisition_trace_summary",
    ),
    "posterior_pipeline_entry_runtime.py": (
        "build_source_acquisition_trace",
        "weather_source_acquisition_manager",
    ),
    "scan_weather_market.py": (
        "weather_source_acquisition_manager",
        "build_source_acquisition_trace",
    ),
    "weather_fact_snapshot_writer.py": (
        "source_acquisition_trace",
        "source_observation_ids",
    ),
}

NETWORK_PATTERN = re.compile(
    r"\b(?:requests\.|urllib\.request|urlopen\(|httpx|aiohttp|curl\b)",
    re.MULTILINE,
)
WEATHER_SOURCE_HINT_PATTERN = re.compile(
    r"\b(?:weather\.com|wunderground|aviationweather|amsc|metar|taf|open-meteo|"
    r"ECMWF|GFS|ICON|Meteoblue|TWC|WU|AWC)\b",
    re.IGNORECASE,
)
NON_PRODUCTION_NETWORK_HINTS = (
    "test_",
    "/tests/",
    "backfill",
    "dry_run",
    "audit",
    "check_",
    "watcher",
    "monitor",
    "peak_window_evidence",
    "climo_reference",
    "ui/server.py",
    "clob_order_service",
    "archive_db_cutover",
    "weatherctl",
    "start.command",
    "OPTIMIZATION_PLAN",
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "executes_sql": False,
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _sample_traces() -> list[dict[str, Any]]:
    return [
        {
            "name": "awc_refresh_hard_observation",
            "trace": build_trace_from_refresh_result(
                {
                    "ok": True,
                    "status": "updated",
                    "event_date": "2026-07-03",
                    "station_id": "ZUUU",
                    "fact_as_of": "2026-07-03T09:05:36Z",
                    "awc_latest_at": "2026-07-03T09:00:00Z",
                    "awc_today_max_at": "2026-07-03T09:00:00Z",
                    "awc_today_max_c": 30.0,
                    "awc_latest_temp_c": 30.0,
                    "awc_latest_dewpoint_c": 22.0,
                    "fetch_duration_ms": 431,
                    "awc_source_url": "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
                },
                source_family="awc",
            ),
        },
        {
            "name": "amsc_metar_taf",
            "trace": build_source_acquisition_trace(
                station_id="ZUUU",
                event_date="2026-07-03",
                fetched_at="2026-07-03T09:02:08Z",
                sources=[
                    {
                        "source": "amsc_metar",
                        "provider": "AMSC PacketRetrieval",
                        "station_id": "ZUUU",
                        "event_date": "2026-07-03",
                        "valid_time_utc": "2026-07-03T09:00:00Z",
                        "valid_time_local": "2026-07-03T17:00:00+08:00",
                        "temperature_c": 30.0,
                        "dewpoint_c": 22.0,
                        "raw_ref": "AMSC METAR ZUUU 030900Z 9999 FEW020 30/22 Q1004",
                        "transport": {
                            "url": "https://www.amsc.net.cn/gateway/api/saas/rest/amc/ReportController/messageRetrievalSort",
                            "request_completed_at": "2026-07-03T09:02:08Z",
                            "duration_ms": 180,
                        },
                    },
                    {
                        "source": "amsc_taf",
                        "provider": "AMSC PacketRetrieval",
                        "station_id": "ZUUU",
                        "event_date": "2026-07-03",
                        "valid_time_utc": "2026-07-03T07:00:00Z",
                        "forecast_peak_c": 33.0,
                        "raw_ref": "AMSC TAF ZUUU 0305/0405 TX33/0307Z",
                        "transport": {
                            "url": "https://www.amsc.net.cn/gateway/api/saas/rest/amc/ReportController/messageRetrievalSort",
                            "request_completed_at": "2026-07-03T09:02:08Z",
                            "duration_ms": 180,
                        },
                    },
                ],
            ),
        },
        {
            "name": "wu_twc_current_diagnostic",
            "trace": build_source_acquisition_trace(
                station_id="ZUUU",
                event_date="2026-07-03",
                fetched_at="2026-07-03T09:15:40Z",
                sources=[
                    {
                        "source": "weather_com_current",
                        "station_id": "ZUUU",
                        "valid_time_utc": "2026-07-03T09:15:37Z",
                        "valid_time_local": "2026-07-03T17:15:37+08:00",
                        "temperature_c": 30.0,
                        "raw_ref": "https://api.weather.com/v3/wx/observations/current?icaoCode=ZUUU",
                        "transport": {
                            "url": "https://api.weather.com/v3/wx/observations/current?icaoCode=ZUUU",
                            "request_completed_at": "2026-07-03T09:15:40Z",
                            "duration_ms": 120,
                        },
                    },
                    {
                        "source": "wu_display_html",
                        "station_id": "ZUUU",
                        "valid_time_utc": "2026-07-03T09:10:00Z",
                        "valid_time_local_text": "5:10 PM CST on July 3, 2026",
                        "temperature_c": 29.4,
                        "raw_ref": "https://www.wunderground.com/weather/cn/chengdu",
                        "transport": {
                            "url": "https://www.wunderground.com/weather/cn/chengdu",
                            "request_completed_at": "2026-07-03T09:15:40Z",
                            "duration_ms": 230,
                        },
                    },
                ],
            ),
        },
        {
            "name": "forecast_market_settlement",
            "trace": build_source_acquisition_trace(
                station_id="ZUUU",
                event_date="2026-07-03",
                fetched_at="2026-07-03T09:00:02Z",
                sources=[
                    {
                        "source": "taf_peak",
                        "valid_time_utc": "2026-07-03T07:00:00Z",
                        "forecast_peak_c": 33.0,
                        "raw_ref": "TAF ZUUU 0305/0405 TX33/0307Z",
                        "transport": {"request_completed_at": "2026-07-03T09:00:02Z", "duration_ms": 90},
                    },
                    {
                        "source": "gfs",
                        "valid_time_utc": "2026-07-03T00:00:00Z",
                        "forecast_peak_c": 32.0,
                        "raw_ref": "model:gfs:2026070300",
                        "transport": {"request_completed_at": "2026-07-03T00:10:00Z", "duration_ms": 500},
                    },
                    {
                        "source": "market_orderbook",
                        "valid_time_utc": "2026-07-03T09:00:02Z",
                        "raw_ref": "clob:market:highest-temperature-in-chengdu-on-july-3-2026",
                        "transport": {"request_completed_at": "2026-07-03T09:00:02Z", "duration_ms": 45},
                    },
                    {
                        "source": "settlement",
                        "valid_time_utc": "2026-07-03T16:00:00Z",
                        "observed_today_max_c": 32.0,
                        "raw_ref": "settlement:official:highest-temperature-in-chengdu-on-july-3-2026",
                        "transport": {"request_completed_at": "2026-07-03T16:01:00Z", "duration_ms": 75},
                    },
                ],
            ),
        },
    ]


def _catalog_audit() -> dict[str, Any]:
    catalog = source_acquisition_catalog()
    sources = {
        str(row.get("source_id") or ""): _as_mapping(row)
        for row in _as_sequence(catalog.get("sources"))
        if isinstance(row, Mapping)
    }
    blockers: list[str] = []
    items: list[dict[str, Any]] = []
    for source_id in REQUIRED_SOURCE_IDS:
        row = sources.get(source_id)
        source_blockers: list[str] = []
        if not row:
            source_blockers.append("source_id_missing_from_catalog")
        else:
            if row.get("owner") != "weather_source_acquisition_manager":
                source_blockers.append("source_owner_not_manager")
            if row.get("write_policy") != "normalized_trace_only_no_db_write":
                source_blockers.append("source_write_policy_not_normalized_trace_only")
            if not row.get("source_class"):
                source_blockers.append("source_class_missing")
            if not row.get("raw_payload_location"):
                source_blockers.append("raw_payload_location_missing")
            if not row.get("parser"):
                source_blockers.append("parser_missing")
            for required in ("source_observation_id", "source_event_id", "valid_time", "fetched_at", "response_duration_ms", "raw_ref"):
                if required not in set(row.get("required_latency_fields") or []):
                    source_blockers.append(f"required_latency_field_missing:{required}")
        items.append({"source_id": source_id, "blockers": source_blockers})
        blockers.extend(f"{source_id}:{item}" for item in source_blockers)

    alias_rows = {
        alias: any(alias in set(row.get("aliases") or []) and target == source_id for source_id, row in sources.items())
        for alias, target in EXPECTED_ALIASES.items()
    }
    missing_aliases = sorted(alias for alias, ok in alias_rows.items() if not ok)
    blockers.extend(f"source_alias_missing:{alias}" for alias in missing_aliases)

    hard_sources = {
        source_id
        for source_id, row in sources.items()
        if row.get("source_class") == SOURCE_CLASSES["hard_observation"] and bool(row.get("hard_eligible"))
    }
    forbidden_hard = sorted(
        source_id
        for source_id, row in sources.items()
        if source_id in {"twc_current_icao_metric", "wu_display_current", "wu_page_api_current", "wu_twc_observed_intraday"}
        and bool(row.get("hard_eligible"))
    )
    blockers.extend(f"diagnostic_source_hard_eligible:{source_id}" for source_id in forbidden_hard)

    return {
        "schema": "source_acquisition_catalog_audit_v1",
        "ok": not blockers,
        "catalog_schema": catalog.get("schema"),
        "source_count": int(catalog.get("source_count") or 0),
        "required_source_count": len(REQUIRED_SOURCE_IDS),
        "missing_required_sources": sorted(set(REQUIRED_SOURCE_IDS) - set(sources)),
        "missing_aliases": missing_aliases,
        "hard_observation_source_ids": sorted(hard_sources),
        "diagnostic_hard_eligible_violations": forbidden_hard,
        "items": items,
        "blockers": blockers,
    }


def _observation_missing_fields(row: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if not validate_lineage_id("source_observation_id", row.get("source_observation_id")):
        missing.append("source_observation_id")
    if not validate_lineage_id("source_event_id", row.get("source_event_id")):
        missing.append("source_event_id")
    for key in ("source_id", "source_class", "role", "fetched_at", "raw_ref", "parser_version"):
        if not str(row.get(key) or "").strip():
            missing.append(key)
    if not (str(row.get("valid_time") or "").strip() or str(row.get("valid_time_local") or "").strip()):
        missing.append("valid_time")
    if row.get("response_duration_ms") in (None, ""):
        missing.append("response_duration_ms")
    if str(row.get("source_class") or "") not in set(SOURCE_CLASSES.values()):
        missing.append("source_class_registered")
    return missing


def _trace_audit(max_items_output: int) -> dict[str, Any]:
    samples = _sample_traces()
    blockers: list[str] = []
    warnings: list[str] = []
    items: list[dict[str, Any]] = []
    source_class_counts: dict[str, int] = {}
    source_ids_seen: set[str] = set()
    observation_count = 0
    for sample in samples:
        name = str(sample.get("name") or "")
        trace = _as_mapping(sample.get("trace"))
        observations = [
            _as_mapping(row)
            for row in _as_sequence(trace.get("source_observations"))
            if isinstance(row, Mapping)
        ]
        latency = [
            _as_mapping(row)
            for row in _as_sequence(trace.get("source_latency_trace"))
            if isinstance(row, Mapping)
        ]
        if not observations:
            blockers.append(f"{name}:source_observations_missing")
        if len(observations) != len(latency):
            blockers.append(f"{name}:source_latency_trace_count_mismatch")
        if len(trace.get("source_observation_ids") or []) != len(observations):
            blockers.append(f"{name}:top_level_source_observation_ids_count_mismatch")
        if len(trace.get("source_event_ids") or []) != len(observations):
            blockers.append(f"{name}:top_level_source_event_ids_count_mismatch")
        for row in observations:
            observation_count += 1
            source_id = str(row.get("source_id") or "")
            source_ids_seen.add(source_id)
            source_class = str(row.get("source_class") or "")
            source_class_counts[source_class] = int(source_class_counts.get(source_class, 0)) + 1
            missing = _observation_missing_fields(row)
            flags = [str(item) for item in row.get("quality_flags") or []]
            row_blockers = [f"source_observation_required_field_missing:{item}" for item in missing]
            row_blockers.extend(f"source_observation_quality_flag:{flag}" for flag in flags if flag in {"source_id_missing", "valid_time_missing", "raw_ref_missing", "weather_value_missing"})
            if source_id in {"twc_current_icao_metric", "wu_display_current", "wu_page_api_current", "wu_twc_observed_intraday"} and bool(row.get("hard_eligible")):
                row_blockers.append("diagnostic_current_promoted_to_hard_source")
            if source_id in {"zuuu_metar_raw_awc", "awc_cache_metars_csv_gz", "zuuu_metar_raw_amsc"} and source_class != SOURCE_CLASSES["hard_observation"]:
                row_blockers.append("metar_hard_source_not_hard_observation")
            if source_id in {"zuuu_taf_forecast_peak", "zuuu_taf_forecast_amsc", "gfs_model_peak"} and source_class != SOURCE_CLASSES["forecast_weak"]:
                row_blockers.append("forecast_source_not_forecast_weak")
            blockers.extend(f"{name}:{source_id}:{item}" for item in row_blockers)
            items.append(
                {
                    "sample": name,
                    "source_id": source_id,
                    "source_class": source_class,
                    "source_observation_id": row.get("source_observation_id"),
                    "source_event_id": row.get("source_event_id"),
                    "valid_time": row.get("valid_time") or row.get("valid_time_local"),
                    "fetched_at": row.get("fetched_at"),
                    "response_duration_ms": row.get("response_duration_ms"),
                    "raw_ref": row.get("raw_ref"),
                    "parser_version": row.get("parser_version"),
                    "hard_eligible": bool(row.get("hard_eligible")),
                    "quality_flags": flags,
                    "blockers": row_blockers,
                }
            )
        if trace.get("guards") and _as_mapping(trace.get("guards")).get("writes_archive_db"):
            blockers.append(f"{name}:trace_guard_writes_archive_db")
        if trace.get("next_boundary") not in {
            "fact_snapshot_writer_dry_run_or_no_apply",
            "refresh_result_trace_read_only_no_trade_decision",
        }:
            warnings.append(f"{name}:unexpected_next_boundary:{trace.get('next_boundary')}")

    missing_sample_sources = sorted(set(REQUIRED_SOURCE_IDS) - source_ids_seen)
    # The samples do not need to instantiate every model/provider, but the core
    # production hard/weak/diagnostic classes must be represented.
    required_seen = {
        "zuuu_metar_raw_awc",
        "zuuu_metar_raw_amsc",
        "zuuu_taf_forecast_amsc",
        "twc_current_icao_metric",
        "wu_display_current",
        "gfs_model_peak",
        "market_realtime_orderbook",
        "settlement_actual",
    }
    missing_core = sorted(required_seen - source_ids_seen)
    blockers.extend(f"core_sample_source_missing:{source_id}" for source_id in missing_core)

    visible_limit = max(0, int(max_items_output))
    return {
        "schema": "source_observation_contract_sample_audit_v1",
        "ok": not blockers,
        "sample_count": len(samples),
        "observation_count": observation_count,
        "source_ids_seen": sorted(source_ids_seen),
        "source_class_counts": dict(sorted(source_class_counts.items())),
        "missing_required_sources_in_samples": missing_sample_sources,
        "missing_core_sources_in_samples": missing_core,
        "items": items[:visible_limit] if visible_limit else [],
        "items_truncated": len(items) > visible_limit,
        "blockers": blockers,
        "warnings": warnings,
    }


def _is_non_production_network_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(hint in normalized for hint in NON_PRODUCTION_NETWORK_HINTS)


def _network_inventory(root: Path, max_items_output: int) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", "data/", "node_modules/", ".playwright-cli/", ".venv/")):
            continue
        if path.suffix not in {"", ".py", ".sh", ".command", ".md", ".mjs", ".js"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not NETWORK_PATTERN.search(text):
            continue
        production_requirements = PRODUCTION_INGRESS_CONTRACTS.get(rel)
        if production_requirements:
            missing = [token for token in production_requirements if token not in text]
            classification = "canonical_production_source_ingress"
            if missing:
                blockers.append(f"{rel}:production_source_ingress_missing_manager_adapter")
        elif _is_non_production_network_file(rel):
            missing = []
            classification = "non_production_diagnostic_backfill_or_test"
        elif WEATHER_SOURCE_HINT_PATTERN.search(text):
            missing = []
            classification = "weather_network_entrypoint_needs_classification"
            warnings.append(f"{rel}:weather_network_entrypoint_needs_classification")
        else:
            missing = []
            classification = "non_weather_network_entrypoint"
        files.append(
            {
                "path": rel,
                "classification": classification,
                "production_requirements": list(production_requirements or ()),
                "missing_production_requirements": missing,
            }
        )
    visible_limit = max(0, int(max_items_output))
    return {
        "schema": "source_acquisition_network_ingress_inventory_v1",
        "ok": not blockers,
        "network_file_count": len(files),
        "production_ingress_count": sum(1 for row in files if row["classification"] == "canonical_production_source_ingress"),
        "non_production_network_count": sum(1 for row in files if row["classification"] == "non_production_diagnostic_backfill_or_test"),
        "weather_network_needs_classification_count": sum(1 for row in files if row["classification"] == "weather_network_entrypoint_needs_classification"),
        "files": files[:visible_limit] if visible_limit else [],
        "files_truncated": len(files) > visible_limit,
        "blockers": blockers,
        "warnings": warnings,
        "boundary": "production_weather_source_ingress_must_import_source_acquisition_manager_or_compatible_adapter",
    }


def _catalog_timeout_by_source() -> dict[str, int]:
    catalog = source_acquisition_catalog()
    out: dict[str, int] = {}
    for row in _as_sequence(catalog.get("sources")):
        item = _as_mapping(row)
        source_id = str(item.get("source_id") or "")
        if source_id:
            out[source_id] = int(item.get("timeout_ms") or 0)
    return out


def _hard_observation_conflicts(trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for raw in _as_sequence(trace.get("source_observations")):
        row = _as_mapping(raw)
        if (
            str(row.get("source_class") or "") != SOURCE_CLASSES["hard_observation"]
            or not bool(row.get("hard_eligible"))
            or row.get("temperature_c") in (None, "")
        ):
            continue
        key = (
            str(row.get("station_id") or "").strip(),
            str(row.get("event_date") or "").strip(),
            str(row.get("valid_time") or row.get("valid_time_local") or "").strip(),
        )
        grouped.setdefault(key, []).append(row)
    conflicts: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        if len(rows) < 2:
            continue
        temps: list[float] = []
        for row in rows:
            try:
                temps.append(float(row.get("temperature_c")))
            except (TypeError, ValueError):
                pass
        if len(temps) < 2:
            continue
        delta = max(temps) - min(temps)
        if delta >= float(METAR_PLATFORM_END_DROP_C):
            conflicts.append(
                {
                    "station_id": key[0],
                    "event_date": key[1],
                    "valid_time": key[2],
                    "temperature_delta_c": round(float(delta), 6),
                    "conflict_threshold_c": float(METAR_PLATFORM_END_DROP_C),
                    "sources": [
                        {
                            "source_id": str(row.get("source_id") or ""),
                            "source_observation_id": str(row.get("source_observation_id") or ""),
                            "temperature_c": row.get("temperature_c"),
                        }
                        for row in rows
                    ],
                }
            )
    return conflicts


def _delayed_latency_rows(trace: Mapping[str, Any], timeout_by_source: Mapping[str, int]) -> list[dict[str, Any]]:
    delayed: list[dict[str, Any]] = []
    for raw in _as_sequence(trace.get("source_latency_trace")):
        row = _as_mapping(raw)
        source_id = str(row.get("source_id") or "")
        timeout_ms = int(timeout_by_source.get(source_id) or 0)
        try:
            duration_ms = float(row.get("response_duration_ms"))
        except (TypeError, ValueError):
            continue
        if timeout_ms > 0 and duration_ms > float(timeout_ms):
            delayed.append(
                {
                    "source_id": source_id,
                    "source_observation_id": str(row.get("source_observation_id") or ""),
                    "response_duration_ms": duration_ms,
                    "catalog_timeout_ms": timeout_ms,
                    "delay_over_timeout_ms": round(duration_ms - float(timeout_ms), 6),
                }
            )
    return delayed


def _data_quality_scenario_audit() -> dict[str, Any]:
    timeout_by_source = _catalog_timeout_by_source()
    missing_trace = build_source_acquisition_trace(
        station_id="ZUUU",
        event_date="2026-07-03",
        fetched_at="2026-07-03T09:02:08Z",
        sources=[
            {
                "source": "amsc_metar",
                "provider": "AMSC PacketRetrieval",
                "station_id": "ZUUU",
                "event_date": "2026-07-03",
                "ok": False,
                "error": "fixture_missing_raw_valid_time_and_weather_value",
                "transport": {
                    "request_completed_at": "2026-07-03T09:02:08Z",
                    "duration_ms": 180,
                },
            }
        ],
    )
    conflict_trace = build_source_acquisition_trace(
        station_id="ZUUU",
        event_date="2026-07-03",
        fetched_at="2026-07-03T09:02:10Z",
        sources=[
            {
                "source": "awc_metar_raw",
                "station_id": "ZUUU",
                "event_date": "2026-07-03",
                "valid_time_utc": "2026-07-03T09:00:00Z",
                "temperature_c": 30.0,
                "raw_ref": "METAR ZUUU 030900Z 9999 FEW020 30/22 Q1004",
                "transport": {"request_completed_at": "2026-07-03T09:01:00Z", "duration_ms": 220},
            },
            {
                "source": "amsc_metar",
                "station_id": "ZUUU",
                "event_date": "2026-07-03",
                "valid_time_utc": "2026-07-03T09:00:00Z",
                "temperature_c": 32.0,
                "raw_ref": "METAR ZUUU 030900Z 9999 FEW020 32/22 Q1004",
                "transport": {"request_completed_at": "2026-07-03T09:01:10Z", "duration_ms": 180},
            },
        ],
    )
    timeout_ms = int(timeout_by_source.get("twc_current_icao_metric") or 3000)
    latency_trace = build_source_acquisition_trace(
        station_id="ZUUU",
        event_date="2026-07-03",
        fetched_at="2026-07-03T09:15:40Z",
        sources=[
            {
                "source": "weather_com_current",
                "station_id": "ZUUU",
                "event_date": "2026-07-03",
                "valid_time_utc": "2026-07-03T09:15:37Z",
                "temperature_c": 30.0,
                "raw_ref": "https://api.weather.com/v3/wx/observations/current?icaoCode=ZUUU",
                "transport": {
                    "url": "https://api.weather.com/v3/wx/observations/current?icaoCode=ZUUU",
                    "request_completed_at": "2026-07-03T09:15:40Z",
                    "duration_ms": timeout_ms + 250,
                },
            }
        ],
    )

    missing_observations = [
        _as_mapping(row)
        for row in _as_sequence(missing_trace.get("source_observations"))
        if isinstance(row, Mapping)
    ]
    missing_flags = sorted(
        {
            str(flag)
            for row in missing_observations
            for flag in _as_sequence(row.get("quality_flags"))
        }
    )
    expected_missing_flags = {"source_not_ok", "valid_time_missing", "raw_ref_missing", "weather_value_missing"}
    conflicts = _hard_observation_conflicts(conflict_trace)
    delayed = _delayed_latency_rows(latency_trace, timeout_by_source)
    checks = {
        "missing_source_quality_flags_detected": expected_missing_flags.issubset(set(missing_flags)),
        "missing_source_quality_summary_counts": (
            _as_mapping(missing_trace.get("quality_summary")).get("source_not_ok_count") == 1
            and _as_mapping(missing_trace.get("quality_summary")).get("missing_raw_ref_count") == 1
            and _as_mapping(missing_trace.get("quality_summary")).get("missing_valid_time_count") == 1
        ),
        "conflicting_hard_observation_detected": bool(conflicts),
        "conflict_threshold_uses_metar_platform_policy": all(
            float(item.get("conflict_threshold_c") or 0.0) == float(METAR_PLATFORM_END_DROP_C)
            for item in conflicts
        ),
        "delayed_source_detected_from_catalog_timeout": bool(delayed),
        "delayed_source_uses_catalog_timeout": all(
            int(item.get("catalog_timeout_ms") or 0) == timeout_ms for item in delayed
        ),
    }
    blockers = [f"data_quality_contract_{key}_failed" for key, ok in checks.items() if not ok]
    return {
        "schema": "source_acquisition_data_quality_scenario_audit_v1",
        "ok": not blockers,
        "read_only": True,
        "checks": checks,
        "missing_source": {
            "quality_flags": missing_flags,
            "quality_summary": dict(_as_mapping(missing_trace.get("quality_summary"))),
            "expected_quality_flags": sorted(expected_missing_flags),
        },
        "conflicting_source": {
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "conflict_threshold_source": "metar_strong_likelihood_runtime.METAR_PLATFORM_END_DROP_C",
        },
        "delayed_source": {
            "delayed_count": len(delayed),
            "delayed_rows": delayed,
            "timeout_source": "weather_source_acquisition_manager.source_acquisition_catalog.timeout_ms",
        },
        "blockers": blockers,
        "warnings": [],
    }


def build_source_acquisition_contract_audit(
    *,
    root: Path = ROOT,
    max_items_output: int = 20,
) -> dict[str, Any]:
    catalog = _catalog_audit()
    trace = _trace_audit(max_items_output=max_items_output)
    network = _network_inventory(Path(root), max_items_output=max_items_output)
    data_quality = _data_quality_scenario_audit()
    blockers = []
    blockers.extend(f"catalog:{item}" for item in catalog.get("blockers") or [])
    blockers.extend(f"trace:{item}" for item in trace.get("blockers") or [])
    blockers.extend(f"network:{item}" for item in network.get("blockers") or [])
    blockers.extend(f"data_quality:{item}" for item in data_quality.get("blockers") or [])
    warnings = []
    warnings.extend(f"trace:{item}" for item in trace.get("warnings") or [])
    warnings.extend(f"network:{item}" for item in network.get("warnings") or [])
    warnings.extend(f"data_quality:{item}" for item in data_quality.get("warnings") or [])
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "source_acquisition_contract_audit_read_only",
        "root": str(Path(root).expanduser().resolve()),
        "parser_version": PARSER_VERSION,
        "coverage": {
            "checked_fact_count": 1,
            "sample_trace_count": int(trace.get("sample_count") or 0),
            "sample_source_observation_count": int(trace.get("observation_count") or 0),
            "catalog_required_source_count": int(catalog.get("required_source_count") or 0),
            "network_file_count": int(network.get("network_file_count") or 0),
            "data_quality_scenario_count": 3,
        },
        "guards": _guards(),
        "catalog_audit": catalog,
        "source_observation_sample_audit": trace,
        "network_ingress_inventory": network,
        "data_quality_scenario_audit": data_quality,
        "blockers": blockers,
        "warnings": warnings,
        "next_boundary": "source_acquisition_contract_ok_continue_to_fact_writer" if not blockers else "fix_source_acquisition_contract_before_release_gate",
    }


def compact_audit(report: Mapping[str, Any]) -> dict[str, Any]:
    catalog = _as_mapping(report.get("catalog_audit"))
    trace = _as_mapping(report.get("source_observation_sample_audit"))
    network = _as_mapping(report.get("network_ingress_inventory"))
    data_quality = _as_mapping(report.get("data_quality_scenario_audit"))
    return {
        "schema": SUMMARY_SCHEMA_VERSION,
        "source_schema": report.get("schema"),
        "ok": bool(report.get("ok")),
        "read_only": bool(report.get("read_only")),
        "execution_mode": "source_acquisition_contract_audit_summary_read_only",
        "parser_version": report.get("parser_version"),
        "catalog": {
            "ok": bool(catalog.get("ok")),
            "source_count": int(catalog.get("source_count") or 0),
            "required_source_count": int(catalog.get("required_source_count") or 0),
            "hard_observation_source_ids": list(catalog.get("hard_observation_source_ids") or []),
            "missing_aliases": list(catalog.get("missing_aliases") or []),
        },
        "source_observation_samples": {
            "ok": bool(trace.get("ok")),
            "sample_count": int(trace.get("sample_count") or 0),
            "observation_count": int(trace.get("observation_count") or 0),
            "source_class_counts": dict(_as_mapping(trace.get("source_class_counts"))),
            "missing_core_sources_in_samples": list(trace.get("missing_core_sources_in_samples") or []),
        },
        "network_ingress": {
            "ok": bool(network.get("ok")),
            "network_file_count": int(network.get("network_file_count") or 0),
            "production_ingress_count": int(network.get("production_ingress_count") or 0),
            "non_production_network_count": int(network.get("non_production_network_count") or 0),
            "weather_network_needs_classification_count": int(network.get("weather_network_needs_classification_count") or 0),
        },
        "data_quality": {
            "ok": bool(data_quality.get("ok")),
            "checks": dict(_as_mapping(data_quality.get("checks"))),
            "missing_quality_flags": list(_as_mapping(data_quality.get("missing_source")).get("quality_flags") or []),
            "conflict_count": int(_as_mapping(data_quality.get("conflicting_source")).get("conflict_count") or 0),
            "delayed_count": int(_as_mapping(data_quality.get("delayed_source")).get("delayed_count") or 0),
        },
        "guards": dict(_as_mapping(report.get("guards"))),
        "blockers": list(report.get("blockers") or []),
        "warnings": list(report.get("warnings") or [])[:20],
        "next_boundary": report.get("next_boundary"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--summary", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Compatibility alias for release/checklist runners; this static audit does not query DB rows.",
    )
    parser.add_argument("--max-items-output", type=int, default=20)
    args = parser.parse_args(argv)
    report = build_source_acquisition_contract_audit(
        root=args.root,
        max_items_output=args.max_items_output,
    )
    payload = compact_audit(report) if args.summary else report
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
