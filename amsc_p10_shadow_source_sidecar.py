"""Direct, shadow-only AMSC source sidecar for the resident P10 inbox.

This module sits *before* T1.  It has one job: receive a primary AMSC
response, preserve the immutable raw/manifest receipt, and atomically publish
the exact bundle to the isolated P10 inbox when the response contains the
expected half-hour report and a newly first-seen primary object.  It does not
write ``archive.db``, materialize a posterior, use the legacy task queue, or
place orders.

The P10 inbox consumer re-verifies this bundle independently.  Therefore a
retry, a stale report, an already archived raw object, or a file read later in
the day can never manufacture a fresh T1.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
from typing import Any, Callable, Mapping
import urllib.parse
import urllib.request

import certifi

from amsc_p10_shadow_bundle_loader import FORWARD_NEW_ONLY, load_amsc_p10_shadow_bundle
from amsc_p10_shadow_source_adapter import build_amsc_p10_shadow_arrival_candidates
from amsc_poll_slot_runtime import poll_slot_utc_for_attempt
from amsc_raw_history_archive import archive_amsc_poll
from scripts.fetch_zuuu_metar_taf import (
    ENDPOINT,
    PARSER_VERSION,
    build_amsc_observation_bundle,
    query_params,
    request_headers,
    source_payloads_from_amsc_payload,
)
from source_observation_runtime import PRIMARY_METAR_SOURCE_ID


SCHEMA_VERSION = "amsc_p10_shadow_source_sidecar_v1"
INBOX_SCHEMA = "amsc_p10_shadow_inbox_bundle_v1"
ROOT = Path(__file__).resolve().parent
AMSC_INTERMEDIATE_CA = ROOT / "config" / "trust" / "amsc_globalsign_rsa_ov_ssl_ca_2018.pem"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> str:
    """Write one immutable inbox record, verifying an exact retry only."""

    encoded = _canonical_json(payload) + b"\n"
    digest = hashlib.sha256(encoded).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("amsc_p10_sidecar_inbox_existing_payload_conflict")
        return "idempotent"
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return "published"


def build_amsc_verified_ssl_context() -> ssl.SSLContext:
    """Use certifi roots plus the pinned AMSC issuer intermediate.

    AMSC currently omits ``GlobalSign RSA OV SSL CA 2018`` from its presented
    chain.  Python's OpenSSL verifier does not fetch AIA intermediates, which
    made the direct P10 sidecar fail before T1.  Loading this public issuer
    certificate preserves full certificate verification; it is not an
    insecure context and it is not a curl/subprocess fallback.
    """

    if not AMSC_INTERMEDIATE_CA.is_file():
        raise FileNotFoundError("amsc_p10_sidecar_intermediate_ca_missing")
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(AMSC_INTERMEDIATE_CA))
    return context


def fetch_amsc_with_observed_transport(
    token: str,
    *,
    count: int,
    timeout_seconds: int,
    timestamp_observer: Callable[[], str] = _utc_now_iso,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Use urllib only; no curl/subprocess fallback exists on the P10 path."""

    url = ENDPOINT + "?" + urllib.parse.urlencode(query_params(token, count))
    request = urllib.request.Request(url, headers=request_headers(token))
    poll_started_at = _text(timestamp_observer())
    with urllib.request.urlopen(
        request,
        timeout=max(1, int(timeout_seconds)),
        context=build_amsc_verified_ssl_context(),
    ) as response:
        response_started_at = _text(timestamp_observer())
        raw = response.read().decode("utf-8", "replace")
    response_completed_at = _text(timestamp_observer())
    parsed = json.loads(raw)
    if not isinstance(parsed, Mapping):
        raise ValueError("amsc_p10_sidecar_response_not_object")
    return dict(parsed), {
        "poll_started_at": poll_started_at,
        "response_started_at": response_started_at,
        "response_completed_at": response_completed_at,
        "response_headers_observed": True,
        "transport_mode": "urllib_observed_headers_certifi_pinned_intermediate_no_subprocess_fallback",
    }


