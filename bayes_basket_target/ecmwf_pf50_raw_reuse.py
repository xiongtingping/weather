"""Content-addressed reuse for retained ECMWF PF50 member fields.

Transport endpoints are deliberately absent from the identity key.  AWS and
GCS are mirrors of ECMWF Open Data; a byte-identical GRIB message is one model
fact even when HTTP headers differ.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from email.utils import parsedate_to_datetime
from typing import Any

from .contracts import ContractError, content_sha256
from .native_raw_object_store import (
    find_content_addressed_message,
    find_range_addressed_message,
    store_content_addressed_message,
    store_range_alias,
)


SYSTEM_ID = "ECMWF_IFS_ENS_OPEN_PF50"
PACKET_SCHEMA = "ecmwf_ifs_open_pf50_historical_multistation_members_v1"


@dataclass(frozen=True)
class RetainedMemberField:
    ensemble_system_id: str
    run_init_utc: str
    member_id: str
    forecast_step: int
    message_sha256: str
    receipt: Mapping[str, Any]
    retained_at_utc: str
    source_packet_content_sha256: str
    source_packet_ref: str

    @property
    def identity_key(self) -> tuple[str, str, str, int, str]:
        return (
            self.ensemble_system_id,
            self.run_init_utc,
            self.member_id,
            self.forecast_step,
            self.message_sha256,
        )

    @property
    def scope_key(self) -> tuple[str, str, str, int]:
        return self.identity_key[:-1]


def _sha(value: Any, code: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ContractError(code)
    return result


def _utc(value: Any, code: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(code)
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _verified_packet(path: Path) -> Mapping[str, Any]:
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("ecmwf_pf50_retained_packet_invalid") from exc
    if not isinstance(packet, Mapping) or packet.get("schema") != PACKET_SCHEMA:
        raise ContractError("ecmwf_pf50_retained_packet_schema_invalid")
    supplied = _sha(packet.get("content_sha256"), "ecmwf_pf50_retained_packet_sha_invalid")
    identity = {key: value for key, value in packet.items() if key != "content_sha256"}
    if supplied != content_sha256(identity):
        raise ContractError("ecmwf_pf50_retained_packet_sha_mismatch")
    if packet.get("ensemble_system_id") != SYSTEM_ID:
        raise ContractError("ecmwf_pf50_retained_packet_system_mismatch")
    return packet


def load_retained_field_index(
    packet_paths: Iterable[Path],
) -> dict[tuple[str, str, str, int], RetainedMemberField]:
    """Index retained receipts by semantic field scope, not artifact path.

    Multiple station captures repeat the same global field receipt.  They are
    collapsed only when the message hash and receipt coordinates agree.
    """

    result: dict[tuple[str, str, str, int], RetainedMemberField] = {}
    for path in sorted({Path(value).resolve() for value in packet_paths}):
        packet = _verified_packet(path)
        packet_sha = _sha(
            packet.get("content_sha256"),
            "ecmwf_pf50_retained_packet_sha_invalid",
        )
        retained_at = _utc(
            packet.get("archive_query_at_utc"),
            "ecmwf_pf50_retained_archive_query_invalid",
        )
        run_init = str(packet.get("run_init_utc") or "")
        captures = packet.get("captures")
        if not run_init or not isinstance(captures, Sequence):
            raise ContractError("ecmwf_pf50_retained_packet_scope_invalid")
        for capture in captures:
            if not isinstance(capture, Mapping):
                raise ContractError("ecmwf_pf50_retained_capture_invalid")
            receipts = capture.get("field_receipts")
            if not isinstance(receipts, Sequence):
                raise ContractError("ecmwf_pf50_retained_receipts_invalid")
            for raw in receipts:
                if not isinstance(raw, Mapping):
                    raise ContractError("ecmwf_pf50_retained_receipt_invalid")
                member_id = str(raw.get("member_id") or "")
                try:
                    step = int(raw.get("forecast_step"))
                except (TypeError, ValueError) as exc:
                    raise ContractError("ecmwf_pf50_retained_step_invalid") from exc
                message_sha = _sha(
                    raw.get("message_sha256"),
                    "ecmwf_pf50_retained_message_sha_invalid",
                )
                field = RetainedMemberField(
                    ensemble_system_id=SYSTEM_ID,
                    run_init_utc=run_init,
                    member_id=member_id,
                    forecast_step=step,
                    message_sha256=message_sha,
                    receipt=dict(raw),
                    retained_at_utc=retained_at,
                    source_packet_content_sha256=packet_sha,
                    source_packet_ref=str(path),
                )
                previous = result.get(field.scope_key)
                if previous is not None and previous.identity_key != field.identity_key:
                    raise ContractError("ecmwf_pf50_retained_field_identity_conflict")
                if previous is not None:
                    for key in ("object_key", "range_start", "range_end", "index_sha256"):
                        if previous.receipt.get(key) != field.receipt.get(key):
                            raise ContractError("ecmwf_pf50_retained_field_receipt_conflict")
                    # A later projection or mirror replay cannot replace the
                    # earliest retained acquisition clock of byte-identical
                    # model content.
                    if previous.retained_at_utc < field.retained_at_utc:
                        continue
                    if previous.retained_at_utc > field.retained_at_utc:
                        result[field.scope_key] = field
                        continue
                    # At an equal retained clock keep a deterministic earliest
                    # immutable-object Last-Modified witness.
                    previous_marker = str(previous.receipt.get("last_modified") or "")
                    field_marker = str(field.receipt.get("last_modified") or "")
                    try:
                        previous_time = parsedate_to_datetime(previous_marker)
                        field_time = parsedate_to_datetime(field_marker)
                    except (TypeError, ValueError, IndexError) as exc:
                        raise ContractError(
                            "ecmwf_pf50_retained_last_modified_invalid"
                        ) from exc
                    if previous_time <= field_time:
                        continue
                result[field.scope_key] = field
    return result
