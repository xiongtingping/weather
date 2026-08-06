"""Minimal content-addressed store for PR10 forward evidence.

Objects are immutable and addressed only by their canonical SHA-256.  The
canonical forward fact stores the manifest address; the manifest in turn owns
the exact native probe plans/results, object receipts, Open-Meteo raw bodies,
decoded curves and component receipts used by that fact.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .contracts import ContractError, content_sha256


STORE_SCHEMA = "republisher_forward_evidence_store_v1"
MANIFEST_SCHEMA = "republisher_forward_evidence_manifest_v1"
PROBE_MANIFEST_SCHEMA = "republisher_native_probe_evidence_manifest_v1"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _kind(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in text):
        raise ContractError("republisher_forward_evidence_kind_invalid")
    return text


def _address(kind: str, digest: str) -> str:
    return f"cas+sha256://{kind}/{digest}"


def _destination(root: Path, kind: str, digest: str) -> Path:
    return root / kind / digest[:2] / digest


def _atomic_create(path: Path, payload: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != path.name:
            raise ContractError("republisher_forward_evidence_existing_object_invalid")
        return False
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != path.name:
            raise ContractError("republisher_forward_evidence_write_hash_mismatch")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != path.name:
                raise ContractError("republisher_forward_evidence_race_invalid")
        return True
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def store_bytes(root: Path | str, *, kind: str, payload: bytes) -> dict[str, Any]:
    target_root = Path(root).expanduser().resolve()
    object_kind = _kind(kind)
    raw = bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    path = _destination(target_root, object_kind, digest)
    created = _atomic_create(path, raw)
    return {
        "schema": STORE_SCHEMA,
        "kind": object_kind,
        "content_sha256": digest,
        "content_bytes": len(raw),
        "content_address": _address(object_kind, digest),
        "created": created,
    }


def store_json(root: Path | str, *, kind: str, value: Any) -> dict[str, Any]:
    return store_bytes(root, kind=kind, payload=_canonical_json_bytes(value))


def verify_address(
    root: Path | str,
    *,
    content_address: str,
    expected_sha256: str,
) -> Path:
    prefix = "cas+sha256://"
    text = str(content_address or "")
    if not text.startswith(prefix):
        raise ContractError("republisher_forward_evidence_address_invalid")
    suffix = text[len(prefix) :]
    parts = suffix.split("/")
    if len(parts) != 2:
        raise ContractError("republisher_forward_evidence_address_invalid")
    kind = _kind(parts[0])
    digest = parts[1]
    if digest != str(expected_sha256) or len(digest) != 64:
        raise ContractError("republisher_forward_evidence_address_hash_mismatch")
    path = _destination(Path(root).expanduser().resolve(), kind, digest)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ContractError("republisher_forward_evidence_object_missing")
    return path


def _load_json_address(
    root: Path | str,
    *,
    content_address: str,
    expected_sha256: str,
) -> Any:
    path = verify_address(
        root,
        content_address=content_address,
        expected_sha256=expected_sha256,
    )
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ContractError("republisher_forward_evidence_json_too_large")
    try:
        return json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_forward_evidence_json_invalid") from exc


def verify_forward_manifest(
    root: Path | str,
    *,
    manifest_content_address: str,
    manifest_content_sha256: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    component_receipts: list[Mapping[str, Any]],
    native_terminal_receipts: list[Mapping[str, Any]],
) -> dict[str, Any]:
    manifest = _load_json_address(
        root,
        content_address=manifest_content_address,
        expected_sha256=manifest_content_sha256,
    )
    if not isinstance(manifest, Mapping) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ContractError("republisher_forward_evidence_manifest_invalid")
    supplied = str(manifest.get("content_sha256") or "")
    if supplied != content_sha256(
        {key: value for key, value in manifest.items() if key != "content_sha256"}
    ):
        raise ContractError("republisher_forward_evidence_manifest_hash_mismatch")
    if (
        str(manifest.get("station_id") or "").upper() != str(station_id).upper()
        or manifest.get("event_date") != event_date
        or manifest.get("settlement_timezone") != settlement_timezone
    ):
        raise ContractError("republisher_forward_evidence_manifest_scope_mismatch")
    entries = manifest.get("object_entries")
    if not isinstance(entries, list) or manifest.get(
        "object_entry_set_sha256"
    ) != content_sha256(entries):
        raise ContractError("republisher_forward_evidence_manifest_entries_invalid")
    by_role: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ContractError("republisher_forward_evidence_manifest_entry_invalid")
        role = str(entry.get("role") or "")
        if not role or role in by_role:
            raise ContractError("republisher_forward_evidence_manifest_role_invalid")
        verify_address(
            root,
            content_address=str(entry.get("content_address") or ""),
            expected_sha256=str(entry.get("content_sha256") or ""),
        )
        by_role[role] = entry
    required_roles = {
        "native_pre_probe_plan",
        "native_pre_probe_result",
        "aifs_open_meteo_raw",
        "aifs_normalized_curve",
        "aifs_component_receipt",
        "icon_open_meteo_raw",
        "icon_normalized_curve",
        "icon_component_receipt",
        "native_post_probe_plan",
        "native_post_probe_result",
        "aifs_native_object_receipt_set",
        "icon_native_object_receipt_set",
        "aifs_native_terminal_receipt",
        "icon_native_terminal_receipt",
    }
    if not required_roles.issubset(by_role):
        raise ContractError("republisher_forward_evidence_manifest_roles_missing")

    def role_json(role: str) -> Any:
        entry = by_role[role]
        return _load_json_address(
            root,
            content_address=str(entry["content_address"]),
            expected_sha256=str(entry["content_sha256"]),
        )

    for phase in ("pre", "post"):
        result = role_json(f"native_{phase}_probe_result")
        if (
            not isinstance(result, Mapping)
            or result.get("fallback_terminal_for_all_products") is not True
            or result.get("native_complete_for_any_product") is not False
            or result.get("indeterminate_for_any_product") is not False
        ):
            raise ContractError(
                f"republisher_forward_evidence_{phase}_probe_not_terminal_fallback"
            )
    expected_components = {
        str(item.get("upstream_model_id") or "").lower(): dict(item)
        for item in component_receipts
    }
    expected_native = {
        str(item.get("upstream_model_id") or "").lower(): dict(item)
        for item in native_terminal_receipts
    }
    for model in ("aifs", "icon"):
        if role_json(f"{model}_component_receipt") != expected_components.get(model):
            raise ContractError("republisher_forward_evidence_component_receipt_mismatch")
        if role_json(f"{model}_native_terminal_receipt") != expected_native.get(model):
            raise ContractError("republisher_forward_evidence_native_receipt_mismatch")
    return dict(manifest)


def store_probe_evidence(
    root: Path | str,
    *,
    phase: str,
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    phase_name = _kind(phase)
    clean_result = {
        key: value for key, value in result.items() if key != "_retained_raw_objects"
    }
    entries: list[dict[str, Any]] = []
    for kind, value, role in (
        ("probe_plans", dict(plan), f"native_{phase_name}_probe_plan"),
        ("probe_results", clean_result, f"native_{phase_name}_probe_result"),
    ):
        stored = store_json(root, kind=kind, value=value)
        entries.append(
            {
                "role": role,
                "schema": stored["schema"],
                "kind": stored["kind"],
                "content_sha256": stored["content_sha256"],
                "content_bytes": stored["content_bytes"],
                "content_address": stored["content_address"],
            }
        )
    retained = result.get("_retained_raw_objects", [])
    if not isinstance(retained, list):
        raise ContractError("republisher_forward_evidence_native_raw_invalid")
    for ordinal, item in enumerate(retained):
        if not isinstance(item, Mapping):
            raise ContractError("republisher_forward_evidence_native_raw_invalid")
        raw = bytes(item.get("raw_body") or b"")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != item.get("raw_object_sha256"):
            raise ContractError("republisher_forward_evidence_native_raw_hash_mismatch")
        stored = store_bytes(root, kind="native_probe_raw", payload=raw)
        entries.append(
            {
                "role": f"native_{phase_name}_probe_raw_{ordinal:03d}",
                "schema": stored["schema"],
                "kind": stored["kind"],
                "content_sha256": stored["content_sha256"],
                "content_bytes": stored["content_bytes"],
                "content_address": stored["content_address"],
            }
        )
    identity = {
        "schema": PROBE_MANIFEST_SCHEMA,
        "phase": phase_name,
        "station_id": str(result.get("station_id") or "").upper(),
        "event_date": str(result.get("event_date") or ""),
        "settlement_timezone": str(result.get("settlement_timezone") or ""),
        "plan_content_sha256": str(plan.get("content_sha256") or ""),
        "probe_result_content_sha256": str(result.get("content_sha256") or ""),
        "object_entries": entries,
        "object_entry_set_sha256": content_sha256(entries),
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    stored_manifest = store_json(root, kind="probe_manifests", value=manifest)
    return {
        "manifest": manifest,
        "manifest_content_sha256": stored_manifest["content_sha256"],
        "manifest_content_address": stored_manifest["content_address"],
        "stored_object_count": len(entries) + 1,
    }


def store_manifest(
    root: Path | str,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    pre_probe_plan: Mapping[str, Any],
    pre_probe_result: Mapping[str, Any],
    captures: Mapping[str, Mapping[str, Any]],
    post_probe_plan: Mapping[str, Any],
    post_probe_result: Mapping[str, Any],
) -> dict[str, Any]:
    object_entries: list[dict[str, Any]] = []

    def immutable_entry(stored: Mapping[str, Any], role: str) -> dict[str, Any]:
        return {
            "role": role,
            "schema": stored["schema"],
            "kind": stored["kind"],
            "content_sha256": stored["content_sha256"],
            "content_bytes": stored["content_bytes"],
            "content_address": stored["content_address"],
        }

    def add_json(kind: str, value: Any, role: str) -> dict[str, Any]:
        stored = store_json(root, kind=kind, value=value)
        object_entries.append(immutable_entry(stored, role))
        return stored

    def store_probe_result(result: Mapping[str, Any], phase: str) -> dict[str, Any]:
        clean = {
            key: value
            for key, value in result.items()
            if key != "_retained_raw_objects"
        }
        add_json("probe_results", clean, f"native_{phase}_probe_result")
        retained = result.get("_retained_raw_objects", [])
        if not isinstance(retained, list):
            raise ContractError("republisher_forward_evidence_native_raw_invalid")
        for ordinal, item in enumerate(retained):
            if not isinstance(item, Mapping):
                raise ContractError("republisher_forward_evidence_native_raw_invalid")
            raw_body = bytes(item.get("raw_body") or b"")
            digest = hashlib.sha256(raw_body).hexdigest()
            if digest != item.get("raw_object_sha256"):
                raise ContractError("republisher_forward_evidence_native_raw_hash_mismatch")
            stored = store_bytes(root, kind="native_probe_raw", payload=raw_body)
            object_entries.append(
                immutable_entry(
                    stored,
                    f"native_{phase}_probe_raw_{ordinal:03d}",
                )
            )
        return clean

    add_json("probe_plans", dict(pre_probe_plan), "native_pre_probe_plan")
    clean_pre_result = store_probe_result(pre_probe_result, "pre")
    for model in ("AIFS", "ICON"):
        capture = captures.get(model)
        if not isinstance(capture, Mapping):
            raise ContractError("republisher_forward_evidence_capture_missing")
        raw = store_bytes(
            root,
            kind="open_meteo_raw",
            payload=bytes(capture.get("raw_body") or b""),
        )
        object_entries.append(
            immutable_entry(raw, f"{model.lower()}_open_meteo_raw")
        )
        add_json(
            "normalized_curves",
            capture.get("normalized_curve"),
            f"{model.lower()}_normalized_curve",
        )
        add_json(
            "component_receipts",
            capture.get("component_receipt"),
            f"{model.lower()}_component_receipt",
        )
    add_json("probe_plans", dict(post_probe_plan), "native_post_probe_plan")
    clean_post_result = store_probe_result(post_probe_result, "post")
    for row in clean_post_result.get("product_results", []):
        if not isinstance(row, Mapping):
            raise ContractError("republisher_forward_evidence_native_result_invalid")
        model = str(row.get("upstream_model_id") or "").lower()
        add_json(
            "native_object_receipts",
            row.get("object_receipts"),
            f"{model}_native_object_receipt_set",
        )
        receipt = row.get("native_unavailability_receipt")
        if receipt is not None:
            add_json(
                "native_terminal_receipts",
                receipt,
                f"{model}_native_terminal_receipt",
            )
    identity = {
        "schema": MANIFEST_SCHEMA,
        "station_id": str(station_id).upper(),
        "event_date": str(event_date),
        "settlement_timezone": str(settlement_timezone),
        "pre_probe_plan_content_sha256": str(pre_probe_plan.get("content_sha256")),
        "pre_probe_result_content_sha256": str(clean_pre_result.get("content_sha256")),
        "post_probe_plan_content_sha256": str(post_probe_plan.get("content_sha256")),
        "post_probe_result_content_sha256": str(clean_post_result.get("content_sha256")),
        "object_entries": object_entries,
        "object_entry_set_sha256": content_sha256(object_entries),
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    stored_manifest = store_json(root, kind="manifests", value=manifest)
    if stored_manifest["content_sha256"] != hashlib.sha256(
        _canonical_json_bytes(manifest)
    ).hexdigest():
        raise ContractError("republisher_forward_evidence_manifest_store_mismatch")
    return {
        "manifest": manifest,
        "manifest_content_sha256": stored_manifest["content_sha256"],
        "manifest_content_address": stored_manifest["content_address"],
        "stored_object_count": len(object_entries) + 1,
    }


__all__ = [
    "MANIFEST_SCHEMA",
    "STORE_SCHEMA",
    "store_bytes",
    "store_json",
    "store_manifest",
    "store_probe_evidence",
    "verify_address",
    "verify_forward_manifest",
]
