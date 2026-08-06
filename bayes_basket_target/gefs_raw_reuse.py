"""Retained receipt index for official NOAA GEFS member fields."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
from typing import Any

from .contracts import ContractError, content_sha256


SYSTEM_ID = "NOAA_GEFS_05"
PACKET_SCHEMA = "noaa_gefs_official_historical_multistation_members_v2"


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


@dataclass(frozen=True)
class RetainedGEFSField:
    run_init_utc: str
    member_id: str
    forecast_hour: int
    message_sha256: str
    receipt: Mapping[str, Any]
    retained_at_utc: str
    source_packet_content_sha256: str
    source_packet_ref: str

    @property
    def scope_key(self) -> tuple[str, str, str, int]:
        return (SYSTEM_ID, self.run_init_utc, self.member_id, self.forecast_hour)


def load_retained_gefs_field_index(
    packet_paths: Iterable[Path],
) -> dict[tuple[str, str, str, int], RetainedGEFSField]:
    result: dict[tuple[str, str, str, int], RetainedGEFSField] = {}
    for path in sorted({Path(value).resolve() for value in packet_paths}):
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("gefs_retained_packet_invalid") from exc
        if not isinstance(packet, Mapping) or packet.get("schema") != PACKET_SCHEMA:
            raise ContractError("gefs_retained_packet_schema_invalid")
        supplied = _sha(packet.get("content_sha256"), "gefs_retained_packet_sha_invalid")
        if supplied != content_sha256(
            {key: value for key, value in packet.items() if key != "content_sha256"}
        ):
            raise ContractError("gefs_retained_packet_sha_mismatch")
        retained_at = _utc(
            packet.get("archive_query_at_utc"),
            "gefs_retained_archive_query_invalid",
        )
        run_init = str(packet.get("run_init_utc") or "")
        captures = packet.get("captures")
        if packet.get("ensemble_system_id") != SYSTEM_ID or not run_init or not isinstance(captures, Sequence):
            raise ContractError("gefs_retained_packet_scope_invalid")
        for capture in captures:
            receipts = capture.get("field_receipts") if isinstance(capture, Mapping) else None
            if not isinstance(receipts, Sequence):
                raise ContractError("gefs_retained_receipts_invalid")
            for raw in receipts:
                if not isinstance(raw, Mapping):
                    raise ContractError("gefs_retained_receipt_invalid")
                try:
                    hour = int(raw.get("forecast_hour"))
                except (TypeError, ValueError) as exc:
                    raise ContractError("gefs_retained_hour_invalid") from exc
                field = RetainedGEFSField(
                    run_init_utc=run_init,
                    member_id=str(raw.get("member_id") or ""),
                    forecast_hour=hour,
                    message_sha256=_sha(raw.get("message_sha256"), "gefs_retained_message_sha_invalid"),
                    receipt=dict(raw),
                    retained_at_utc=retained_at,
                    source_packet_content_sha256=supplied,
                    source_packet_ref=str(path),
                )
                previous = result.get(field.scope_key)
                if previous is not None:
                    if previous.message_sha256 != field.message_sha256:
                        raise ContractError("gefs_retained_field_identity_conflict")
                    for key in ("object_key", "range_start", "range_end", "index_sha256", "index_line"):
                        if previous.receipt.get(key) != field.receipt.get(key):
                            raise ContractError("gefs_retained_field_receipt_conflict")
                    if previous.retained_at_utc < field.retained_at_utc:
                        continue
                    if previous.retained_at_utc > field.retained_at_utc:
                        result[field.scope_key] = field
                        continue
                    try:
                        old_time = parsedate_to_datetime(str(previous.receipt.get("last_modified") or ""))
                        new_time = parsedate_to_datetime(str(field.receipt.get("last_modified") or ""))
                    except (TypeError, ValueError, IndexError) as exc:
                        raise ContractError("gefs_retained_last_modified_invalid") from exc
                    if old_time <= new_time:
                        continue
                result[field.scope_key] = field
    return result
