"""Canonical pre-ingress manifest for a native model ET-day maximum.

The V13 model-OOS event contract currently binds one raw object.  A daily
maximum normally depends on several native objects, so their set may not be
renamed into that singular field.  This module freezes the exact point/raw
vector first.  Only a complete manifest may later be adapted by a revised
model-input receipt contract; incomplete retained evidence produces one
non-consumable terminal per exact scope.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .model_product_identity import validate_model_product_identity_receipt
from .model_product_identity import build_registered_model_product_identity_receipt


MANIFEST_SCHEMA = "bayes_basket_target_native_daily_max_manifest_v1"
TERMINAL_SCHEMA = "bayes_basket_target_native_daily_max_terminal_v1"
BUNDLE_SCHEMA = "bayes_basket_target_native_daily_max_basis_bundle_v1"
BUILDER_ID = "pr10.native_daily_max_manifest.v1"
SETTLEMENT_TIMEZONE = "America/New_York"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
POINT_FIELDS = {
    "valid_time_utc", "forecast_temperature_c", "published_at_utc",
    "received_at_utc", "decision_at_utc", "raw_object_sha256",
    "transport_receipt_sha256", "decoder_id",
}
MANIFEST_FIELDS = {
    "schema", "builder_id", "authority_scope", "state",
    "model_product_identity_receipt",
    "model_product_identity_content_sha256", "station_id", "model_id",
    "ensemble_system_id", "model_family", "provider_id",
    "provider_product_id", "independence_family_id", "source_class",
    "settlement_timezone", "lead_bucket", "lead_bucket_scope", "run_cycle",
    "run_cycle_scope", "event_oos_protocol_version", "event_date",
    "run_init_utc", "expected_hourly_slots", "points", "point_vector_sha256",
    "raw_object_sha256s", "raw_object_set_sha256",
    "transport_receipt_sha256s", "transport_receipt_set_sha256",
    "model_available_at_utc", "retrieved_at_utc", "forecast_decision_at_utc",
    "forecast_value_c", "consumable", "content_sha256",
}
DECODE_POINT_FIELDS = {
    "coordinate_lineage", "decoder_id", "event_date", "latitude",
    "longitude", "model_id", "model_run_initialization_utc", "provider_id",
    "published_at_utc", "raw_message_sha256", "receipt_sha256",
    "received_at_utc", "selector", "station_id", "temperature_2m_c",
    "valid_time_utc",
}
DECODE_EVIDENCE_SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
NATIVE_DECODE_PROVIDER_BY_MODEL = {
    "ECMWF": "ecmwf_ifs025_official",
    "GFS": "gfs_0p25_official",
    "ICON": "icon_global_official",
}
NATIVE_PRODUCT_BY_MODEL = {
    "ECMWF": "ECMWF_IFS_0P25",
    "GFS": "NOAA_GFS_0P25",
    "ICON": "DWD_ICON_GLOBAL",
}


def _sha(value: Any, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"native_daily_max_{label}_invalid")
    return result


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"native_daily_max_{label}_missing")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"native_daily_max_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"native_daily_max_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _temperature(value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError("native_daily_max_temperature_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("native_daily_max_temperature_invalid") from exc
    if not (-150.0 <= result <= 80.0):
        raise ContractError("native_daily_max_temperature_out_of_range")
    return result


def expected_et_hourly_slots(event_date: str) -> list[str]:
    try:
        day = date.fromisoformat(str(event_date))
    except ValueError as exc:
        raise ContractError("native_daily_max_event_date_invalid") from exc
    zone = ZoneInfo(SETTLEMENT_TIMEZONE)
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(
        timezone.utc
    )
    end = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), tzinfo=zone
    ).astimezone(timezone.utc)
    slots: list[str] = []
    cursor = start
    while cursor < end:
        slots.append(cursor.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        cursor += timedelta(hours=1)
    if len(slots) not in {23, 24, 25}:
        raise ContractError("native_daily_max_expected_slot_count_invalid")
    return slots


def _normalized_point(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != POINT_FIELDS:
        raise ContractError("native_daily_max_point_fields_mismatch")
    point = {
        "valid_time_utc": _utc(raw["valid_time_utc"], "valid_time_utc"),
        "forecast_temperature_c": _temperature(raw["forecast_temperature_c"]),
        "published_at_utc": _utc(raw["published_at_utc"], "published_at_utc"),
        "received_at_utc": _utc(raw["received_at_utc"], "received_at_utc"),
        "decision_at_utc": _utc(raw["decision_at_utc"], "decision_at_utc"),
        "raw_object_sha256": _sha(raw["raw_object_sha256"], "raw_object_sha256"),
        "transport_receipt_sha256": _sha(
            raw["transport_receipt_sha256"], "transport_receipt_sha256"
        ),
        "decoder_id": _text(raw["decoder_id"], "decoder_id"),
    }
    if not (
        point["published_at_utc"]
        <= point["received_at_utc"]
        <= point["decision_at_utc"]
    ):
        raise ContractError("native_daily_max_point_clock_order_invalid")
    return point


def build_native_daily_max_manifest(
    *, identity_receipt: Mapping[str, Any], event_date: str, run_init_utc: str,
    points: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build one complete ET-hourly manifest; never interpolate missing slots."""

    product_identity = validate_model_product_identity_receipt(identity_receipt)
    if product_identity.get("source_class") != "native_official":
        raise ContractError("native_daily_max_native_source_required")
    if product_identity.get("settlement_timezone") != SETTLEMENT_TIMEZONE:
        raise ContractError("native_daily_max_settlement_timezone_mismatch")
    identity_sha = _sha(product_identity.get("content_sha256"), "identity_sha256")
    event_day = date.fromisoformat(str(event_date)).isoformat()
    run_init = _utc(run_init_utc, "run_init_utc")
    normalized = [_normalized_point(point) for point in points]
    normalized.sort(key=lambda point: point["valid_time_utc"])
    expected = expected_et_hourly_slots(event_day)
    observed = [point["valid_time_utc"] for point in normalized]
    if len(observed) != len(set(observed)):
        raise ContractError("native_daily_max_valid_time_duplicate")
    if observed != expected:
        raise ContractError("native_daily_max_hourly_coverage_incomplete")
    raw_objects = sorted({point["raw_object_sha256"] for point in normalized})
    receipt_set = sorted({point["transport_receipt_sha256"] for point in normalized})
    maximum = max(point["forecast_temperature_c"] for point in normalized)
    identity = {
        "schema": MANIFEST_SCHEMA,
        "builder_id": BUILDER_ID,
        "authority_scope": AUTHORITY_SCOPE,
        "state": "MATERIALIZED_UNPUBLISHED",
        "model_product_identity_receipt": dict(product_identity),
        "model_product_identity_content_sha256": identity_sha,
        **{key: product_identity[key] for key in (
            "station_id", "model_id", "ensemble_system_id", "model_family",
            "provider_id", "provider_product_id", "independence_family_id",
            "source_class", "settlement_timezone", "lead_bucket",
            "lead_bucket_scope", "run_cycle", "run_cycle_scope",
            "event_oos_protocol_version",
        )},
        "event_date": event_day,
        "run_init_utc": run_init,
        "expected_hourly_slots": expected,
        "points": normalized,
        "point_vector_sha256": content_sha256(normalized),
        "raw_object_sha256s": raw_objects,
        "raw_object_set_sha256": content_sha256(raw_objects),
        "transport_receipt_sha256s": receipt_set,
        "transport_receipt_set_sha256": content_sha256(receipt_set),
        "model_available_at_utc": max(point["published_at_utc"] for point in normalized),
        "retrieved_at_utc": max(point["received_at_utc"] for point in normalized),
        "forecast_decision_at_utc": max(
            point["decision_at_utc"] for point in normalized
        ),
        "forecast_value_c": maximum,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_native_daily_max_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    if set(raw) != MANIFEST_FIELDS:
        raise ContractError("native_daily_max_manifest_fields_mismatch")
    supplied = _sha(raw.get("content_sha256"), "manifest_content_sha256")
    if supplied != content_sha256({
        key: item for key, item in raw.items() if key != "content_sha256"
    }):
        raise ContractError("native_daily_max_manifest_content_sha_mismatch")
    product_identity = raw.get("model_product_identity_receipt")
    if not isinstance(product_identity, Mapping):
        raise ContractError("native_daily_max_manifest_identity_mapping_required")
    rebuilt = build_native_daily_max_manifest(
        identity_receipt=product_identity,
        event_date=str(raw.get("event_date") or ""),
        run_init_utc=str(raw.get("run_init_utc") or ""),
        points=raw.get("points") if isinstance(raw.get("points"), list) else [],
    )
    if rebuilt != dict(raw):
        raise ContractError("native_daily_max_manifest_rebuild_mismatch")
    return rebuilt


def build_decode_evidence_terminal_bundle(
    evidences: Iterable[Mapping[str, Any]], *, as_of_utc: str,
    arrival_evidences: Iterable[Mapping[str, Any]] = (),
    transport_bindings: Mapping[tuple[str, ...], Mapping[str, Any]] | None = None,
    identity_receipts: Mapping[tuple[str, ...], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze retained decode evidence into exact non-consumable terminals."""

    cutoff = _utc(as_of_utc, "as_of_utc")
    arrival_index: dict[tuple[str, ...], dict[str, Any]] = {}
    arrival_evidence_shas: list[str] = []
    for source in arrival_evidences:
        if source.get("schema") != "bayes_basket_target_model_arrival_evidence_v1":
            raise ContractError("native_daily_max_arrival_evidence_schema_invalid")
        supplied = _sha(source.get("receipt_sha256"), "arrival_evidence_receipt_sha256")
        if supplied != content_sha256({k: v for k, v in source.items() if k != "receipt_sha256"}):
            raise ContractError("native_daily_max_arrival_evidence_receipt_mismatch")
        arrival_evidence_shas.append(supplied)
        arrivals = source.get("arrivals")
        if not isinstance(arrivals, list):
            raise ContractError("native_daily_max_arrivals_list_required")
        for raw in arrivals:
            if not isinstance(raw, Mapping):
                raise ContractError("native_daily_max_arrival_mapping_required")
            supplied_content = _sha(raw.get("content_sha256"), "arrival_content_sha256")
            if supplied_content != content_sha256(
                {key: item for key, item in raw.items() if key != "content_sha256"}
            ):
                raise ContractError("native_daily_max_arrival_content_mismatch")
            hashes = raw.get("raw_payload_sha256s")
            if not isinstance(hashes, list) or len(hashes) != 1:
                raise ContractError("native_daily_max_arrival_raw_object_not_single")
            key = (
                _text(raw.get("station_id"), "arrival_station_id").upper(),
                _text(raw.get("model_id"), "arrival_model_id").upper(),
                _text(raw.get("provider_id"), "arrival_provider_id"),
                str(raw.get("target_local_date") or ""),
                _utc(raw.get("run_initialization_utc"), "arrival_run_init_utc"),
                _utc(raw.get("valid_time_utc"), "arrival_valid_time_utc"),
                _sha(hashes[0], "arrival_raw_object_sha256"),
            )
            if key in arrival_index:
                raise ContractError("native_daily_max_arrival_join_key_duplicate")
            published = _utc(raw.get("published_at_utc"), "arrival_published_at_utc")
            received = _utc(raw.get("received_at_utc"), "arrival_received_at_utc")
            decision = _utc(raw.get("decision_at_utc"), "arrival_decision_at_utc")
            if not published <= received <= decision <= cutoff:
                raise ContractError("native_daily_max_arrival_clock_order_invalid")
            arrival_index[key] = {
                "arrival_content_sha256": supplied_content,
                "decision_at_utc": decision,
            }
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    evidence_shas: list[str] = []
    for source in evidences:
        if source.get("schema") != DECODE_EVIDENCE_SCHEMA:
            raise ContractError("native_daily_max_decode_evidence_schema_invalid")
        supplied = _sha(source.get("receipt_sha256"), "decode_evidence_receipt_sha256")
        if supplied != content_sha256({k: v for k, v in source.items() if k != "receipt_sha256"}):
            raise ContractError("native_daily_max_decode_evidence_receipt_mismatch")
        if _utc(source.get("as_of_utc"), "decode_evidence_as_of_utc") > cutoff:
            raise ContractError("native_daily_max_decode_evidence_after_asof")
        if source.get("target_timezone") != SETTLEMENT_TIMEZONE:
            raise ContractError("native_daily_max_decode_timezone_mismatch")
        if int(source.get("decoded_point_count", -1)) != len(source.get("decoded_points") or []):
            raise ContractError("native_daily_max_decode_point_count_mismatch")
        curves = source.get("curves")
        if (
            not isinstance(curves, Mapping)
            or curves.get("production_authority") is not False
            or curves.get("promotion_allowed") is not False
        ):
            raise ContractError("native_daily_max_decode_authority_invalid")
        evidence_shas.append(supplied)
        points = source.get("decoded_points")
        if not isinstance(points, list):
            raise ContractError("native_daily_max_decode_points_list_required")
        for raw in points:
            if not isinstance(raw, Mapping) or set(raw) != DECODE_POINT_FIELDS:
                raise ContractError("native_daily_max_decode_point_fields_mismatch")
            station = _text(raw["station_id"], "decode_station_id").upper()
            model = _text(raw["model_id"], "decode_model_id").upper()
            provider = _text(raw["provider_id"], "decode_provider_id")
            if NATIVE_DECODE_PROVIDER_BY_MODEL.get(model) != provider:
                raise ContractError("native_daily_max_decode_model_provider_mismatch")
            event_day = str(raw["event_date"])
            run_init = _utc(raw["model_run_initialization_utc"], "decode_run_init_utc")
            valid = _utc(raw["valid_time_utc"], "decode_valid_time_utc")
            if datetime.fromisoformat(valid.replace("Z", "+00:00")).astimezone(
                ZoneInfo(SETTLEMENT_TIMEZONE)
            ).date().isoformat() != event_day:
                raise ContractError("native_daily_max_decode_event_date_mismatch")
            published = _utc(raw["published_at_utc"], "decode_published_at_utc")
            received = _utc(raw["received_at_utc"], "decode_received_at_utc")
            if published > received:
                raise ContractError("native_daily_max_decode_clock_order_invalid")
            raw_sha = _sha(raw["raw_message_sha256"], "decode_raw_sha256")
            arrival = arrival_index.get(
                (station, model, provider, event_day, run_init, valid, raw_sha)
            )
            transport = (transport_bindings or {}).get(
                (station, model, provider, event_day, run_init, valid, raw_sha)
            )
            groups[(station, model, provider, event_day, run_init)].append({
                "valid_time_utc": valid,
                "forecast_temperature_c": _temperature(raw["temperature_2m_c"]),
                "published_at_utc": published,
                "received_at_utc": received,
                "raw_object_sha256": raw_sha,
                "point_receipt_sha256": _sha(raw["receipt_sha256"], "decode_point_receipt_sha256"),
                "decoder_id": _text(raw["decoder_id"], "decode_decoder_id"),
                "arrival_content_sha256": (
                    arrival["arrival_content_sha256"] if arrival is not None else None
                ),
                "decision_at_utc": (
                    arrival["decision_at_utc"] if arrival is not None else None
                ),
                "transport_receipt_content_sha256": (
                    str(transport["transport_receipt_content_sha256"])
                    if transport is not None else None
                ),
                "raw_object_verified": (
                    transport.get("raw_object_verified") is True
                    if transport is not None else False
                ),
            })
    terminals: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for scope, points in sorted(groups.items()):
        station, model, provider, event_day, run_init = scope
        points.sort(key=lambda point: point["valid_time_utc"])
        expected = expected_et_hourly_slots(event_day)
        observed = [point["valid_time_utc"] for point in points]
        if len(observed) != len(set(observed)):
            raise ContractError("native_daily_max_decode_valid_time_duplicate")
        missing = [slot for slot in expected if slot not in set(observed)]
        product_identity = (identity_receipts or {}).get(scope)
        if product_identity is not None:
            product_identity = validate_model_product_identity_receipt(product_identity)
            if any(product_identity[field] != expected for field, expected in (
                ("station_id", station), ("model_id", model),
                ("provider_id", provider),
            )):
                raise ContractError("native_daily_max_identity_scope_mismatch")
        data_complete = (
            not missing
            and all(point["decision_at_utc"] is not None for point in points)
            and all(
                point["transport_receipt_content_sha256"] is not None
                and point["raw_object_verified"] is True
                for point in points
            )
        )
        if data_complete and product_identity is not None:
            manifests.append(build_native_daily_max_manifest(
                identity_receipt=product_identity,
                event_date=event_day,
                run_init_utc=run_init,
                points=[{
                    "valid_time_utc": point["valid_time_utc"],
                    "forecast_temperature_c": point["forecast_temperature_c"],
                    "published_at_utc": point["published_at_utc"],
                    "received_at_utc": point["received_at_utc"],
                    "decision_at_utc": point["decision_at_utc"],
                    "raw_object_sha256": point["raw_object_sha256"],
                    "transport_receipt_sha256": point[
                        "transport_receipt_content_sha256"
                    ],
                    "decoder_id": point["decoder_id"],
                } for point in points],
            ))
            continue
        diagnostics = []
        if missing:
            diagnostics.append("NATIVE_DAILY_MAX_SLOT_COVERAGE_INCOMPLETE")
        if product_identity is None:
            diagnostics.append("CANONICAL_NATIVE_PRODUCT_IDENTITY_MISSING")
        if any(point["decision_at_utc"] is None for point in points):
            diagnostics.append("FORECAST_DECISION_CLOCK_MISSING_FROM_DECODE_EVIDENCE")
        if any(
            point["transport_receipt_content_sha256"] is None
            or point["raw_object_verified"] is not True
            for point in points
        ):
            diagnostics.append("TRANSPORT_RECEIPT_OBJECT_BINDING_NOT_EMBEDDED")
        blocker = "LINEAGE_MISMATCH" if product_identity is None else "DATA_MISSING"
        identity = {
            "schema": TERMINAL_SCHEMA,
            "builder_id": BUILDER_ID,
            "as_of_utc": cutoff,
            "state": "BLOCKED",
            "blocker": blocker,
            "authority_scope": AUTHORITY_SCOPE,
            "station_id": station,
            "model_id": model,
            "provider_id": provider,
            "event_date": event_day,
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "run_init_utc": run_init,
            "expected_hourly_slots": expected,
            "observed_hourly_slots": observed,
            "missing_hourly_slots": missing,
            "observed_point_count": len(points),
            "raw_object_sha256s": sorted({point["raw_object_sha256"] for point in points}),
            "point_vector_sha256": content_sha256(points),
            "arrival_vector_sha256": (
                content_sha256([point["arrival_content_sha256"] for point in points])
                if all(point["arrival_content_sha256"] is not None for point in points)
                else None
            ),
            "forecast_decision_at_utc": (
                max(str(point["decision_at_utc"]) for point in points)
                if all(point["decision_at_utc"] is not None for point in points)
                else None
            ),
            "transport_receipt_set_sha256": (
                content_sha256(sorted({
                    str(point["transport_receipt_content_sha256"])
                    for point in points
                }))
                if all(
                    point["transport_receipt_content_sha256"] is not None
                    and point["raw_object_verified"] is True
                    for point in points
                ) else None
            ),
            "diagnostic_gaps": diagnostics,
            "daily_max_data_complete": data_complete,
            "model_product_identity_content_sha256": (
                product_identity["content_sha256"]
                if product_identity is not None else None
            ),
            "writes_database": False,
            "publishes_pr04_pointer": False,
            "starts_services": False,
            "releases_live_pause": False,
            "orders_submitted": 0,
            "consumable": False,
        }
        terminals.append({**identity, "content_sha256": content_sha256(identity)})
    manifests.sort(key=lambda item: item["content_sha256"])
    terminal_hashes = [item["content_sha256"] for item in terminals]
    if len(terminal_hashes) != len(set(terminal_hashes)):
        raise ContractError("native_daily_max_terminal_duplicate")
    identity = {
        "schema": BUNDLE_SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "source_decode_evidence_sha256s": sorted(evidence_shas),
        "source_arrival_evidence_sha256s": sorted(arrival_evidence_shas),
        "scope_count": len(terminals) + len(manifests),
        "daily_max_data_complete_scope_count": len(manifests) + sum(
            item["daily_max_data_complete"] is True for item in terminals
        ),
        "daily_max_data_complete_by_model": {
            model_id: sum(item["model_id"] == model_id for item in manifests) + sum(
                item["model_id"] == model_id
                and item["daily_max_data_complete"] is True
                for item in terminals
            )
            for model_id in sorted({
                item["model_id"] for item in [*manifests, *terminals]
            })
        },
        "manifest_count": len(manifests),
        "manifests": manifests,
        "terminal_count": len(terminals),
        "blocker_counts": {
            blocker: sum(item["blocker"] == blocker for item in terminals)
            for blocker in sorted({item["blocker"] for item in terminals})
        },
        "terminals": terminals,
        "future_data_counts_as_oos": False,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def build_native_identity_index_from_decode_evidence(
    evidences: Iterable[Mapping[str, Any]], *, lead_bucket: str, run_cycle: str,
) -> dict[tuple[str, ...], dict[str, Any]]:
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for source in evidences:
        source_sha = _sha(source.get("receipt_sha256"), "identity_decode_evidence_sha256")
        points = source.get("decoded_points")
        if not isinstance(points, list):
            raise ContractError("native_daily_max_identity_decode_points_required")
        for raw in points:
            if not isinstance(raw, Mapping):
                raise ContractError("native_daily_max_identity_decode_point_mapping_required")
            model = str(raw.get("model_id") or "").upper()
            provider = str(raw.get("provider_id") or "")
            if NATIVE_DECODE_PROVIDER_BY_MODEL.get(model) != provider:
                raise ContractError("native_daily_max_identity_model_provider_mismatch")
            key = (
                str(raw.get("station_id") or "").upper(), model, provider,
                str(raw.get("event_date") or ""),
                _utc(raw.get("model_run_initialization_utc"), "identity_run_init_utc"),
            )
            receipt = build_registered_model_product_identity_receipt(
                product_id=NATIVE_PRODUCT_BY_MODEL[model],
                station_id=key[0], lead_bucket=lead_bucket,
                run_cycle=run_cycle,
                identity_source_receipt_sha256=source_sha,
            )
            previous = result.get(key)
            if previous is not None and previous != receipt:
                raise ContractError("native_daily_max_identity_scope_conflict")
            result[key] = receipt
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_arrival_transport_bindings(
    arrival_evidence: Mapping[str, Any],
) -> dict[tuple[str, ...], dict[str, Any]]:
    """Verify wrapper→transport receipt→raw bytes for every arrival row."""

    arrivals = arrival_evidence.get("arrivals")
    artifacts = arrival_evidence.get("input_artifacts")
    if not isinstance(arrivals, list) or not isinstance(artifacts, list):
        raise ContractError("native_daily_max_transport_input_lists_required")
    if len(arrivals) != len(artifacts):
        raise ContractError("native_daily_max_transport_input_count_mismatch")
    arrival_by_hash = {
        _sha(item.get("content_sha256"), "transport_arrival_content_sha256"): item
        for item in arrivals if isinstance(item, Mapping)
    }
    if len(arrival_by_hash) != len(arrivals):
        raise ContractError("native_daily_max_transport_arrival_duplicate")
    receipt_cache: dict[Path, tuple[str, Mapping[str, Any]]] = {}
    raw_cache: dict[Path, str] = {}
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or set(artifact) != {"path", "sha256"}:
            raise ContractError("native_daily_max_transport_artifact_fields_mismatch")
        wrapper_path = Path(_text(artifact["path"], "transport_wrapper_path"))
        if not wrapper_path.is_file():
            raise ContractError("native_daily_max_transport_wrapper_missing")
        if _file_sha256(wrapper_path) != _sha(artifact["sha256"], "transport_wrapper_sha256"):
            raise ContractError("native_daily_max_transport_wrapper_sha_mismatch")
        try:
            wrapper = json.loads(wrapper_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("native_daily_max_transport_wrapper_json_invalid") from exc
        if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("arrival"), Mapping):
            raise ContractError("native_daily_max_transport_wrapper_arrival_missing")
        wrapped_arrival = dict(wrapper["arrival"])
        arrival_sha = _sha(
            wrapped_arrival.get("content_sha256"), "transport_wrapped_arrival_sha256"
        )
        if arrival_sha not in arrival_by_hash or arrival_by_hash[arrival_sha] != wrapped_arrival:
            raise ContractError("native_daily_max_transport_arrival_join_mismatch")
        receipt_path = Path(_text(
            wrapper.get("transport_receipt_path"), "transport_receipt_path"
        ))
        if receipt_path not in receipt_cache:
            if not receipt_path.is_file():
                raise ContractError("native_daily_max_transport_receipt_missing")
            receipt_file_sha = _file_sha256(receipt_path)
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContractError("native_daily_max_transport_receipt_json_invalid") from exc
            if not isinstance(receipt, Mapping):
                raise ContractError("native_daily_max_transport_receipt_mapping_required")
            receipt_content_sha = _sha(
                receipt.get("receipt_content_sha256"), "transport_receipt_content_sha256"
            )
            if receipt_content_sha != content_sha256({
                key: item for key, item in receipt.items()
                if key != "receipt_content_sha256"
            }):
                raise ContractError("native_daily_max_transport_receipt_content_mismatch")
            receipt_cache[receipt_path] = (receipt_file_sha, receipt)
        receipt_file_sha, receipt = receipt_cache[receipt_path]
        if receipt_file_sha != _sha(
            wrapped_arrival.get("receipt_sha256"), "transport_receipt_file_sha256"
        ):
            raise ContractError("native_daily_max_transport_receipt_file_sha_mismatch")
        raw_hashes = wrapped_arrival.get("raw_payload_sha256s")
        raw_receipts = receipt.get("raw_receipts")
        if (
            not isinstance(raw_hashes, list) or len(raw_hashes) != 1
            or not isinstance(raw_receipts, list)
        ):
            raise ContractError("native_daily_max_transport_raw_receipt_invalid")
        raw_sha = _sha(raw_hashes[0], "transport_raw_sha256")
        matches = [
            item for item in raw_receipts
            if isinstance(item, Mapping) and item.get("raw_sha256") == raw_sha
        ]
        if len(matches) != 1:
            raise ContractError("native_daily_max_transport_raw_receipt_not_unique")
        raw_path = Path(_text(matches[0].get("raw_object_ref"), "transport_raw_path"))
        if raw_path not in raw_cache:
            if not raw_path.is_file():
                raise ContractError("native_daily_max_transport_raw_missing")
            raw_cache[raw_path] = _file_sha256(raw_path)
        if raw_cache[raw_path] != raw_sha:
            raise ContractError("native_daily_max_transport_raw_sha_mismatch")
        key = (
            str(wrapped_arrival["station_id"]).upper(),
            str(wrapped_arrival["model_id"]).upper(),
            str(wrapped_arrival["provider_id"]),
            str(wrapped_arrival["target_local_date"]),
            _utc(wrapped_arrival["run_initialization_utc"], "transport_run_init_utc"),
            _utc(wrapped_arrival["valid_time_utc"], "transport_valid_time_utc"),
            raw_sha,
        )
        if key in result:
            raise ContractError("native_daily_max_transport_binding_duplicate")
        result[key] = {
            "transport_receipt_content_sha256": str(receipt["receipt_content_sha256"]),
            "transport_receipt_file_sha256": receipt_file_sha,
            "raw_object_verified": True,
        }
    if len(result) != len(arrivals):
        raise ContractError("native_daily_max_transport_binding_count_mismatch")
    return result


__all__ = [
    "BUNDLE_SCHEMA", "BUILDER_ID", "MANIFEST_SCHEMA", "TERMINAL_SCHEMA",
    "build_decode_evidence_terminal_bundle", "build_native_daily_max_manifest",
    "build_native_identity_index_from_decode_evidence",
    "expected_et_hourly_slots", "verify_arrival_transport_bindings",
    "validate_native_daily_max_manifest",
]
