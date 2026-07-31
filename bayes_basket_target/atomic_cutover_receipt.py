"""Bounded, content-addressed contracts for the M9-06 atomic cutover.

This module is deliberately independent of the probability/model modules.  It
only seals the maintenance-window evidence and validates the safety boundary
around the target-only cutover.  A receipt is an audit object, never an order
or a live-authorizing object.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "bayes_basket_target_m9_06_atomic_cutover_receipt_v1"
STAGE_SCHEMA = "bayes_basket_target_m9_06_stage_receipt_v1"
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
MAX_RECOVERY_BYTES = 256 * 1024 * 1024
MAX_STAGE_COUNT = 32

REQUIRED_STAGES = (
    "preflight",
    "writer_quiescence",
    "wal_checkpoint",
    "schema_activation_replace",
    "resident_restart",
    "protected_pid_postcheck",
    "resident_acceptance_round_1",
    "resident_acceptance_round_2",
    "recovery_cleanup",
)

PROTECTED_SERVICES = (
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)

FALSE_BOUNDARY_KEYS = {
    "authorizes_order",
    "calls_order_gateway",
    "live_decision_write_authorized",
    "live_order_authorized",
    "live_submit_authorized",
    "places_orders",
    "writes_production_db",
    "legacy_fallback_used",
    "old_fallback_used",
}
ZERO_BOUNDARY_KEYS = {
    "orders_submitted",
    "exchange_orders_submitted",
    "gateway_writes",
}

FORWARD_OOS_BOUNDARY = {
    "blocks_production_no_live": False,
    "blocks_formal_promotion_or_live": True,
}


class AtomicCutoverReceiptError(ValueError):
    """Raised when M9-06 evidence cannot be trusted."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AtomicCutoverReceiptError("atomic_receipt_not_json") from exc


def content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _without_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): child for key, child in value.items() if key != "content_sha256"}


def bounded_json_bytes(value: Any, *, limit: int = MAX_RECEIPT_BYTES) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    ).encode("utf-8") + b"\n"
    if len(raw) > int(limit):
        raise AtomicCutoverReceiptError("atomic_receipt_bytes_exceeded")
    return raw


