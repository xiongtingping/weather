"""Content-addressed archival boundary for a market's raw rule snapshot.

The archive retains raw bytes and transport facts only.  It does not parse a
market outcome, price, order book, position or P&L, and cannot write weather
facts, posterior state or orders.  The L0 target-contract parser consumes a
separately explicit rule-only extraction bound to one of these raw objects.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse


RAW_OBJECT_SCHEMA = "market_rule_raw_object_v1"
MANIFEST_SCHEMA = "market_rule_raw_manifest_v1"
ARCHIVE_RESULT_SCHEMA = "market_rule_raw_archive_result_v1"
SENSITIVE_KEY_RE = re.compile(r"(?:token|session|cookie|authorization|password|secret|credential|api[_-]?key)", re.IGNORECASE)
GAMMA_EVENT_HOST = "gamma-api.polymarket.com"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_timestamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", _text(value))[:32] or "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sanitize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    return {
        str(key): _text(value)
        for key, value in dict(headers or {}).items()
        if not SENSITIVE_KEY_RE.search(str(key)) and not SENSITIVE_KEY_RE.search(_text(value))
    }


def _manifest_core(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "target_identity": _text(manifest.get("target_identity")),
        "public_slug": _text(manifest.get("public_slug")),
        "resolution_local_date": _text(manifest.get("resolution_local_date")),
        "source_url": _text(manifest.get("source_url")),
        "requested_at": _text(manifest.get("requested_at")),
        "response_started_at": _text(manifest.get("response_started_at")),
        "response_completed_at": _text(manifest.get("response_completed_at")),
        "response_status": int(manifest.get("response_status") or 0),
        "response_headers": _sanitize_headers(manifest.get("response_headers")),
        "raw_sha256": _text(manifest.get("raw_sha256")),
        "raw_object_ref": _text(manifest.get("raw_object_ref")),
        "fetcher_version": _text(manifest.get("fetcher_version")),
    }


def build_gamma_event_capture_url(*, public_slug: str) -> str:
    """Return the fixed Gamma event endpoint for one target slug."""

    slug = _text(public_slug)
    if not slug:
        raise ValueError("market_rule_raw_public_slug_missing")
    return f"https://{GAMMA_EVENT_HOST}/events?{urlencode({'slug': slug})}"


def _validate_gamma_event_url(value: str) -> str:
    parsed = urlparse(_text(value))
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != GAMMA_EVENT_HOST or parsed.path.rstrip("/") != "/events":
        raise ValueError("market_rule_raw_source_must_be_gamma_events_https")
    return _text(value)


def archive_market_rule_raw_snapshot(
    *,
    archive_dir: str | Path,
    target_identity: str,
    public_slug: str,
    resolution_local_date: str,
    source_url: str,
    raw_payload: bytes,
    requested_at: str,
    response_started_at: str,
    response_completed_at: str,
    response_status: int,
    response_headers: Mapping[str, Any] | None = None,
    fetcher_version: str = "market_rule_raw_fetcher_v1",
) -> dict[str, Any]:
    """Append a raw rule object and immutable transport manifest."""

    root = Path(archive_dir).expanduser()
    day = _text(resolution_local_date)[:10]
    try:
        datetime.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("market_rule_raw_resolution_local_date_invalid") from exc
    if not _text(target_identity) or not _text(public_slug):
        raise ValueError("market_rule_raw_target_identity_or_slug_missing")
    if not _text(source_url).startswith("https://"):
        raise ValueError("market_rule_raw_source_url_must_be_https")
    if not isinstance(raw_payload, (bytes, bytearray)) or not raw_payload:
        raise ValueError("market_rule_raw_payload_missing")
    raw = bytes(raw_payload)
    raw_sha256 = _sha256(raw)
    object_ref = f"objects/{raw_sha256[:2]}/{raw_sha256}.raw.gz"
    object_path = root / object_ref
    object_value = {
        "schema": RAW_OBJECT_SCHEMA,
        "target_identity": _text(target_identity),
        "public_slug": _text(public_slug),
        "resolution_local_date": day,
        "source_url": _text(source_url),
        "raw_sha256": raw_sha256,
        "raw_payload_base64": base64.b64encode(raw).decode("ascii"),
    }
    if object_path.exists():
        try:
            with gzip.open(object_path, "rb") as handle:
                existing = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("market_rule_raw_object_unreadable") from exc
        if existing != object_value:
            raise ValueError("market_rule_raw_object_identity_drift")
        object_reused = True
    else:
        _atomic_write(object_path, gzip.compress(_canonical_json_bytes(object_value)))
        object_reused = False
    manifest = _manifest_core(
        {
            **object_value,
            "requested_at": requested_at,
            "response_started_at": response_started_at,
            "response_completed_at": response_completed_at,
            "response_status": response_status,
            "response_headers": response_headers,
            "raw_object_ref": object_ref,
            "fetcher_version": fetcher_version,
        }
    )
    manifest_sha256 = _sha256(_canonical_json_bytes(manifest))
    manifest = {**manifest, "manifest_sha256": manifest_sha256}
    try:
        completed = datetime.fromisoformat(_text(response_completed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("market_rule_raw_response_completed_at_invalid") from exc
    timestamp = _safe_timestamp(_text(response_completed_at))
    manifest_ref = f"manifests/{completed.year:04d}/{completed.month:02d}/{completed.day:02d}/{timestamp}_{manifest_sha256[:16]}.json"
    manifest_path = root / manifest_ref
    if manifest_path.exists():
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing_manifest != manifest:
            raise ValueError("market_rule_raw_manifest_identity_drift")
    else:
        _atomic_write(manifest_path, _canonical_json_bytes(manifest))
    return {
        "schema": ARCHIVE_RESULT_SCHEMA,
        "ok": True,
        "target_identity": _text(target_identity),
        "public_slug": _text(public_slug),
        "resolution_local_date": day,
        "source_url": _text(source_url),
        "raw_sha256": raw_sha256,
        "raw_object_ref": object_ref,
        "raw_object_reused": object_reused,
        "manifest_ref": manifest_ref,
        "manifest_sha256": manifest_sha256,
        "guards": {
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def load_market_rule_raw_snapshot(*, archive_dir: str | Path, manifest_ref: str) -> dict[str, Any]:
    """Load and reverify one raw rule snapshot without parsing its semantics."""

    root = Path(archive_dir).expanduser().resolve()
    manifest_path = (root / _text(manifest_ref)).resolve()
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("market_rule_raw_manifest_ref_outside_archive") from exc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    core = _manifest_core(manifest)
    if _text(manifest.get("manifest_sha256")) != _sha256(_canonical_json_bytes(core)):
        raise ValueError("market_rule_raw_manifest_sha256_invalid")
    raw = load_market_rule_raw_object(archive_dir=root, raw_ref=_text(manifest.get("raw_object_ref")))
    if _text(manifest.get("raw_sha256")) != _text(raw.get("raw_sha256")):
        raise ValueError("market_rule_raw_manifest_object_binding_invalid")
    return {
        "schema": ARCHIVE_RESULT_SCHEMA,
        "ok": True,
        "target_identity": _text(manifest.get("target_identity")),
        "public_slug": _text(manifest.get("public_slug")),
        "resolution_local_date": _text(manifest.get("resolution_local_date")),
        "source_url": _text(manifest.get("source_url")),
        "raw_payload": raw.get("raw_payload"),
        "raw_sha256": _text(raw.get("raw_sha256")),
        "raw_ref": _text(manifest.get("raw_object_ref")),
        "manifest_ref": _text(manifest_ref),
        "manifest_sha256": _text(manifest.get("manifest_sha256")),
        "requested_at": _text(manifest.get("requested_at")),
        "response_started_at": _text(manifest.get("response_started_at")),
        "response_completed_at": _text(manifest.get("response_completed_at")),
    }


def load_market_rule_raw_object(*, archive_dir: str | Path, raw_ref: str) -> dict[str, Any]:
    """Load a content-addressed raw object when a target contract holds its ref."""

    root = Path(archive_dir).expanduser().resolve()
    object_path = (root / _text(raw_ref)).resolve()
    try:
        object_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("market_rule_raw_object_ref_outside_archive") from exc
    try:
        with gzip.open(object_path, "rb") as handle:
            raw_object = json.loads(handle.read().decode("utf-8"))
        raw_payload = base64.b64decode(_text(raw_object.get("raw_payload_base64")), validate=True)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValueError("market_rule_raw_object_unreadable") from exc
    if raw_object.get("schema") != RAW_OBJECT_SCHEMA or _text(raw_object.get("raw_sha256")) != _sha256(raw_payload):
        raise ValueError("market_rule_raw_object_integrity_invalid")
    return {
        "schema": ARCHIVE_RESULT_SCHEMA,
        "ok": True,
        "target_identity": _text(raw_object.get("target_identity")),
        "public_slug": _text(raw_object.get("public_slug")),
        "resolution_local_date": _text(raw_object.get("resolution_local_date")),
        "source_url": _text(raw_object.get("source_url")),
        "raw_payload": raw_payload,
        "raw_sha256": _text(raw_object.get("raw_sha256")),
        "raw_ref": _text(raw_ref),
    }


def fetch_and_archive_market_rule_raw_snapshot(
    *,
    archive_dir: str | Path,
    target_identity: str,
    public_slug: str,
    resolution_local_date: str,
    source_url: str | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Fetch one raw Gamma event response and archive it without parsing it."""

    url = _validate_gamma_event_url(source_url or build_gamma_event_capture_url(public_slug=public_slug))
    requested_at = _utc_now()
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "weather-target-rule-raw-capture/1.0"})
    with urllib.request.urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:  # noqa: S310 - fixed Gamma HTTPS authority
        response_started_at = _utc_now()
        raw_payload = response.read()
        response_completed_at = _utc_now()
        headers = {str(key): str(value) for key, value in response.headers.items()}
        status = int(getattr(response, "status", 200) or 200)
    return archive_market_rule_raw_snapshot(
        archive_dir=archive_dir,
        target_identity=target_identity,
        public_slug=public_slug,
        resolution_local_date=resolution_local_date,
        source_url=url,
        raw_payload=raw_payload,
        requested_at=requested_at,
        response_started_at=response_started_at,
        response_completed_at=response_completed_at,
        response_status=status,
        response_headers=headers,
    )


__all__ = [
    "ARCHIVE_RESULT_SCHEMA",
    "GAMMA_EVENT_HOST",
    "MANIFEST_SCHEMA",
    "RAW_OBJECT_SCHEMA",
    "archive_market_rule_raw_snapshot",
    "build_gamma_event_capture_url",
    "fetch_and_archive_market_rule_raw_snapshot",
    "load_market_rule_raw_snapshot",
    "load_market_rule_raw_object",
]
