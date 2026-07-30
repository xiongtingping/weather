"""Resident official-NWP decode and P0 inbox producer.

The network adapters hand this module an explicit, bounded set of immutable
arrival-capture receipts.  This module owns the rest of the resident path:

``capture receipts -> native decode -> mature compatible run -> P0 inbox``.

It deliberately does not know about orders, gateways, capital, or live
authorization.  Stable capture and registry content produces no filesystem
write and no new probability checkpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .calibration import (
    resolve_active_ensemble_artifact,
    validate_formal_ensemble_artifact,
)
from .contracts import (
    ContractError,
    ModelEnsembleArtifact,
    content_sha256,
)
from .database import DEFAULT_TARGET_DB
from .market_registry import load_market_registry_snapshot
from .native_arrival_decode import build as decode_arrivals
from .native_model_decode import materialize_native_daily_curves
from .p0_producer import build_packets
from .resource_bounds import (
    MAX_INPUT_FILES_PER_BATCH,
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from .settlement_time import settlement_day_window
from .settlement_time import registry_settlement_timezone


SCHEMA = "bayes_basket_target_resident_nwp_producer_v1"
STATE_SCHEMA = "bayes_basket_target_resident_nwp_state_v1"
P0_FRESHNESS_SECONDS = 6 * 60 * 60
MAX_P0_FRESHNESS_SECONDS = P0_FRESHNESS_SECONDS
MAX_P0_INBOX_PACKETS = 256


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("resident_nwp_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("resident_nwp_timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


def _sha256_file(path: Path, *, max_bytes: int = MAX_JSON_INPUT_BYTES) -> str:
    resolved = path.expanduser().resolve()
    try:
        size = int(resolved.stat().st_size)
    except OSError as exc:
        raise ContractError("resident_nwp_capture_stat_failed") from exc
    if size <= 0 or size > int(max_bytes):
        raise ContractError("resident_nwp_capture_size_invalid")
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            remaining = size
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    raise ContractError("resident_nwp_capture_short_read")
                digest.update(block)
                remaining -= len(block)
            if handle.read(1):
                raise ContractError("resident_nwp_capture_grew_during_hash")
    except OSError as exc:
        raise ContractError("resident_nwp_capture_read_failed") from exc
    return digest.hexdigest()


def capture_set_identity(
    capture_paths: Sequence[Path],
    *,
    registry_snapshot_sha256: str,
    event_date: str,
) -> dict[str, Any]:
    """Return a deterministic identity without reading unbounded payloads."""

    if not capture_paths or len(capture_paths) > MAX_INPUT_FILES_PER_BATCH:
        raise ContractError("resident_nwp_capture_count_invalid")
    rows = []
    seen: set[str] = set()
    for raw in capture_paths:
        path = raw.expanduser().resolve()
        text = str(path)
        if text in seen:
            raise ContractError("resident_nwp_capture_path_duplicate")
        seen.add(text)
        rows.append({"path": text, "sha256": _sha256_file(path)})
        assert_process_memory_budget()
    rows.sort(key=lambda item: item["path"])
    identity = {
        "schema": "bayes_basket_target_resident_nwp_capture_set_v1",
        "event_date": str(event_date),
        "registry_snapshot_sha256": str(registry_snapshot_sha256),
        "capture_count": len(rows),
        "captures": rows,
    }
    return {**identity, "capture_set_sha256": content_sha256(identity)}


def _latest_received_at(decode: Mapping[str, Any]) -> str:
    points = decode.get("decoded_points")
    if not isinstance(points, list) or not points:
        raise ContractError("resident_nwp_decoded_points_missing")
    values = [
        str(row.get("received_at_utc") or "")
        for row in points
        if isinstance(row, Mapping) and row.get("received_at_utc")
    ]
    if len(values) != len(points):
        raise ContractError("resident_nwp_received_at_missing")
    return max(values, key=_parse_utc)


def _capture_materialization_clock(
    captures: Sequence[Mapping[str, Any]],
) -> str:
    values = []
    for item in captures:
        payload = load_bounded_json(Path(str(item["path"])))
        arrival = payload.get("arrival")
        if isinstance(arrival, Mapping):
            value = str(arrival.get("decision_at_utc") or "")
        elif payload.get("schema") == (
            "bayes_basket_target_native_model_arrival_decode_evidence_v1"
        ):
            value = max(
                (
                    str(row.get("received_at_utc") or "")
                    for row in payload.get("decoded_points") or []
                    if isinstance(row, Mapping)
                    and row.get("received_at_utc")
                ),
                default="",
                key=_parse_utc,
            )
        else:
            value = ""
        if value:
            values.append(value)
    if not values:
        raise ContractError("resident_nwp_capture_clock_missing")
    return max(values, key=_parse_utc)


def _decode_capture_bundle(
    *,
    capture_paths: list[Path],
    as_of_utc: str,
    market_registry_snapshot_path: Path,
) -> dict[str, Any]:
    """Merge native arrival receipts with validated predecoded model curves."""

    native_paths = []
    predecoded = []
    for path in capture_paths:
        payload = load_bounded_json(path)
        if payload.get("schema") == (
            "bayes_basket_target_native_model_arrival_decode_evidence_v1"
        ):
            identity = {
                key: value
                for key, value in payload.items()
                if key != "receipt_sha256"
            }
            if (
                str(payload.get("receipt_sha256") or "")
                != content_sha256(identity)
            ):
                raise ContractError(
                    "resident_nwp_predecoded_receipt_sha_mismatch"
                )
            predecoded.append((path, payload))
        else:
            native_paths.append(path)
    native = (
        decode_arrivals(
            capture_paths=native_paths,
            as_of_utc=as_of_utc,
            market_registry_snapshot_path=market_registry_snapshot_path,
        )
        if native_paths
        else None
    )
    registry = load_market_registry_snapshot(
        market_registry_snapshot_path
    )
    points = list(native.get("decoded_points") or []) if native else []
    input_artifacts = list(native.get("input_artifacts") or []) if native else []
    timezones = {
        str(native.get("target_timezone") or "")
    } if native else set()
    as_of = _parse_utc(as_of_utc)
    for path, payload in predecoded:
        checks = payload.get("checks")
        boundary = payload.get("boundary")
        decoded_points = payload.get("decoded_points")
        curves = payload.get("curves")
        if (
            not isinstance(checks, Mapping)
            or not isinstance(boundary, Mapping)
            or not isinstance(decoded_points, list)
            or not decoded_points
            or payload.get("blocked_captures") not in ([], None)
            or not isinstance(curves, Mapping)
            or curves.get("all_complete") is not True
            or any(
                boundary.get(key) is not False
                for key in (
                    "writes_production_database",
                    "calls_order_gateway",
                    "places_orders",
                    "live_submit_authorized",
                )
            )
        ):
            raise ContractError("resident_nwp_predecoded_boundary_invalid")
        for row in decoded_points:
            if not isinstance(row, Mapping):
                raise ContractError("resident_nwp_predecoded_point_invalid")
            station = str(row.get("station_id") or "").strip().upper()
            event_date = str(row.get("event_date") or "")
            market_timezone = registry_settlement_timezone(
                registry["rows"],
                station_id=station,
                event_date=event_date,
            )
            if _parse_utc(str(row.get("received_at_utc") or "")) > as_of:
                raise ContractError(
                    "resident_nwp_predecoded_received_after_as_of"
                )
            timezones.add(market_timezone)
            points.append(dict(row))
        input_artifacts.append({
            "path": str(path.resolve()),
            "sha256": _sha256_file(path),
        })
    timezones.discard("")
    if len(timezones) != 1 or not points:
        raise ContractError("resident_nwp_decode_timezone_scope_invalid")
    target_timezone = next(iter(timezones))
    curves = materialize_native_daily_curves(
        points,
        target_timezone=target_timezone,
    )
    if curves.get("all_complete") is not True:
        raise ContractError("resident_nwp_combined_curve_incomplete")
    identity = {
        "schema": (
            "bayes_basket_target_native_model_arrival_decode_evidence_v1"
        ),
        "as_of_utc": _stamp(as_of),
        "target_timezone": target_timezone,
        "market_registry_snapshot_sha256": registry["snapshot_sha256"],
        "input_artifacts": input_artifacts,
        "decoded_point_count": len(points),
        "decoded_points": points,
        "blocked_captures": [],
        "curves": curves,
        "checks": {
            "mixed_native_and_predecoded_provider_bundle": bool(
                native_paths and predecoded
            ),
            "all_curves_complete": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "boundary": {
            "target_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_order_authorized": False,
        },
    }
    evidence = content_sha256(identity)
    return {
        **identity,
        "evidence_sha256": evidence,
        "receipt_sha256": content_sha256({
            **identity,
            "evidence_sha256": evidence,
        }),
    }


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _fresh_until(
    *,
    materialized_at_utc: str,
    settlement_close_utc: str,
    freshness_seconds: int,
) -> str:
    materialized = _parse_utc(materialized_at_utc)
    close = _parse_utc(settlement_close_utc)
    ttl = int(freshness_seconds)
    if ttl <= 0 or ttl > MAX_P0_FRESHNESS_SECONDS:
        raise ContractError("resident_nwp_freshness_policy_invalid")
    if materialized >= close:
        raise ContractError("resident_nwp_materialization_after_settlement_close")
    return _stamp(min(materialized + timedelta(seconds=ttl), close))


def _validated_inbox(
    inbox: Mapping[str, Any],
    *,
    materialized_at_utc: str,
    fresh_until_utc: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate the resident boundary again before replacing the live inbox."""

    if inbox.get("schema") != "bayes_basket_target_p0_inbox_v1":
        raise ContractError("resident_nwp_p0_inbox_schema_invalid")
    rows = inbox.get("packets")
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) > MAX_P0_INBOX_PACKETS
    ):
        raise ContractError("resident_nwp_p0_inbox_packet_count_invalid")
    identity = {
        "schema": inbox["schema"],
        "generated_at_utc": inbox.get("generated_at_utc"),
        "packets": rows,
    }
    if str(inbox.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("resident_nwp_p0_inbox_content_sha_mismatch")

    selected_scopes: list[dict[str, str]] = []
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or row.get("source_class") != "official_forward_model_bundle"
            or not isinstance(row.get("packet"), Mapping)
        ):
            raise ContractError("resident_nwp_p0_forward_packet_invalid")
        packet = row["packet"]
        if str(row.get("packet_content_sha256") or "") != content_sha256(packet):
            raise ContractError("resident_nwp_p0_packet_content_sha_mismatch")
        if (
            packet.get("stage") != "P0"
            or str(packet.get("decision_at_utc") or "") != materialized_at_utc
            or str(packet.get("fresh_until_utc") or "") != fresh_until_utc
        ):
            raise ContractError("resident_nwp_p0_materialization_clock_mismatch")
        runs = packet.get("current_model_runs")
        if not isinstance(runs, list) or len(runs) < 2:
            raise ContractError("resident_nwp_formal_ensemble_minimum_not_met")
        models = tuple(sorted(
            str(run.get("model_id") or "").strip().upper()
            for run in runs
            if isinstance(run, Mapping)
        ))
        run_initializations = {
            str(run.get("run_init_utc") or "")
            for run in runs
            if isinstance(run, Mapping)
        }
        if (
            len(models) != len(runs)
            or len(set(models)) != len(models)
            or len(run_initializations) != 1
            or not next(iter(run_initializations))
        ):
            raise ContractError("resident_nwp_model_bundle_scope_invalid")
        if any(
            _parse_utc(str(run.get("received_at_utc") or ""))
            > _parse_utc(materialized_at_utc)
            for run in runs
        ):
            raise ContractError("resident_nwp_source_arrival_future_forbidden")

        raw_ensemble = packet.get("ensemble_artifact")
        raw_activations = packet.get("calibration_activations")
        if not isinstance(raw_ensemble, Mapping) or not isinstance(
            raw_activations, list
        ):
            raise ContractError("resident_nwp_ensemble_authority_missing")
        ensemble = ModelEnsembleArtifact.from_mapping(raw_ensemble)
        validate_formal_ensemble_artifact(ensemble)
        resolved, _activation = resolve_active_ensemble_artifact(
            activations=raw_activations,
            artifacts={ensemble.ensemble_artifact_id: ensemble},
            station_id=str(packet.get("station_id") or ""),
            season_bin=ensemble.season_bin,
            lead_bucket=ensemble.lead_bucket,
            run_cycle=ensemble.run_cycle,
            settlement_definition=ensemble.settlement_definition,
            available_models=models,
            decision_at_utc=materialized_at_utc,
            required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
        )
        if resolved.content_sha256 != ensemble.content_sha256:
            raise ContractError("resident_nwp_ensemble_activation_mismatch")
        selected_scopes.append({
            "station_id": str(packet.get("station_id") or ""),
            "event_date": str(packet.get("event_date") or ""),
            "run_initialization_utc": next(iter(run_initializations)),
            "model_ids": ",".join(models),
            "ensemble_artifact_id": ensemble.ensemble_artifact_id,
            "ensemble_artifact_sha256": ensemble.content_sha256,
        })
    selected_scopes.sort(
        key=lambda item: (item["station_id"], item["event_date"])
    )
    return dict(inbox), selected_scopes


