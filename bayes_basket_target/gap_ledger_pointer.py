"""Read-only, content-addressed current-pointer evidence for a gap ledger.

This PR04 slice does not select an activation or make a ledger consumable.  It
only proves which immutable ledger bytes a later, separately-authorized
consumer would have to bind.  Any malformed canonical replay or blocked
builder scope fails closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, content_sha256
from .gap_ledger import (
    BuilderScopeStatus,
    GapLedger,
    GapRecord,
    SCHEMA as LEDGER_SCHEMA,
)
from .resource_bounds import (
    MAX_JSON_DEPTH,
    MAX_JSON_INPUT_BYTES,
    MAX_JSON_NODES,
    assert_process_memory_budget,
)


SCHEMA = "bayes_basket_target_canonical_gap_ledger_pointer_v1"
POINTER_VERSION = "3.0.0"
_HEX = frozenset("0123456789abcdef")
_BUILDER_STATES = frozenset({"BUILT", "BLOCKED"})


class GapLedgerPointerError(ContractError):
    """Raised when an immutable ledger cannot be bound without guessing."""


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise GapLedgerPointerError(f"{field}_type_invalid")
    result = value.strip().lower()
    if len(result) != 64 or any(char not in _HEX for char in result):
        raise GapLedgerPointerError(f"{field}_invalid")
    return result


def _utc_z(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GapLedgerPointerError(f"{field}_missing")
    text = value.strip()
    if not text.endswith("Z"):
        raise GapLedgerPointerError(f"{field}_must_be_utc_z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise GapLedgerPointerError(f"{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GapLedgerPointerError(f"{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise GapLedgerPointerError("ledger_pointer_input_stat_failed") from exc
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _assert_json_shape(value: object) -> None:
    """Apply the target lane's node/depth bounds to this stable byte read."""

    nodes = 0
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > int(MAX_JSON_NODES):
            raise GapLedgerPointerError("ledger_pointer_json_nodes_exceeded")
        if depth > int(MAX_JSON_DEPTH):
            raise GapLedgerPointerError("ledger_pointer_json_depth_exceeded")
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise GapLedgerPointerError("ledger_pointer_json_key_invalid")
                pending.append((nested, depth + 1))
        elif isinstance(current, list):
            pending.extend((nested, depth + 1) for nested in current)


def _read_ledger(path: Path | str) -> tuple[Path, bytes, Mapping[str, Any]]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise GapLedgerPointerError("ledger_pointer_symlink_forbidden")
    try:
        source = candidate.resolve(strict=True)
    except OSError as exc:
        raise GapLedgerPointerError("ledger_pointer_input_missing") from exc
    if not source.is_file():
        raise GapLedgerPointerError("ledger_pointer_input_not_regular_file")
    before = _file_identity(source)
    if before[2] > int(MAX_JSON_INPUT_BYTES):
        raise GapLedgerPointerError("ledger_pointer_input_bytes_exceeded")
    assert_process_memory_budget(projected_bytes=before[2])
    try:
        raw = source.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GapLedgerPointerError("ledger_pointer_input_json_invalid") from exc
    after = _file_identity(source)
    if before != after or len(raw) != before[2]:
        raise GapLedgerPointerError("ledger_pointer_input_changed_during_read")
    if not isinstance(value, Mapping):
        raise GapLedgerPointerError("ledger_pointer_ledger_mapping_required")
    _assert_json_shape(value)
    assert_process_memory_budget()
    return source, raw, value


