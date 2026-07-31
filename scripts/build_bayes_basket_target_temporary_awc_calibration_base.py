#!/usr/bin/env python3
"""Build temporary-AWC no-live calibration base facts for the 9 AMSC stations.

This command reads only local audit JSON, writes only one bounded JSON packet
under ``output/``, and never touches a production/shared database, a gateway,
or live state.  The packet is intentionally honest about lineage:

- station registry lane stays canonical ``AMSC`` for the registry's 9 stations
- truth rows come from AWC calibration datasets with
  ``TEMPORARY_AWC_CALIBRATION_LABEL``
- all model rows remain historical replay candidates only
- capture provider/source lineage from the five-model OOS files is retained

The current five-model calibration captures for ECMWF/GFS are sourced from
explicit Open-Meteo previous-runs transports.  Those transports are registered
as historical-replay-only aggregator evidence and can never claim native
publication/arrival authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration_ingress import (  # noqa: E402
    NO_LIVE_REPLAY,
    TEMPORARY_AWC_LABEL,
    ingest_calibration_base_facts,
)
from bayes_basket_target.contracts import (  # noqa: E402
    MODEL_IDS,
    ContractError,
    assert_timestamp_not_future,
    content_sha256,
)
from bayes_basket_target.model_sources import (  # noqa: E402
    HISTORICAL_REPLAY_ONLY_PROVIDERS,
    model_id_for_provider,
    provider_role,
    provider_source_class,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MIB,
    ResourceBoundError,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402


SCHEMA = "bayes_basket_target_calibration_evidence_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
REGISTRY_ROLE = "CANONICAL_AMSC_AWC_INGRESS"
DEFAULT_AUDIT_ROOT = ROOT / "output/audits/bayes_basket_target_45station_five_model_calibration"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_temporary_awc_calibration_base.json"
DEFAULT_STATION_LANE_MAP = ROOT / "config/trading/bayes_basket_target_station_lane_map_v1.json"
DEFAULT_MARKET_REGISTRY = ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
DEFAULT_SOURCE_NAME = "five_model_calibration_audit"
WHOLE_HOUR_MINUTES = {0}
MAX_INPUT_ARTIFACTS = 32
MAX_INPUT_ARTIFACT_BYTES = 64 * MIB


@dataclass(frozen=True)
class StationMeta:
    station_id: str
    timezone_name: str
    city_label: str


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_input_bundle_bounded(paths: Iterable[Path]) -> None:
    resolved = [Path(path).expanduser().resolve() for path in paths]
    if len(resolved) > MAX_INPUT_ARTIFACTS:
        raise ResourceBoundError("temporary_awc_input_file_count_exceeded")
    total_bytes = 0
    for path in resolved:
        try:
            if not path.is_file():
                raise ResourceBoundError("temporary_awc_input_file_missing")
            total_bytes += int(path.stat().st_size)
        except OSError as exc:
            raise ResourceBoundError("temporary_awc_input_stat_failed") from exc
        if total_bytes > MAX_INPUT_ARTIFACT_BYTES:
            raise ResourceBoundError("temporary_awc_input_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=min(total_bytes * 4, 128 * MIB))


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label}_mapping_required")
    return dict(value)


def _rows(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError(f"{label}_list_required")
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ContractError(f"{label}_row_invalid")
        rows.append(dict(item))
    return rows


def _text(value: Any, *, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label}_missing")
    return result


def _hex64(value: Any, *, label: str) -> str:
    result = _text(value, label=label).lower()
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ContractError(f"{label}_invalid")
    return result


def _iso_utc_hour(value: str, *, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label}_invalid_iso8601") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label}_timezone_required")
    if (
        parsed.astimezone(timezone.utc).minute not in WHOLE_HOUR_MINUTES
        or parsed.astimezone(timezone.utc).second != 0
        or parsed.astimezone(timezone.utc).microsecond != 0
    ):
        raise ContractError(f"{label}_must_be_whole_hour")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _derive_run_init_utc(event_date: str, lead_days: int) -> str:
    base = date.fromisoformat(event_date) - timedelta(days=int(lead_days))
    return datetime(base.year, base.month, base.day, tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _with_content_sha256(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["content_sha256"] = content_sha256(payload)
    return payload


def _station_scope(path: Path) -> list[str]:
    payload = load_bounded_json(path.expanduser().resolve())
    stations = payload.get("stations") if isinstance(payload, Mapping) else payload
    if not isinstance(stations, Mapping):
        raise ContractError("temporary_awc_station_lane_map_invalid")
    selected = sorted(
        station_id.strip().upper()
        for station_id, lane in stations.items()
        if str(lane or "").strip().lower() == "amsc"
    )
    if not selected:
        raise ContractError("temporary_awc_station_scope_empty")
    return selected


def _station_metadata(path: Path, station_ids: Iterable[str]) -> dict[str, StationMeta]:
    payload = load_bounded_json(path.expanduser().resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("temporary_awc_market_registry_rows_missing")
    target = {station_id.upper() for station_id in station_ids}
    timezones: dict[str, set[str]] = defaultdict(set)
    cities: dict[str, set[str]] = defaultdict(set)
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        station_id = str(item.get("station_id") or "").strip().upper()
        if station_id not in target:
            continue
        timezone_name = str(item.get("settlement_timezone") or "").strip()
        if timezone_name:
            timezones[station_id].add(timezone_name)
        city_label = str(item.get("city") or item.get("city_cn") or "").strip()
        if city_label:
            cities[station_id].add(city_label)
    missing = sorted(target - set(timezones))
    if missing:
        raise ContractError("temporary_awc_station_timezone_missing:" + ",".join(missing))
    result: dict[str, StationMeta] = {}
    for station_id in sorted(target):
        zone_values = sorted(timezones[station_id])
        if len(zone_values) != 1:
            raise ContractError("temporary_awc_station_timezone_not_unique:" + station_id)
        result[station_id] = StationMeta(
            station_id=station_id,
            timezone_name=zone_values[0],
            city_label=sorted(cities[station_id])[0] if cities[station_id] else "",
        )
    return result


def _station_row(meta: StationMeta) -> dict[str, Any]:
    return _with_content_sha256(
        {
            "station_id": meta.station_id,
            "icao_code": meta.station_id,
            "city_label": meta.city_label,
            "timezone_name": meta.timezone_name,
            "source_lane": "AMSC",
            "settlement_definition": SETTLEMENT_DEFINITION,
            "registry_version": SCHEMA,
            "source_role": REGISTRY_ROLE,
        }
    )


def _truth_row(
    *,
    station_id: str,
    timezone_name: str,
    sample: Mapping[str, Any],
    dataset_file_sha256: str,
    dataset_content_sha256: str,
    dataset_path: Path,
    received_at_utc: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    truth = _mapping(sample.get("truth"), label="temporary_awc_truth")
    event_date = _text(sample.get("event_date"), label="temporary_awc_event_date")
    sample_id = _text(sample.get("sample_id"), label="temporary_awc_sample_id")
    if _text(sample.get("station_id"), label="temporary_awc_sample_station_id").upper() != station_id:
        raise ContractError("temporary_awc_truth_station_mismatch")
    if _text(truth.get("source_lane"), label="temporary_awc_truth_source_lane").upper() != "AWC":
        raise ContractError("temporary_awc_truth_source_lane_must_be_awc")
    observations = _rows(truth.get("observations"), label="temporary_awc_truth_observations")
    if not observations:
        raise ContractError("temporary_awc_truth_observations_empty")
    observation_ids: list[str] = []
    observation_raw_sha256s: list[str] = []
    normalized_observations: list[dict[str, Any]] = []
    for index, obs in enumerate(observations):
        valid_at_utc = _iso_utc_hour(
            _text(obs.get("valid_at_utc"), label="temporary_awc_observation_valid_at_utc"),
            label="temporary_awc_observation_valid_at_utc",
        )
        if _text(obs.get("station_id"), label="temporary_awc_observation_station_id").upper() != station_id:
            raise ContractError("temporary_awc_observation_station_mismatch")
        if _text(obs.get("source_lane"), label="temporary_awc_observation_source_lane").upper() != "AWC":
            raise ContractError("temporary_awc_observation_source_lane_must_be_awc")
        raw_sha256 = _hex64(obs.get("raw_sha256"), label="temporary_awc_observation_raw_sha256")
        observation_raw_sha256s.append(raw_sha256)
        observation_ids.append(f"{station_id}-{event_date}-{index:02d}-{raw_sha256[:16]}")
        normalized_observations.append(
            {
                "raw_sha256": raw_sha256,
                "report_type": _text(obs.get("report_type"), label="temporary_awc_observation_report_type").upper(),
                "reported_temperature_c": float(obs["reported_temperature_c"]),
                "source_lane": "AWC",
                "station_id": station_id,
                "valid_at_utc": valid_at_utc,
            }
        )
    truth_rows_sha256 = content_sha256(normalized_observations)
    truth_id = f"temp-awc-truth-{station_id}-{event_date}"
    row = _with_content_sha256(
        {
            "truth_id": truth_id,
            "station_id": station_id,
            "local_date": event_date,
            "target_timezone": timezone_name,
            "official_tmax_c": float(truth["tmax_c"]),
            "source_lane": "AWC",
            "source_role": TEMPORARY_AWC_LABEL,
            "settlement_definition": SETTLEMENT_DEFINITION,
            "revision_no": 0,
            "published_at_utc": None,
            "received_at_utc": received_at_utc,
            "canonical_committed_at_utc": received_at_utc,
            "timestamps_complete": True,
            "qc_status": "ACCEPTED",
            "source_payload_sha256": truth_rows_sha256,
            "observation_ids": observation_ids,
            "input_dataset_content_sha256": dataset_content_sha256,
            "input_dataset_file_sha256": dataset_file_sha256,
            "input_dataset_path": str(dataset_path),
            "truth_sample_id": sample_id,
            "observation_raw_sha256s": observation_raw_sha256s,
            "whole_hour_observations_only": True,
        }
    )
    return row, {
        "truth_id": truth_id,
        "truth_rows_sha256": truth_rows_sha256,
        "truth_sample_id": sample_id,
        "observation_raw_sha256s": observation_raw_sha256s,
    }


def _model_row(
    *,
    station_id: str,
    timezone_name: str,
    event_date: str,
    lead_days: int,
    oos_path: Path,
    oos_file_sha256: str,
    oos_fetched_at_utc: str,
    capture: Mapping[str, Any],
    daily_row: Mapping[str, Any],
    identity_prefix: str = "temp-awc",
    maturity_prefix: str = "TEMPORARY_AWC_PREVIOUS_RUN",
    capture_mode: str = "temporary_awc_calibration_base",
) -> dict[str, Any]:
    model_id = _text(capture.get("model_id"), label="temporary_awc_capture_model_id").upper()
    lead_bucket = f"d{lead_days}"
    run_init_utc = _derive_run_init_utc(event_date, lead_days)
    provider_id = _text(capture.get("provider_id"), label="temporary_awc_capture_provider_id").lower()
    source_class = _text(capture.get("source_class"), label="temporary_awc_capture_source_class").lower()
    expected_source_class = provider_source_class(provider_id)
    if model_id_for_provider(provider_id) != model_id:
        raise ContractError("temporary_awc_capture_model_provider_mismatch")
    if source_class != expected_source_class:
        raise ContractError("temporary_awc_capture_source_class_mismatch")
    if bool(capture.get("formal_lineage_eligible")):
        raise ContractError("temporary_awc_capture_formal_lineage_forbidden")
    response_sha256 = _hex64(capture.get("response_sha256"), label="temporary_awc_capture_response_sha256")
    capture_index = int(capture.get("_capture_index", 0))
    return _with_content_sha256(
        {
            "model_run_id": (
                f"{identity_prefix}-run-{station_id}-{event_date}-"
                f"{model_id}-d{lead_days}"
            ),
            "model_id": model_id,
            "station_id": station_id,
            "target_local_date": event_date,
            "target_timezone": timezone_name,
            "run_init_utc": run_init_utc,
            "published_at_utc": None,
            "received_at_utc": oos_fetched_at_utc,
            "timestamps_complete": False,
            "lead_bucket": lead_bucket,
            "maturity_key": f"{maturity_prefix}_D{lead_days}",
            "run_cycle": f"PREVIOUS_DAY{lead_days}",
            "raw_tmax_c": float(daily_row["forecast_tmax_c"]),
            "settlement_definition": SETTLEMENT_DEFINITION,
            "provider_id": provider_id,
            "provider_role": provider_role(provider_id),
            "source_class": source_class,
            "aggregator_or_republisher": source_class == "aggregator_republisher",
            "open_meteo": "open_meteo" in provider_id,
            "source_url": _text(capture.get("source_url"), label="temporary_awc_capture_source_url"),
            "raw_object_ref": f"{oos_path}#captures/{capture_index}/daily_rows/{daily_row.get('_daily_index', 0)}",
            "source_object_sha256": response_sha256,
            "asof_status": "HISTORICAL_REPLAY_ONLY",
            "capture_modes": [
                "historical_replay",
                capture_mode,
            ],
            "historical_replay_eligible": True,
            "prospective_strict_asof_eligible": False,
            "input_oos_file_sha256": oos_file_sha256,
            "capture_response_sha256": response_sha256,
            "capture_api_model": _text(capture.get("api_model"), label="temporary_awc_capture_api_model"),
            "capture_formal_lineage_eligible": bool(capture.get("formal_lineage_eligible")),
            "capture_previous_run_semantics": _text(
                capture.get("previous_run_semantics"),
                label="temporary_awc_capture_previous_run_semantics",
            ),
            "truth_rows_sha256": _hex64(daily_row.get("truth_rows_sha256"), label="temporary_awc_daily_truth_rows_sha256"),
            "truth_sample_id": _text(daily_row.get("truth_sample_id"), label="temporary_awc_daily_truth_sample_id"),
        }
    )


def _residual_row(
    *,
    station_id: str,
    event_date: str,
    lead_days: int,
    season_bin: str,
    model_row: Mapping[str, Any],
    truth_id: str,
    daily_row: Mapping[str, Any],
    identity_prefix: str = "temp-awc",
) -> dict[str, Any]:
    model_id = _text(model_row.get("model_id"), label="temporary_awc_model_id")
    pair_sha256 = content_sha256(
        {
            "truth_id": truth_id,
            "model_run_id": _text(model_row.get("model_run_id"), label="temporary_awc_model_run_id"),
            "station_id": station_id,
            "model_id": model_id,
            "event_date": event_date,
            "lead_days": lead_days,
            "forecast_tmax_c": float(daily_row["forecast_tmax_c"]),
            "official_tmax_c": float(daily_row["official_tmax_c"]),
            "residual_c": float(daily_row["residual_c"]),
            "truth_rows_sha256": _hex64(daily_row.get("truth_rows_sha256"), label="temporary_awc_daily_truth_rows_sha256"),
        }
    )
    return _with_content_sha256(
        {
            "residual_id": (
                f"{identity_prefix}-residual-{station_id}-{event_date}-"
                f"{model_id}-d{lead_days}"
            ),
            "truth_id": truth_id,
            "model_run_id": _text(model_row.get("model_run_id"), label="temporary_awc_model_run_id"),
            "station_id": station_id,
            "model_id": model_id,
            "season_bin": season_bin,
            "lead_bucket": f"d{lead_days}",
            "maturity_key": _text(
                model_row.get("maturity_key"),
                label="temporary_awc_maturity_key",
            ),
            "run_cycle": f"PREVIOUS_DAY{lead_days}",
            "e_y_minus_f_raw_c": float(daily_row["residual_c"]),
            "paired_asof_utc": _text(model_row.get("run_init_utc"), label="temporary_awc_run_init_utc"),
            "qc_status": "ACCEPTED",
            "historical_replay_eligible": True,
            "prospective_strict_asof_eligible": False,
            "pair_sha256": pair_sha256,
            "truth_rows_sha256": _hex64(daily_row.get("truth_rows_sha256"), label="temporary_awc_daily_truth_rows_sha256"),
            "truth_sample_id": _text(daily_row.get("truth_sample_id"), label="temporary_awc_daily_truth_sample_id"),
        }
    )


def _load_truth_dataset(
    *,
    station_id: str,
    timezone_name: str,
    dataset_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _mapping(load_bounded_json(dataset_path.expanduser().resolve()), label="temporary_awc_truth_dataset")
    if _text(payload.get("schema"), label="temporary_awc_truth_schema") != "bayes_basket_station_calibration_dataset_v1":
        raise ContractError("temporary_awc_truth_dataset_schema_invalid")
    checks = _mapping(payload.get("checks"), label="temporary_awc_truth_checks")
    if checks.get("closed_days_only") is not True:
        raise ContractError("temporary_awc_truth_closed_days_only_required")
    if checks.get("temporary_awc_calibration_label") is not True:
        raise ContractError("temporary_awc_truth_label_required")
    if _text(payload.get("label_authority"), label="temporary_awc_label_authority") != TEMPORARY_AWC_LABEL:
        raise ContractError("temporary_awc_truth_label_authority_invalid")
    boundary = _mapping(payload.get("boundary"), label="temporary_awc_truth_boundary")
    if any(
        boundary.get(key) is not False
        for key in (
            "calls_order_gateway",
            "places_orders",
            "writes_production_database",
            "enters_realtime_truth",
            "enters_p1",
            "enters_hard_no",
            "live_submit_authorized",
        )
    ):
        raise ContractError("temporary_awc_truth_boundary_invalid")
    if _text(payload.get("station_id"), label="temporary_awc_truth_station").upper() != station_id:
        raise ContractError("temporary_awc_truth_station_scope_invalid")
    dataset_file_sha256 = _sha256_path(dataset_path)
    dataset_content_sha256 = _hex64(payload.get("content_sha256"), label="temporary_awc_truth_content_sha256")
    received_at_utc = assert_timestamp_not_future(
        _text(payload.get("as_of_utc"), label="temporary_awc_truth_as_of_utc"),
        name="temporary_awc_truth_as_of_utc",
    )
    samples = _rows(payload.get("samples"), label="temporary_awc_truth_samples")
    truth_rows: list[dict[str, Any]] = []
    truth_index: dict[str, dict[str, Any]] = {}
    for sample in samples:
        row, lineage = _truth_row(
            station_id=station_id,
            timezone_name=timezone_name,
            sample=sample,
            dataset_file_sha256=dataset_file_sha256,
            dataset_content_sha256=dataset_content_sha256,
            dataset_path=dataset_path,
            received_at_utc=received_at_utc,
        )
        key = _text(sample.get("event_date"), label="temporary_awc_event_date")
        if key in truth_index:
            raise ContractError("temporary_awc_truth_event_date_duplicate")
        truth_rows.append(row)
        truth_index[key] = {
            "truth_id": row["truth_id"],
            "truth_sample_id": lineage["truth_sample_id"],
            "truth_rows_sha256": lineage["truth_rows_sha256"],
            "observation_raw_sha256s": lineage["observation_raw_sha256s"],
        }
    return truth_rows, truth_index, {
        "path": str(dataset_path),
        "sha256": dataset_file_sha256,
        "content_sha256": dataset_content_sha256,
        "sample_count": len(samples),
        "station_id": station_id,
    }


def _load_oos_rows(
    *,
    station_id: str,
    timezone_name: str,
    oos_path: Path,
    truth_index: Mapping[str, dict[str, Any]],
    identity_prefix: str = "temp-awc",
    maturity_prefix: str = "TEMPORARY_AWC_PREVIOUS_RUN",
    capture_mode: str = "temporary_awc_calibration_base",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], Counter[tuple[str, str, str, str]]]:
    payload = _mapping(load_bounded_json(oos_path.expanduser().resolve()), label="temporary_awc_oos")
    if _text(payload.get("schema"), label="temporary_awc_oos_schema") != "rkpk_open_meteo_five_model_historical_oos_v1":
        raise ContractError("temporary_awc_oos_schema_invalid")
    boundary = _mapping(payload.get("boundary"), label="temporary_awc_oos_boundary")
    if any(
        boundary.get(key) is not False
        for key in (
            "calls_order_gateway",
            "places_orders",
            "writes_production_database",
            "writes_target_database",
            "live_submit_authorized",
        )
    ):
        raise ContractError("temporary_awc_oos_boundary_invalid")
    authority = _mapping(payload.get("authority"), label="temporary_awc_oos_authority")
    if authority.get("research_oos_complete") is not True:
        raise ContractError("temporary_awc_oos_incomplete")
    if _text(payload.get("station_id"), label="temporary_awc_oos_station").upper() != station_id:
        raise ContractError("temporary_awc_oos_station_scope_invalid")
    lead_days = int(payload.get("lead_days") or 0)
    if lead_days not in {1, 2}:
        raise ContractError("temporary_awc_oos_lead_days_invalid")
    oos_fetched_at_utc = assert_timestamp_not_future(
        _text(payload.get("fetched_at_utc"), label="temporary_awc_oos_fetched_at_utc"),
        name="temporary_awc_oos_fetched_at_utc",
    )
    season_bin = _text(payload.get("season_bin"), label="temporary_awc_oos_season_bin")
    captures = _rows(payload.get("captures"), label="temporary_awc_oos_captures")
    oos_file_sha256 = _sha256_path(oos_path)
    model_rows: list[dict[str, Any]] = []
    residual_rows: list[dict[str, Any]] = []
    scope_models: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    model_run_ids: set[str] = set()
    residual_ids: set[str] = set()
    for capture_index, capture in enumerate(captures):
        capture = dict(capture)
        capture["_capture_index"] = capture_index
        daily_rows = _rows(capture.get("daily_rows"), label="temporary_awc_capture_daily_rows")
        for daily_index, daily_row in enumerate(daily_rows):
            daily_row = dict(daily_row)
            daily_row["_daily_index"] = daily_index
            event_date = _text(daily_row.get("event_date"), label="temporary_awc_daily_event_date")
            lineage = truth_index.get(event_date)
            if lineage is None:
                raise ContractError("temporary_awc_oos_truth_lineage_missing")
            if _text(daily_row.get("truth_sample_id"), label="temporary_awc_daily_truth_sample_id") != lineage["truth_sample_id"]:
                raise ContractError("temporary_awc_oos_truth_sample_id_mismatch")
            if _hex64(daily_row.get("truth_rows_sha256"), label="temporary_awc_daily_truth_rows_sha256") != lineage["truth_rows_sha256"]:
                raise ContractError("temporary_awc_oos_truth_rows_sha256_mismatch")
            model_row = _model_row(
                station_id=station_id,
                timezone_name=timezone_name,
                event_date=event_date,
                lead_days=lead_days,
                oos_path=oos_path,
                oos_file_sha256=oos_file_sha256,
                oos_fetched_at_utc=oos_fetched_at_utc,
                capture=capture,
                daily_row=daily_row,
                identity_prefix=identity_prefix,
                maturity_prefix=maturity_prefix,
                capture_mode=capture_mode,
            )
            residual_row = _residual_row(
                station_id=station_id,
                event_date=event_date,
                lead_days=lead_days,
                season_bin=season_bin,
                model_row=model_row,
                truth_id=lineage["truth_id"],
                daily_row=daily_row,
                identity_prefix=identity_prefix,
            )
            if model_row["model_run_id"] in model_run_ids:
                raise ContractError("temporary_awc_model_run_duplicate")
            if residual_row["residual_id"] in residual_ids:
                raise ContractError("temporary_awc_residual_duplicate")
            model_run_ids.add(model_row["model_run_id"])
            residual_ids.add(residual_row["residual_id"])
            model_rows.append(model_row)
            residual_rows.append(residual_row)
            scope_models[(
                station_id,
                event_date,
                model_row["lead_bucket"],
                model_row["maturity_key"],
            )].add(model_row["model_id"])
    for scope, models_in_scope in scope_models.items():
        if len(models_in_scope) < 2:
            raise ContractError(
                "temporary_awc_multi_model_scope_required:" + "|".join(scope)
            )
    return model_rows, residual_rows, {
        "path": str(oos_path),
        "sha256": oos_file_sha256,
        "content_sha256": _hex64(payload.get("content_sha256"), label="temporary_awc_oos_content_sha256"),
        "lead_days": lead_days,
        "station_id": station_id,
        "capture_count": len(captures),
    }, Counter({scope: len(models_in_scope) for scope, models_in_scope in scope_models.items()})


def build_temporary_awc_calibration_base_packet(
    *,
    audit_root: Path = DEFAULT_AUDIT_ROOT,
    station_lane_map_path: Path = DEFAULT_STATION_LANE_MAP,
    market_registry_path: Path = DEFAULT_MARKET_REGISTRY,
    source_name: str = DEFAULT_SOURCE_NAME,
) -> dict[str, Any]:
    station_ids = _station_scope(station_lane_map_path)
    expected_station_ids = {
        "RCSS", "ZBAA", "ZGGG", "ZGSZ", "ZHHH",
        "ZSPD", "ZSQD", "ZUCK", "ZUUU",
    }
    if set(station_ids) != expected_station_ids:
        raise ContractError("temporary_awc_station_scope_must_be_canonical_nine")
    input_paths = [
        station_lane_map_path,
        market_registry_path,
    ]
    for station_id in station_ids:
        station_dir = audit_root.expanduser().resolve() / station_id
        input_paths.extend((
            station_dir / "calibration_truth_dataset.json",
            station_dir / "five_model_d1_oos.json",
            station_dir / "five_model_d2_oos.json",
        ))
    _assert_input_bundle_bounded(input_paths)
    station_meta = _station_metadata(market_registry_path, station_ids)
    station_registry = [_station_row(station_meta[station_id]) for station_id in station_ids]

    truth_rows: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []
    residual_rows: list[dict[str, Any]] = []
    input_artifacts: dict[str, Any] = {
        "station_lane_map": {
            "path": str(station_lane_map_path.expanduser().resolve()),
            "sha256": _sha256_path(station_lane_map_path.expanduser().resolve()),
        },
        "market_registry_snapshot": {
            "path": str(market_registry_path.expanduser().resolve()),
            "sha256": _sha256_path(market_registry_path.expanduser().resolve()),
        },
    }
    all_scope_counts: Counter[tuple[str, str, str, str]] = Counter()
    truth_index_by_station: dict[str, dict[str, dict[str, Any]]] = {}
    latest_input_as_of_utc = "1970-01-01T00:00:00Z"
    for station_id in station_ids:
        station_dir = audit_root.expanduser().resolve() / station_id
        truth_path = station_dir / "calibration_truth_dataset.json"
        d1_path = station_dir / "five_model_d1_oos.json"
        d2_path = station_dir / "five_model_d2_oos.json"
        for candidate in (truth_path, d1_path, d2_path):
            if not candidate.is_file():
                raise FileNotFoundError(candidate)
        station_truth_rows, truth_index, truth_artifact = _load_truth_dataset(
            station_id=station_id,
            timezone_name=station_meta[station_id].timezone_name,
            dataset_path=truth_path,
        )
        truth_rows.extend(station_truth_rows)
        truth_index_by_station[station_id] = truth_index
        latest_input_as_of_utc = max(latest_input_as_of_utc, station_truth_rows[0]["received_at_utc"])
        input_artifacts[f"{station_id}_truth_dataset"] = truth_artifact
        for oos_path in (d1_path, d2_path):
            station_model_rows, station_residual_rows, oos_artifact, scope_counts = _load_oos_rows(
                station_id=station_id,
                timezone_name=station_meta[station_id].timezone_name,
                oos_path=oos_path,
                truth_index=truth_index,
            )
            model_rows.extend(station_model_rows)
            residual_rows.extend(station_residual_rows)
            all_scope_counts.update(scope_counts)
            latest_input_as_of_utc = max(
                latest_input_as_of_utc,
                station_model_rows[0]["received_at_utc"],
            )
            input_artifacts[
                f"{station_id}_oos_d{oos_artifact['lead_days']}"
            ] = oos_artifact
    if not truth_rows or not model_rows or not residual_rows:
        raise ContractError("temporary_awc_packet_incomplete")
    if len(all_scope_counts) != len(truth_rows) * 2:
        raise ContractError("temporary_awc_d1_d2_scope_count_mismatch")
    if len(model_rows) != len(all_scope_counts) * len(MODEL_IDS):
        raise ContractError("temporary_awc_five_model_row_count_mismatch")
    if len(residual_rows) != len(model_rows):
        raise ContractError("temporary_awc_residual_row_count_mismatch")
    if set(all_scope_counts.values()) != {len(MODEL_IDS)}:
        raise ContractError("temporary_awc_five_model_scope_incomplete")
    if any(
        row["provider_id"] in HISTORICAL_REPLAY_ONLY_PROVIDERS
        and (
            row["asof_status"] != "HISTORICAL_REPLAY_ONLY"
            or row["prospective_strict_asof_eligible"] is not False
            or row["timestamps_complete"] is not False
        )
        for row in model_rows
    ):
        raise ContractError("temporary_awc_historical_transport_scope_invalid")
    whole_hour_truth_observation_count = sum(
        len(row.get("observation_raw_sha256s") or [])
        for row in truth_rows
    )
    packet = {
        "schema": SCHEMA,
        "as_of_utc": latest_input_as_of_utc,
        "source_name": str(source_name).strip() or DEFAULT_SOURCE_NAME,
        "station_registry": station_registry,
        "truth_evidence": truth_rows,
        "calibration_model_rows": model_rows,
        "residual_evidence": residual_rows,
        "temporal_boundary": {
            "closed_settlement_event_days_only": True,
            "source_truth_observations_whole_hour_only": True,
        },
        "checks": {
            "truth_source_canonical_ingress": False,
            "temporary_awc_calibration_label_explicit": True,
            "truth_observations_whole_hour_only": True,
            "closed_settlement_event_days_only": True,
            "station_registry_canonical_lane_amsc": True,
            "authority_scope_only_no_live_replay_candidate": True,
            "minimum_two_models_per_scope": True,
            "model_capture_lineage_bound": True,
            "truth_dataset_sha_bound": True,
            "truth_observation_raw_sha_bound": True,
        },
        "boundary": {
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
            "writes_production_database": False,
            "writes_target_database": False,
            "reads_probability_state": False,
            "reads_market_state": False,
        },
        "scope": {
            "mode": "allowlist",
            "included_station_ids": station_ids,
            "canonical_station_registry_lane": "AMSC",
            "temporary_truth_source_lane": "AWC",
            "authority_scope_intent": NO_LIVE_REPLAY,
        },
        "input_artifacts": input_artifacts,
        "stats": {
            "station_count": len(station_registry),
            "truth_row_count": len(truth_rows),
            "model_row_count": len(model_rows),
            "residual_row_count": len(residual_rows),
            "whole_hour_truth_observation_count": whole_hour_truth_observation_count,
            "scope_count": len(all_scope_counts),
            "min_models_per_scope": min(all_scope_counts.values()),
            "max_models_per_scope": max(all_scope_counts.values()),
            "provider_ids": sorted({str(row["provider_id"]) for row in model_rows}),
        },
    }
    packet["validation_preview"] = preview_ingress_validation(packet)
    packet["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in packet.items()
            if key not in {"content_sha256", "packet_id"}
        }
    )
    packet["packet_id"] = f"temp-awc-calibration-base-{packet['content_sha256'][:16]}"
    return packet


def preview_ingress_validation(packet: Mapping[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        try:
            result = ingest_calibration_base_facts(conn, packet, commit=False)
        except Exception as exc:  # pragma: no cover - exercised by tests via exact blocker
            blocker = str(exc) or exc.__class__.__name__
            return {
                "accepted": False,
                "state": "BLOCKED",
                "blocker": blocker,
                "exception_type": exc.__class__.__name__,
                "authority_scope_intent": NO_LIVE_REPLAY,
            }
        return {
            "accepted": True,
            "state": result.get("state"),
            "validated_counts": result.get("validated_counts"),
            "temporary_awc_truth_rows": result.get("temporary_awc_truth_rows"),
            "no_live_replay_model_rows": result.get("no_live_replay_model_rows"),
            "authority_scope_intent": NO_LIVE_REPLAY,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, default=DEFAULT_AUDIT_ROOT)
    parser.add_argument("--station-lane-map", type=Path, default=DEFAULT_STATION_LANE_MAP)
    parser.add_argument("--market-registry", type=Path, default=DEFAULT_MARKET_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-name", default=DEFAULT_SOURCE_NAME)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    packet = build_temporary_awc_calibration_base_packet(
        audit_root=args.audit_root,
        station_lane_map_path=args.station_lane_map,
        market_registry_path=args.market_registry,
        source_name=args.source_name,
    )
    output_path = args.output.expanduser().resolve()
    write_bounded_json(output_path, packet)
    print(
        json.dumps(
            {
                "schema": packet["schema"],
                "output": str(output_path),
                "packet_id": packet["packet_id"],
                "content_sha256": packet["content_sha256"],
                "station_count": packet["stats"]["station_count"],
                "truth_row_count": packet["stats"]["truth_row_count"],
                "model_row_count": packet["stats"]["model_row_count"],
                "residual_row_count": packet["stats"]["residual_row_count"],
                "validation_state": packet["validation_preview"]["state"],
                "validation_blocker": packet["validation_preview"].get("blocker"),
                "authority_scope_intent": NO_LIVE_REPLAY,
                "live_order_authorized": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