def write_receipt_if_changed(
    path: Path | str,
    value: Mapping[str, Any],
    *,
    limit: int = MAX_RECEIPT_BYTES,
) -> bool:
    """Write a bounded receipt atomically; identical bytes cause zero writes."""

    destination_raw = Path(path).expanduser()
    if destination_raw.is_symlink():
        raise AtomicCutoverReceiptError("atomic_receipt_output_symlink_forbidden")
    destination = destination_raw.resolve()
    raw = bounded_json_bytes(value, limit=limit)
    if destination.is_file():
        try:
            if destination.stat().st_size == len(raw) and destination.read_bytes() == raw:
                return False
        except OSError as exc:
            raise AtomicCutoverReceiptError("atomic_receipt_existing_read_failed") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _boundary_errors(value: Any, *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FALSE_BOUNDARY_KEYS and child is not False:
                errors.append(f"{child_path}_must_be_false")
            if key in ZERO_BOUNDARY_KEYS:
                try:
                    is_zero = float(child) == 0.0
                except (TypeError, ValueError):
                    is_zero = False
                if not is_zero:
                    errors.append(f"{child_path}_must_be_zero")
            errors.extend(_boundary_errors(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_boundary_errors(child, path=f"{path}[{index}]"))
    return errors


def seal_stage(
    stage: str,
    payload: Mapping[str, Any],
    *,
    source_path: str | None = None,
    source_sha256: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Create one immutable stage receipt without embedding large source data."""

    stage_name = str(stage or "").strip()
    if stage_name not in REQUIRED_STAGES:
        raise AtomicCutoverReceiptError("atomic_receipt_stage_invalid")
    boundary_errors = _boundary_errors(payload, path="$.payload")
    if boundary_errors:
        raise AtomicCutoverReceiptError(
            "atomic_receipt_boundary_invalid:" + ",".join(boundary_errors)
        )
    payload_sha = content_sha256(payload)
    fact_keys = (
        "quiescent",
        "all_core_services_absent",
        "writer_process_hits",
        "probe_errors",
        "wal_bytes_after",
        "checkpointed_frames",
        "log_frames",
        "target_writer_restarted",
        "pid_stability",
        "station_scope",
        "recovery",
    )
    facts = {
        key: payload[key]
        for key in fact_keys
        if key in payload
    }
    identity: dict[str, Any] = {
        "schema": STAGE_SCHEMA,
        "stage": stage_name,
        "state": str(state or payload.get("state") or "OBSERVED"),
        "payload_content_sha256": payload_sha,
        "payload_schema": str(payload.get("schema") or ""),
        "payload_state": str(payload.get("state") or ""),
        "facts": facts,
        "forward_oos_boundary": dict(FORWARD_OOS_BOUNDARY),
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    if source_path is not None:
        identity["source_path"] = str(Path(source_path).expanduser().resolve())
    if source_sha256 is not None:
        sha = str(source_sha256).lower()
        if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
            raise AtomicCutoverReceiptError("atomic_receipt_source_sha256_invalid")
        identity["source_sha256"] = sha
    identity["content_sha256"] = content_sha256(identity)
    return identity


def validate_sealed_receipt(
    receipt: Mapping[str, Any],
    *,
    schema: str | None = None,
    stage: str | None = None,
) -> None:
    if not isinstance(receipt, Mapping):
        raise AtomicCutoverReceiptError("atomic_receipt_mapping_required")
    supplied = str(receipt.get("content_sha256") or "").lower()
    if len(supplied) != 64 or content_sha256(_without_hash(receipt)) != supplied:
        raise AtomicCutoverReceiptError("atomic_receipt_content_sha256_mismatch")
    if schema is not None and receipt.get("schema") != schema:
        raise AtomicCutoverReceiptError("atomic_receipt_schema_mismatch")
    if stage is not None and receipt.get("stage") != stage:
        raise AtomicCutoverReceiptError("atomic_receipt_stage_mismatch")
    if receipt.get("production_no_live") is not True:
        raise AtomicCutoverReceiptError("atomic_receipt_production_mode_invalid")
    if receipt.get("calls_order_gateway") is not False:
        raise AtomicCutoverReceiptError("atomic_receipt_gateway_boundary_invalid")
    if receipt.get("live_submit_authorized") is not False:
        raise AtomicCutoverReceiptError("atomic_receipt_live_boundary_invalid")
    if float(receipt.get("orders_submitted") or 0) != 0.0:
        raise AtomicCutoverReceiptError("atomic_receipt_orders_nonzero")
    if receipt.get("forward_oos_boundary") != FORWARD_OOS_BOUNDARY:
        raise AtomicCutoverReceiptError("atomic_receipt_forward_oos_boundary_invalid")
    errors = _boundary_errors(receipt)
    if errors:
        raise AtomicCutoverReceiptError(
            "atomic_receipt_boundary_invalid:" + ",".join(errors)
        )


def _normalize_scope(station_ids: Sequence[str]) -> tuple[str, ...]:
    raw = tuple(str(value or "").strip().upper() for value in station_ids)
    if not raw or any(not value for value in raw) or len(set(raw)) != len(raw):
        raise AtomicCutoverReceiptError("atomic_receipt_station_scope_invalid")
    return tuple(sorted(raw))


def build_cutover_receipt(
    *,
    station_ids: Sequence[str],
    stage_receipts: Mapping[str, Mapping[str, Any]],
    release: Mapping[str, Any],
    protected_pids_before: Mapping[str, int],
    protected_pids_after: Mapping[str, int],
    target_pid_before: int,
    target_pid_after: int,
    recovery: Mapping[str, Any],
    target_database_fingerprint: Mapping[str, Any] | None = None,
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    """Seal the complete M9-06 stage chain and enforce its exit contract."""

    scope = _normalize_scope(station_ids)
    if len(stage_receipts) > MAX_STAGE_COUNT:
        raise AtomicCutoverReceiptError("atomic_receipt_stage_count_exceeded")
    missing = [stage for stage in REQUIRED_STAGES if stage not in stage_receipts]
    if missing:
        raise AtomicCutoverReceiptError(
            "atomic_receipt_required_stage_missing:" + ",".join(missing)
        )
    stage_hashes: dict[str, str] = {}
    for stage in REQUIRED_STAGES:
        receipt = stage_receipts[stage]
        validate_sealed_receipt(receipt, schema=STAGE_SCHEMA, stage=stage)
        if str(receipt.get("state") or "").upper().startswith(("BLOCKED", "FAIL")):
            raise AtomicCutoverReceiptError(
                "atomic_receipt_stage_not_successful:" + stage
            )
        stage_hashes[stage] = str(receipt["content_sha256"])
    if stage_receipts["writer_quiescence"].get("facts", {}).get("quiescent") is not True:
        raise AtomicCutoverReceiptError("atomic_receipt_writer_not_quiescent")
    if stage_receipts["resident_restart"].get("facts", {}).get("target_writer_restarted") is not True:
        raise AtomicCutoverReceiptError("atomic_receipt_target_writer_not_restarted")
    if stage_receipts["protected_pid_postcheck"].get("facts", {}).get("pid_stability") is not True:
        raise AtomicCutoverReceiptError("atomic_receipt_protected_pid_drift")
    if set(protected_pids_before) != set(PROTECTED_SERVICES):
        raise AtomicCutoverReceiptError("atomic_receipt_protected_pid_set_invalid")
    if dict(protected_pids_before) != dict(protected_pids_after):
        raise AtomicCutoverReceiptError("atomic_receipt_protected_pid_drift")
    if int(target_pid_before) <= 0 or int(target_pid_after) <= 0:
        raise AtomicCutoverReceiptError("atomic_receipt_target_pid_invalid")
    if int(target_pid_before) == int(target_pid_after):
        raise AtomicCutoverReceiptError("atomic_receipt_target_writer_not_restarted")
    if not isinstance(release, Mapping) or not release:
        raise AtomicCutoverReceiptError("atomic_receipt_release_missing")
    release_sha = str(release.get("release_sha256") or "").lower()
    release_identity = {
        key: value
        for key, value in release.items()
        if key != "release_sha256"
    }
    if len(release_sha) != 64 or content_sha256(release_identity) != release_sha:
        raise AtomicCutoverReceiptError("atomic_receipt_release_sha256_invalid")
    if release.get("station_scope_mode") != "canonical_allowlist":
        raise AtomicCutoverReceiptError("atomic_receipt_release_scope_invalid")
    if release.get("artifact_role") != "schema_bootstrap_only":
        raise AtomicCutoverReceiptError("atomic_receipt_release_artifact_role_invalid")
    if not isinstance(recovery, Mapping):
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_missing")
    if int(recovery.get("bytes") or 0) > MAX_RECOVERY_BYTES:
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_bytes_exceeded")
    if recovery.get("bounded") is not True:
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_not_bounded")
    database_fingerprint = dict(target_database_fingerprint or {})
    if not database_fingerprint or any(
        key not in database_fingerprint
        for key in ("device", "inode", "size", "mtime_ns")
    ):
        raise AtomicCutoverReceiptError("atomic_receipt_database_fingerprint_missing")
    recovery_deleted = recovery.get("deleted_after_success")
    if recovery_deleted not in {True, False}:
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_state_invalid")
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "state": (
            "NOOP_IDEMPOTENT"
            if idempotent_replay
            else (
                "COMPLETED"
                if recovery_deleted is True
                else "COMPLETED_RECOVERY_RETAINED"
            )
        ),
        "station_scope": {
            "mode": "canonical_allowlist",
            "station_ids": list(scope),
        },
        "stage_hashes": stage_hashes,
        "release": dict(release),
        "protected_service_pids_before": dict(protected_pids_before),
        "protected_service_pids_after": dict(protected_pids_after),
        "target_service_pid_before": int(target_pid_before),
        "target_service_pid_after": int(target_pid_after),
        "recovery": dict(recovery),
        "target_database_fingerprint_after": database_fingerprint,
        "idempotent_replay": bool(idempotent_replay),
        "writes": 0 if idempotent_replay else 1,
        "forward_oos_boundary": dict(FORWARD_OOS_BOUNDARY),
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
        "legacy_fallback_used": False,
    }
    identity["content_sha256"] = content_sha256(identity)
    return identity


def validate_cutover_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_station_ids: Sequence[str] | None = None,
) -> None:
    validate_sealed_receipt(receipt, schema=SCHEMA)
    station_scope = receipt.get("station_scope")
    if not isinstance(station_scope, Mapping):
        raise AtomicCutoverReceiptError("atomic_receipt_station_scope_invalid")
    if station_scope.get("mode") != "canonical_allowlist":
        raise AtomicCutoverReceiptError("atomic_receipt_scope_mode_invalid")
    receipt_station_ids = tuple(station_scope.get("station_ids") or ())
    if receipt_station_ids != tuple(sorted(receipt_station_ids)):
        raise AtomicCutoverReceiptError("atomic_receipt_scope_not_canonicalized")
    if expected_station_ids is not None:
        expected_scope = _normalize_scope(expected_station_ids)
        if receipt_station_ids != expected_scope:
            raise AtomicCutoverReceiptError("atomic_receipt_scope_mismatch")
    stage_hashes = receipt.get("stage_hashes") or {}
    if set(stage_hashes) != set(REQUIRED_STAGES) or any(
        len(str(stage_hashes.get(stage) or "")) != 64
        for stage in REQUIRED_STAGES
    ):
        raise AtomicCutoverReceiptError("atomic_receipt_stage_hashes_incomplete")
    if receipt.get("legacy_fallback_used") is not False:
        raise AtomicCutoverReceiptError("atomic_receipt_old_fallback_used")
    release = receipt.get("release")
    if not isinstance(release, Mapping):
        raise AtomicCutoverReceiptError("atomic_receipt_release_missing")
    release_sha = str(release.get("release_sha256") or "").lower()
    if len(release_sha) != 64 or content_sha256({
        key: value for key, value in release.items() if key != "release_sha256"
    }) != release_sha:
        raise AtomicCutoverReceiptError("atomic_receipt_release_sha256_invalid")
    if tuple(release.get("station_ids") or ()) != receipt_station_ids:
        raise AtomicCutoverReceiptError("atomic_receipt_release_scope_invalid")
    if release.get("station_scope_mode") != "canonical_allowlist":
        raise AtomicCutoverReceiptError("atomic_receipt_release_scope_invalid")
    if release.get("artifact_role") != "schema_bootstrap_only":
        raise AtomicCutoverReceiptError("atomic_receipt_release_artifact_role_invalid")
    protected_before = receipt.get("protected_service_pids_before")
    protected_after = receipt.get("protected_service_pids_after")
    if (
        not isinstance(protected_before, Mapping)
        or not isinstance(protected_after, Mapping)
        or set(protected_before) != set(PROTECTED_SERVICES)
        or dict(protected_before) != dict(protected_after)
    ):
        raise AtomicCutoverReceiptError("atomic_receipt_protected_pid_drift")
    fingerprint = receipt.get("target_database_fingerprint_after")
    if not isinstance(fingerprint, Mapping) or any(
        key not in fingerprint for key in ("device", "inode", "size", "mtime_ns")
    ):
        raise AtomicCutoverReceiptError("atomic_receipt_database_fingerprint_missing")
    if int(receipt.get("target_service_pid_before") or 0) <= 0:
        raise AtomicCutoverReceiptError("atomic_receipt_target_pid_invalid")
    if int(receipt.get("target_service_pid_after") or 0) <= 0:
        raise AtomicCutoverReceiptError("atomic_receipt_target_pid_invalid")
    if (
        receipt.get("idempotent_replay") is not True
        and int(receipt["target_service_pid_before"])
        == int(receipt["target_service_pid_after"])
    ):
        raise AtomicCutoverReceiptError("atomic_receipt_target_writer_not_restarted")
    recovery = receipt.get("recovery")
    if not isinstance(recovery, Mapping) or recovery.get("bounded") is not True:
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_not_bounded")
    recovery_deleted = recovery.get("deleted_after_success")
    if recovery_deleted not in {True, False}:
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_state_invalid")
    state = str(receipt.get("state") or "")
    if recovery_deleted is True and state not in {"COMPLETED", "NOOP_IDEMPOTENT"}:
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_state_mismatch")
    if recovery_deleted is False and state != "COMPLETED_RECOVERY_RETAINED":
        raise AtomicCutoverReceiptError("atomic_receipt_recovery_state_mismatch")
    if receipt.get("idempotent_replay") is True and int(receipt.get("writes") or 0) != 0:
        raise AtomicCutoverReceiptError("atomic_receipt_idempotent_write_nonzero")


def build_idempotent_replay_receipt(
    previous: Mapping[str, Any],
    *,
    station_ids: Sequence[str],
    release_sha256: str,
    target_database_fingerprint_after: Mapping[str, Any] | None = None,
    target_service_pid_after: int | None = None,
) -> dict[str, Any]:
    """Prove a repeated cutover is a deterministic no-op for the same release."""

    scope = _normalize_scope(station_ids)
    validate_cutover_receipt(previous, expected_station_ids=scope)
    if previous.get("state") not in {"COMPLETED", "NOOP_IDEMPOTENT"}:
        raise AtomicCutoverReceiptError("atomic_replay_cleanup_incomplete")
    previous_scope = tuple(
        (previous.get("station_scope") or {}).get("station_ids") or ()
    )
    if previous_scope != scope:
        raise AtomicCutoverReceiptError("atomic_replay_station_scope_mismatch")
    if str((previous.get("release") or {}).get("release_sha256") or "") != str(
        release_sha256
    ).lower():
        raise AtomicCutoverReceiptError("atomic_replay_release_mismatch")
    identity = {
        key: value
        for key, value in previous.items()
        if key != "content_sha256"
    }
    identity.update({
        "state": "NOOP_IDEMPOTENT",
        "station_scope": {"mode": "canonical_allowlist", "station_ids": list(scope)},
        "replay_of_sha256": str(previous["content_sha256"]),
        "idempotent_replay": True,
        "writes": 0,
    })
    if target_database_fingerprint_after is not None:
        fingerprint = dict(target_database_fingerprint_after)
        if any(
            key not in fingerprint
            for key in ("device", "inode", "size", "mtime_ns")
        ):
            raise AtomicCutoverReceiptError(
                "atomic_replay_database_fingerprint_invalid"
            )
        identity["target_database_fingerprint_after"] = fingerprint
    if target_service_pid_after is not None:
        current_pid = int(target_service_pid_after)
        if current_pid <= 0:
            raise AtomicCutoverReceiptError(
                "atomic_replay_target_pid_invalid"
            )
        identity["target_service_pid_after"] = current_pid
        if int(identity.get("target_service_pid_before") or 0) == current_pid:
            identity["target_service_pid_before"] = int(
                previous.get("target_service_pid_after") or 0
            )
    identity["content_sha256"] = content_sha256(identity)
    validate_cutover_receipt(identity)
    return identity


def finalize_recovery_cleanup_receipt(
    previous: Mapping[str, Any],
    *,
    recovery: Mapping[str, Any],
    recovery_stage_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Advance an already durable cutover receipt after bounded cleanup.

    The database cutover is committed before recovery cleanup begins.  This
    keeps rollback material available until the durable receipt exists and
    prevents a failed receipt write from leaving neither receipt nor backup.
    """

    validate_cutover_receipt(previous)
    if previous.get("state") != "COMPLETED_RECOVERY_RETAINED":
        raise AtomicCutoverReceiptError("atomic_cleanup_previous_state_invalid")
    if recovery.get("deleted_after_success") is not True:
        raise AtomicCutoverReceiptError("atomic_cleanup_recovery_not_deleted")
    validate_sealed_receipt(
        recovery_stage_receipt,
        schema=STAGE_SCHEMA,
        stage="recovery_cleanup",
    )
    identity = {
        key: value
        for key, value in previous.items()
        if key != "content_sha256"
    }
    identity["state"] = "COMPLETED"
    identity["recovery"] = dict(recovery)
    identity["stage_hashes"] = {
        **dict(previous.get("stage_hashes") or {}),
        "recovery_cleanup": str(recovery_stage_receipt["content_sha256"]),
    }
    identity["content_sha256"] = content_sha256(identity)
    validate_cutover_receipt(identity)
    return identity


def build_release_identity(
    *,
    station_ids: Sequence[str],
    schema_sha256: str,
    activation_sha256: str,
    artifact_sha256: str,
) -> dict[str, Any]:
    scope = _normalize_scope(station_ids)
    values = {
        "schema": "bayes_basket_target_m9_06_release_identity_v1",
        "station_ids": list(scope),
        "station_scope_mode": "canonical_allowlist",
        "schema_sha256": str(schema_sha256).lower(),
        "activation_sha256": str(activation_sha256).lower(),
        "artifact_sha256": str(artifact_sha256).lower(),
        "artifact_role": "schema_bootstrap_only",
        "execution_mode": "production_no_live",
        "legacy_fallback_used": False,
    }
    values["release_sha256"] = content_sha256(values)
    return values


def recovery_file_identity(path: Path | str) -> dict[str, Any]:
    candidate_raw = Path(path).expanduser()
    if candidate_raw.is_symlink():
        raise AtomicCutoverReceiptError("atomic_recovery_path_symlink_forbidden")
    candidate = candidate_raw.resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise AtomicCutoverReceiptError("atomic_recovery_path_invalid")
    size = int(candidate.stat().st_size)
    if size <= 0 or size > MAX_RECOVERY_BYTES:
        raise AtomicCutoverReceiptError("atomic_recovery_bytes_invalid")
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(candidate),
        "bytes": size,
        "sha256": digest.hexdigest(),
        "bounded": True,
        "deleted_after_success": False,
    }


def delete_recovery_after_success(
    path: Path | str,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    """Delete exactly one bounded recovery file and return an audit identity."""

    identity = recovery_file_identity(path)
    if identity["sha256"] != str(expected_sha256).lower():
        raise AtomicCutoverReceiptError("atomic_recovery_sha256_mismatch")
    candidate = Path(identity["path"])
    candidate.unlink()
    identity["deleted_after_success"] = not candidate.exists()
    if not identity["deleted_after_success"]:
        raise AtomicCutoverReceiptError("atomic_recovery_delete_not_confirmed")
    return identity


def source_stage_receipt(stage: str, path: Path | str) -> dict[str, Any]:
    """Seal an existing bounded JSON receipt as one M9-06 stage."""

    candidate_raw = Path(path).expanduser()
    if candidate_raw.is_symlink():
        raise AtomicCutoverReceiptError("atomic_stage_source_symlink_forbidden")
    candidate = candidate_raw.resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise AtomicCutoverReceiptError("atomic_stage_source_invalid")
    if candidate.stat().st_size > MAX_RECEIPT_BYTES:
        raise AtomicCutoverReceiptError("atomic_stage_source_bytes_exceeded")
    raw = candidate.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AtomicCutoverReceiptError("atomic_stage_source_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise AtomicCutoverReceiptError("atomic_stage_source_mapping_required")
    return seal_stage(
        stage,
        payload,
        source_path=str(candidate),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = [
    "AtomicCutoverReceiptError",
    "FORWARD_OOS_BOUNDARY",
    "MAX_RECEIPT_BYTES",
    "MAX_RECOVERY_BYTES",
    "PROTECTED_SERVICES",
    "REQUIRED_STAGES",
    "SCHEMA",
    "STAGE_SCHEMA",
    "build_cutover_receipt",
    "build_idempotent_replay_receipt",
    "build_release_identity",
    "bounded_json_bytes",
    "content_sha256",
    "delete_recovery_after_success",
    "finalize_recovery_cleanup_receipt",
    "recovery_file_identity",
    "seal_stage",
    "source_stage_receipt",
    "validate_cutover_receipt",
    "validate_sealed_receipt",
    "write_receipt_if_changed",
]