def replay_canonical_gap_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly parse and exactly reserialize the one canonical ledger."""

    if ledger.get("schema") != LEDGER_SCHEMA:
        raise GapLedgerPointerError("ledger_pointer_ledger_schema_invalid")
    supplied_sha = _sha256(
        ledger.get("content_sha256"), field="ledger_pointer_ledger_content_sha256"
    )
    identity = {key: value for key, value in ledger.items() if key != "content_sha256"}
    if supplied_sha != content_sha256(identity):
        raise GapLedgerPointerError("ledger_pointer_ledger_content_sha256_mismatch")
    raw_records = ledger.get("records")
    raw_statuses = ledger.get("builder_scope_statuses")
    raw_pointers = ledger.get("source_pointers")
    if not isinstance(raw_records, list):
        raise GapLedgerPointerError("ledger_pointer_records_invalid")
    if not isinstance(raw_statuses, list):
        raise GapLedgerPointerError("ledger_pointer_builder_statuses_invalid")
    if not isinstance(raw_pointers, Mapping):
        raise GapLedgerPointerError("ledger_pointer_source_pointers_invalid")
    as_of_utc = _utc_z(
        ledger.get("as_of_utc"), field="ledger_pointer_ledger_as_of_utc"
    )
    try:
        records = [GapRecord.from_mapping(item) for item in raw_records]
        statuses = [BuilderScopeStatus.from_mapping(item) for item in raw_statuses]
        rebuilt = GapLedger(
            records,
            as_of_utc=as_of_utc,
            source_pointers=raw_pointers,
            builder_scope_statuses=statuses,
        ).to_mapping()
    except ContractError as exc:
        raise GapLedgerPointerError(
            f"ledger_pointer_canonical_replay_failed:{type(exc).__name__}"
        ) from exc
    if dict(ledger) != rebuilt:
        raise GapLedgerPointerError("ledger_pointer_canonical_replay_mismatch")
    for status in statuses:
        if status.state not in _BUILDER_STATES:
            raise GapLedgerPointerError("ledger_pointer_builder_state_invalid")
        if status.state == "BUILT" and status.blockers:
            raise GapLedgerPointerError("ledger_pointer_built_status_has_blocker")
        if status.state == "BLOCKED" and len(status.blockers) != 1:
            raise GapLedgerPointerError("ledger_pointer_blocked_status_requires_one_blocker")
    return rebuilt


def build_gap_ledger_pointer(
    *,
    ledger_path: Path | str,
    inner_oos_snapshot_content_sha256: str,
) -> dict[str, Any]:
    """Build no-live pointer evidence from one stable canonical ledger file.

    The function is deliberately pure with respect to persistent state: it
    reads the ledger and returns an evidence mapping, but creates no pointer
    file, database row, activation, order, or consumable artifact.
    """

    inner_sha = _sha256(
        inner_oos_snapshot_content_sha256,
        field="ledger_pointer_inner_oos_snapshot_content_sha256",
    )
    source, raw, ledger = _read_ledger(ledger_path)
    rebuilt = replay_canonical_gap_ledger(ledger)
    blocked_scopes = [
        status.scope_key
        for status in (
            BuilderScopeStatus.from_mapping(item)
            for item in rebuilt["builder_scope_statuses"]
        )
        if status.state == "BLOCKED"
    ]
    blockers = [
        f"BUILDER_SCOPE_BLOCKED:{scope_key}" for scope_key in sorted(blocked_scopes)
    ]
    pointer_identity = {
        "schema": SCHEMA,
        "pointer_version": POINTER_VERSION,
        "state": "BLOCKED" if blockers else "VERIFIED_NO_LIVE",
        "blockers": blockers,
        "ledger_path": str(source),
        "ledger_content_sha256": rebuilt["content_sha256"],
        "ledger_file_sha256": hashlib.sha256(raw).hexdigest(),
        "record_count": len(rebuilt["records"]),
        "effective_as_of_utc": rebuilt["as_of_utc"],
        "inner_oos_snapshot_content_sha256": inner_sha,
        "production_no_live": True,
        "formal_p0_eligible": False,
        "formal_live_for_republisher": False,
        "future_data_counts_as_oos": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "writes_production_db": False,
        "writes_target_database": False,
        "read_only": True,
    }
    return {
        **pointer_identity,
        "content_sha256": content_sha256(pointer_identity),
    }


__all__ = [
    "GapLedgerPointerError",
    "POINTER_VERSION",
    "SCHEMA",
    "build_gap_ledger_pointer",
    "replay_canonical_gap_ledger",
]
