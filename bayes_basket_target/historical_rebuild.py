"""Bounded, registry-scoped historical truth/model reconstruction.

This module is intentionally a research-side boundary.  It consumes retained
exact arrival and decode evidence only; it never fetches, opens SQLite, writes
raw copies, calls runtime code, or promotes a probability artifact.  ET is not
used anywhere in the reconstruction decision.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping

from .contracts import ContractError, content_sha256
from .resource_bounds import assert_process_memory_budget
from .settlement_time import (
    registry_settlement_timezone,
    settlement_day_window,
)


SCHEMA = "bayes_basket_target_settlement_history_rebuild_v1"
MAX_REBUILD_ROWS = 100_000
MAX_CURVES = 10_000
MAX_POINTS_PER_CURVE = 4_096
_HASH_FIELDS = ("raw_sha256", "receipt_sha256", "decode_sha256")


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _utc(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _finite(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid_number") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid_number")
    return result


def _scope_pairs(
    registry_rows: Iterable[Mapping[str, Any]],
    *,
    station_ids: Iterable[str],
    event_dates: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
    rows = [dict(row) for row in registry_rows if isinstance(row, Mapping)]
    stations = sorted({str(value or "").strip().upper() for value in station_ids if str(value or "").strip()})
    dates = sorted({str(value or "").strip() for value in event_dates if str(value or "").strip()})
    if not stations or not dates:
        raise ContractError("historical_rebuild_scope_empty")
    if len(stations) * len(dates) > MAX_REBUILD_ROWS:
        raise ContractError("historical_rebuild_scope_too_large")
    selected: list[dict[str, Any]] = []
    timezones: dict[tuple[str, str], str] = {}
    for station in stations:
        for event_date in dates:
            timezone_name = registry_settlement_timezone(
                rows,
                station_id=station,
                event_date=event_date,
            )
            timezones[(station, event_date)] = timezone_name
            selected.extend(
                row
                for row in rows
                if str(row.get("station_id") or "").strip().upper() == station
                and str(row.get("event_date") or "").strip() == event_date
            )
    return selected, timezones


def _lineage(row: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """Merge explicit nested arrival/decode envelopes without guessing fields."""

    result = dict(row)
    for key in ("arrival", "decode"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            for field, value in nested.items():
                result.setdefault(field, value)
    if kind == "model" and not result.get("arrival_id"):
        raise ContractError("historical_model_arrival_id_missing")
    arrival = result.get("arrival_at_utc") or result.get("received_at_utc")
    _utc("historical_arrival_at_utc", arrival)
    for field in _HASH_FIELDS:
        _sha(f"historical_{kind}_{field}", result.get(field))
    return result


def _validate_probability_artifacts(
    artifacts: Iterable[Mapping[str, Any]],
    *,
    expected_timezone: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise ContractError("historical_probability_artifact_mapping_required")
        row = dict(raw)
        lineage = row.get("lineage") if isinstance(row.get("lineage"), Mapping) else {}
        supplied = row.get("settlement_timezone") or row.get("target_timezone") or lineage.get("settlement_timezone")
        timezone_name = _text("historical_probability_artifact_timezone", supplied)
        if timezone_name != expected_timezone:
            raise ContractError("historical_probability_artifact_settlement_timezone_mismatch")
        normalized.append(row)
    return normalized


def rebuild_settlement_history(
    *,
    registry_rows: Iterable[Mapping[str, Any]],
    truth_rows: Iterable[Mapping[str, Any]],
    model_rows: Iterable[Mapping[str, Any]],
    cutoff_utc: str | datetime,
    station_ids: Iterable[str],
    event_dates: Iterable[str],
    probability_artifacts: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build one exact settlement-time history artifact.

    The scope is deliberately one timezone.  Mixed registry timezones must be
    rebuilt as separate artifacts; a shared artifact would silently lose the
    market's date boundary and is therefore rejected.
    """

    cutoff = cutoff_utc if isinstance(cutoff_utc, datetime) else _utc("historical_rebuild_cutoff_utc", cutoff_utc)
    if isinstance(cutoff_utc, datetime):
        if cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() is None:
            raise ContractError("historical_rebuild_cutoff_timezone_required")
        cutoff = cutoff_utc.astimezone(timezone.utc)
    registry_scope, timezone_by_pair = _scope_pairs(
        registry_rows,
        station_ids=station_ids,
        event_dates=event_dates,
    )
    scope_timezones = set(timezone_by_pair.values())
    if len(scope_timezones) != 1:
        raise ContractError("historical_rebuild_mixed_settlement_timezones_forbidden")
    target_timezone = next(iter(scope_timezones))
    artifacts = _validate_probability_artifacts(
        probability_artifacts,
        expected_timezone=target_timezone,
    )
    windows = {
        pair: settlement_day_window(pair[1], timezone_name)
        for pair, timezone_name in timezone_by_pair.items()
    }
    accepted_truth: list[dict[str, Any]] = []
    accepted_models: list[dict[str, Any]] = []
    truth_slots: dict[tuple[str, str, str, str], str] = {}
    model_slots: dict[tuple[str, str, str, str, str], str] = {}
    truth_input = [dict(row) for row in truth_rows if isinstance(row, Mapping)]
    model_input = [dict(row) for row in model_rows if isinstance(row, Mapping)]
    if len(truth_input) + len(model_input) > MAX_REBUILD_ROWS:
        raise ContractError("historical_rebuild_row_budget_exceeded")
    for kind, source, destination in (("truth", truth_input, accepted_truth), ("model", model_input, accepted_models)):
        for raw in source:
            assert_process_memory_budget()
            row = _lineage(raw, kind=kind)
            station = _text("historical_station_id", row.get("station_id")).upper()
            event_date = _text("historical_event_date", row.get("event_date"))
            pair = (station, event_date)
            if pair not in timezone_by_pair:
                raise ContractError("historical_row_outside_registry_scope")
            timezone_name = timezone_by_pair[pair]
            supplied_timezone = _text("historical_target_timezone", row.get("target_timezone") or row.get("settlement_timezone"))
            if supplied_timezone != timezone_name:
                raise ContractError("historical_row_settlement_timezone_mismatch")
            arrival = _utc("historical_arrival_at_utc", row.get("arrival_at_utc") or row.get("received_at_utc"))
            if arrival > cutoff:
                raise ContractError("historical_exact_arrival_after_cutoff")
            if kind == "truth":
                valid = _utc("historical_truth_valid_at_utc", row.get("valid_at_utc") or row.get("observed_at_utc"))
                value = _finite("historical_truth_temperature_c", row.get("reported_temperature_c", row.get("temperature_c")))
                slot = (station, event_date, _iso(valid), _text("historical_truth_source_lane", row.get("source_lane")))
                window = windows[pair]
                start = _utc("target_day_start_utc", window["target_day_start_utc"])
                end = _utc("target_day_end_utc", window["target_day_end_utc"])
                if not (start <= valid < end):
                    raise ContractError("historical_truth_outside_registry_window")
                identity = content_sha256({"slot": slot, "value": value, "raw_sha256": row["raw_sha256"], "decode_sha256": row["decode_sha256"]})
                previous = truth_slots.get(slot)
                if previous is not None and previous != identity:
                    raise ContractError("historical_truth_exact_slot_conflict")
                if previous is not None:
                    continue
                truth_slots[slot] = identity
                destination.append({**row, "station_id": station, "event_date": event_date, "target_timezone": timezone_name, "valid_at_utc": _iso(valid), "reported_temperature_c": value, "arrival_at_utc": _iso(arrival)})
            else:
                valid = _utc("historical_model_valid_time_utc", row.get("valid_time_utc"))
                run = _utc("historical_model_run_initialization_utc", row.get("run_initialization_utc", row.get("model_run_initialization_utc")))
                provider = _text("historical_model_provider_id", row.get("provider_id")).lower()
                model_id = _text("historical_model_id", row.get("model_id"))
                point_value = _finite("historical_model_temperature_c", row.get("temperature_2m_c", row.get("temperature_c")))
                window = windows[pair]
                start = _utc("target_day_start_utc", window["target_day_start_utc"])
                end = _utc("target_day_end_utc", window["target_day_end_utc"])
                if not (start <= valid < end):
                    raise ContractError("historical_model_outside_registry_window")
                slot = (station, event_date, provider, _iso(run), _iso(valid))
                identity = content_sha256({"slot": slot, "value": point_value, "decode_sha256": row["decode_sha256"], "receipt_sha256": row["receipt_sha256"]})
                previous = model_slots.get(slot)
                if previous is not None and previous != identity:
                    raise ContractError("historical_model_exact_point_conflict")
                if previous is not None:
                    continue
                model_slots[slot] = identity
                destination.append({**row, "station_id": station, "event_date": event_date, "target_timezone": timezone_name, "valid_time_utc": _iso(valid), "run_initialization_utc": _iso(run), "provider_id": provider, "model_id": model_id, "temperature_2m_c": point_value, "arrival_at_utc": _iso(arrival)})
    curves_by_key: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in accepted_models:
        key = (row["station_id"], row["event_date"], row["target_timezone"], row["provider_id"], row["run_initialization_utc"])
        curves_by_key[key].append(row)
    if len(curves_by_key) > MAX_CURVES:
        raise ContractError("historical_model_curve_budget_exceeded")
    curves: list[dict[str, Any]] = []
    for key, points in sorted(curves_by_key.items()):
        if len(points) > MAX_POINTS_PER_CURVE:
            raise ContractError("historical_model_curve_point_budget_exceeded")
        ordered = sorted(points, key=lambda row: row["valid_time_utc"])
        curves.append({
            "station_id": key[0], "event_date": key[1], "target_timezone": key[2],
            "provider_id": key[3], "run_initialization_utc": key[4],
            "points": ordered, "point_count": len(ordered),
            "curve_sha256": content_sha256({"key": key, "points": ordered}),
        })
    truth_by_pair: dict[tuple[str, str], int] = defaultdict(int)
    curves_by_pair: dict[tuple[str, str], int] = defaultdict(int)
    for row in accepted_truth:
        truth_by_pair[(row["station_id"], row["event_date"])] += 1
    for curve in curves:
        curves_by_pair[(curve["station_id"], curve["event_date"])] += 1
    joins: list[dict[str, Any]] = []
    for pair, window in sorted(windows.items()):
        frozen = cutoff >= _utc("target_day_end_utc", window["target_day_end_utc"])
        joins.append({
            "station_id": pair[0], "event_date": pair[1], "target_timezone": timezone_by_pair[pair],
            "window": window, "truth_count": truth_by_pair[pair],
            "model_curve_count": curves_by_pair[pair], "truth_frozen": frozen,
            "calibration_eligible": bool(frozen and truth_by_pair[pair] and curves_by_pair[pair]),
        })
    identity = {
        "schema": SCHEMA,
        "cutoff_utc": _iso(cutoff),
        "settlement_timezone": target_timezone,
        "market_registry_scope": registry_scope,
        "market_registry_scope_sha256": content_sha256(registry_scope),
        "windows": [item["window"] for item in joins],
        "truth": sorted(accepted_truth, key=lambda row: (row["station_id"], row["event_date"], row["valid_at_utc"], row["raw_sha256"])),
        "model_curves": curves,
        "exact_joins": joins,
        "probability_artifacts": artifacts,
        "checks": {
            "market_registry_timezone_authoritative": True,
            "et_operational_boundary_used": False,
            "truth_model_artifact_timezone_exact": True,
            "exact_arrival_cutoff_enforced": True,
            "exact_decode_lineage_required": True,
            "mixed_timezone_artifact_rejected": True,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    identity["evidence_sha256"] = content_sha256(identity)
    return identity


__all__ = ["MAX_REBUILD_ROWS", "SCHEMA", "rebuild_settlement_history"]
