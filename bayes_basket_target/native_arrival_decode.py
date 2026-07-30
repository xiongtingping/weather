"""Decode retained native model-arrival payloads into bounded target evidence.

This is the package-owned read-only target-lane implementation.  It never downloads a provider,
writes SQLite, touches the production archive, or invokes an order gateway.
Raw payloads remain content-addressed and are not copied into the output.  A
decoded point is not a calibration sample until the local-day curve coverage
gate passes.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.model_arrival import (  # noqa: E402
    ALLOWED_PROVIDERS,
    build_blocked_receipt,
    validate_model_arrival_capture,
)
from bayes_basket_target.market_registry import load_market_registry_snapshot  # noqa: E402
from bayes_basket_target.native_model_decode import (  # noqa: E402
    decode_native_temperature_points,
    materialize_native_daily_curves,
    prepare_native_temperature_payload,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_INPUT_FILES_PER_BATCH,
    load_bounded_json,
)
from bayes_basket_target.settlement_time import (  # noqa: E402
    assert_probability_timezone_matches_market,
    registry_settlement_timezone,
)


SCHEMA = "bayes_basket_target_native_model_arrival_decode_evidence_v1"
MAX_ICON_PREPARED_CACHE_BYTES = 256 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _blocked(path: str, reason: str) -> dict[str, Any]:
    return {
        "path": path,
        "status": "BLOCKED",
        "receipt": build_blocked_receipt(reason_codes=[reason], received_at_utc=_now()),
    }


def build(
    *,
    capture_paths: list[Path],
    as_of_utc: str,
    market_registry_snapshot_path: Path,
    grib_get_binary: str | None = None,
    cdo_binary: str | None = None,
    icon_grid_path: Path | None = None,
    icon_weights_path: Path | None = None,
) -> dict[str, Any]:
    if len(capture_paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("native_decode_capture_batch_size_exceeded")
    as_of = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ContractError("native_decode_as_of_timezone_required")
    registry = load_market_registry_snapshot(market_registry_snapshot_path)
    registry_rows = registry["rows"]
    points: list[dict[str, Any]] = []
    observed_timezones: set[str] = set()
    blocked: list[dict[str, Any]] = []
    input_artifacts: list[dict[str, str]] = []
    icon_prepared_cache: dict[str, Path] = {}
    icon_prepared_cache_bytes = 0
    with ExitStack() as prepared_stack:
        for raw_path in capture_paths:
            path = raw_path.expanduser().resolve()
            path_text = str(path)
            try:
                capture = load_bounded_json(path)
                if not isinstance(capture, Mapping):
                    raise ContractError("native_decode_capture_mapping_required")
                input_artifacts.append({"path": path_text, "sha256": _sha256(path)})
                if capture.get("status") != "CAPTURED" or not isinstance(capture.get("arrival"), Mapping):
                    blocked.append(_blocked(path_text, "native_decode_capture_not_captured"))
                    continue
                # Native decode is part of the P0 preparation plane and may
                # run before the target ET day opens. It never creates truth,
                # P1, production authority, or an order.
                arrival = validate_model_arrival_capture(dict(capture["arrival"]))
                provider_id = str(arrival.get("provider_id") or "").strip().lower()
                if provider_id not in ALLOWED_PROVIDERS:
                    raise ContractError("native_decode_provider_not_allowed")
                market_timezone = registry_settlement_timezone(
                    registry_rows,
                    station_id=str(arrival["station_id"]),
                    event_date=str(arrival["target_local_date"]),
                )
                assert_probability_timezone_matches_market(
                    probability_timezone=str(arrival.get("target_timezone") or ""),
                    market_timezone=market_timezone,
                )
                observed_timezones.add(market_timezone)
                received_at = datetime.fromisoformat(str(arrival["received_at_utc"]).replace("Z", "+00:00"))
                if received_at > as_of:
                    raise ContractError("native_decode_received_after_as_of")
                decision_at = datetime.fromisoformat(str(arrival["decision_at_utc"]).replace("Z", "+00:00"))
                if decision_at > as_of:
                    raise ContractError("native_decode_decision_after_as_of")
                receipt_path = Path(str(capture.get("transport_receipt_path") or "")).expanduser().resolve()
                receipt = load_bounded_json(receipt_path)
                if not isinstance(receipt, Mapping) or not isinstance(receipt.get("raw_receipts"), list):
                    raise ContractError("native_decode_transport_receipt_invalid")
                if _sha256(receipt_path) != str(arrival.get("receipt_sha256") or "").lower():
                    raise ContractError("native_decode_receipt_lineage_mismatch")
                expected_payloads = {str(item).lower() for item in arrival.get("raw_payload_sha256s") or []}
                for raw_receipt in receipt["raw_receipts"]:
                    if not isinstance(raw_receipt, Mapping):
                        raise ContractError("native_decode_raw_receipt_invalid")
                    raw_ref = Path(str(raw_receipt.get("raw_object_ref") or "")).expanduser().resolve()
                    raw_sha = str(raw_receipt.get("raw_sha256") or "").strip().lower()
                    if len(raw_sha) != 64 or _sha256(raw_ref) != raw_sha or raw_sha not in expected_payloads:
                        raise ContractError("native_decode_raw_lineage_mismatch")
                    prepared_path = None
                    if provider_id == "icon_global_official":
                        prepared_path = icon_prepared_cache.get(raw_sha)
                        if prepared_path is None:
                            prepared_path = prepared_stack.enter_context(
                                prepare_native_temperature_payload(
                                    raw_ref,
                                    provider_id=provider_id,
                                    cdo_binary=cdo_binary,
                                    icon_grid_path=icon_grid_path,
                                    icon_weights_path=icon_weights_path,
                                )
                            )
                            icon_prepared_cache_bytes += int(prepared_path.stat().st_size)
                            if icon_prepared_cache_bytes > MAX_ICON_PREPARED_CACHE_BYTES:
                                raise ContractError("native_icon_prepared_cache_budget_exceeded")
                            icon_prepared_cache[raw_sha] = prepared_path
                    decoded = decode_native_temperature_points(
                        raw_ref,
                        provider_id=provider_id,
                        station_id=str(arrival["station_id"]),
                        grib_get_binary=grib_get_binary,
                        cdo_binary=cdo_binary,
                        icon_grid_path=icon_grid_path,
                        icon_weights_path=icon_weights_path,
                        prepared_payload_path=prepared_path,
                    )
                    for decoded_point in decoded["points"]:
                        expected_valid = str(arrival.get("valid_time_utc") or "").replace("+00:00", "Z")
                        if expected_valid and decoded_point["grib_valid_time_utc"] != expected_valid:
                            raise ContractError("native_decode_valid_time_lineage_mismatch")
                        expected_run = str(arrival.get("run_initialization_utc") or "").replace("+00:00", "Z")
                        if decoded_point["grib_run_initialization_utc"] != expected_run:
                            raise ContractError("native_decode_run_initialization_lineage_mismatch")
                        points.append(
                            {
                                "station_id": str(arrival["station_id"]),
                                "event_date": str(arrival["target_local_date"]),
                                "provider_id": provider_id,
                                "model_id": str(arrival["model_id"]),
                                "model_run_initialization_utc": expected_run,
                                "valid_time_utc": decoded_point["grib_valid_time_utc"],
                                "temperature_2m_c": decoded_point["temperature_2m_c"],
                                "raw_message_sha256": raw_sha,
                                "receipt_sha256": str(arrival["receipt_sha256"]),
                                "published_at_utc": str(arrival["published_at_utc"]),
                                "received_at_utc": str(arrival["received_at_utc"]),
                                "decoder_id": decoded["decoder_id"],
                                "selector": decoded["selector"],
                                "latitude": decoded["latitude"],
                                "longitude": decoded["longitude"],
                                "coordinate_lineage": decoded["coordinate_lineage"],
                            }
                        )
            except (OSError, UnicodeDecodeError, ValueError, ContractError) as exc:
                # ContractError already carries the stable machine-readable
                # reason code. Keep transport/parser exceptions typed, but do
                # not leak the Python exception class into the target contract.
                reason = str(exc) if isinstance(exc, ContractError) else f"{type(exc).__name__}:{exc}"
                blocked.append(_blocked(path_text, reason))

    artifact_timezones = observed_timezones | {
        registry_settlement_timezone(
            registry_rows,
            station_id=str(point["station_id"]),
            event_date=str(point["event_date"]),
        )
        for point in points
    }
    if len(artifact_timezones) != 1:
        raise ContractError("native_decode_registry_timezone_not_unique")
    target_timezone = next(iter(artifact_timezones))
    curves = materialize_native_daily_curves(points, target_timezone=target_timezone)
    identity = {
        "schema": SCHEMA,
        "as_of_utc": str(as_of.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")),
        "target_timezone": target_timezone,
        "market_registry_snapshot_sha256": registry["snapshot_sha256"],
        "station_coordinate_registry": coordinate_registry_manifest(),
        "input_artifacts": input_artifacts,
        "decoded_point_count": len(points),
        "decoded_points": points,
        "blocked_captures": blocked,
        "curves": curves,
        "checks": {
            "native_provider_only": True,
            "official_ecmwf_gfs_icon_only": True,
            "decoded_points_have_raw_lineage": bool(points) and all(len(str(item["raw_message_sha256"])) == 64 for item in points),
            "decoded_points_have_coordinate_lineage": bool(points) and all(isinstance(item.get("coordinate_lineage"), Mapping) for item in points),
            "no_default_station_coordinate_fallback": True,
            "complete_curve_count_positive": curves["complete_curve_count"] > 0,
            "icon_prepared_payload_cache_bounded": icon_prepared_cache_bytes <= MAX_ICON_PREPARED_CACHE_BYTES,
            "promotion_allowed": False,
            "production_authority": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "icon_prepared_cache_bytes": icon_prepared_cache_bytes,
        },
        "boundary": {
            "target_only": True,
            "read_only_decode": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    return {**identity, "evidence_sha256": content_sha256(identity), "receipt_sha256": content_sha256({**identity, "evidence_sha256": content_sha256(identity)})}