def _assert_no_selected_run_rewind(
    state_path: Path,
    selected_scopes: Sequence[Mapping[str, str]],
) -> None:
    if not state_path.is_file():
        return
    previous = load_bounded_json(state_path)
    if not isinstance(previous, Mapping) or previous.get("schema") != STATE_SCHEMA:
        raise ContractError("resident_nwp_previous_state_invalid")
    previous_rows = previous.get("selected_bundle_scopes")
    if not isinstance(previous_rows, list):
        return
    previous_by_scope = {
        (str(row.get("station_id") or ""), str(row.get("event_date") or "")):
        str(row.get("run_initialization_utc") or "")
        for row in previous_rows
        if isinstance(row, Mapping)
    }
    for row in selected_scopes:
        key = (str(row["station_id"]), str(row["event_date"]))
        previous_run = previous_by_scope.get(key)
        if previous_run and _parse_utc(
            str(row["run_initialization_utc"])
        ) < _parse_utc(previous_run):
            raise ContractError("resident_nwp_selected_run_rewind_forbidden")


def _unchanged_state(
    state_path: Path,
    *,
    capture_set_sha256: str,
    inbox_path: Path,
    materialized_at_utc: str,
) -> dict[str, Any] | None:
    if not state_path.is_file() or not inbox_path.is_file():
        return None
    try:
        state = load_bounded_json(state_path)
        inbox = load_bounded_json(inbox_path)
    except Exception:
        return None
    if (
        not isinstance(state, Mapping)
        or state.get("schema") != STATE_SCHEMA
        or state.get("capture_set_sha256") != capture_set_sha256
        or not isinstance(inbox, Mapping)
        or inbox.get("schema") != "bayes_basket_target_p0_inbox_v1"
        or state.get("p0_inbox_content_sha256") != inbox.get("content_sha256")
        or not isinstance(inbox.get("packets"), list)
        or not inbox["packets"]
        or any(not isinstance(row, Mapping) for row in inbox["packets"])
        or any(
            _parse_utc(materialized_at_utc)
            >= _parse_utc(
                str((row.get("packet") or {}).get("fresh_until_utc") or "")
            )
            for row in inbox["packets"]
            if isinstance(row, Mapping)
        )
    ):
        return None
    return {
        "schema": SCHEMA,
        "state": "unchanged_zero_write",
        "event_date": str(state.get("event_date") or ""),
        "capture_set_sha256": capture_set_sha256,
        "p0_inbox_content_sha256": str(inbox.get("content_sha256") or ""),
        "packet_count": len(inbox.get("packets") or []),
        "materialized_at_utc": str(state.get("materialized_at_utc") or ""),
        "fresh_until_utc": str(state.get("fresh_until_utc") or ""),
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def refresh_resident_p0(
    *,
    capture_paths: Sequence[Path],
    registry_snapshot_path: Path,
    event_date: str,
    output_root: Path,
    inbox_path: Path,
    db_path: Path = DEFAULT_TARGET_DB,
    materialized_at_utc: str | None = None,
    freshness_seconds: int = P0_FRESHNESS_SECONDS,
    decode_builder: Callable[..., dict[str, Any]] = _decode_capture_bundle,
    packet_builder: Callable[..., dict[str, Any]] = build_packets,
) -> dict[str, Any]:
    """Compile one changed capture set and atomically refresh the P0 inbox."""

    assert_process_memory_budget()
    registry_path = registry_snapshot_path.expanduser().resolve()
    registry = load_market_registry_snapshot(registry_path)
    identity = capture_set_identity(
        capture_paths,
        registry_snapshot_sha256=str(registry["snapshot_sha256"]),
        event_date=event_date,
    )
    destination = output_root.expanduser().resolve() / str(event_date)
    state_path = destination / "state.json"
    inbox = inbox_path.expanduser().resolve()
    capture_clock = _capture_materialization_clock(identity["captures"])
    materialized_at = _stamp(
        _parse_utc(materialized_at_utc)
        if materialized_at_utc is not None
        else _parse_utc(capture_clock)
    )
    unchanged = _unchanged_state(
        state_path,
        capture_set_sha256=str(identity["capture_set_sha256"]),
        inbox_path=inbox,
        materialized_at_utc=materialized_at,
    )
    if unchanged is not None:
        return unchanged

    decode = decode_builder(
        capture_paths=[Path(item["path"]) for item in identity["captures"]],
        as_of_utc=capture_clock,
        market_registry_snapshot_path=registry_path,
    )
    if not isinstance(decode, Mapping):
        raise ContractError("resident_nwp_decode_mapping_required")
    source_bundle_ready_at = _latest_received_at(decode)
    if _parse_utc(source_bundle_ready_at) > _parse_utc(materialized_at):
        raise ContractError("resident_nwp_source_arrival_future_forbidden")
    timezone_name = str(decode.get("target_timezone") or "")
    window = settlement_day_window(event_date, timezone_name)
    fresh_until = _fresh_until(
        materialized_at_utc=materialized_at,
        settlement_close_utc=str(window["target_day_end_utc"]),
        freshness_seconds=freshness_seconds,
    )

    destination.mkdir(parents=True, exist_ok=True)
    decode_path = destination / "native_decode.json"
    write_bounded_json_if_changed(decode_path, dict(decode))
    candidate_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".resident-p0-candidate-",
            suffix=".json",
            dir=destination,
            delete=False,
        ) as handle:
            candidate_path = Path(handle.name)
        candidate_path.unlink()
        build = packet_builder(
            db_path=db_path,
            decode_evidence_path=[decode_path],
            registry_snapshot_path=registry_path,
            event_date=event_date,
            decision_at_utc=materialized_at,
            fresh_until_utc=fresh_until,
            output_dir=destination / "p0",
            inbox_path=candidate_path,
            emit_packet_files=False,
        )
        if not candidate_path.is_file():
            raise ContractError("resident_nwp_p0_candidate_inbox_missing")
        candidate = load_bounded_json(candidate_path)
        if not isinstance(candidate, Mapping):
            raise ContractError("resident_nwp_p0_candidate_inbox_invalid")
        validated_inbox, selected_scopes = _validated_inbox(
            candidate,
            materialized_at_utc=materialized_at,
            fresh_until_utc=fresh_until,
        )
        _assert_no_selected_run_rewind(state_path, selected_scopes)
        inbox_wrote = write_bounded_json_if_changed(inbox, validated_inbox)
    finally:
        if candidate_path is not None:
            candidate_path.unlink(missing_ok=True)
    if not isinstance(build, Mapping):
        raise ContractError("resident_nwp_p0_build_mapping_required")
    p0_state = build.get("p0_inbox")
    if not isinstance(p0_state, Mapping):
        raise ContractError("resident_nwp_p0_inbox_receipt_missing")
    state_identity = {
        "schema": STATE_SCHEMA,
        "event_date": event_date,
        "capture_set_sha256": identity["capture_set_sha256"],
        "registry_snapshot_sha256": registry["snapshot_sha256"],
        "materialized_at_utc": materialized_at,
        "source_bundle_ready_at_utc": source_bundle_ready_at,
        "fresh_until_utc": fresh_until,
        "native_decode_receipt_sha256": str(
            decode.get("receipt_sha256") or decode.get("evidence_sha256") or ""
        ),
        "p0_inbox_content_sha256": str(validated_inbox["content_sha256"]),
        "packet_count": len(validated_inbox["packets"]),
        "selected_bundle_scopes": selected_scopes,
    }
    state = {**state_identity, "content_sha256": content_sha256(state_identity)}
    state_wrote = write_bounded_json_if_changed(state_path, state)
    assert_process_memory_budget()
    return {
        "schema": SCHEMA,
        "state": (
            "updated"
            if p0_state.get("state") == "updated" or state_wrote
            else "unchanged_zero_write"
        ),
        "event_date": event_date,
        "capture_set_sha256": identity["capture_set_sha256"],
        "decision_at_utc": materialized_at,
        "source_bundle_ready_at_utc": source_bundle_ready_at,
        "fresh_until_utc": fresh_until,
        "packet_count": len(validated_inbox["packets"]),
        "blocked_count": int(build.get("blocked_count") or 0),
        "selected_bundle_scopes": selected_scopes,
        "p0_inbox": {
            **dict(p0_state),
            "state": "updated" if inbox_wrote else "unchanged_zero_write",
            "path": str(inbox),
            "content_sha256": str(validated_inbox["content_sha256"]),
            "packet_count": len(validated_inbox["packets"]),
        },
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


__all__ = [
    "SCHEMA",
    "STATE_SCHEMA",
    "capture_set_identity",
    "refresh_resident_p0",
]