def _latest_primary_valid_time(source_payloads: list[Mapping[str, Any]]) -> str:
    values = [
        _text(source.get("valid_time_utc") or source.get("valid_time"))
        for source in source_payloads
        if _text(source.get("source_id")) == PRIMARY_METAR_SOURCE_ID
    ]
    parsed = [value for value in values if _parse_utc(value) is not None]
    return max(parsed, key=lambda value: _parse_utc(value) or datetime.min.replace(tzinfo=timezone.utc)) if parsed else ""


def _inbox_path(inbox_dir: Path, *, expected_slot_at: str, manifest_sha256: str) -> Path:
    slot = "".join(char for char in _text(expected_slot_at) if char.isdigit()) or "unknown"
    digest = _text(manifest_sha256)
    if not digest:
        raise ValueError("amsc_p10_sidecar_manifest_sha_missing")
    return inbox_dir / f"amsc_{slot}_{digest[:24]}.json"


def capture_amsc_p10_shadow_source_attempt(
    *,
    payload: Mapping[str, Any],
    transport: Mapping[str, Any],
    raw_archive_dir: str | Path,
    inbox_dir: str | Path,
    report_count: int,
) -> dict[str, Any]:
    """Archive and publish one already received AMSC response.

    The function is intentionally injection-friendly for deterministic tests.
    It does not fetch, sleep, create a clock, or touch SQLite.  T1 is the raw
    archive's immutable first-seen timestamp, sourced from the observed
    response-completed timestamp passed by the transport boundary.
    """

    archive_root = Path(raw_archive_dir).expanduser().resolve()
    transport_map = dict(transport) if isinstance(transport, Mapping) else {}
    poll_started_at = _text(transport_map.get("poll_started_at"))
    response_started_at = _text(transport_map.get("response_started_at"))
    response_completed_at = _text(transport_map.get("response_completed_at"))
    blockers: list[str] = []
    if _parse_utc(poll_started_at) is None:
        blockers.append("amsc_p10_sidecar_poll_started_at_invalid")
    if _parse_utc(response_started_at) is None:
        blockers.append("amsc_p10_sidecar_response_started_at_invalid")
    if _parse_utc(response_completed_at) is None:
        blockers.append("amsc_p10_sidecar_response_completed_at_invalid")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_before_archive",
            "blockers": blockers,
            "raw_archive_dir": str(archive_root),
            "shadow_only": True,
            "places_orders": False,
        }
    payload_map = dict(payload) if isinstance(payload, Mapping) else {}
    if payload_map.get("code") != 200:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "source_response_not_ok",
            "blockers": ["amsc_p10_sidecar_response_code_not_200"],
            "raw_archive_dir": str(archive_root),
            "shadow_only": True,
            "places_orders": False,
        }
    expected_slot_at = poll_slot_utc_for_attempt(poll_started_at)
    source_payloads = source_payloads_from_amsc_payload(
        payload_map,
        fetched_at=response_completed_at,
        duration_ms=(
            max(
                0.0,
                (_parse_utc(response_completed_at) - _parse_utc(poll_started_at)).total_seconds() * 1000.0,
            )
            if _parse_utc(response_completed_at) is not None and _parse_utc(poll_started_at) is not None
            else None
        ),
    )
    archive = archive_amsc_poll(
        archive_dir=archive_root,
        source_payloads=source_payloads,
        poll_started_at=poll_started_at,
        response_started_at=response_started_at,
        response_headers_observed=transport_map.get("response_headers_observed") is True,
        response_completed_at=response_completed_at,
        response_status=payload_map.get("code"),
        request_metadata={"cccc": "ZUUU", "count": str(int(report_count)), "tt": "SA,SP,FC,FT,"},
        fetcher_version=PARSER_VERSION,
    )
    bundle = build_amsc_observation_bundle(
        payload_map,
        fetched_at=response_completed_at,
        duration_ms=(
            max(
                0.0,
                (_parse_utc(response_completed_at) - _parse_utc(poll_started_at)).total_seconds() * 1000.0,
            )
            if _parse_utc(response_completed_at) is not None and _parse_utc(poll_started_at) is not None
            else None
        ),
        source_payloads=source_payloads,
        raw_history_archive=archive,
    )
    latest_metar_at = _latest_primary_valid_time(source_payloads)
    slot_fresh = latest_metar_at == expected_slot_at
    loaded = load_amsc_p10_shadow_bundle(
        bundle,
        raw_archive_dir=raw_archive_dir,
        mode=FORWARD_NEW_ONLY,
    )
    selection = (
        build_amsc_p10_shadow_arrival_candidates(loaded.get("bundle") or {})
        if loaded.get("ok") is True
        else {}
    )
    inbox_state = "not_published"
    inbox_path = ""
    if slot_fresh and loaded.get("ok") is True and selection.get("candidate_count"):
        try:
            path = _inbox_path(
                Path(inbox_dir).expanduser().resolve(),
                expected_slot_at=expected_slot_at,
                manifest_sha256=_text(archive.get("manifest_sha256")),
            )
            inbox_state = _atomic_write(path, bundle)
            inbox_path = str(path)
        except (OSError, ValueError) as exc:
            blockers.append(f"amsc_p10_sidecar_inbox_publish_failed:{type(exc).__name__}")
            inbox_state = "blocked"
    if not slot_fresh:
        blockers.append("amsc_p10_sidecar_expected_slot_not_observed")
    if loaded.get("ok") is not True:
        blockers.extend(str(value) for value in loaded.get("blockers") or ["amsc_p10_sidecar_bundle_loader_blocked"])
    if selection and selection.get("ok") is not True:
        blockers.extend(str(value) for value in selection.get("blockers") or ["amsc_p10_sidecar_source_selection_blocked"])
    if slot_fresh and not blockers and not selection.get("candidate_count"):
        inbox_state = "no_new_primary_raw_object"
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": (
            "published" if inbox_state in {"published", "idempotent"} and not blockers
            else "ready_without_new_primary" if inbox_state == "no_new_primary_raw_object" and not blockers
            else "blocked_or_stale"
        ),
        "blockers": sorted(set(blockers)),
        "raw_archive_dir": str(archive_root),
        "expected_slot_at": expected_slot_at,
        "latest_primary_metar_at": latest_metar_at,
        "slot_fresh": slot_fresh,
        "archive": {
            "manifest_ref": _text(archive.get("manifest_ref")),
            "manifest_sha256": _text(archive.get("manifest_sha256")),
            "objects_written": int(archive.get("objects_written") or 0),
            "objects_reused": int(archive.get("objects_reused") or 0),
        },
        "bundle_load": {
            "ok": loaded.get("ok") is True,
            "state": _text(loaded.get("state")),
            "selected_primary_count": int(loaded.get("selected_primary_count") or 0),
            "selected_source_observation_ids": list((loaded.get("bundle") or {}).get("source_observation_ids") or []),
            "rejected_primary_rows": list(loaded.get("rejected_primary_rows") or []),
        },
        "source_selection": {
            "ok": selection.get("ok") is True,
            "candidate_count": int(selection.get("candidate_count") or 0),
            "state": _text(selection.get("state")),
        },
        "inbox": {"state": inbox_state, "path": inbox_path},
        "shadow_only": True,
        "places_orders": False,
        "boundary": {
            "network_acquisition_before_t1": True,
            "raw_archive_before_inbox_publish": True,
            "writes_archive_db": False,
            "generic_task_queue": False,
            "subprocess_per_event": False,
            "forecast_refresh": False,
            "clob": False,
            "t1_synthesized": False,
        },
    }


__all__ = [
    "INBOX_SCHEMA",
    "SCHEMA_VERSION",
    "build_amsc_verified_ssl_context",
    "capture_amsc_p10_shadow_source_attempt",
    "fetch_amsc_with_observed_transport",
]
