"""Pure contracts and bounded JSON export for forward/no-live observations.

The resident materialized projection lives in ``forward_observation_store``.
This module validates a unified atomic cycle receipt and provides an offline
JSON interchange format; it never calls a gateway or connects to a market.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, content_sha256
from .resource_bounds import (
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_OUTPUT_BYTES,
    MAX_TARGET_PROCESS_RSS_BYTES,
    MIN_TARGET_FREE_BYTES,
    assert_process_memory_budget,
    bounded_json_bytes,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_target_cash_phase_forward_observations_v1"
OBSERVATION_SCHEMA = "bayes_basket_target_cash_phase_forward_observation_v1"
DEFAULT_TARGET_RUNTIME_ROOT = (
    Path(__file__).resolve().parents[1] / "data" / "runtime"
).resolve()
MAX_ROWS = 4096
MAX_INPUT_ROWS = 4096
MAX_OUTPUT_BYTES = min(MAX_JSON_OUTPUT_BYTES, 16 * 1024 * 1024)
SHA256_HEX = frozenset("0123456789abcdef")
VALID_PHASES = frozenset({"HEAVY", "STANDARD", "TAIL"})
NO_LIVE_FIELDS = {
    "execution_mode": "production_no_live",
    "fill_status": "NO_EXECUTION",
    "evidence_only": True,
    "authorizes_order": False,
    "orders_submitted": 0,
}
OBSERVATION_REQUIRED_KEYS = frozenset({
    "schema",
    "station_id",
    "event_date",
    "phase",
    "decision_at_utc",
    "observation_key",
    "settlement_timezone",
    "cycle_receipt_sha256",
    "cycle_id",
    "decision_id",
    "decision_sha256",
    "forward_evidence_binding_sha256",
    "probability_stage",
    "probability_artifact_sha256",
    "probability_lineage_sha256",
    "probability_decision_at_utc",
    "ws_snapshot_sha256",
    "ws_quote_observed_at_utc",
    "ws_quote_expires_at_utc",
    "ws_quote_count",
    "cash_checkpoint_id",
    "cash_observed_at_utc",
    "cash_decision_sha256",
    "cash_policy_sha256",
    "station_policy_sha256",
    "delta_v2_artifact_sha256",
    "delta_v2_evaluation_sha256",
    "inventory_sha256",
    "order_state_sha256",
    *NO_LIVE_FIELDS.keys(),
    "observation_sha256",
})


class ForwardObservationError(ContractError):
    """A fail-closed input, identity, duplicate, or resource violation."""


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ForwardObservationError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in SHA256_HEX for char in result):
        raise ForwardObservationError(f"{name}_invalid")
    return result


def _utc(name: str, value: Any) -> str:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardObservationError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardObservationError(f"{name}_timezone_required")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    head, fraction = normalized[:-1].split(".", 1)
    fraction = fraction.rstrip("0") or "0"
    fraction = fraction.ljust(3, "0")
    return f"{head}.{fraction}Z"


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ForwardObservationError(f"{name}_invalid") from exc
    return result


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ForwardObservationError(f"{name}_mapping_required")
    return value


def resolve_forward_observation_target(
    path: Path | str,
    *,
    allowed_root: Path | str | None = None,
) -> Path:
    """Resolve a non-database JSON target without following writable symlinks."""

    candidate = Path(path).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    if absolute.is_symlink():
        raise ForwardObservationError("observation_target_symlink_forbidden")
    destination = absolute.resolve()
    if destination.suffix.lower() in {
        ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"
    }:
        raise ForwardObservationError("observation_target_database_forbidden")
    if destination.suffix.lower() != ".json":
        raise ForwardObservationError("observation_target_json_required")
    if allowed_root is not None:
        root_candidate = Path(allowed_root).expanduser()
        root_absolute = (
            root_candidate
            if root_candidate.is_absolute()
            else Path.cwd() / root_candidate
        )
        if root_absolute.is_symlink():
            raise ForwardObservationError(
                "observation_allowed_root_symlink_forbidden"
            )
        root = root_absolute.resolve()
        if not destination.is_relative_to(root):
            raise ForwardObservationError(
                "observation_target_outside_allowed_root"
            )
    return destination


@contextmanager
def _observation_write_lock(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_name(destination.name + ".lock")
    if lock_path.is_symlink():
        raise ForwardObservationError("observation_lock_symlink_forbidden")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ForwardObservationError("observation_lock_open_failed") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ForwardObservationError("observation_lock_failed") from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _station_event_phase(
    source: Mapping[str, Any],
    *,
    name: str,
) -> tuple[str, str, str, str]:
    station = _text(f"{name}_station_id", _first(source, "station_id", "station"))
    event = _date(f"{name}_event_date", _first(source, "event_date", "event_date_local"))
    phase = _text(f"{name}_phase", _first(source, "phase", "current_phase", "clock_phase")).upper()
    if phase not in VALID_PHASES:
        raise ForwardObservationError(f"{name}_phase_invalid")
    decision = _utc(
        f"{name}_decision_at_utc",
        _first(source, "decision_at_utc", "decision_at", "observed_at_utc", "observed_at"),
    )
    return station, event, phase, decision


def _registry_timezone(registry: Mapping[str, Any], station: str, event: str) -> str:
    rows = registry.get("rows", registry.get("markets", registry.get("registry_rows")))
    if isinstance(rows, Mapping):
        rows = [rows]
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise ForwardObservationError("registry_rows_sequence_required")
    if len(rows) > MAX_INPUT_ROWS or any(not isinstance(row, Mapping) for row in rows):
        raise ForwardObservationError("registry_rows_invalid")
    matches = [
        row for row in rows
        if str(row.get("station_id") or row.get("station") or "").strip() == station
        and str(row.get("event_date") or row.get("event_date_local") or "").strip() == event
    ]
    if not matches:
        raise ForwardObservationError("registry_station_event_missing")
    timezones = {
        _text("registry_settlement_timezone", row.get("settlement_timezone"))
        for row in matches
    }
    if len(timezones) != 1:
        raise ForwardObservationError("registry_settlement_timezone_not_unique")
    timezone_name = next(iter(timezones))
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ForwardObservationError("registry_settlement_timezone_invalid") from exc
    return timezone_name


def _require_settlement_timezone(
    source: Mapping[str, Any],
    *,
    name: str,
    expected: str,
) -> None:
    supplied = _text(
        f"{name}_settlement_timezone",
        source.get("settlement_timezone"),
    )
    if supplied != expected:
        raise ForwardObservationError(f"{name}_settlement_timezone_mismatch")


def _canonical_key(station: str, event: str, phase: str, decision: str) -> str:
    return f"{station}|{event}|{phase}|{decision}"


def _row_from_unified_receipt(
    receipt: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    if receipt.get("schema") != "bayes_basket_target_atomic_forward_cycle_receipt_v1":
        raise ForwardObservationError("unified_cycle_receipt_schema_invalid")
    supplied_receipt_sha = _sha(
        "unified_cycle_receipt_sha256", receipt.get("receipt_sha256")
    )
    expected_receipt_sha = content_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if supplied_receipt_sha != expected_receipt_sha:
        raise ForwardObservationError("unified_cycle_receipt_content_sha256_mismatch")
    if receipt.get("execution_mode") != "production_no_live":
        raise ForwardObservationError(
            "cycle_execution_mode_must_be_production_no_live"
        )
    for name, expected in (
        ("writes_target_ledger", True),
        ("orders_submitted", 0),
        ("places_orders", False),
        ("calls_order_gateway", False),
        ("live_submit_authorized", False),
        ("authorizes_order", False),
        ("evidence_only", True),
    ):
        if receipt.get(name) != expected:
            raise ForwardObservationError(f"unified_cycle_receipt_{name}_invalid")
    for name in ("cycle_id", "decision_id", "plan_id"):
        _text(name, receipt.get(name))
    _sha("plan_sha256", receipt.get("plan_sha256"))
    _sha("decision_sha256", receipt.get("decision_sha256"))

    binding = _mapping(
        "forward_evidence_binding", receipt.get("forward_evidence_binding")
    )
    if binding.get("schema") != "bayes_basket_target_forward_evidence_binding_v1":
        raise ForwardObservationError("forward_evidence_binding_schema_invalid")
    supplied_binding_sha = _sha(
        "forward_evidence_binding_sha256", binding.get("content_sha256")
    )
    expected_binding_sha = content_sha256(
        {key: value for key, value in binding.items() if key != "content_sha256"}
    )
    if supplied_binding_sha != expected_binding_sha:
        raise ForwardObservationError(
            "forward_evidence_binding_content_sha256_mismatch"
        )

    station = _text("binding_station_id", binding.get("station_id"))
    event = _date("binding_event_date", binding.get("event_date"))
    phase = _text("binding_cash_phase", binding.get("cash_phase")).upper()
    if phase not in VALID_PHASES:
        raise ForwardObservationError("binding_cash_phase_invalid")
    decision = _utc("binding_decision_at_utc", binding.get("decision_at_utc"))
    if _utc("receipt_decision_at_utc", receipt.get("decision_at_utc")) != decision:
        raise ForwardObservationError("receipt_binding_decision_at_mismatch")
    settlement_timezone = _registry_timezone(registry, station, event)
    _require_settlement_timezone(
        binding,
        name="binding",
        expected=settlement_timezone,
    )
    stage = _text(
        "binding_probability_stage", binding.get("probability_stage")
    ).upper()
    if stage != "P1":
        raise ForwardObservationError("binding_probability_stage_requires_p1")
    if str(binding.get("quote_source") or "").strip().upper() != "OFFICIAL_CLOB_WS":
        raise ForwardObservationError("binding_quote_source_not_official_clob_ws")

    probability_at = _utc(
        "binding_probability_decision_at_utc",
        binding.get("probability_decision_at_utc"),
    )
    quote_at = _utc(
        "binding_ws_quote_observed_at_utc",
        binding.get("ws_quote_observed_at_utc"),
    )
    quote_expires_at = _utc(
        "binding_ws_quote_expires_at_utc",
        binding.get("ws_quote_expires_at_utc"),
    )
    cash_at = _utc(
        "binding_cash_observed_at_utc",
        binding.get("cash_observed_at_utc"),
    )
    parsed = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    decision_dt = parsed(decision)
    if parsed(probability_at) > decision_dt:
        raise ForwardObservationError("binding_probability_after_decision")
    if parsed(quote_at) > decision_dt:
        raise ForwardObservationError("binding_quote_after_decision")
    if decision_dt >= parsed(quote_expires_at):
        raise ForwardObservationError("binding_quote_expired_at_decision")
    if parsed(cash_at) > decision_dt:
        raise ForwardObservationError("binding_cash_after_decision")
    quote_count = binding.get("ws_quote_count")
    if isinstance(quote_count, bool):
        raise ForwardObservationError("binding_ws_quote_count_invalid")
    try:
        quote_count = int(quote_count)
    except (TypeError, ValueError) as exc:
        raise ForwardObservationError("binding_ws_quote_count_invalid") from exc
    if quote_count <= 0 or quote_count > MAX_INPUT_ROWS:
        raise ForwardObservationError("binding_ws_quote_count_invalid")

    row = {
        "schema": OBSERVATION_SCHEMA,
        "station_id": station,
        "event_date": event,
        "phase": phase,
        "decision_at_utc": decision,
        "observation_key": _canonical_key(station, event, phase, decision),
        "settlement_timezone": settlement_timezone,
        "cycle_receipt_sha256": supplied_receipt_sha,
        "cycle_id": _text("cycle_id", receipt.get("cycle_id")),
        "decision_id": _text("decision_id", receipt.get("decision_id")),
        "decision_sha256": _sha(
            "decision_sha256", receipt.get("decision_sha256")
        ),
        "forward_evidence_binding_sha256": supplied_binding_sha,
        "probability_stage": stage,
        "probability_artifact_sha256": _sha(
            "probability_artifact_sha256",
            binding.get("probability_artifact_sha256"),
        ),
        "probability_lineage_sha256": _sha(
            "probability_lineage_sha256",
            binding.get("probability_lineage_sha256"),
        ),
        "probability_decision_at_utc": probability_at,
        "ws_snapshot_sha256": _sha(
            "ws_snapshot_sha256", binding.get("ws_snapshot_sha256")
        ),
        "ws_quote_observed_at_utc": quote_at,
        "ws_quote_expires_at_utc": quote_expires_at,
        "ws_quote_count": quote_count,
        "cash_checkpoint_id": _text(
            "cash_checkpoint_id", binding.get("cash_checkpoint_id")
        ),
        "cash_observed_at_utc": cash_at,
        "cash_decision_sha256": _sha(
            "cash_decision_sha256", binding.get("cash_decision_sha256")
        ),
        "cash_policy_sha256": _sha(
            "cash_policy_sha256", binding.get("cash_policy_sha256")
        ),
        "station_policy_sha256": _sha(
            "station_policy_sha256", binding.get("station_policy_sha256")
        ),
        "delta_v2_artifact_sha256": _sha(
            "delta_v2_artifact_sha256",
            binding.get("delta_v2_artifact_sha256"),
        ),
        "delta_v2_evaluation_sha256": _sha(
            "delta_v2_evaluation_sha256",
            binding.get("delta_v2_evaluation_sha256"),
        ),
        "inventory_sha256": _sha(
            "inventory_sha256", binding.get("inventory_sha256")
        ),
        "order_state_sha256": _sha(
            "order_state_sha256", binding.get("order_state_sha256")
        ),
        **NO_LIVE_FIELDS,
    }
    row["observation_sha256"] = content_sha256(
        {key: value for key, value in row.items() if key != "observation_sha256"}
    )
    return row


def _validate_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping) or row.get("schema") != OBSERVATION_SCHEMA:
        raise ForwardObservationError("observation_schema_invalid")
    keys = frozenset(row)
    missing = sorted(OBSERVATION_REQUIRED_KEYS - keys)
    unknown = sorted(keys - OBSERVATION_REQUIRED_KEYS)
    if missing:
        raise ForwardObservationError(
            "observation_required_fields_missing:" + ",".join(missing)
        )
    if unknown:
        raise ForwardObservationError(
            "observation_unknown_fields:" + ",".join(unknown)
        )
    identity = _station_event_phase(row, name="observation")
    if row.get("observation_key") != _canonical_key(*identity):
        raise ForwardObservationError("observation_key_invalid")
    for name, expected in NO_LIVE_FIELDS.items():
        if row.get(name) != expected:
            raise ForwardObservationError(f"observation_{name}_invalid")
    timezone_name = _text(
        "settlement_timezone",
        row.get("settlement_timezone"),
    )
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ForwardObservationError(
            "observation_settlement_timezone_invalid"
        ) from exc
    supplied_sha = _sha(
        "observation_sha256", row.get("observation_sha256")
    )
    expected_sha = content_sha256(
        {
            key: value
            for key, value in row.items()
            if key != "observation_sha256"
        }
    )
    if supplied_sha != expected_sha:
        raise ForwardObservationError(
            "observation_content_sha256_mismatch"
        )
    return dict(row)


def validate_forward_observation_row(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Public strict validator for target-owned materialized stores."""

    return _validate_row(row)


