#!/usr/bin/env python3
"""Build one incremental 45-station, 29-day calibration base packet.

The builder reads the frozen truth/OOS artifacts and the current target SSOT
in read-only mode.  Existing identical truth/model facts are bound through
content-addressed references; changed truth values become a new revision.
Nothing is written to SQLite, production state, live state, or a gateway.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.calibration_ingress import (  # noqa: E402
    CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1,
    HISTORICAL_AWC_REPLAY,
    NO_LIVE_REPLAY,
)
from bayes_basket_target.contracts import (  # noqa: E402
    MODEL_IDS,
    ContractError,
    assert_timestamp_not_future,
    content_sha256,
)
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.model_sources import (  # noqa: E402
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
from zoneinfo import ZoneInfo  # noqa: E402


FORMAL_TRUTH = "FORMAL_STATION_LANE_TRUTH"
PACKET_SCHEMA = "bayes_basket_target_calibration_evidence_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
DEFAULT_TRUTH_ROOT = (
    ROOT
    / "output/audits"
    / "bayes_basket_target_45station_truth_29d_direct_20260730_v1"
)
DEFAULT_OOS_ROOT = (
    ROOT
    / "output/audits"
    / "bayes_basket_target_45station_five_model_29d_direct_20260730_v1"
)
DEFAULT_OUTPUT = (
    ROOT
    / "output/audits"
    / "bayes_basket_target_45station_calibration_base_29d_v1"
)
MAX_INPUT_FILES = 160
MAX_INPUT_BYTES = 256 * MIB
MAX_OUTPUT_BYTES = 256 * MIB
EXPECTED_STATION_COUNT = expected_station_count()
EXPECTED_DAY_COUNT = 29


def _assert_input_bundle(paths: Iterable[Path]) -> None:
    resolved = [Path(path).expanduser().resolve() for path in paths]
    if len(resolved) > MAX_INPUT_FILES:
        raise ResourceBoundError("calibration_45station_input_count_exceeded")
    total = 0
    for path in resolved:
        if not path.is_file() or path.is_symlink():
            raise ResourceBoundError("calibration_45station_input_invalid")
        total += int(path.stat().st_size)
        if total > MAX_INPUT_BYTES:
            raise ResourceBoundError("calibration_45station_input_bytes_exceeded")
    assert_process_memory_budget(
        projected_bytes=min(total * 3, 256 * MIB)
    )


def _utc(value: Any, *, label: str) -> str:
    raw = _text(value, label=label)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label}_mapping_required")
    return dict(value)


def _rows(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractError(f"{label}_list_required")
    if any(not isinstance(item, Mapping) for item in value):
        raise ContractError(f"{label}_row_invalid")
    return [dict(item) for item in value]


def _text(value: Any, *, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{label}_missing")
    return result


def _hex64(value: Any, *, label: str) -> str:
    result = _text(value, label=label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{label}_invalid")
    return result


def _with_content_sha256(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["content_sha256"] = content_sha256(payload)
    return payload


def _derive_run_init_utc(event_date: str, lead_days: int) -> str:
    base = date.fromisoformat(event_date) - timedelta(days=lead_days)
    return datetime(
        base.year, base.month, base.day, tzinfo=timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    identity_prefix: str,
    maturity_prefix: str,
    capture_mode: str,
) -> dict[str, Any]:
    model_id = _text(
        capture.get("model_id"), label="calibration_oos_capture_model_id"
    ).upper()
    provider_id = _text(
        capture.get("provider_id"), label="calibration_oos_capture_provider_id"
    ).lower()
    source_class = _text(
        capture.get("source_class"),
        label="calibration_oos_capture_source_class",
    ).lower()
    if model_id_for_provider(provider_id) != model_id:
        raise ContractError("calibration_oos_capture_model_provider_mismatch")
    if source_class != provider_source_class(provider_id):
        raise ContractError("calibration_oos_capture_source_class_mismatch")
    if bool(capture.get("formal_lineage_eligible")):
        raise ContractError("calibration_oos_capture_formal_lineage_forbidden")
    response_sha256 = _hex64(
        capture.get("response_sha256"),
        label="calibration_oos_capture_response_sha256",
    )
    capture_index = int(capture.get("_capture_index", 0))
    return _with_content_sha256({
        "model_run_id": (
            f"{identity_prefix}-run-{station_id}-{event_date}-"
            f"{model_id}-d{lead_days}"
        ),
        "model_id": model_id,
        "station_id": station_id,
        "target_local_date": event_date,
        "target_timezone": timezone_name,
        "run_init_utc": _derive_run_init_utc(event_date, lead_days),
        "published_at_utc": None,
        "received_at_utc": oos_fetched_at_utc,
        "timestamps_complete": False,
        "lead_bucket": f"d{lead_days}",
        "maturity_key": f"{maturity_prefix}_D{lead_days}",
        "run_cycle": f"PREVIOUS_DAY{lead_days}",
        "raw_tmax_c": float(daily_row["forecast_tmax_c"]),
        "settlement_definition": SETTLEMENT_DEFINITION,
        "provider_id": provider_id,
        "provider_role": provider_role(provider_id),
        "source_class": source_class,
        "aggregator_or_republisher": source_class == "aggregator_republisher",
        "open_meteo": "open_meteo" in provider_id,
        "source_url": _text(
            capture.get("source_url"), label="calibration_oos_capture_source_url"
        ),
        "raw_object_ref": (
            f"{oos_path}#captures/{capture_index}/daily_rows/"
            f"{daily_row.get('_daily_index', 0)}"
        ),
        "source_object_sha256": response_sha256,
        "asof_status": "HISTORICAL_REPLAY_ONLY",
        "capture_modes": ["historical_replay", capture_mode],
        "historical_replay_eligible": True,
        "prospective_strict_asof_eligible": False,
        "input_oos_file_sha256": oos_file_sha256,
        "capture_response_sha256": response_sha256,
        "capture_api_model": _text(
            capture.get("api_model"), label="calibration_oos_capture_api_model"
        ),
        "capture_formal_lineage_eligible": bool(
            capture.get("formal_lineage_eligible")
        ),
        "capture_previous_run_semantics": _text(
            capture.get("previous_run_semantics"),
            label="calibration_oos_capture_previous_run_semantics",
        ),
        "truth_rows_sha256": _hex64(
            daily_row.get("truth_rows_sha256"),
            label="calibration_oos_daily_truth_rows_sha256",
        ),
        "truth_sample_id": _text(
            daily_row.get("truth_sample_id"),
            label="calibration_oos_daily_truth_sample_id",
        ),
    })


def _residual_row(
    *,
    station_id: str,
    event_date: str,
    lead_days: int,
    season_bin: str,
    model_row: Mapping[str, Any],
    truth_id: str,
    daily_row: Mapping[str, Any],
    identity_prefix: str,
) -> dict[str, Any]:
    model_id = _text(model_row.get("model_id"), label="calibration_oos_model_id")
    model_run_id = _text(
        model_row.get("model_run_id"), label="calibration_oos_model_run_id"
    )
    truth_rows_sha256 = _hex64(
        daily_row.get("truth_rows_sha256"),
        label="calibration_oos_daily_truth_rows_sha256",
    )
    pair_sha256 = content_sha256({
        "truth_id": truth_id,
        "model_run_id": model_run_id,
        "station_id": station_id,
        "model_id": model_id,
        "event_date": event_date,
        "lead_days": lead_days,
        "forecast_tmax_c": float(daily_row["forecast_tmax_c"]),
        "official_tmax_c": float(daily_row["official_tmax_c"]),
        "residual_c": float(daily_row["residual_c"]),
        "truth_rows_sha256": truth_rows_sha256,
    })
    return _with_content_sha256({
        "residual_id": (
            f"{identity_prefix}-residual-{station_id}-{event_date}-"
            f"{model_id}-d{lead_days}"
        ),
        "truth_id": truth_id,
        "model_run_id": model_run_id,
        "station_id": station_id,
        "model_id": model_id,
        "season_bin": season_bin,
        "lead_bucket": f"d{lead_days}",
        "maturity_key": _text(
            model_row.get("maturity_key"), label="calibration_oos_maturity_key"
        ),
        "run_cycle": f"PREVIOUS_DAY{lead_days}",
        "e_y_minus_f_raw_c": float(daily_row["residual_c"]),
        "paired_asof_utc": _text(
            model_row.get("run_init_utc"), label="calibration_oos_run_init_utc"
        ),
        "qc_status": "ACCEPTED",
        "historical_replay_eligible": True,
        "prospective_strict_asof_eligible": False,
        "pair_sha256": pair_sha256,
        "truth_rows_sha256": truth_rows_sha256,
        "truth_sample_id": _text(
            daily_row.get("truth_sample_id"),
            label="calibration_oos_daily_truth_sample_id",
        ),
    })


def _load_oos_rows(
    *,
    station_id: str,
    timezone_name: str,
    oos_path: Path,
    truth_index: Mapping[str, dict[str, Any]],
    identity_prefix: str,
    maturity_prefix: str,
    capture_mode: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    Counter[tuple[str, str, str, str]],
]:
    payload = _mapping(
        load_bounded_json(oos_path.expanduser().resolve()),
        label="calibration_oos",
    )
    if _text(payload.get("schema"), label="calibration_oos_schema") != (
        "rkpk_open_meteo_five_model_historical_oos_v1"
    ):
        raise ContractError("calibration_oos_schema_invalid")
    boundary = _mapping(
        payload.get("boundary"), label="calibration_oos_boundary"
    )
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
        raise ContractError("calibration_oos_boundary_invalid")
    authority = _mapping(
        payload.get("authority"), label="calibration_oos_authority"
    )
    if authority.get("research_oos_complete") is not True:
        raise ContractError("calibration_oos_incomplete")
    if _text(payload.get("station_id"), label="calibration_oos_station").upper() != station_id:
        raise ContractError("calibration_oos_station_scope_invalid")
    lead_days = int(payload.get("lead_days") or 0)
    if lead_days not in {1, 2}:
        raise ContractError("calibration_oos_lead_days_invalid")
    oos_fetched_at_utc = assert_timestamp_not_future(
        _text(payload.get("fetched_at_utc"), label="calibration_oos_fetched_at_utc"),
        name="calibration_oos_fetched_at_utc",
    )
    season_bin = _text(payload.get("season_bin"), label="calibration_oos_season_bin")
    captures = _rows(payload.get("captures"), label="calibration_oos_captures")
    oos_file_sha256 = _sha256_path(oos_path)
    model_rows: list[dict[str, Any]] = []
    residual_rows: list[dict[str, Any]] = []
    scope_models: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    model_run_ids: set[str] = set()
    residual_ids: set[str] = set()
    for capture_index, capture in enumerate(captures):
        capture = {**capture, "_capture_index": capture_index}
        daily_rows = _rows(
            capture.get("daily_rows"), label="calibration_oos_capture_daily_rows"
        )
        for daily_index, daily_row in enumerate(daily_rows):
            daily_row = {**daily_row, "_daily_index": daily_index}
            event_date = _text(
                daily_row.get("event_date"), label="calibration_oos_daily_event_date"
            )
            lineage = truth_index.get(event_date)
            if lineage is None:
                raise ContractError("calibration_oos_truth_lineage_missing")
            if _text(
                daily_row.get("truth_sample_id"),
                label="calibration_oos_daily_truth_sample_id",
            ) != lineage["truth_sample_id"]:
                raise ContractError("calibration_oos_truth_sample_id_mismatch")
            if _hex64(
                daily_row.get("truth_rows_sha256"),
                label="calibration_oos_daily_truth_rows_sha256",
            ) != lineage["truth_rows_sha256"]:
                raise ContractError("calibration_oos_truth_rows_sha256_mismatch")
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
                raise ContractError("calibration_oos_model_run_duplicate")
            if residual_row["residual_id"] in residual_ids:
                raise ContractError("calibration_oos_residual_duplicate")
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
                "calibration_oos_multi_model_scope_required:" + "|".join(scope)
            )
    return model_rows, residual_rows, {
        "path": str(oos_path),
        "sha256": oos_file_sha256,
        "content_sha256": _hex64(
            payload.get("content_sha256"), label="calibration_oos_content_sha256"
        ),
        "lead_days": lead_days,
        "station_id": station_id,
        "capture_count": len(captures),
    }, Counter({
        scope: len(models_in_scope)
        for scope, models_in_scope in scope_models.items()
    })


def _reference(row: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    result = {key: row[key] for key in keys}
    result["reference_sha256"] = content_sha256(result)
    return result


def _station_rows(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    cursor = conn.execute(
        "SELECT station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256 "
        "FROM calibration_station_registry ORDER BY station_id"
    )
    names = tuple(item[0] for item in cursor.description or ())
    rows = [dict(zip(names, tuple(row))) for row in cursor.fetchall()]
    if len(rows) != EXPECTED_STATION_COUNT:
        raise ContractError("calibration_45station_registry_scope_invalid")
    for row in rows:
        supplied = str(row["content_sha256"])
        if supplied != content_sha256({
            key: value
            for key, value in row.items()
            if key != "content_sha256"
        }):
            raise ContractError("calibration_station_registry_hash_invalid")
        if (
            str(row["source_lane"]).upper() != "AWC"
            or str(row["source_role"])
            != CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1
            or str(row["settlement_definition"]) != SETTLEMENT_DEFINITION
        ):
            raise ContractError("calibration_station_registry_awc_authority_invalid")
    return rows, {
        str(row["station_id"]): str(row["timezone_name"])
        for row in rows
    }


def _existing_truth(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, Any]]:
    cursor = conn.execute(
        "SELECT truth_id,station_id,local_date,official_tmax_c,source_lane,"
        "settlement_definition,revision_no,qc_status,source_role,"
        "authority_scope,content_sha256 "
        "FROM calibration_station_daily_high_truth "
        "ORDER BY station_id,local_date,revision_no"
    )
    names = tuple(item[0] for item in cursor.description or ())
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for value in cursor.fetchall():
        row = dict(zip(names, tuple(value)))
        latest[(str(row["station_id"]), str(row["local_date"]))] = row
    return latest


def _existing_models(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    cursor = conn.execute(
        "SELECT * FROM calibration_historical_asof_model_run"
    )
    names = tuple(item[0] for item in cursor.description or ())
    result: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for value in cursor.fetchall():
        row = dict(zip(names, tuple(value)))
        key = (
            str(row["model_id"]),
            str(row["station_id"]),
            str(row["target_local_date"]),
            str(row["run_init_utc"]),
            str(row["run_cycle"]),
        )
        if key in result:
            raise ContractError("calibration_existing_model_natural_key_duplicate")
        result[key] = row
    return result


def _existing_residuals(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, Any]]:
    cursor = conn.execute(
        "SELECT residual_id,truth_id,model_run_id,e_y_minus_f_raw_c,"
        "content_sha256 FROM calibration_model_station_residual"
    )
    names = tuple(item[0] for item in cursor.description or ())
    return {
        (str(row["truth_id"]), str(row["model_run_id"])): row
        for row in (
            dict(zip(names, tuple(value))) for value in cursor.fetchall()
        )
    }


def _truth_sample(
    *,
    station_id: str,
    timezone_name: str,
    sample: Mapping[str, Any],
    label_authority: str,
    dataset_path: Path,
    dataset_sha256: str,
    dataset_content_sha256: str,
    received_at_utc: str,
    existing: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    event_date = _text(sample.get("event_date"), label="truth_event_date")
    truth = _mapping(sample.get("truth"), label="truth_sample")
    sample_id = _text(sample.get("sample_id"), label="truth_sample_id")
    if sample_id != "truth-" + content_sha256({
        key: value for key, value in sample.items() if key != "sample_id"
    })[:32]:
        raise ContractError("calibration_truth_sample_id_invalid")
    source_lane = _text(truth.get("source_lane"), label="truth_source_lane").upper()
    if label_authority != FORMAL_TRUTH:
        raise ContractError("calibration_truth_label_authority_invalid")
    if source_lane != "AWC":
        raise ContractError("calibration_truth_source_lane_invalid")
    if (
        sample.get("settlement_timezone") != timezone_name
        or truth.get("source_role") != HISTORICAL_AWC_REPLAY
        or truth.get("authority_scope") != NO_LIVE_REPLAY
    ):
        raise ContractError("calibration_truth_lineage_invalid")
    if _text(sample.get("station_id"), label="truth_station").upper() != station_id:
        raise ContractError("calibration_truth_station_mismatch")
    try:
        event_day = date.fromisoformat(event_date)
        zone = ZoneInfo(timezone_name)
    except (ValueError, KeyError) as exc:
        raise ContractError("calibration_truth_settlement_window_invalid") from exc
    expected_start = datetime.combine(
        event_day, datetime.min.time(), tzinfo=zone
    ).astimezone(timezone.utc)
    expected_end = datetime.combine(
        event_day + timedelta(days=1), datetime.min.time(), tzinfo=zone
    ).astimezone(timezone.utc)
    received_at = datetime.fromisoformat(received_at_utc.replace("Z", "+00:00"))
    if expected_end > received_at:
        raise ContractError("calibration_truth_not_closed_as_of")
    window = _mapping(
        sample.get("settlement_window"), label="truth_settlement_window"
    )
    if (
        _utc(window.get("start_utc"), label="truth_window_start")
        != expected_start.isoformat(timespec="seconds").replace("+00:00", "Z")
        or _utc(window.get("end_utc"), label="truth_window_end")
        != expected_end.isoformat(timespec="seconds").replace("+00:00", "Z")
    ):
        raise ContractError("calibration_truth_settlement_window_invalid")
    observations = _rows(truth.get("observations"), label="truth_observations")
    if not observations:
        raise ContractError("calibration_truth_observations_empty")
    normalized = []
    observation_ids = []
    raw_sha256s = []
    seen_observations: set[tuple[str, str, str]] = set()
    for index, item in enumerate(observations):
        raw_sha = _hex64(item.get("raw_sha256"), label="truth_raw_sha256")
        valid_at = _utc(item.get("valid_at_utc"), label="truth_valid_at_utc")
        if (
            _text(item.get("station_id"), label="truth_observation_station").upper()
            != station_id
            or _text(item.get("source_lane"), label="truth_observation_lane").upper()
            != "AWC"
            or item.get("source_role") != HISTORICAL_AWC_REPLAY
            or item.get("authority_scope") != NO_LIVE_REPLAY
            or item.get("settlement_timezone") != timezone_name
            or item.get("evidence_role") != "FORMAL"
            or item.get("reference_only") is not False
        ):
            raise ContractError("calibration_truth_observation_scope_mismatch")
        if item.get("qc_status") != "ACCEPTED":
            raise ContractError("calibration_truth_observation_qc_invalid")
        report_type = _text(
            item.get("report_type"), label="truth_report_type"
        ).upper()
        if report_type not in {"METAR", "SPECI"}:
            raise ContractError("calibration_truth_observation_report_type_invalid")
        observed_at = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
        if report_type == "METAR" and (
            observed_at.minute != 0 or observed_at.second != 0
        ):
            raise ContractError("calibration_truth_observation_metar_not_hourly")
        if not expected_start <= observed_at < expected_end:
            raise ContractError("calibration_truth_observation_window_mismatch")
        identity_key = (report_type, valid_at, raw_sha)
        if identity_key in seen_observations:
            raise ContractError("calibration_truth_observation_duplicate")
        seen_observations.add(identity_key)
        try:
            temperature = float(item["reported_temperature_c"])
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "calibration_truth_observation_temperature_invalid"
            ) from exc
        if not math.isfinite(temperature):
            raise ContractError("calibration_truth_observation_temperature_invalid")
        normalized.append({
            "raw_sha256": raw_sha,
            "report_type": report_type,
            "reported_temperature_c": temperature,
            "source_lane": source_lane,
            "source_role": HISTORICAL_AWC_REPLAY,
            "authority_scope": NO_LIVE_REPLAY,
            "settlement_timezone": timezone_name,
            "evidence_role": "FORMAL",
            "reference_only": False,
            "station_id": station_id,
            "valid_at_utc": valid_at,
        })
        raw_sha256s.append(raw_sha)
        observation_ids.append(
            f"rebuild29-{station_id}-{event_date}-{index:02d}-{raw_sha[:16]}"
        )
    truth_rows_sha256 = content_sha256(normalized)
    official_tmax_c = float(truth["tmax_c"])
    if not math.isfinite(official_tmax_c):
        raise ContractError("calibration_truth_temperature_invalid")
    if official_tmax_c != max(
        float(row["reported_temperature_c"]) for row in normalized
    ):
        raise ContractError("calibration_truth_tmax_observation_mismatch")
    source_role = HISTORICAL_AWC_REPLAY

    if (
        existing is not None
        and float(existing["official_tmax_c"]) == official_tmax_c
        and str(existing["source_lane"]) == source_lane
        and str(existing["settlement_definition"]) == SETTLEMENT_DEFINITION
        and str(existing["qc_status"]) == "ACCEPTED"
        and str(existing["source_role"]) == source_role
    ):
        reference = _reference(existing, (
            "truth_id",
            "station_id",
            "local_date",
            "official_tmax_c",
            "source_lane",
            "settlement_definition",
            "revision_no",
            "qc_status",
            "source_role",
            "authority_scope",
            "content_sha256",
        ))
        truth_id = str(existing["truth_id"])
        row = None
    else:
        revision = int(existing["revision_no"]) + 1 if existing else 0
        seed = content_sha256({
            "station_id": station_id,
            "event_date": event_date,
            "revision_no": revision,
            "official_tmax_c": official_tmax_c,
            "source_lane": source_lane,
            "source_payload_sha256": truth_rows_sha256,
        })
        truth_id = (
            f"rebuild29-truth-{station_id}-{event_date}-"
            f"r{revision}-{seed[:12]}"
        )
        row = _with_content_sha256({
            "truth_id": truth_id,
            "station_id": station_id,
            "local_date": event_date,
            "target_timezone": timezone_name,
            "official_tmax_c": official_tmax_c,
            "source_lane": source_lane,
            "source_role": source_role,
            "settlement_definition": SETTLEMENT_DEFINITION,
            "revision_no": revision,
            "published_at_utc": None,
            "received_at_utc": received_at_utc,
            "canonical_committed_at_utc": received_at_utc,
            "timestamps_complete": False,
            "authority_scope": NO_LIVE_REPLAY,
            "qc_status": "ACCEPTED",
            "source_payload_sha256": truth_rows_sha256,
            "observation_ids": observation_ids,
            "input_dataset_path": str(dataset_path),
            "input_dataset_file_sha256": dataset_sha256,
            "input_dataset_content_sha256": dataset_content_sha256,
            "truth_sample_id": sample_id,
            "observation_raw_sha256s": raw_sha256s,
        })
        reference = None
    return row, reference, {
        "truth_id": truth_id,
        "truth_sample_id": sample_id,
        "truth_rows_sha256": truth_rows_sha256,
        "official_tmax_c": official_tmax_c,
    }


def _load_station_truth(
    *,
    path: Path,
    station_id: str,
    timezone_name: str,
    existing: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    payload = _mapping(load_bounded_json(path), label="truth_dataset")
    boundary = payload.get("boundary")
    if (
        payload.get("schema") != "bayes_basket_station_calibration_dataset_v1"
        or str(payload.get("station_id") or "").upper() != station_id
        or payload.get("settlement_timezone") != timezone_name
        or str(payload.get("source_lane") or "").upper() != "AWC"
        or payload.get("source_role") != HISTORICAL_AWC_REPLAY
        or payload.get("authority_scope") != NO_LIVE_REPLAY
        or payload.get("label_authority") != FORMAL_TRUTH
        or (payload.get("checks") or {}).get("closed_days_only") is not True
        or (payload.get("checks") or {}).get(
            "formal_awc_truth_only"
        ) is not True
        or (payload.get("checks") or {}).get(
            "awc_hourly_metar_and_speci_only"
        ) is not True
        or (payload.get("checks") or {}).get(
            "historical_awc_replay_only"
        ) is not True
        or (payload.get("checks") or {}).get(
            "input_evidence_content_hashes_verified"
        ) is not True
        or (payload.get("checks") or {}).get(
            "settlement_timezone_exact_match"
        ) is not True
        or (payload.get("checks") or {}).get(
            "reference_only_excluded"
        ) is not True
        or (payload.get("checks") or {}).get("complete_date_coverage") is not True
        or not isinstance(boundary, Mapping)
        or any(
            boundary.get(key) is not False
            for key in (
                "writes_production_database",
                "calls_order_gateway",
                "places_orders",
                "live_submit_authorized",
                "enters_realtime_truth",
                "enters_p1",
                "enters_hard_no",
            )
        )
    ):
        raise ContractError("calibration_truth_dataset_invalid")
    samples = _rows(payload.get("samples"), label="truth_samples")
    if len(samples) != EXPECTED_DAY_COUNT:
        raise ContractError("calibration_truth_day_count_invalid")
    label = _text(payload.get("label_authority"), label="truth_label_authority")
    received = assert_timestamp_not_future(
        _text(payload.get("as_of_utc"), label="truth_as_of_utc"),
        name="truth_as_of_utc",
    )
    dataset_sha = _sha256_path(path)
    dataset_content_sha = _hex64(
        payload.get("content_sha256"),
        label="truth_dataset_content_sha256",
    )
    if dataset_content_sha != content_sha256({
        key: value for key, value in payload.items() if key != "content_sha256"
    }):
        raise ContractError("calibration_truth_dataset_content_sha256_mismatch")
    evidence_shas = payload.get("input_evidence_sha256s")
    if (
        not isinstance(evidence_shas, list)
        or not evidence_shas
        or len(set(evidence_shas)) != len(evidence_shas)
    ):
        raise ContractError("calibration_truth_input_evidence_sha_invalid")
    for evidence_sha in evidence_shas:
        _hex64(
            evidence_sha,
            label="calibration_truth_input_evidence_sha",
        )
    rows = []
    references = []
    index = {}
    for sample in samples:
        event_date = _text(sample.get("event_date"), label="truth_event_date")
        row, reference, lineage = _truth_sample(
            station_id=station_id,
            timezone_name=timezone_name,
            sample=sample,
            label_authority=label,
            dataset_path=path,
            dataset_sha256=dataset_sha,
            dataset_content_sha256=dataset_content_sha,
            received_at_utc=received,
            existing=existing.get((station_id, event_date)),
        )
        if event_date in index:
            raise ContractError("calibration_truth_date_duplicate")
        index[event_date] = lineage
        if row is not None:
            rows.append(row)
        if reference is not None:
            references.append(reference)
    return rows, references, index, {
        "path": str(path),
        "sha256": dataset_sha,
        "content_sha256": dataset_content_sha,
        "station_id": station_id,
            "label_authority": label,
            "source_role": HISTORICAL_AWC_REPLAY,
            "authority_scope": NO_LIVE_REPLAY,
        "sample_count": len(samples),
        "as_of_utc": received,
    }


def _model_reference(row: Mapping[str, Any]) -> dict[str, Any]:
    return _reference(row, (
        "model_run_id",
        "model_id",
        "station_id",
        "target_local_date",
        "run_init_utc",
        "lead_bucket",
        "maturity_key",
        "run_cycle",
        "raw_tmax_c",
        "settlement_definition",
        "authority_scope",
        "content_sha256",
    ))


def build_packet(
    *,
    truth_root: Path,
    oos_root: Path,
    database: Path,
) -> dict[str, Any]:
    truth_root = truth_root.expanduser().resolve()
    oos_root = oos_root.expanduser().resolve()
    database = database.expanduser().resolve()
    truth_manifest = truth_root / "manifest.json"
    oos_manifest = oos_root / "manifest.json"
    if not database.is_file() or database.is_symlink():
        raise ResourceBoundError("calibration_45station_database_invalid")
    paths = [truth_manifest, oos_manifest]
    station_ids = sorted(
        path.name
        for path in truth_root.iterdir()
        if path.is_dir() and len(path.name) == 4
    )
    if len(station_ids) != EXPECTED_STATION_COUNT:
        raise ContractError("calibration_45station_truth_scope_invalid")
    for station_id in station_ids:
        paths.extend((
            truth_root / station_id / "calibration_truth_dataset.json",
            oos_root / station_id / "five_model_d1_oos.json",
            oos_root / station_id / "five_model_d2_oos.json",
        ))
    _assert_input_bundle(paths)

    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        station_registry, station_timezones = _station_rows(conn)
        if sorted(station_timezones) != station_ids:
            raise ContractError("calibration_45station_registry_mismatch")
        existing_truth = _existing_truth(conn)
        existing_models = _existing_models(conn)
        existing_residuals = _existing_residuals(conn)

        truth_rows: list[dict[str, Any]] = []
        truth_references: list[dict[str, Any]] = []
        model_rows: list[dict[str, Any]] = []
        model_references_by_id: dict[str, dict[str, Any]] = {}
        residual_rows: list[dict[str, Any]] = []
        existing_residual_reuse_count = 0
        scope_counts: Counter[tuple[str, str, str, str]] = Counter()
        input_artifacts: list[dict[str, Any]] = []
        as_of_values = []
        truth_index_by_station: dict[str, dict[str, dict[str, Any]]] = {}

        for station_id in station_ids:
            truth_path = (
                truth_root / station_id / "calibration_truth_dataset.json"
            )
            rows, references, truth_index, truth_artifact = _load_station_truth(
                path=truth_path,
                station_id=station_id,
                timezone_name=station_timezones[station_id],
                existing=existing_truth,
            )
            truth_rows.extend(rows)
            truth_references.extend(references)
            truth_index_by_station[station_id] = truth_index
            input_artifacts.append(truth_artifact)
            as_of_values.append(str(truth_artifact["as_of_utc"]))

            for lead in (1, 2):
                oos_path = (
                    oos_root
                    / station_id
                    / f"five_model_d{lead}_oos.json"
                )
                generated_models, generated_residuals, oos_artifact, counts = (
                    _load_oos_rows(
                        station_id=station_id,
                        timezone_name=station_timezones[station_id],
                        oos_path=oos_path,
                        truth_index=truth_index,
                        identity_prefix="replay29",
                        maturity_prefix="PREVIOUS_RUN_REPLAY",
                        capture_mode="canonical_45station_29day_calibration_base",
                    )
                )
                input_artifacts.append(oos_artifact)
                scope_counts.update(counts)
                payload = load_bounded_json(oos_path)
                as_of_values.append(str(payload["fetched_at_utc"]))
                residual_by_model_run = {
                    str(row["model_run_id"]): row
                    for row in generated_residuals
                }
                for model_row in generated_models:
                    natural_key = (
                        str(model_row["model_id"]),
                        station_id,
                        str(model_row["target_local_date"]),
                        str(model_row["run_init_utc"]),
                        str(model_row["run_cycle"]),
                    )
                    existing_model = existing_models.get(natural_key)
                    if existing_model is not None:
                        if (
                            float(existing_model["raw_tmax_c"])
                            != float(model_row["raw_tmax_c"])
                            or str(existing_model["provider_id"])
                            != str(model_row["provider_id"])
                        ):
                            model_row["run_cycle"] = (
                                f"{model_row['run_cycle']}_G29"
                            )
                            model_row["maturity_key"] = (
                                f"{model_row['maturity_key']}_G29"
                            )
                            model_row["content_sha256"] = content_sha256({
                                key: value
                                for key, value in model_row.items()
                                if key != "content_sha256"
                            })
                            selected_model = model_row
                            model_rows.append(model_row)
                        else:
                            selected_model = existing_model
                            model_references_by_id[
                                str(existing_model["model_run_id"])
                            ] = _model_reference(existing_model)
                    else:
                        selected_model = model_row
                        model_rows.append(model_row)

                    residual = residual_by_model_run[
                        str(model_row["model_run_id"])
                    ]
                    event_date = str(model_row["target_local_date"])
                    truth_lineage = truth_index[event_date]
                    residual["truth_id"] = truth_lineage["truth_id"]
                    residual["model_run_id"] = selected_model["model_run_id"]
                    residual["maturity_key"] = selected_model["maturity_key"]
                    residual["run_cycle"] = selected_model["run_cycle"]
                    residual["paired_asof_utc"] = selected_model["run_init_utc"]
                    residual["pair_sha256"] = content_sha256({
                        "truth_id": residual["truth_id"],
                        "model_run_id": residual["model_run_id"],
                        "station_id": station_id,
                        "model_id": residual["model_id"],
                        "event_date": event_date,
                        "lead_bucket": residual["lead_bucket"],
                        "forecast_tmax_c": float(selected_model["raw_tmax_c"]),
                        "official_tmax_c": float(
                            truth_lineage["official_tmax_c"]
                        ),
                        "residual_c": float(
                            truth_lineage["official_tmax_c"]
                        ) - float(selected_model["raw_tmax_c"]),
                    })
                    residual["e_y_minus_f_raw_c"] = (
                        float(truth_lineage["official_tmax_c"])
                        - float(selected_model["raw_tmax_c"])
                    )
                    residual["content_sha256"] = content_sha256({
                        key: value
                        for key, value in residual.items()
                        if key != "content_sha256"
                    })
                    existing_residual = existing_residuals.get((
                        str(residual["truth_id"]),
                        str(residual["model_run_id"]),
                    ))
                    if existing_residual is not None:
                        if float(existing_residual["e_y_minus_f_raw_c"]) != float(
                            residual["e_y_minus_f_raw_c"]
                        ):
                            raise ContractError(
                                "calibration_existing_residual_value_conflict"
                            )
                        existing_residual_reuse_count += 1
                    else:
                        residual_rows.append(residual)

    expected_model_pairs = (
        EXPECTED_STATION_COUNT
        * EXPECTED_DAY_COUNT
        * 2
        * len(MODEL_IDS)
    )
    if (
        len(truth_rows) + len(truth_references)
        != EXPECTED_STATION_COUNT * EXPECTED_DAY_COUNT
        or len(scope_counts)
        != EXPECTED_STATION_COUNT * EXPECTED_DAY_COUNT * 2
        or set(scope_counts.values()) != {len(MODEL_IDS)}
        or len(model_rows) + len(model_references_by_id)
        != expected_model_pairs
        or len(residual_rows) + existing_residual_reuse_count
        != expected_model_pairs
    ):
        raise ContractError("calibration_45station_packet_coverage_invalid")
    latest_as_of = max(as_of_values)
    packet = {
        "schema": PACKET_SCHEMA,
        "as_of_utc": latest_as_of,
        "source_name": "canonical_45station_29day_calibration_base_v1",
        "station_registry": station_registry,
        "truth_evidence": truth_rows,
        "truth_references": truth_references,
        "calibration_model_rows": model_rows,
        "model_references": sorted(
            model_references_by_id.values(),
            key=lambda row: str(row["model_run_id"]),
        ),
        "residual_evidence": residual_rows,
        "temporal_boundary": {
            "closed_settlement_event_days_only": True,
            "future_target_used": False,
        },
        "checks": {
            "truth_source_canonical_ingress": True,
            "formal_awc_truth_only": True,
            "existing_truth_bound_by_hash": True,
            "changed_truth_emits_new_revision": True,
            "existing_model_bound_by_hash": True,
            "minimum_two_models_per_scope": True,
            "five_models_per_scope": True,
            "d1_d2_complete": True,
            "all_model_rows_historical_replay_only": True,
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
            "station_count": EXPECTED_STATION_COUNT,
            "day_count": EXPECTED_DAY_COUNT,
            "lead_buckets": ["d1", "d2"],
            "model_ids": list(MODEL_IDS),
            "authority_scope_intent": NO_LIVE_REPLAY,
        },
        "input_artifacts": input_artifacts,
        "stats": {
            "station_count": EXPECTED_STATION_COUNT,
            "truth_identity_count": (
                len(truth_rows) + len(truth_references)
            ),
            "new_truth_row_count": len(truth_rows),
            "existing_truth_reference_count": len(truth_references),
            "new_model_row_count": len(model_rows),
            "existing_model_reference_count": len(
                model_references_by_id
            ),
            "new_residual_row_count": len(residual_rows),
            "existing_residual_reuse_count": (
                existing_residual_reuse_count
            ),
            "scope_count": len(scope_counts),
            "min_models_per_scope": min(scope_counts.values()),
            "max_models_per_scope": max(scope_counts.values()),
        },
        "database_reference": {
            "path": str(database),
            "sha256": _sha256_path(database),
            "read_only": True,
        },
    }
    identity = dict(packet)
    packet["content_sha256"] = content_sha256(identity)
    packet["packet_id"] = (
        f"calibration-base-45station-29d-{packet['content_sha256'][:16]}"
    )
    return packet


def shard_packets(packet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    station_ids = sorted(
        str(row["station_id"])
        for row in packet["station_registry"]
    )
    result: dict[str, dict[str, Any]] = {}
    for station_id in station_ids:
        station_registry = [
            dict(row)
            for row in packet["station_registry"]
            if str(row["station_id"]) == station_id
        ]
        truth_rows = [
            dict(row)
            for row in packet["truth_evidence"]
            if str(row["station_id"]) == station_id
        ]
        truth_references = [
            dict(row)
            for row in packet["truth_references"]
            if str(row["station_id"]) == station_id
        ]
        model_rows = [
            dict(row)
            for row in packet["calibration_model_rows"]
            if str(row["station_id"]) == station_id
        ]
        model_references = [
            dict(row)
            for row in packet["model_references"]
            if str(row["station_id"]) == station_id
        ]
        residuals = [
            dict(row)
            for row in packet["residual_evidence"]
            if str(row["station_id"]) == station_id
        ]
        input_artifacts = [
            dict(row)
            for row in packet["input_artifacts"]
            if str(row.get("station_id") or "") == station_id
        ]
        if (
            len(station_registry) != 1
            or len(truth_rows) + len(truth_references)
            != EXPECTED_DAY_COUNT
            or len(model_rows) + len(model_references)
            != EXPECTED_DAY_COUNT * 2 * len(MODEL_IDS)
        ):
            raise ContractError("calibration_station_shard_scope_invalid")
        identity = {
            "schema": PACKET_SCHEMA,
            "as_of_utc": packet["as_of_utc"],
            "source_name": (
                "canonical_45station_29day_calibration_base_v1:"
                + station_id
            ),
            "station_registry": station_registry,
            "truth_evidence": truth_rows,
            "truth_references": truth_references,
            "calibration_model_rows": model_rows,
            "model_references": model_references,
            "residual_evidence": residuals,
            "temporal_boundary": dict(packet["temporal_boundary"]),
            "checks": dict(packet["checks"]),
            "boundary": dict(packet["boundary"]),
            "scope": {
                **dict(packet["scope"]),
                "station_count": 1,
                "station_ids": [station_id],
            },
            "input_artifacts": input_artifacts,
            "stats": {
                "station_count": 1,
                "truth_identity_count": (
                    len(truth_rows) + len(truth_references)
                ),
                "new_truth_row_count": len(truth_rows),
                "existing_truth_reference_count": len(truth_references),
                "new_model_row_count": len(model_rows),
                "existing_model_reference_count": len(model_references),
                "new_residual_row_count": len(residuals),
            },
            "database_reference": dict(packet["database_reference"]),
        }
        shard = {
            **identity,
            "content_sha256": content_sha256(identity),
        }
        shard["packet_id"] = (
            f"calibration-base-{station_id}-29d-"
            f"{shard['content_sha256'][:16]}"
        )
        result[station_id] = shard
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth-root", type=Path, default=DEFAULT_TRUTH_ROOT)
    parser.add_argument("--oos-root", type=Path, default=DEFAULT_OOS_ROOT)
    parser.add_argument("--database", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    packet = build_packet(
        truth_root=args.truth_root,
        oos_root=args.oos_root,
        database=args.database,
    )
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    shards = shard_packets(packet)
    shard_rows = []
    for station_id, shard in sorted(shards.items()):
        path = output / station_id / "calibration_base.json"
        write_bounded_json(path, shard, limit=MAX_OUTPUT_BYTES)
        shard_rows.append({
            "station_id": station_id,
            "path": str(path),
            "packet_id": shard["packet_id"],
            "content_sha256": shard["content_sha256"],
            "stats": shard["stats"],
        })
    manifest_identity = {
        "schema": "bayes_basket_target_45station_calibration_base_manifest_v1",
        "as_of_utc": packet["as_of_utc"],
        "station_count": len(shard_rows),
        "station_shards": shard_rows,
        "aggregate_stats": packet["stats"],
        "database_reference": packet["database_reference"],
        "boundary": packet["boundary"],
    }
    manifest = {
        **manifest_identity,
        "content_sha256": content_sha256(manifest_identity),
    }
    manifest_path = output / "manifest.json"
    write_bounded_json(manifest_path, manifest)
    print(json.dumps({
        "schema": manifest["schema"],
        "content_sha256": manifest["content_sha256"],
        "output": str(manifest_path),
        "stats": packet["stats"],
        "writes_target_database": False,
        "calls_order_gateway": False,
        "live_order_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
