"""Bounded resident TAF input producer for the single P0-M checkpoint.

This module does not calculate probabilities and does not write the target
database.  It validates an already activated strict-OOS
``TafRevisionArtifact`` and one or more native forward ``ForecastRevision``
inboxes, then atomically publishes the exact two inputs consumed by
``TargetProductionWorker._materialize_resident_p0m``.

The original receipt clocks and capture bodies are preserved byte-for-value.
Missing formal inputs return explicit ``not_configured`` evidence and never
create a production-looking artifact or inbox.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .event_contracts import ForecastRevision, validate_forecast_revision_chain
from .resource_bounds import (
    MIB,
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from .settlement_time import settlement_day_window
from .taf_oos_contract import OBSERVATIONAL_CANDIDATE_NOT_OOS
from .taf_calibration import MIN_TAF_CALIBRATION_SAMPLE_N
from .taf_revision import (
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
    TafRevisionArtifact,
)
from .fleet import expected_station_count


SCHEMA = "bayes_basket_target_resident_taf_producer_v1"
INBOX_SCHEMA = "bayes_basket_target_taf_revision_inbox_v1"
MAX_STATIONS = expected_station_count()
MAX_SOURCE_INBOXES = expected_station_count()
MAX_CAPTURES = 256
MAX_SOURCE_INBOX_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024
# Up to 45 source inboxes (2 MiB each) plus one strict-OOS artifact may be
# joined atomically.  The per-file caps remain unchanged.
MAX_TOTAL_INPUT_BYTES = 128 * MIB
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_TEMPORARY_BYTES = MAX_OUTPUT_BYTES
CORRELATION_TOLERANCE = 1e-12


def _parse_utc(name: str, value: Any) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _bounded_file_size(path: Path, *, limit: int, name: str) -> int:
    resolved = path.expanduser().resolve()
    try:
        size = int(resolved.stat().st_size)
    except OSError as exc:
        raise ContractError(f"{name}_stat_failed") from exc
    if size <= 0 or size > int(limit):
        raise ContractError(f"{name}_size_invalid")
    return size


def _not_configured(
    *,
    as_of_utc: str,
    missing: Sequence[str],
    source_inbox_paths: Sequence[Path],
    artifact_path: Path | None,
) -> dict[str, Any]:
    identity = {
        "schema": SCHEMA,
        "state": "not_configured",
        "reason": "resident_taf_formal_input_missing",
        "evidence_role": OBSERVATIONAL_CANDIDATE_NOT_OOS,
        "as_of_utc": as_of_utc,
        "missing": sorted(set(str(item) for item in missing)),
        "source_inbox_paths": sorted(
            str(path.expanduser().resolve()) for path in source_inbox_paths
        ),
        "artifact_path": (
            str(artifact_path.expanduser().resolve())
            if artifact_path is not None
            else None
        ),
        "writes_production_inputs": False,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}


def _validate_artifact(
    raw: Mapping[str, Any],
    *,
    as_of_utc: str,
) -> TafRevisionArtifact:
    artifact = TafRevisionArtifact.from_mapping(raw)
    as_of = _parse_utc("resident_taf_as_of_utc", as_of_utc)
    valid_from = _parse_utc(
        "resident_taf_artifact_valid_from_utc", artifact.valid_from_utc
    )
    valid_until = _parse_utc(
        "resident_taf_artifact_valid_until_utc", artifact.valid_until_utc
    )
    trained_through = _parse_utc(
        "resident_taf_artifact_trained_through_utc",
        artifact.trained_through_utc,
    )
    if not (valid_from <= as_of < valid_until):
        raise ContractError("resident_taf_artifact_expired_or_not_yet_valid")
    if trained_through >= valid_from:
        raise ContractError("resident_taf_artifact_not_strict_oos")
    if artifact.sample_n < MIN_TAF_CALIBRATION_SAMPLE_N:
        raise ContractError("resident_taf_artifact_sample_n_below_strict_oos_min")
    if (
        artifact.nwp_combination_artifact_kind
        != TAF_NWP_CROSSFIT_ARTIFACT_KIND
        or artifact.nwp_live_compatibility_contract_version
        != TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        or not artifact.nwp_live_compatibility_fingerprint_sha256
        or not artifact.nwp_live_compatibility_evidence_sha256
        or artifact.nwp_live_compatibility_validated is not True
    ):
        raise ContractError(
            "resident_taf_nwp_live_compatibility_bridge_missing"
        )
    expected_discount = max(
        0.20,
        min(1.0, 1.0 - abs(artifact.oos_taf_nwp_residual_correlation)),
    )
    if abs(artifact.correlation_discount - expected_discount) > (
        CORRELATION_TOLERANCE
    ):
        raise ContractError("resident_taf_artifact_correlation_discount_invalid")
    return artifact


def _capture_identity(capture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in capture.items()
        if key != "content_sha256"
    }


def _validate_taf_fields(
    raw: Any,
    *,
    wind_speed_cap_mps: float,
) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise ContractError("resident_taf_bare_taf_forbidden")
    if set(raw) != {
        "tx_c",
        "cloud_cover_fraction",
        "precipitation_probability",
        "wind_speed_mps",
    }:
        raise ContractError("resident_taf_physical_fields_incomplete")
    try:
        values = {name: float(value) for name, value in raw.items()}
    except (TypeError, ValueError) as exc:
        raise ContractError("resident_taf_physical_fields_invalid") from exc
    if not (-100.0 <= values["tx_c"] <= 70.0):
        raise ContractError("resident_taf_tx_c_out_of_range")
    for name in ("cloud_cover_fraction", "precipitation_probability"):
        if not (0.0 <= values[name] <= 1.0):
            raise ContractError(f"resident_taf_{name}_out_of_range")
    if not (0.0 <= values["wind_speed_mps"] <= max(150.0, wind_speed_cap_mps)):
        raise ContractError("resident_taf_wind_speed_mps_out_of_range")
    return values


def _validate_capture(
    raw: Any,
    *,
    artifact: TafRevisionArtifact,
    as_of_utc: str,
) -> tuple[dict[str, Any], ForecastRevision]:
    if not isinstance(raw, Mapping):
        raise ContractError("resident_taf_capture_invalid")
    capture = dict(raw)
    supplied_sha = str(capture.get("content_sha256") or "")
    if supplied_sha != content_sha256(_capture_identity(capture)):
        raise ContractError("resident_taf_capture_content_sha_mismatch")
    boundary = capture.get("boundary")
    if (
        capture.get("source_class") != "official_forward_native_receipt"
        or not isinstance(boundary, Mapping)
        or boundary.get("native_local_receipt") is not True
        or boundary.get("retrospective_replay_only") is not False
    ):
        raise ContractError("resident_taf_forward_native_receipt_required")
    if (
        boundary.get("writes_production_db") is not False
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
    ):
        raise ContractError("resident_taf_capture_boundary_invalid")
    revision_raw = capture.get("forecast_revision")
    if not isinstance(revision_raw, Mapping):
        raise ContractError("resident_taf_forecast_revision_missing")
    revision = ForecastRevision.from_mapping(revision_raw)
    if (
        str(capture.get("station_id") or "").strip().upper()
        != revision.station_id
        or str(capture.get("event_date") or "") != revision.event_date
    ):
        raise ContractError("resident_taf_capture_scope_mismatch")
    if revision.station_id != artifact.station_id:
        raise ContractError("resident_taf_artifact_station_mismatch")
    if (
        str(capture.get("settlement_timezone") or "")
        != artifact.settlement_timezone
    ):
        raise ContractError("resident_taf_settlement_timezone_mismatch")
    if _parse_utc(
        "resident_taf_revision_received_at_utc", revision.received_at_utc
    ) > _parse_utc("resident_taf_as_of_utc", as_of_utc):
        raise ContractError("resident_taf_future_arrival_forbidden")
    window = settlement_day_window(
        revision.event_date,
        artifact.settlement_timezone,
    )
    window_start = _parse_utc(
        "resident_taf_settlement_window_start",
        window["target_day_start_utc"],
    )
    window_end = _parse_utc(
        "resident_taf_settlement_window_end",
        window["target_day_end_utc"],
    )
    revision_start = _parse_utc(
        "resident_taf_revision_valid_from_utc", revision.valid_from_utc
    )
    revision_end = _parse_utc(
        "resident_taf_revision_valid_until_utc", revision.valid_until_utc
    )
    if revision_start >= window_end or revision_end <= window_start:
        raise ContractError("resident_taf_revision_settlement_window_mismatch")
    if _parse_utc(
        "resident_taf_revision_issued_at_utc", revision.issued_at_utc
    ) >= window_end:
        raise ContractError("resident_taf_revision_issued_after_settlement_close")
    local_received = str(capture.get("local_received_at_utc") or "")
    if local_received != revision.received_at_utc:
        raise ContractError("resident_taf_local_receipt_clock_mismatch")
    _validate_taf_fields(
        capture.get("taf_fields"),
        wind_speed_cap_mps=artifact.wind_speed_cap_mps,
    )
    raw_taf = str(capture.get("raw_taf") or "").strip()
    raw_sha = str(capture.get("raw_taf_sha256") or "")
    if not raw_taf or len(raw_sha) != 64:
        raise ContractError("resident_taf_raw_lineage_missing")
    if hashlib.sha256(raw_taf.encode("utf-8")).hexdigest() != raw_sha:
        raise ContractError("resident_taf_raw_content_sha_mismatch")
    if revision.source_object_sha256 != raw_sha:
        raise ContractError("resident_taf_raw_revision_sha_mismatch")
    return capture, revision


def _validate_inbox(
    raw: Any,
    *,
    artifact: TafRevisionArtifact,
    as_of_utc: str,
) -> list[tuple[dict[str, Any], ForecastRevision]]:
    if not isinstance(raw, Mapping) or raw.get("schema") != INBOX_SCHEMA:
        raise ContractError("resident_taf_source_inbox_schema_invalid")
    captures = raw.get("captures")
    if (
        not isinstance(captures, list)
        or not captures
        or len(captures) > MAX_CAPTURES
        or int(raw.get("capture_count", -1)) != len(captures)
    ):
        raise ContractError("resident_taf_source_inbox_capture_count_invalid")
    boundary = raw.get("boundary")
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("forward_only") is not True
        or int(boundary.get("max_captures", -1)) != MAX_CAPTURES
        or boundary.get("calls_order_gateway") is not False
        or boundary.get("places_orders") is not False
    ):
        raise ContractError("resident_taf_source_inbox_boundary_invalid")
    evidence_role = str(raw.get("evidence_role") or "").strip().lower()
    if evidence_role and evidence_role != OBSERVATIONAL_CANDIDATE_NOT_OOS:
        raise ContractError("resident_taf_forward_evidence_role_invalid")
    identity = {
        str(key): value
        for key, value in raw.items()
        if key != "content_sha256"
    }
    if str(raw.get("content_sha256") or "") != content_sha256(identity):
        raise ContractError("resident_taf_source_inbox_content_sha_mismatch")
    return [
        _validate_capture(capture, artifact=artifact, as_of_utc=as_of_utc)
        for capture in captures
    ]


def _merge_captures(
    groups: Sequence[Sequence[tuple[dict[str, Any], ForecastRevision]]],
) -> list[dict[str, Any]]:
    by_revision: dict[str, tuple[dict[str, Any], ForecastRevision]] = {}
    for group in groups:
        for capture, revision in group:
            previous = by_revision.get(revision.revision_id)
            if previous is not None:
                if previous[0] != capture:
                    raise ContractError("resident_taf_revision_id_content_conflict")
                continue
            by_revision[revision.revision_id] = (capture, revision)
    by_scope: dict[tuple[str, str], list[ForecastRevision]] = {}
    for _capture, revision in by_revision.values():
        by_scope.setdefault(
            (revision.station_id, revision.event_date), []
        ).append(revision)
    if not by_scope:
        raise ContractError("resident_taf_capture_set_empty")
    stations = {station for station, _day in by_scope}
    if len(stations) > MAX_STATIONS:
        raise ContractError("resident_taf_station_count_exceeded")
    for revisions in by_scope.values():
        validate_forecast_revision_chain(revisions)
    ordered = sorted(
        by_revision.values(),
        key=lambda item: (
            item[1].station_id,
            item[1].event_date,
            item[1].sequence,
            item[1].revision_id,
        ),
    )
    if len(ordered) > MAX_CAPTURES:
        raise ContractError("resident_taf_merged_capture_count_exceeded")
    return [capture for capture, _revision in ordered]


def _load_existing_if_compatible(
    path: Path,
    *,
    artifact: TafRevisionArtifact,
    as_of_utc: str,
) -> list[tuple[dict[str, Any], ForecastRevision]]:
    if not path.is_file():
        return []
    raw = load_bounded_json(path, limit=MAX_OUTPUT_BYTES)
    return _validate_inbox(raw, artifact=artifact, as_of_utc=as_of_utc)


def _same_published_artifact(
    path: Path,
    *,
    artifact: TafRevisionArtifact,
) -> bool:
    if not path.is_file():
        return False
    raw = load_bounded_json(path, limit=MAX_ARTIFACT_BYTES)
    if not isinstance(raw, Mapping):
        raise ContractError("resident_taf_existing_artifact_mapping_required")
    existing = TafRevisionArtifact.from_mapping(raw)
    return existing.content_sha256 == artifact.content_sha256


def refresh_resident_taf_inputs(
    *,
    source_inbox_paths: Sequence[Path],
    artifact_path: Path | None,
    output_inbox_path: Path,
    output_artifact_path: Path,
    as_of_utc: str,
) -> dict[str, Any]:
    """Validate and atomically publish resident P0-M worker inputs.

    Existing target captures are merged with new native captures by revision
    id.  The full linear chain is retained, so TAF -> AMD -> COR is consumed
    by the worker as successive replacements rooted in the same P0.
    """

    assert_process_memory_budget()
    _parse_utc("resident_taf_as_of_utc", as_of_utc)
    paths = tuple(Path(path).expanduser().resolve() for path in source_inbox_paths)
    missing: list[str] = []
    if artifact_path is None or not artifact_path.expanduser().resolve().is_file():
        missing.append("activated_strict_oos_taf_artifact")
    if not paths:
        missing.append("forward_forecast_revision_inbox")
    else:
        missing.extend(
            f"forward_forecast_revision_inbox:{path}"
            for path in paths
            if not path.is_file()
        )
    if missing:
        return _not_configured(
            as_of_utc=as_of_utc,
            missing=missing,
            source_inbox_paths=paths,
            artifact_path=artifact_path,
        )
    if len(paths) > MAX_SOURCE_INBOXES or len(set(paths)) != len(paths):
        raise ContractError("resident_taf_source_inbox_count_invalid")

    resolved_artifact = artifact_path.expanduser().resolve()
    total_bytes = _bounded_file_size(
        resolved_artifact,
        limit=MAX_ARTIFACT_BYTES,
        name="resident_taf_artifact",
    )
    for path in paths:
        total_bytes += _bounded_file_size(
            path,
            limit=MAX_SOURCE_INBOX_BYTES,
            name="resident_taf_source_inbox",
        )
        if total_bytes > MAX_TOTAL_INPUT_BYTES:
            raise ContractError("resident_taf_total_input_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=total_bytes * 3)

    artifact_raw = load_bounded_json(
        resolved_artifact, limit=MAX_ARTIFACT_BYTES
    )
    if not isinstance(artifact_raw, Mapping):
        raise ContractError("resident_taf_artifact_mapping_required")
    artifact = _validate_artifact(artifact_raw, as_of_utc=as_of_utc)
    groups = [
        _validate_inbox(
            load_bounded_json(path, limit=MAX_SOURCE_INBOX_BYTES),
            artifact=artifact,
            as_of_utc=as_of_utc,
        )
        for path in paths
    ]
    output_inbox = output_inbox_path.expanduser().resolve()
    output_artifact = output_artifact_path.expanduser().resolve()
    if output_inbox == output_artifact:
        raise ContractError("resident_taf_output_paths_must_be_distinct")

    # Preserve already published forward revisions.  This gives incremental
    # source inboxes linear append semantics and prevents a source rewind from
    # silently deleting AMD/COR history.
    had_published_artifact = output_artifact.is_file()
    same_published_artifact = _same_published_artifact(
        output_artifact,
        artifact=artifact,
    )
    if same_published_artifact:
        existing = _load_existing_if_compatible(
            output_inbox,
            artifact=artifact,
            as_of_utc=as_of_utc,
        )
        if existing:
            groups.insert(0, existing)
    captures = _merge_captures(groups)
    inbox_identity = {
        "schema": INBOX_SCHEMA,
        "capture_count": len(captures),
        "captures": captures,
        "evidence_role": OBSERVATIONAL_CANDIDATE_NOT_OOS,
        "boundary": {
            "forward_only": True,
            "max_captures": MAX_CAPTURES,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    inbox = {
        **inbox_identity,
        "content_sha256": content_sha256(inbox_identity),
    }

    # Publish the authority first.  On a first install the worker still sees
    # no inbox until the pair is complete; on any mismatch it fails closed.
    # Both individual files use same-directory temp + os.replace and retain no
    # temporary payloads.
    artifact_wrote = write_bounded_json_if_changed(
        output_artifact,
        artifact.to_dict(),
        limit=MAX_OUTPUT_BYTES,
    )
    inbox_wrote = write_bounded_json_if_changed(
        output_inbox,
        inbox,
        limit=MAX_OUTPUT_BYTES,
    )
    assert_process_memory_budget()
    scopes = sorted({
        (
            str(capture["station_id"]),
            str(capture["event_date"]),
        )
        for capture in captures
    })
    identity = {
        "schema": SCHEMA,
        "state": (
            "updated" if artifact_wrote or inbox_wrote
            else "unchanged_zero_write"
        ),
        "as_of_utc": as_of_utc,
        "artifact_id": artifact.artifact_id,
        "activation_id": artifact.activation_id,
        "artifact_content_sha256": artifact.content_sha256,
        "artifact_generation_transition": (
            had_published_artifact and not same_published_artifact
        ),
        "artifact_output_path": str(output_artifact),
        "inbox_output_path": str(output_inbox),
        "inbox_content_sha256": inbox["content_sha256"],
        "capture_count": len(captures),
        "station_count": len({station for station, _day in scopes}),
        "scopes": [
            {"station_id": station, "event_date": day}
            for station, day in scopes
        ],
        "checkpoint_contract": {
            "stage": "P0-M",
            "single_atomic_checkpoint": True,
            "tx_calibration": True,
            "cloud_precipitation_wind_secondary_confirmation": True,
            "second_probability_center_forbidden": True,
            "revision_chain_root": "same_P0_checkpoint",
        },
        "resource_bounds": {
            "max_stations": MAX_STATIONS,
            "max_source_inboxes": MAX_SOURCE_INBOXES,
            "max_captures": MAX_CAPTURES,
            "max_source_inbox_bytes": MAX_SOURCE_INBOX_BYTES,
            "max_total_input_bytes": MAX_TOTAL_INPUT_BYTES,
            "max_output_bytes": MAX_OUTPUT_BYTES,
            "max_temporary_bytes": MAX_TEMPORARY_BYTES,
        },
        "writes_production_inputs": artifact_wrote or inbox_wrote,
        "writes_target_database": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    return {**identity, "evidence_sha256": content_sha256(identity)}


__all__ = [
    "INBOX_SCHEMA",
    "MAX_CAPTURES",
    "MAX_SOURCE_INBOXES",
    "MAX_STATIONS",
    "SCHEMA",
    "refresh_resident_taf_inputs",
]