def _existing_rows(existing: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if existing is None:
        return []
    if existing.get("schema") != SCHEMA:
        raise ForwardObservationError("existing_observation_schema_invalid")
    rows = existing.get("observations", [])
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence) or len(rows) > MAX_ROWS:
        raise ForwardObservationError("existing_observation_rows_invalid")
    validated = [_validate_row(row) for row in rows]
    seen: dict[str, dict[str, Any]] = {}
    for row in validated:
        key = row["observation_key"]
        if key in seen and seen[key] != row:
            raise ForwardObservationError("existing_observation_duplicate_conflict")
        seen[key] = row
    return list(seen.values())


def _validate_packet(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(packet, Mapping) or packet.get("schema") != SCHEMA:
        raise ForwardObservationError("observation_packet_schema_invalid")
    for name, expected in NO_LIVE_FIELDS.items():
        if packet.get(name) != expected:
            raise ForwardObservationError(f"observation_packet_{name}_invalid")
    rows = _existing_rows(packet)
    supplied = packet.get("content_sha256")
    if supplied is None:
        raise ForwardObservationError("observation_packet_content_sha256_missing")
    _sha("observation_packet_content_sha256", supplied)
    expected = content_sha256({k: v for k, v in packet.items() if k != "content_sha256"})
    if supplied != expected:
        raise ForwardObservationError("observation_packet_content_sha256_mismatch")
    labels = packet.get("settlement_labels", [])
    if isinstance(labels, (str, bytes, bytearray)) or not isinstance(labels, Sequence) or len(labels) > MAX_ROWS:
        raise ForwardObservationError("settlement_labels_invalid")
    known = {row["observation_key"] for row in rows}
    rows_by_key = {row["observation_key"]: row for row in rows}
    seen_labels: dict[str, tuple[str, str, str, str, str]] = {}
    for label in labels:
        if not isinstance(label, Mapping):
            raise ForwardObservationError("settlement_label_mapping_required")
        key = str(label.get("observation_key") or "")
        if key not in known:
            raise ForwardObservationError("settlement_label_unknown_observation")
        truth = _text("settlement_label_truth", _first(label, "winning_bucket", "truth_bucket"))
        labeled_at = _utc("settlement_label_labeled_at_utc", _first(label, "labeled_at_utc", "settled_at_utc"))
        authority = _text(
            "settlement_label_authority",
            label.get("settlement_authority"),
        )
        if authority != "market_registry_official_resolution":
            raise ForwardObservationError(
                "settlement_label_authority_invalid"
            )
        settlement_sha = _sha(
            "official_settlement_sha256",
            label.get("official_settlement_sha256"),
        )
        timezone_name = _text(
            "settlement_label_timezone",
            label.get("settlement_timezone"),
        )
        if timezone_name != rows_by_key[key]["settlement_timezone"]:
            raise ForwardObservationError(
                "settlement_label_timezone_mismatch"
            )
        identity = (
            truth,
            labeled_at,
            authority,
            settlement_sha,
            timezone_name,
        )
        if key in seen_labels and seen_labels[key] != identity:
            raise ForwardObservationError("settlement_label_conflict")
        seen_labels[key] = identity
    return rows


def _write_budget(path: Path, raw: bytes) -> None:
    if len(raw) > MAX_OUTPUT_BYTES:
        raise ForwardObservationError("observation_output_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=len(raw), limit=MAX_TARGET_PROCESS_RSS_BYTES)
    try:
        free = shutil.disk_usage(path.parent if path.parent.exists() else Path.cwd()).free
    except OSError as exc:
        raise ForwardObservationError("observation_disk_probe_failed") from exc
    if free < MIN_TARGET_FREE_BYTES + len(raw):
        raise ForwardObservationError("observation_min_free_space_guard")


def build_forward_observation_packet(
    *,
    cycle_receipt: Mapping[str, Any],
    registry: Mapping[str, Any],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build from one atomic receipt; cross-file evidence joins are forbidden."""

    for name, value in (("cycle_receipt", cycle_receipt), ("registry", registry)):
        _mapping(name, value)
        try:
            bounded_json_bytes(value, limit=MAX_JSON_INPUT_BYTES)
        except (ContractError, ValueError, TypeError) as exc:
            raise ForwardObservationError(f"{name}_resource_bound_failed") from exc
    current = _validate_packet(existing) if existing is not None else []
    incoming = _row_from_unified_receipt(cycle_receipt, registry)
    by_key = {row["observation_key"]: row for row in current}
    old = by_key.get(incoming["observation_key"])
    if old is not None and old != incoming:
        raise ForwardObservationError("observation_duplicate_conflict")
    by_key[incoming["observation_key"]] = incoming
    if len(by_key) > MAX_ROWS:
        raise ForwardObservationError("observation_rows_exceeded")
    observations = [by_key[key] for key in sorted(by_key)]
    retained_labels = (
        list(existing.get("settlement_labels", []))
        if existing is not None
        else []
    )
    packet = {
        "schema": SCHEMA,
        **NO_LIVE_FIELDS,
        "settlement_labels": retained_labels,
        "observations": observations,
    }
    packet["content_sha256"] = content_sha256({k: v for k, v in packet.items() if k != "content_sha256"})
    bounded_json_bytes(packet, limit=MAX_OUTPUT_BYTES)
    return packet


def append_settlement_labels(packet: Mapping[str, Any], labels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Append externally supplied labels only; this function never derives truth."""

    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes, bytearray)) or len(labels) > MAX_ROWS:
        raise ForwardObservationError("settlement_labels_sequence_invalid")
    _validate_packet(packet)
    result = dict(packet)
    rows = _existing_rows(packet)
    rows_by_key = {row["observation_key"]: row for row in rows}
    known = set(rows_by_key)
    existing_labels = packet.get("settlement_labels", [])
    if not isinstance(existing_labels, Sequence) or isinstance(existing_labels, (str, bytes, bytearray)):
        raise ForwardObservationError("settlement_labels_invalid")
    merged: dict[str, Mapping[str, Any]] = {}
    for label in [*existing_labels, *labels]:
        if not isinstance(label, Mapping):
            raise ForwardObservationError("settlement_label_mapping_required")
        key = _text("settlement_label_observation_key", label.get("observation_key"))
        if key not in known:
            raise ForwardObservationError("settlement_label_unknown_observation")
        truth = _text("settlement_label_truth", _first(label, "winning_bucket", "truth_bucket"))
        labeled_at = _utc("settlement_label_labeled_at_utc", _first(label, "labeled_at_utc", "settled_at_utc"))
        authority = _text(
            "settlement_label_authority",
            label.get("settlement_authority"),
        )
        if authority != "market_registry_official_resolution":
            raise ForwardObservationError(
                "settlement_label_authority_invalid"
            )
        settlement_sha = _sha(
            "official_settlement_sha256",
            label.get("official_settlement_sha256"),
        )
        timezone_name = _text(
            "settlement_label_timezone",
            label.get("settlement_timezone"),
        )
        if timezone_name != rows_by_key[key]["settlement_timezone"]:
            raise ForwardObservationError(
                "settlement_label_timezone_mismatch"
            )
        normalized = {
            "observation_key": key,
            "winning_bucket": truth,
            "labeled_at_utc": labeled_at,
            "settlement_authority": authority,
            "official_settlement_sha256": settlement_sha,
            "settlement_timezone": timezone_name,
        }
        if key in merged and dict(merged[key]) != normalized:
            raise ForwardObservationError("settlement_label_conflict")
        merged[key] = normalized
    result["settlement_labels"] = [merged[key] for key in sorted(merged)]
    result["content_sha256"] = content_sha256({k: v for k, v in result.items() if k != "content_sha256"})
    bounded_json_bytes(result, limit=MAX_OUTPUT_BYTES)
    return result


def _merge_packets(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    incoming_rows = _validate_packet(incoming)
    existing_rows = _validate_packet(existing) if existing is not None else []
    rows_by_key = {
        row["observation_key"]: row
        for row in existing_rows
    }
    for row in incoming_rows:
        old = rows_by_key.get(row["observation_key"])
        if old is not None and old != row:
            raise ForwardObservationError(
                "observation_concurrent_duplicate_conflict"
            )
        rows_by_key[row["observation_key"]] = row
    if len(rows_by_key) > MAX_ROWS:
        raise ForwardObservationError("observation_rows_exceeded")
    merged = {
        "schema": SCHEMA,
        **NO_LIVE_FIELDS,
        "settlement_labels": [],
        "observations": [
            rows_by_key[key] for key in sorted(rows_by_key)
        ],
    }
    labels = [
        *(
            list(existing.get("settlement_labels", []))
            if existing is not None
            else []
        ),
        *list(incoming.get("settlement_labels", [])),
    ]
    merged["content_sha256"] = content_sha256(merged)
    if labels:
        merged = append_settlement_labels(merged, labels)
    return merged


def write_forward_observation_packet(
    path: Path | str,
    packet: Mapping[str, Any],
    *,
    allowed_root: Path | str | None = None,
) -> bool:
    """CAS-merge and atomically write one bounded target-local JSON packet."""

    _validate_packet(packet)
    destination = resolve_forward_observation_target(
        path,
        allowed_root=allowed_root,
    )
    raw = bounded_json_bytes(packet, limit=MAX_OUTPUT_BYTES)
    _write_budget(destination, raw)
    with _observation_write_lock(destination):
        existing = (
            load_bounded_json(destination)
            if destination.is_file()
            else None
        )
        merged = _merge_packets(existing, packet)
        merged_raw = bounded_json_bytes(merged, limit=MAX_OUTPUT_BYTES)
        _write_budget(destination, merged_raw)
        return write_bounded_json_if_changed(
            destination,
            merged,
            limit=MAX_OUTPUT_BYTES,
        )


__all__ = [
    "MAX_ROWS",
    "DEFAULT_TARGET_RUNTIME_ROOT",
    "OBSERVATION_SCHEMA",
    "SCHEMA",
    "ForwardObservationError",
    "resolve_forward_observation_target",
    "validate_forward_observation_row",
    "append_settlement_labels",
    "build_forward_observation_packet",
    "write_forward_observation_packet",
]
